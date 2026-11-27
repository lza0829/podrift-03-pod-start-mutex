from __future__ import annotations
from app.db import connect, init_schema
from app.services import seats
from app.services.common import now_iso


def seed_if_empty() -> None:
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    init_schema(conn)
    seats.ensure_defaults(conn)
    n = conn.execute("SELECT COUNT(*) AS c FROM pods").fetchone()["c"]
    if n:
        # 老库升级：单例配置行缺失会让启动在配额/口径上 500，必须补齐
        _ensure_singletons(conn)
        conn.commit()
        conn.close()
        return
    ts = now_iso()
    conn.execute(
        "INSERT INTO images(name, tag, pull_state, layers_done, layers_total, ready_at) VALUES (?,?,?,?,?,?)",
        ("app/core", "1.4.2", "已就绪", 4, 4, ts),
    )
    conn.execute(
        "INSERT INTO images(name, tag, pull_state, layers_done, layers_total, ready_at) VALUES (?,?,?,?,?,?)",
        ("app/worker", "0.9.1", "拉取中", 2, 4, None),
    )
    conn.execute(
        "INSERT INTO pods(name, image_id, run_state, sidecar, cpu_quota, mem_quota_mb) VALUES (?,?,?,?,?,?)",
        ("web-a", 1, "已停止", "log-ship", 0.5, 256),
    )
    conn.execute(
        "INSERT INTO pods(name, image_id, run_state, sidecar, cpu_quota, mem_quota_mb) VALUES (?,?,?,?,?,?)",
        ("worker-b", 2, "已停止", None, 1.0, 512),
    )
    conn.execute(
        "INSERT INTO probes(pod_id, color, fail_streak, updated_at) VALUES (?,?,?,?)",
        (1, "绿", 0, ts),
    )
    conn.execute(
        "INSERT INTO probes(pod_id, color, fail_streak, updated_at) VALUES (?,?,?,?)",
        (2, "红", 3, ts),
    )
    _ensure_singletons(conn)
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?,?)",
        ("probe_fail_threshold", "3"),
    )
    conn.execute(
        "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
        ("种子", "系统", "舱隙底座初始化", ts),
    )
    conn.commit()
    conn.close()


def _ensure_singletons(conn) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO quotas(id, cpu_used, cpu_limit, mem_used_mb, mem_limit_mb) VALUES (1,?,?,?,?)",
        (1.2, 4.0, 768, 2048),
    )
    conn.execute(
        "INSERT OR IGNORE INTO restart_policy(id, mode, backoff_sec) VALUES (1,?,?)",
        ("失败重启", 8),
    )
