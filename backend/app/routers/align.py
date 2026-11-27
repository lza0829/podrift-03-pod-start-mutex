from __future__ import annotations
from fastapi import APIRouter
from app.db import db
from app.services import seats
from app.services.common import rows

router = APIRouter(tags=["对读屏"])


@router.get("/对读")
def align_view():
    with db() as conn:
        pods = rows(
            conn.execute(
                """
                SELECT p.id, p.name, p.run_state, pr.color AS probe_color, pr.fail_streak,
                       i.pull_state, o.status AS last_order_status, o.note AS last_order_note
                FROM pods p
                LEFT JOIN probes pr ON pr.pod_id = p.id
                LEFT JOIN images i ON i.id = p.image_id
                LEFT JOIN start_orders o ON o.id = (
                  SELECT MAX(id) FROM start_orders WHERE pod_id = p.id
                )
                ORDER BY p.id
                """
            )
        )
        for pod in pods:
            pid = pod["id"]
            open_orders = rows(
                conn.execute(
                    "SELECT id, status, seat_no, seat_stamp, merge_count FROM start_orders "
                    "WHERE pod_id=? AND status=? ORDER BY id",
                    (pid, seats.ST_STARTING),
                )
            )
            active_seats = rows(
                conn.execute(
                    "SELECT id, order_id, seat_no, stamp, hold_sec, extended_sec, opened_at, expire_at "
                    "FROM run_seats WHERE pod_id=? AND state=? ORDER BY id",
                    (pid, seats.SEAT_ACTIVE),
                )
            )
            merged = rows(
                conn.execute(
                    "SELECT id, merged_into_id, extend_sec, seat_stamp FROM start_orders "
                    "WHERE pod_id=? AND coboot=1 ORDER BY id",
                    (pid,),
                )
            )
            issues = seats.seat_issues(conn, pid)
            pod["未收口单"] = open_orders
            pod["占用中运行席"] = active_seats
            pod["并启单"] = merged
            pod["脏项"] = issues
            pod["放行再开"] = len(issues) == 0
            pod["再开方式"] = seats.reopen_mode(conn, pid)
            # 双席桶：该舱占用中运行席数，硬上限为 1，并启单绝不计入
            pod["双席桶"] = len(active_seats)
            # 成功席只认「舱已运行中且无未收口单」，启动中/并启单都不算成功
            pod["已成功席"] = 1 if (pod["run_state"] == "运行中" and not open_orders) else 0
            pod["并启不装成功"] = all(m["extend_sec"] > 0 for m in merged)
        quota = dict(conn.execute("SELECT * FROM quotas WHERE id=1").fetchone())
        policy = dict(conn.execute("SELECT * FROM restart_policy WHERE id=1").fetchone())
        buckets = {
            "双席桶合计": sum(p["双席桶"] for p in pods),
            "已成功席合计": sum(p["已成功席"] for p in pods),
            "并启单数": sum(len(p["并启单"]) for p in pods),
            "未收口舱数": sum(1 for p in pods if p["未收口单"]),
            "脏舱数": sum(1 for p in pods if p["脏项"]),
        }
        return {
            "舱体投影": pods,
            "配额": quota,
            "重启策": policy,
            "互斥口径": seats.COBOOT_POLICY,
            "双席桶": buckets,
        }
