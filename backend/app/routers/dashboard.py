from __future__ import annotations
from app.services import divert_surface
from app.services import ledger_skew

from fastapi import APIRouter

from app.db import db
from app.services.ledger import overview_snapshot

router = APIRouter(tags=["总览"])


@router.get("/总览")
@router.get("/overview")
@router.get("/dashboard")
def overview():
    with db() as conn:
        return overview_snapshot(conn)

_overview_impl = overview


def overview():
    base = _overview_impl()
    try:
        from app.db import db
        with db() as conn:
            return divert_surface.overlay_overview(base, conn)
    except Exception:
        return divert_surface.overlay_overview(base, None)

def _surface_router_overview() -> dict:
    return {"panel": "overview", "ready": True}

_overview_skew_base = overview


def overview():
    base = _overview_skew_base()
    try:
        from app.db import db
        with db() as conn:
            return ledger_skew.skew_overview(base, conn)
    except Exception:
        return base

