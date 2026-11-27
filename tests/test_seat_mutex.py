from __future__ import annotations
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    from app import main  # noqa: F401
    from app.seed import seed_if_empty
    from fastapi.testclient import TestClient

    seed_if_empty()  # 同一进程内多次换库时也要建表/播种

    with TestClient(main.app) as c:
        yield c


def gua(c, i):
    return c.get("/api/舱规").json() and i


def pod_state(c, pod_id):
    for p in c.get("/api/舱体").json():
        if p["id"] == pod_id:
            return p
    raise AssertionError("pod missing")


def align_pod(c, pod_id):
    for p in c.get("/api/对读").json()["舱体投影"]:
        if p["id"] == pod_id:
            return p
    raise AssertionError("pod missing")


def orders_for(c, pod_id):
    return [o for o in c.get("/api/启停单").json() if o["pod_id"] == pod_id]


def open_order(c, pod_id, **kw):
    return c.post("/api/启停单", json={"pod_id": pod_id, "action": "启动", **kw}).json()


def close_order(c, order_id, outcome="已完成", note=""):
    return c.post(f"/api/启停单/{order_id}/收口", json={"outcome": outcome, "note": note}).json()


def available_images(c):
    return [i for i in c.get("/api/镜像").json() if i["pull_state"] == "已就绪"]


# ---------------------------------------------------------------- 基本开席
def test_start_opens_seat_not_instant_done(client):
    r = open_order(client, 1)
    assert r["status"] == "启动中"
    assert r["seat"]["seat_no"] == 1
    p = pod_state(client, 1)
    assert p["run_state"] == "启动中"
    # 运行席戳、前单占用、第二张回执、对读屏四者互证
    a = align_pod(client, 1)
    assert a["双席桶"] == 1 and a["已成功席"] == 0
    assert a["占用中运行席"][0]["stamp"] == r["seat"]["stamp"]
    assert a["未收口单"][0]["seat_stamp"] == r["seat"]["stamp"]
    assert a["未收口单"][0]["seat_no"] == r["seat"]["seat_no"]
    assert a["放行再开"] is True  # 干净的单席，可收口；不是脏


# ---------------------------------------------------------------- 同舱第二张：并入延占
def test_second_start_merges_and_extends(client):
    first = open_order(client, 1)
    second = open_order(client, 1)
    assert second["status"] == "已并入"
    assert second["merged_into_id"] == first["id"]
    assert second["coboot"] is True
    assert second["extend_sec"] > 0
    assert second["seat"]["seat_no"] == first["seat"]["seat_no"]
    assert second["seat"]["stamp"] == first["seat"]["stamp"]
    assert second["seat"]["extended_sec"] == second["extend_sec"]
    assert second["seat"]["expire_at"] > first["seat"]["expire_at"]

    # 不得出现两张独立「已完成」
    assert not [o for o in orders_for(client, 1) if o["status"] == "已完成"]
    # 运行席只一个，双席桶不得计成 2
    a = align_pod(client, 1)
    assert a["双席桶"] == 1
    assert a["已成功席"] == 0
    assert [m["id"] for m in a["并启单"]] == [second["id"]]
    assert a["脏项"] == []

    # 前单延占可核：运行席延占 = 并启单延占合计
    seats = client.get("/api/运行席").json()
    seat = [s for s in seats if s["state"] == "占用中"][0]
    assert seat["extended_sec"] == second["extend_sec"]
    assert len([s for s in seats if s["state"] == "占用中"]) == 1


def test_merge_requires_same_image(client):
    first = open_order(client, 1)
    image2 = client.get("/api/镜像").json()[1]
    for _ in range(image2["layers_total"] - image2["layers_done"]):
        client.post(f"/api/镜像/{image2['id']}/推进拉取")
    assert client.get("/api/镜像").json()[1]["pull_state"] == "已就绪"
    r = open_order(client, 1, target_image_id=image2["id"])
    assert r["status"] == "失败"
    assert "镜像" in r["note"]
    assert r["merged_into_id"] == first["id"]
    # 失败不得改动前单与运行席
    a = align_pod(client, 1)
    assert a["脏项"] == []
    assert a["未收口单"][0]["seat_stamp"] == first["seat"]["stamp"]
    assert a["占用中运行席"][0]["extended_sec"] == 0


# ---------------------------------------------------------------- 收口
def test_close_success_releases_seat(client):
    first = open_order(client, 1)
    open_order(client, 1)  # 一张并启
    r = close_order(client, first["id"], "已完成")
    assert r["status"] == "已完成"
    assert r["merged_order_ids"]  # 并启单随前单一起收口
    assert pod_state(client, 1)["run_state"] == "运行中"
    a = align_pod(client, 1)
    assert a["双席桶"] == 0
    # 成功席只在收口后成立，且并启单不计入
    assert a["已成功席"] == 1
    assert a["并启单"] and all(m["extend_sec"] > 0 for m in a["并启单"])
    assert a["脏项"] == []
    # 收口后可再开：新席
    again = open_order(client, 1)
    assert again["status"] == "启动中"
    assert again["seat"]["seat_no"] == 2


def test_close_merged_order_rejected(client):
    first = open_order(client, 1)
    second = open_order(client, 1)
    r = close_order(client, second["id"])
    assert "错误" in r
    assert r["merged_into_id"] == first["id"]


def test_close_failure_restores_prev_state_and_quota(client):
    before = client.get("/api/配额").json()
    first = open_order(client, 1)
    mid = client.get("/api/配额").json()
    assert mid["cpu_used"] > before["cpu_used"]
    r = close_order(client, first["id"], "失败", "探针未过")
    assert r["status"] == "失败"
    assert pod_state(client, 1)["run_state"] == "已停止"
    assert client.get("/api/配额").json()["cpu_used"] == before["cpu_used"]


# ---------------------------------------------------------------- 停止与未收口
def test_stop_rejected_while_open(client):
    first = open_order(client, 1)
    r = client.post("/api/启停单", json={"pod_id": 1, "action": "停止"}).json()
    assert r["status"] == "失败"
    # 前单运行席不得被清零
    a = align_pod(client, 1)
    assert a["双席桶"] == 1
    assert a["脏项"] == []
    assert a["未收口单"][0]["id"] == first["id"]


# ---------------------------------------------------------------- 半截并启脏运行席
def test_dirty_half_merge_blocks_and_is_replayable(client):
    open_order(client, 1)
    open_order(client, 1)
    # 手工制造「延占回执与运行席不一致」的半截并启
    import sqlite3
    from app import config
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE run_seats SET extended_sec=0 WHERE pod_id=1 AND state='占用中'")
    conn.commit()
    conn.close()

    r = open_order(client, 1)
    assert r["status"] == "失败"
    assert "脏" in r["note"]
    a = align_pod(client, 1)
    assert a["放行再开"] is False
    assert any("延占" in issue for issue in a["脏项"])
    rep = client.get("/api/运行席/可否再开", params={"pod_id": 1}).json()
    assert rep["放行再开"] is False and rep["脏项"]
    # 停止同样被脏运行席挡住，且不改状态
    s = client.post("/api/启停单", json={"pod_id": 1, "action": "停止"}).json()
    assert s["status"] == "失败"
    assert align_pod(client, 1)["双席桶"] == 1


def test_dirty_two_open_orders_detected(client):
    import sqlite3
    from app import config
    open_order(client, 1)
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "INSERT INTO start_orders(pod_id, action, target_image_id, status, note, created_at)"
        " VALUES (1,'启动',1,'启动中','伪造', '2020-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    a = align_pod(client, 1)
    assert a["放行再开"] is False
    assert any("2 张" in issue for issue in a["脏项"])
    assert align_pod(client, 1)["双席桶"] == 1


# ---------------------------------------------------------------- 可重放
def test_can_open_replay_is_read_only(client):
    first = open_order(client, 1)
    before_orders = len(orders_for(client, 1))
    for _ in range(3):
        rep = client.get("/api/运行席/可否再开", params={"pod_id": 1}).json()
        assert rep["放行再开"] is True and rep["并启延占"] is True
        assert rep["未收口单"][0]["id"] == first["id"]
        assert rep["占用中运行席"][0]["stamp"] == first["seat"]["stamp"]
    assert len(orders_for(client, 1)) == before_orders


# ---------------------------------------------------------------- 口径不得切换
def test_policy_cannot_switch(client):
    r = client.post("/api/舱规", json={"key": "互斥口径", "value": "硬拒第二张"}).json()
    assert "错误" in r
    assert client.get("/api/对读").json()["互斥口径"] == "并入延占"
    assert align_pod(client, 1)["脏项"] == []
    ok = client.post("/api/舱规", json={"key": "互斥口径", "value": "并入延占"}).json()
    assert ok.get("ok") is True


# ---------------------------------------------------------------- 配额失败整笔回退
def test_quota_failure_leaves_no_seat(client):
    # 占满配额
    import sqlite3
    from app import config
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("UPDATE quotas SET cpu_used=cpu_limit, mem_used_mb=mem_limit_mb WHERE id=1")
    conn.commit()
    conn.close()
    r = open_order(client, 1)
    assert r["status"] == "失败" and r["note"] == "配额不足"
    assert align_pod(client, 1)["双席桶"] == 0
    assert align_pod(client, 1)["脏项"] == []
    assert pod_state(client, 1)["run_state"] == "已停止"


def test_image_not_ready_failure(client):
    r = open_order(client, 2)  # worker-b 的镜像拉取中
    assert r["status"] == "失败" and r["note"] == "镜像未就绪"
    assert align_pod(client, 2)["双席桶"] == 0


# ---------------------------------------------------------------- 双人同时开单
def test_concurrent_open_single_seat(client):
    import concurrent.futures

    def go(_):
        return open_order(client, 1)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(go, range(8)))
    statuses = sorted(r["status"] for r in results)
    assert statuses.count("启动中") == 1
    assert statuses.count("已并入") == 7
    a = align_pod(client, 1)
    assert a["双席桶"] == 1
    assert a["脏项"] == []
    merged = [o for o in orders_for(client, 1) if o["status"] == "已并入"]
    assert sum(o["extend_sec"] for o in merged) == a["占用中运行席"][0]["extended_sec"]


# ---------------------------------------------------------------- 老库迁移
def test_legacy_db_migrates(tmp_path, monkeypatch):
    import sqlite3
    from app import config

    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """
        CREATE TABLE images (id INTEGER PRIMARY KEY, name TEXT NOT NULL, tag TEXT NOT NULL,
          pull_state TEXT NOT NULL, layers_done INTEGER NOT NULL DEFAULT 0,
          layers_total INTEGER NOT NULL DEFAULT 4, ready_at TEXT);
        CREATE TABLE pods (id INTEGER PRIMARY KEY, name TEXT NOT NULL, image_id INTEGER,
          run_state TEXT NOT NULL, sidecar TEXT, cpu_quota REAL NOT NULL DEFAULT 1.0,
          mem_quota_mb INTEGER NOT NULL DEFAULT 512);
        CREATE TABLE start_orders (id INTEGER PRIMARY KEY, pod_id INTEGER NOT NULL,
          action TEXT NOT NULL, target_image_id INTEGER, status TEXT NOT NULL, note TEXT,
          created_at TEXT NOT NULL);
        INSERT INTO images VALUES (1,'app/core','1.4.2','已就绪',4,4,'2020-01-01T00:00:00Z');
        INSERT INTO pods VALUES (1,'legacy-a',1,'已停止',NULL,0.5,256);
        INSERT INTO start_orders VALUES (1,1,'启动',1,'已完成','启动成功','2020-01-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", legacy)
    from app.seed import seed_if_empty
    seed_if_empty()
    from fastapi.testclient import TestClient
    from app import main

    with TestClient(main.app) as c:
        r = open_order(c, 1)
        assert r["status"] == "启动中"
        a = align_pod(c, 1)
        assert a["双席桶"] == 1 and a["脏项"] == []
        # 旧流水仍在且不参与新收口
        ids = [o["id"] for o in orders_for(c, 1)]
        assert 1 in ids
