from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from app.db import db, rows_to_list
from app.services.common import scan_audit_clause
from app.services.ledger import replay_scan

router = APIRouter(tags=["审计"])


@router.get("/审计")
@router.get("/audit")
def list_audit(
    扫描编号: int | None = Query(default=None),
    scan_id: int | None = Query(default=None),
    scanId: int | None = Query(default=None),
    kind: str | None = Query(default=None),
    类型: str | None = Query(default=None),
    动作: str | None = Query(default=None),
    action: str | None = Query(default=None),
    limit: int = Query(default=300, le=1000),
):
    target_scan_id = 扫描编号 or scan_id or scanId
    kind_value = kind or 类型
    action_value = 动作 or action
    sql = "SELECT * FROM audit_events WHERE 1=1"
    params: list[object] = []
    if target_scan_id is not None:
        scan_clause, scan_params = scan_audit_clause(int(target_scan_id))
        sql += f" AND {scan_clause}"
        params.extend(scan_params)
    if kind_value:
        sql += " AND kind=?"
        params.append(kind_value)
    if action_value:
        sql += " AND action LIKE ?"
        params.append(f"%{action_value}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
        result = rows_to_list(rows)
        for row in result:
            row["类型"] = row.get("kind")
            row["动作"] = row.get("action")
            row["时间"] = row.get("created_at")
        return result


def _replay(target: int | None):
    if target is None:
        raise HTTPException(422, "必须提供扫描编号/scan_id")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM scans WHERE id=?", (target,)).fetchone():
            raise HTTPException(404, "扫描不存在")
        return replay_scan(conn, target)


@router.get("/审计/重放")
@router.get("/审计/replay")
@router.get("/audit/replay")
@router.get("/audit/重放")
def replay_audit_get(
    扫描编号: int | None = Query(default=None),
    scan_id: int | None = Query(default=None),
    scanId: int | None = Query(default=None),
):
    return _replay(扫描编号 or scan_id or scanId)


@router.post("/审计/重放")
@router.post("/审计/replay")
@router.post("/audit/replay")
@router.post("/audit/重放")
def replay_audit_post(
    payload: dict | None = None,
    扫描编号: int | None = Query(default=None),
    scan_id: int | None = Query(default=None),
    scanId: int | None = Query(default=None),
):
    payload = payload or {}
    target = (
        payload.get("扫描编号")
        or payload.get("scan_id")
        or payload.get("scanId")
        or payload.get("id")
        or 扫描编号
        or scan_id
        or scanId
    )
    return _replay(int(target) if target is not None else None)

def _surface_router_audit() -> dict:
    return {"panel": "audit", "ready": True}

