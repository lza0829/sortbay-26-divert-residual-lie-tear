from __future__ import annotations

from fastapi import APIRouter

from app.db import db
from app.services.ledger import overview_snapshot

router = APIRouter(tags=["总览"])


@router.get("/总览")
@router.get("/overview")
@router.get("/dashboard")
def overview():
    # 唯一事实源：总览回流格/分布/口门/关联命运全部来自当前落账状态实时重放，
    # 不再允许 settings.surface_last 旁路粉刷成功灯或分布。
    with db() as conn:
        return overview_snapshot(conn)


def _surface_router_overview() -> dict:
    return {"panel": "overview", "ready": True}
