from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from app.db import db, write_db
from app.services.ledger import get_scan
from app.services.reflow_fate import (
    association_fate,
    find_detect_event_for_batch,
    global_fate_summary,
    list_open_gaps,
    record_gaps,
)
from app.services.reflow_repair import rollback_half_reflow

router = APIRouter(tags=["回流关联"])


class FateIn(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    扫描编号: int | None = None
    scan_id: int | None = None
    scanId: int | None = None
    id: int | None = None
    动作: str | None = None
    action: str | None = None
    mode: str | None = None


def _sid(payload: FateIn | None, query_id: int | None) -> int | None:
    if query_id is not None:
        return query_id
    if payload is None:
        return None
    return payload.扫描编号 or payload.scan_id or payload.scanId or payload.id


@router.get("/回流关联")
@router.get("/reflow-associations")
@router.get("/reflow/fate")
@router.get("/divert-associations")
def fate_overview(
    扫描编号: int | None = Query(default=None),
    scan_id: int | None = Query(default=None),
    scanId: int | None = Query(default=None),
):
    target = 扫描编号 or scan_id or scanId
    with db() as conn:
        if target is not None:
            scan = get_scan(conn, int(target))
            if not scan:
                raise HTTPException(404, "扫描不存在")
            return scan.get("关联命运") or association_fate(conn, scan)
        return global_fate_summary(conn)


@router.get("/回流关联/缺口")
@router.get("/回流关联缺口")
@router.get("/reflow-association-gaps")
@router.get("/reflow/gaps")
def gap_list():
    with db() as conn:
        return {"缺口": list_open_gaps(conn), "gaps": list_open_gaps(conn)}


@router.get("/回流关联/对账")
@router.post("/回流关联/对账")
@router.get("/回流关联/reconcile")
@router.post("/回流关联/reconcile")
@router.get("/reflow-associations/reconcile")
@router.post("/reflow-associations/reconcile")
@router.get("/reflow/reconcile")
@router.post("/reflow/reconcile")
@router.get("/回流关联/同命运")
def reconcile_fate(
    payload: FateIn | None = None,
    扫描编号: int | None = Query(default=None),
    scan_id: int | None = Query(default=None),
):
    target = _sid(payload, 扫描编号 or scan_id)
    with db() as conn:
        if target is not None:
            scan = get_scan(conn, int(target))
            if not scan:
                raise HTTPException(404, "扫描不存在")
            fate = scan.get("关联命运") or association_fate(conn, scan)
            record_gaps(conn, fate, source="回流关联对账")
            return fate
        summary = global_fate_summary(conn)
        for fate in summary.get("缺口明细") or []:
            record_gaps(conn, fate, source="回流关联全局对账")
        return summary


@router.get("/回流关联/{scan_id}")
@router.get("/reflow-associations/{scan_id}")
@router.get("/reflow/fate/{scan_id}")
def fate_one(scan_id: int, 入账: int | None = Query(default=None), record: int | None = Query(default=None)):
    with db() as conn:
        scan = get_scan(conn, scan_id)
        if not scan:
            raise HTTPException(404, "扫描不存在")
        fate = scan.get("关联命运") or association_fate(conn, scan)
        if 入账 or record:
            record_gaps(conn, fate, source="回流关联查询入账")
        return fate


@router.post("/回流关联/{scan_id}/回滚")
@router.post("/回流关联/{scan_id}/rollback")
@router.post("/回流关联/{scan_id}/修复")
@router.post("/回流关联/{scan_id}/repair")
@router.post("/reflow-associations/{scan_id}/rollback")
@router.post("/reflow/{scan_id}/rollback")
@router.post("/扫描/{scan_id}/回滚")
@router.post("/扫描/{scan_id}/rollback")
@router.post("/scans/{scan_id}/rollback")
@router.post("/scan/{scan_id}/rollback")
@router.post("/错分检出/{scan_id}/回滚")
@router.post("/diverts/{scan_id}/rollback")
def rollback_one(scan_id: int, payload: FateIn | None = None):
    with write_db() as conn:
        return rollback_half_reflow(conn, scan_id)


def _scan_id_for_batch(conn, batch_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM scans WHERE divert_batch_id=? ORDER BY id LIMIT 1",
        (batch_id,),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    # Orphan open batch: locate the scan referenced by its detect receipt event.
    ev = find_detect_event_for_batch(conn, batch_id)
    return int(ev["aggregate_id"]) if ev is not None else None


@router.post("/回流批次/{batch_id}/回滚")
@router.post("/回流批次/{batch_id}/rollback")
@router.post("/divert-batches/{batch_id}/rollback")
@router.post("/batches/{batch_id}/rollback")
def rollback_batch(batch_id: int, payload: FateIn | None = None):
    with write_db() as conn:
        scan_id = _scan_id_for_batch(conn, batch_id)
        if scan_id is None:
            raise HTTPException(409, "该回流批次没有可回滚的关联扫描（挂载与回执均缺失）")
        return rollback_half_reflow(conn, scan_id)


@router.post("/回流关联/回滚")
@router.post("/回流关联/rollback")
@router.post("/reflow/rollback")
@router.post("/reflow-associations/rollback")
def rollback_by_body(
    payload: FateIn | None = None,
    扫描编号: int | None = Query(default=None),
    scan_id: int | None = Query(default=None),
    scanId: int | None = Query(default=None),
    id: int | None = Query(default=None),
):
    scan_id = _sid(payload, None)
    if scan_id is None:
        scan_id = 扫描编号 or scan_id or scanId or id
    if scan_id is None:
        raise HTTPException(422, "必须提供扫描编号")
    with write_db() as conn:
        return rollback_half_reflow(conn, int(scan_id))
