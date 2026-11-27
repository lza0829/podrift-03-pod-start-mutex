from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from app.db import db, tx
from app.services import seats
from app.services.common import rows

router = APIRouter(tags=["舱规"])


class SettingIn(BaseModel):
    key: str
    value: str


@router.get("/舱规")
def list_settings():
    with db() as conn:
        return rows(conn.execute("SELECT * FROM settings ORDER BY key"))


@router.post("/舱规")
def set_setting(body: SettingIn):
    if body.key == seats.POLICY_KEY and body.value != seats.COBOOT_POLICY:
        return {
            "错误": f"互斥口径已落定为《{seats.COBOOT_POLICY}》，不得再切换",
            "互斥口径": seats.COBOOT_POLICY,
        }
    with tx() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (body.key, body.value),
        )
        return {"ok": True}


@router.get("/事件")
def list_events():
    with db() as conn:
        return rows(conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 80"))
