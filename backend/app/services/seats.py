"""同舱运行席与未收口启停单。

互斥口径（已落定，不得再切换）：**并入延占**。

- 同舱同一时刻至多一张「启动中」启停单、至多一个「占用中」运行席。
- 前单未收口时再开启动：第二张并入前单、延长前单占用（延占），
  第二张自身落“已并入”回执，绝不计为独立完成，也不新开运行席。
- 第二张插单与运行席推进在同一个 BEGIN IMMEDIATE 事务里同成同败；
  整笔失败时运行席、前单状态、前单占用戳一概不动。
- 对读屏的「放行再开 / 再开方式」由订单/运行席真源实时重放得出，不是缓存标记。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta

from app.services.common import now_iso

COBOOT_POLICY = "并入延占"
POLICY_KEY = "互斥口径"
HOLD_KEY = "启动占据秒"
EXTEND_KEY = "并启延占秒"
DEFAULT_HOLD_SEC = 60
DEFAULT_EXTEND_SEC = 30

ST_STARTING = "启动中"
ST_MERGED = "已并入"
ST_DONE = "已完成"
ST_FAILED = "失败"

SEAT_ACTIVE = "占用中"
SEAT_RELEASED = "已释放"

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, _TS_FMT)


def _fmt_ts(value: datetime) -> str:
    return value.strftime(_TS_FMT)


def add_secs(ts: str, secs: int) -> str:
    return _fmt_ts(_parse_ts(ts) + timedelta(seconds=int(secs)))


def setting_int(conn: sqlite3.Connection, key: str, default: int) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return default


def ensure_defaults(conn: sqlite3.Connection) -> None:
    defaults = (
        (POLICY_KEY, COBOOT_POLICY),
        (HOLD_KEY, str(DEFAULT_HOLD_SEC)),
        (EXTEND_KEY, str(DEFAULT_EXTEND_SEC)),
    )
    for key, value in defaults:
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?,?)", (key, value))


def policy_conflict(conn: sqlite3.Connection) -> str | None:
    """互斥口径落成后不得再切换；返回冲突的现值（无冲突返回 None）。"""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (POLICY_KEY,)).fetchone()
    if row is None:
        return None
    return None if row["value"] == COBOOT_POLICY else row["value"]


def _fmt_stamp(pod_id: int, seat_no: int) -> str:
    return f"IS-{pod_id}-{seat_no}-{uuid.uuid4().hex[:10]}"


def _log(conn: sqlite3.Connection, kind: str, ref: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
        (kind, ref, detail, now_iso()),
    )


def _insert_failed_order(
    conn: sqlite3.Connection,
    pod_id: int,
    action: str,
    target_image_id: int | None,
    note: str,
    *,
    merged_into_id: int | None = None,
    extend_sec: int = 0,
    seat_no: int | None = None,
    seat_stamp: str | None = None,
) -> dict:
    """整笔失败：只留一张失败回执，不碰运行席与前单。"""
    ts = now_iso()
    cur = conn.execute(
        """
        INSERT INTO start_orders(
          pod_id, action, target_image_id, status, note, created_at,
          merged_into_id, coboot, extend_sec, seat_no, seat_stamp, closed_at, merge_count
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)
        """,
        (
            pod_id, action, target_image_id, ST_FAILED, note, ts,
            merged_into_id, 0, extend_sec, seat_no, seat_stamp, ts,
        ),
    )
    order_id = int(cur.lastrowid)
    _log(conn, "启停单", str(order_id), f"{action} → {ST_FAILED} {note}")
    result = {
        "id": order_id,
        "status": ST_FAILED,
        "note": note,
        "错误": note,
        "互斥口径": COBOOT_POLICY,
    }
    if merged_into_id is not None:
        result["merged_into_id"] = merged_into_id
        result["coboot"] = False
        result["extend_sec"] = extend_sec
    return result


def _open_orders(conn, pod_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM start_orders WHERE pod_id=? AND status=? ORDER BY id",
        (pod_id, ST_STARTING),
    ).fetchall()


def _active_seats(conn, pod_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM run_seats WHERE pod_id=? AND state=? ORDER BY id",
        (pod_id, SEAT_ACTIVE),
    ).fetchall()


def reopen_mode(conn: sqlite3.Connection, pod_id: int) -> str:
    """对读屏用的「再开会怎样」，与 open_start 的分支保持同一套判据。"""
    issues = seat_issues(conn, pod_id)
    if issues:
        return "不可（脏运行席）"
    open_orders = _open_orders(conn, pod_id)
    if open_orders:
        return f"并入前单 #{open_orders[0]['id']} 并延占"
    return "新开运行席"


def seat_issues(conn: sqlite3.Connection, pod_id: int) -> list[str]:
    """对读屏可重放的脏运行席判据：任一项成立即视为不可再开。"""
    issues: list[str] = []
    pod = conn.execute("SELECT * FROM pods WHERE id=?", (pod_id,)).fetchone()
    if pod is None:
        return ["舱体不存在"]

    open_orders = _open_orders(conn, pod_id)
    seats = _active_seats(conn, pod_id)

    if len(open_orders) > 1:
        issues.append(f"同舱出现 {len(open_orders)} 张启动中启停单")
    if len(seats) > 1:
        issues.append(f"同舱出现 {len(seats)} 个占用中运行席")
    if len(open_orders) and not seats:
        issues.append(f"启停单 #{open_orders[0]['id']} 启动中但运行席缺失")
    if seats and not open_orders:
        issues.append(f"运行席 #{seats[0]['id']} 占用中但没有启动中启停单")
    if pod["run_state"] == ST_STARTING and not open_orders:
        issues.append("舱体停在启动中但无启动中启停单")
    if pod["run_state"] != ST_STARTING and open_orders:
        issues.append(f"舱体运行态为《{pod['run_state']}》但仍有启动中启停单")

    if len(open_orders) == 1 and len(seats) == 1:
        order, seat = open_orders[0], seats[0]
        if seat["order_id"] != order["id"]:
            issues.append(f"运行席 #{seat['id']} 挂的是 #{seat['order_id']}，与启动中单 #{order['id']} 不配对")
        if order["seat_no"] != seat["seat_no"]:
            issues.append(f"启停单席次 {order['seat_no']} 与运行席席次 {seat['seat_no']} 不一致")
        if order["seat_stamp"] != seat["stamp"]:
            issues.append("启停单占用戳与运行席戳不一致")
        merged = conn.execute(
            "SELECT * FROM start_orders WHERE merged_into_id=? AND coboot=1 ORDER BY id",
            (order["id"],),
        ).fetchall()
        merged_extends = sum(int(m["extend_sec"] or 0) for m in merged)
        if int(seat["extended_sec"]) != merged_extends:
            issues.append(
                f"运行席延占 {seat['extended_sec']}s 与并启单延占合计 {merged_extends}s 不一致"
            )
        expected_expire = add_secs(
            seat["opened_at"], int(seat["hold_sec"]) + int(seat["extended_sec"])
        )
        if seat["expire_at"] != expected_expire:
            issues.append(
                f"运行席到期 {seat['expire_at']} 与「开启+占据+延占」推算 {expected_expire} 不一致"
            )
        for m in merged:
            if int(m["extend_sec"] or 0) <= 0:
                issues.append(f"并启单 #{m['id']} 延占秒数非正")
            if m["merged_into_id"] != order["id"]:
                issues.append(f"并启单 #{m['id']} 并入指向不一致")
            if m["seat_stamp"] != seat["stamp"]:
                issues.append(f"并启单 #{m['id']} 回执戳与运行席戳不一致")

    # 并启回执被装成独立完成，视为脏
    bad_merged = conn.execute(
        "SELECT id FROM start_orders WHERE pod_id=? AND coboot=1 AND status!=?",
        (pod_id, ST_MERGED),
    ).fetchall()
    for row in bad_merged:
        issues.append(f"并启单 #{row['id']} 状态不是《{ST_MERGED}》，涉嫌装成独立完成")
    orphan_merged = conn.execute(
        """
        SELECT o.id FROM start_orders o
        LEFT JOIN start_orders p ON p.id = o.merged_into_id
        WHERE o.pod_id=? AND o.coboot=1 AND (o.merged_into_id IS NULL OR p.id IS NULL)
        """,
        (pod_id,),
    ).fetchall()
    for row in orphan_merged:
        issues.append(f"并启单 #{row['id']} 找不到前单")

    conflict = policy_conflict(conn)
    if conflict is not None:
        issues.append(f"互斥口径被改成《{conflict}》，与落定口径《{COBOOT_POLICY}》不符")
    return issues


def open_start(conn: sqlite3.Connection, pod_id: int, target_image_id: int | None) -> dict:
    """开启动。调用方必须已开启 BEGIN IMMEDIATE 事务。"""
    pod = conn.execute("SELECT * FROM pods WHERE id=?", (pod_id,)).fetchone()
    if pod is None:
        return {"错误": "舱体不存在", "status": ST_FAILED}

    img_id = target_image_id or pod["image_id"]
    open_orders = _open_orders(conn, pod_id)

    if open_orders:
        return _merge_into_open(conn, pod, open_orders[0], img_id)

    # —— 新开运行席 ——
    issues = seat_issues(conn, pod_id)
    if issues:
        return _insert_failed_order(
            conn, pod_id, "启动", img_id, "运行席脏，拒绝新开：" + "；".join(issues)
        )

    img = conn.execute("SELECT * FROM images WHERE id=?", (img_id,)).fetchone()
    if img is None:
        return _insert_failed_order(conn, pod_id, "启动", img_id, "镜像不存在")
    if img["pull_state"] != "已就绪":
        return _insert_failed_order(conn, pod_id, "启动", img_id, "镜像未就绪")

    prev_run_state = pod["run_state"]
    already_running = prev_run_state == "运行中"
    charge_cpu = 0.0 if already_running else float(pod["cpu_quota"])
    charge_mem = 0 if already_running else int(pod["mem_quota_mb"])
    q = conn.execute("SELECT * FROM quotas WHERE id=1").fetchone()
    if (
        float(q["cpu_used"]) + charge_cpu > float(q["cpu_limit"])
        or int(q["mem_used_mb"]) + charge_mem > int(q["mem_limit_mb"])
    ):
        return _insert_failed_order(conn, pod_id, "启动", img_id, "配额不足")

    now = now_iso()
    hold = setting_int(conn, HOLD_KEY, DEFAULT_HOLD_SEC)
    seat_no_row = conn.execute(
        "SELECT COALESCE(MAX(seat_no), 0) AS m FROM run_seats WHERE pod_id=?", (pod_id,)
    ).fetchone()
    seat_no = int(seat_no_row["m"]) + 1
    stamp = _fmt_stamp(pod_id, seat_no)
    expire_at = add_secs(now, hold)

    cur = conn.execute(
        """
        INSERT INTO start_orders(
          pod_id, action, target_image_id, status, note, created_at,
          merged_into_id, coboot, extend_sec, seat_no, seat_stamp, closed_at, merge_count
        ) VALUES (?,?,?,?,?,?,NULL,0,0,?,?,NULL,0)
        """,
        (
            pod_id, "启动", img_id, ST_STARTING,
            f"运行席 #{seat_no} 已占，占据至 {expire_at}", now,
            seat_no, stamp,
        ),
    )
    order_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO run_seats(
          pod_id, order_id, seat_no, stamp, hold_sec, extended_sec,
          opened_at, expire_at, state, prev_run_state, closed_at,
          charged_cpu, charged_mem_mb
        ) VALUES (?,?,?,?,?,0,?,?,?,?,NULL,?,?)
        """,
        (
            pod_id, order_id, seat_no, stamp, hold, now, expire_at,
            SEAT_ACTIVE, prev_run_state, charge_cpu, charge_mem,
        ),
    )
    conn.execute(
        "UPDATE pods SET run_state=?, image_id=? WHERE id=?", (ST_STARTING, img_id, pod_id)
    )
    if charge_cpu or charge_mem:
        conn.execute(
            "UPDATE quotas SET cpu_used=cpu_used+?, mem_used_mb=mem_used_mb+? WHERE id=1",
            (charge_cpu, charge_mem),
        )
    _log(conn, "运行席", str(pod_id), f"开席 #{seat_no} 戳 {stamp}，单 #{order_id}，占据 {hold}s")
    _log(conn, "启停单", str(order_id), f"启动 → {ST_STARTING} 运行席 #{seat_no} 戳 {stamp}")
    return {
        "id": order_id,
        "status": ST_STARTING,
        "note": f"运行席 #{seat_no} 已占，占据至 {expire_at}",
        "互斥口径": COBOOT_POLICY,
        "seat": {
            "seat_no": seat_no,
            "stamp": stamp,
            "hold_sec": hold,
            "extended_sec": 0,
            "opened_at": now,
            "expire_at": expire_at,
        },
    }


def _merge_into_open(
    conn: sqlite3.Connection, pod: sqlite3.Row, parent: sqlite3.Row, img_id: int | None
) -> dict:
    pod_id = int(pod["id"])
    issues = seat_issues(conn, pod_id)
    if issues:
        return _insert_failed_order(
            conn, pod_id, "启动", img_id, "运行席脏，拒绝并启：" + "；".join(issues)
        )

    seats = _active_seats(conn, pod_id)
    if len(seats) != 1:
        # seat_issues 已覆盖，此处兜底
        return _insert_failed_order(conn, pod_id, "启动", img_id, "运行席缺失，拒绝并启")
    seat = seats[0]

    if parent["target_image_id"] != img_id:
        return _insert_failed_order(
            conn, pod_id, "启动", img_id,
            f"并启目标镜像与未收口前单 #{parent['id']} 不一致（前单镜像 {parent['target_image_id']}）",
            merged_into_id=int(parent["id"]),
            seat_no=int(seat["seat_no"]),
            seat_stamp=seat["stamp"],
        )

    extend = setting_int(conn, EXTEND_KEY, DEFAULT_EXTEND_SEC)
    if extend <= 0:
        return _insert_failed_order(conn, pod_id, "启动", img_id, "并启延占秒数配置非正")

    now = now_iso()
    new_expire = add_secs(seat["expire_at"], extend)
    conn.execute(
        "UPDATE run_seats SET extended_sec=extended_sec+?, expire_at=? WHERE id=?",
        (extend, new_expire, seat["id"]),
    )
    conn.execute(
        "UPDATE start_orders SET merge_count=merge_count+1 WHERE id=?", (parent["id"],)
    )
    cur = conn.execute(
        """
        INSERT INTO start_orders(
          pod_id, action, target_image_id, status, note, created_at,
          merged_into_id, coboot, extend_sec, seat_no, seat_stamp, closed_at, merge_count
        ) VALUES (?,?,?,?,?,?,?,1,?,?,?,?,0)
        """,
        (
            pod_id, "启动", img_id, ST_MERGED,
            f"并入前单 #{parent['id']}，延占 {extend}s，占据至 {new_expire}", now,
            int(parent["id"]), extend, int(seat["seat_no"]), seat["stamp"], now,
        ),
    )
    order_id = int(cur.lastrowid)
    _log(
        conn, "运行席", str(pod_id),
        f"并启：单 #{order_id} 并入 #{parent['id']}，席 #{seat['seat_no']} 延占 {extend}s，至 {new_expire}",
    )
    _log(conn, "启停单", str(order_id), f"启动 → {ST_MERGED} 并入 #{parent['id']} 延占 {extend}s")
    return {
        "id": order_id,
        "status": ST_MERGED,
        "note": f"并入前单 #{parent['id']}，延占 {extend}s，占据至 {new_expire}",
        "互斥口径": COBOOT_POLICY,
        "merged_into_id": int(parent["id"]),
        "coboot": True,
        "extend_sec": extend,
        "seat": {
            "seat_no": int(seat["seat_no"]),
            "stamp": seat["stamp"],
            "hold_sec": int(seat["hold_sec"]),
            "extended_sec": int(seat["extended_sec"]) + extend,
            "opened_at": seat["opened_at"],
            "expire_at": new_expire,
        },
    }


def close_order(conn: sqlite3.Connection, order_id: int, outcome: str, note: str = "") -> dict:
    """收口启动中启停单；成功/失败都释放运行席。调用方须已开写事务。"""
    order = conn.execute("SELECT * FROM start_orders WHERE id=?", (order_id,)).fetchone()
    if order is None:
        return {"错误": "启停单不存在"}
    if order["status"] == ST_MERGED:
        return {
            "错误": f"并启单 #{order_id} 已并入前单 #{order['merged_into_id']}，收口请用前单",
            "merged_into_id": order["merged_into_id"],
        }
    if order["status"] != ST_STARTING:
        return {"错误": f"启停单状态为《{order['status']}》，不可收口"}

    seat = conn.execute(
        "SELECT * FROM run_seats WHERE order_id=? AND state=? ORDER BY id DESC LIMIT 1",
        (order_id, SEAT_ACTIVE),
    ).fetchone()
    if seat is None:
        return {"错误": "运行席缺失，拒绝收口（对读屏会报脏运行席）"}

    if outcome not in (ST_DONE, ST_FAILED):
        return {"错误": f"未知收口结论:{outcome}"}

    pod_id = int(order["pod_id"])
    ts = now_iso()
    merged_rows = conn.execute(
        "SELECT id FROM start_orders WHERE merged_into_id=? AND coboot=1 ORDER BY id",
        (order_id,),
    ).fetchall()
    merged_ids = [int(r["id"]) for r in merged_rows]

    if outcome == ST_DONE:
        final_note = note or "启动成功"
        conn.execute(
            "UPDATE start_orders SET status=?, note=?, closed_at=? WHERE id=?",
            (ST_DONE, final_note, ts, order_id),
        )
        conn.execute(
            "UPDATE run_seats SET state=?, closed_at=? WHERE id=?", (SEAT_RELEASED, ts, seat["id"])
        )
        conn.execute("UPDATE pods SET run_state=? WHERE id=?", ("运行中", pod_id))
    else:
        final_note = note or "启动失败"
        conn.execute(
            "UPDATE start_orders SET status=?, note=?, closed_at=? WHERE id=?",
            (ST_FAILED, final_note, ts, order_id),
        )
        conn.execute(
            "UPDATE run_seats SET state=?, closed_at=? WHERE id=?", (SEAT_RELEASED, ts, seat["id"])
        )
        conn.execute(
            "UPDATE pods SET run_state=? WHERE id=?", (seat["prev_run_state"], pod_id)
        )
        if float(seat["charged_cpu"]) or int(seat["charged_mem_mb"]):
            conn.execute(
                "UPDATE quotas SET cpu_used=MAX(0, cpu_used-?), mem_used_mb=MAX(0, mem_used_mb-?) WHERE id=1",
                (float(seat["charged_cpu"]), int(seat["charged_mem_mb"])),
            )
    _log(conn, "运行席", str(pod_id), f"收席 #{seat['seat_no']}（单 #{order_id}）→ {outcome}")
    _log(conn, "启停单", str(order_id), f"{outcome} {final_note}")
    return {
        "id": order_id,
        "status": outcome,
        "note": final_note,
        "seat": {
            "seat_no": int(seat["seat_no"]),
            "stamp": seat["stamp"],
            "extended_sec": int(seat["extended_sec"]),
        },
        "merged_order_ids": merged_ids,
        "互斥口径": COBOOT_POLICY,
    }


def stop_pod(conn: sqlite3.Connection, pod_id: int) -> dict:
    """停止：前单未收口时整笔失败且不改任何状态。"""
    pod = conn.execute("SELECT * FROM pods WHERE id=?", (pod_id,)).fetchone()
    if pod is None:
        return {"错误": "舱体不存在", "status": ST_FAILED}

    open_orders = _open_orders(conn, pod_id)
    if open_orders:
        return _insert_failed_order(
            conn, pod_id, "停止", pod["image_id"],
            f"前单 #{open_orders[0]['id']} 启动中未收口，停止整笔失败",
        )

    issues = seat_issues(conn, pod_id)
    if issues:
        return _insert_failed_order(
            conn, pod_id, "停止", pod["image_id"], "运行席脏，拒绝停止：" + "；".join(issues)
        )

    ts = now_iso()
    if pod["run_state"] == "运行中":
        conn.execute("UPDATE pods SET run_state=? WHERE id=?", ("已停止", pod_id))
        conn.execute(
            "UPDATE quotas SET cpu_used=MAX(0, cpu_used-?), mem_used_mb=MAX(0, mem_used_mb-?) WHERE id=1",
            (float(pod["cpu_quota"]), int(pod["mem_quota_mb"])),
        )
        note = "停止成功"
    else:
        note = "已不在运行，停止无事可做"
    cur = conn.execute(
        """
        INSERT INTO start_orders(
          pod_id, action, target_image_id, status, note, created_at,
          merged_into_id, coboot, extend_sec, seat_no, seat_stamp, closed_at, merge_count
        ) VALUES (?,?,?,?,?,?,NULL,0,0,NULL,NULL,?,0)
        """,
        (pod_id, "停止", pod["image_id"], ST_DONE, note, ts, ts),
    )
    order_id = int(cur.lastrowid)
    _log(conn, "启停单", str(order_id), f"停止 → {ST_DONE} {note}")
    return {"id": order_id, "status": ST_DONE, "note": note, "互斥口径": COBOOT_POLICY}
