from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from app.db import db, rows_to_list, write_db
from app.services.divert import build_detect_result, detect_scan
from app.services.ledger import POLICY, overview_snapshot, replay_scan

router = APIRouter(tags=["错分回流"])


class DetectIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    扫描编号: int | None = None
    scan_id: int | None = None
    scanId: int | None = None
    id: int | None = None
    条码: str | None = None
    barcode: str | None = None
    bar_code: str | None = None
    code: str | None = None
    备注: str = ""
    note: str | None = None
    幂等键: str | None = None
    idempotency_key: str | None = None


def _first_int(*values):
    for value in values:
        if value is not None:
            return value
    return None


@router.get("/回流批次")
@router.get("/divert-batches")
@router.get("/return-batches")
@router.get("/batches")
def list_batches():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT b.*,
                   s.id AS 扫描编号,
                   s.barcode AS 条码,
                   s.status AS 扫描状态,
                   s.chute_id AS 当前口门编号,
                   s.original_chute_id AS 原口门编号,
                   cc.code AS 当前口门码,
                   oc.code AS 原口门码,
                   s.bag_id AS 活动袋编号,
                   s.original_bag_id AS 原袋编号,
                   ob.code AS 原袋码
            FROM divert_batches b
            LEFT JOIN scans s ON s.divert_batch_id = b.id
            LEFT JOIN chutes cc ON cc.id = s.chute_id
            LEFT JOIN chutes oc ON oc.id = COALESCE(s.original_chute_id, s.chute_id)
            LEFT JOIN bags ob ON ob.id = s.original_bag_id
            ORDER BY b.id DESC, s.id DESC
            LIMIT 200
            """
        ).fetchall()
        result = rows_to_list(rows)
        for row in result:
            row["回流批次号"] = row.get("code")
            row["批次号"] = row.get("code")
            row["batch_code"] = row.get("code")
            row["batch_no"] = row.get("code")
        return result


@router.get("/错分检出/口径")
@router.get("/回流批次/口径")
@router.get("/diverts/policy")
@router.get("/divert/policy")
def divert_policy():
    return POLICY


@router.get("/错分桶")
@router.get("/divert-bucket")
@router.get("/diverts/bucket")
def divert_bucket():
    with db() as conn:
        overview = overview_snapshot(conn)
        return overview["错分桶"]


@router.get("/回流批次/{batch_id}")
@router.get("/divert-batches/{batch_id}")
@router.get("/return-batches/{batch_id}")
@router.get("/batches/{batch_id}")
def get_batch(batch_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM divert_batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            raise HTTPException(404, "回流批次不存在")
        result = rows_to_list([row])[0]
        result["回流批次号"] = result.get("code")
        result["批次号"] = result.get("code")
        result["batch_code"] = result.get("code")
        result["batch_no"] = result.get("code")
        scans = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM scans WHERE divert_batch_id=? ORDER BY id",
                (batch_id,),
            ).fetchall()
        ]
        result["扫描"] = scans
        result["scans"] = scans
        return result


@router.post("/错分检出")
@router.post("/错分检出/")
@router.post("/回流批次")
@router.post("/回流批次/")
@router.post("/diverts")
@router.post("/diverts/detect")
@router.post("/divert")
@router.post("/divert/detect")
@router.post("/scans/detect")
@router.post("/scans/divert")
def detect_entry(
    body: DetectIn | None = None,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    query_cn_scan_id: int | None = Query(default=None, alias="扫描编号"),
    scan_id_query: int | None = Query(default=None, alias="scan_id"),
    scan_id_camel_query: int | None = Query(default=None, alias="scanId"),
    id_query: int | None = Query(default=None, alias="id"),
    barcode_query: str | None = Query(default=None, alias="barcode"),
    条码_query: str | None = Query(default=None, alias="条码"),
):
    body = body or DetectIn()
    scan_id = _first_int(
        body.扫描编号,
        body.scan_id,
        body.scanId,
        body.id,
        query_cn_scan_id,
        scan_id_query,
        scan_id_camel_query,
        id_query,
    )
    barcode = body.条码 or body.barcode or body.bar_code or body.code or 条码_query or barcode_query
    note = body.备注 or body.note or ""
    idem = idempotency_key_header or body.幂等键 or body.idempotency_key
    if scan_id is None and not barcode:
        raise HTTPException(status_code=422, detail="必须提供扫描编号或条码")
    with write_db() as conn:
        return detect_scan(
            conn,
            scan_id=scan_id,
            barcode=barcode,
            note=note,
            idempotency_key=idem,
        )


@router.post("/扫描/{scan_id}/错分检出")
@router.post("/scans/{scan_id}/divert")
@router.post("/scans/{scan_id}/detect")
@router.post("/scan/{scan_id}/divert")
@router.post("/scan/{scan_id}/detect")
@router.post("/diverts/{scan_id}")
@router.post("/divert/{scan_id}")
def detect_by_path(
    scan_id: int,
    body: DetectIn | None = None,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
):
    body = body or DetectIn()
    note = body.备注 or body.note or ""
    idem = idempotency_key_header or body.幂等键 or body.idempotency_key
    with write_db() as conn:
        return detect_scan(
            conn,
            scan_id=scan_id,
            barcode=body.条码 or body.barcode or body.bar_code or body.code,
            note=note,
            idempotency_key=idem,
        )


@router.get("/错分检出/重放")
@router.get("/错分检出/replay")
@router.get("/diverts/replay")
def replay_detect_query(
    扫描编号: int | None = Query(default=None),
    scan_id: int | None = Query(default=None),
):
    target = 扫描编号 or scan_id
    if target is None:
        raise HTTPException(422, "必须提供扫描编号")
    with db() as conn:
        return replay_scan(conn, target)


@router.get("/错分检出/{scan_id}/重放")
@router.get("/错分检出/{scan_id}/replay")
@router.get("/diverts/{scan_id}/replay")
@router.get("/scans/{scan_id}/divert/replay")
@router.get("/scan/{scan_id}/divert/replay")
def replay_detect_path(scan_id: int):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM scans WHERE id=?", (scan_id,)).fetchone():
            raise HTTPException(404, "扫描不存在")
        return replay_scan(conn, scan_id)


@router.get("/错分检出/{scan_id}")
def get_latest_detect(scan_id: int):
    with db() as conn:
        event = conn.execute(
            """
            SELECT event_uid FROM domain_events
            WHERE aggregate_type='扫描' AND aggregate_id=? AND event_type='错分检出'
            ORDER BY id DESC LIMIT 1
            """,
            (scan_id,),
        ).fetchone()
        if not event:
            raise HTTPException(404, "该扫描尚无错分检出事件")
        return build_detect_result(conn, scan_id, event_uid=event["event_uid"], idempotent=True)

def _surface_router_diverts() -> dict:
    return {"panel": "diverts", "ready": True}

