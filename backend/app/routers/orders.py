from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from app.db import db, tx
from app.services import seats
from app.services.common import rows

router = APIRouter(tags=["启停单"])


class OrderIn(BaseModel):
    pod_id: int
    action: str  # 启动 | 停止
    target_image_id: int | None = None


class CloseIn(BaseModel):
    outcome: str  # 已完成 | 失败
    note: str = ""


@router.get("/启停单")
def list_orders():
    with db() as conn:
        return rows(conn.execute("SELECT * FROM start_orders ORDER BY id DESC"))


@router.post("/启停单")
def create_order(body: OrderIn):
    action = (body.action or "").strip()
    if action in ("启动", "start", "Start"):
        stored = "启动"
    elif action in ("停止", "stop", "Stop"):
        stored = "停止"
    else:
        return {"status": "失败", "错误": f"未知动作:{action}"}
    with tx() as conn:
        if stored == "启动":
            return seats.open_start(conn, body.pod_id, body.target_image_id)
        return seats.stop_pod(conn, body.pod_id)


@router.post("/启停单/{order_id}/收口")
def close_order(order_id: int, body: CloseIn):
    with tx() as conn:
        res = seats.close_order(conn, order_id, (body.outcome or "").strip(), body.note)
        return res


@router.get("/运行席")
def list_seats():
    with db() as conn:
        return rows(conn.execute("SELECT * FROM run_seats ORDER BY id DESC"))


@router.get("/运行席/可否再开")
def can_open(pod_id: int):
    """对读屏重放口：只读真源表推演，不写、不改。"""
    with db() as conn:
        issues = seats.seat_issues(conn, pod_id)
        open_orders = conn.execute(
            "SELECT * FROM start_orders WHERE pod_id=? AND status=? ORDER BY id",
            (pod_id, seats.ST_STARTING),
        ).fetchall()
        active = conn.execute(
            "SELECT * FROM run_seats WHERE pod_id=? AND state=? ORDER BY id",
            (pod_id, seats.SEAT_ACTIVE),
        ).fetchall()
        if not conn.execute("SELECT 1 FROM pods WHERE id=?", (pod_id,)).fetchone():
            return {"错误": "舱体不存在"}
        return {
            "pod_id": pod_id,
            "互斥口径": seats.COBOOT_POLICY,
            "放行再开": len(issues) == 0,
            "再开方式": seats.reopen_mode(conn, pod_id),
            "未收口单": [dict(r) for r in open_orders],
            "占用中运行席": [dict(r) for r in active],
            "并启延占": len(open_orders) == 1 and len(active) == 1,
            "脏项": issues,
        }
