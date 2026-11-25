from __future__ import annotations
from app.services import ledger_skew

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.db import db, row_to_dict, write_db
from app.services.common import audit, now_str, require_unsealed
from app.services.ledger import (
    add_occupancy_event,
    append_domain_event,
    get_scan,
    list_scans,
    overview_snapshot,
)

router = APIRouter(tags=["过机扫描"])


class ScanIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    条码: str | None = Field(default=None, min_length=1)
    barcode: str | None = Field(default=None, min_length=1)
    bar_code: str | None = Field(default=None, min_length=1)
    scan_code: str | None = Field(default=None, min_length=1)
    code: str | None = Field(default=None, min_length=1)
    目的地码: str = ""
    dest_code: str | None = None
    destination: str | None = None
    destination_code: str | None = None
    分拣线编号: int | None = None
    line_id: int | None = None
    lineId: int | None = None
    备注: str = ""
    note: str | None = None


class RouteIn(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    目的地码: str | None = None
    dest_code: str | None = None
    destination: str | None = None
    destination_code: str | None = None
    dest: str | None = None
    口门编号: int | None = None
    chute_id: int | None = None
    chuteId: int | None = None
    target_chute_id: int | None = None
    targetChuteId: int | None = None
    口门码: str | None = None
    chute_code: str | None = None
    chuteCode: str | None = None
    备注: str = ""
    note: str | None = None


class AdjudicateIn(RouteIn):
    扫描编号: int | None = None
    scan_id: int | None = None
    scanId: int | None = None
    id: int | None = None


@router.get("/扫描")
@router.get("/扫描册")
@router.get("/scans")
@router.get("/scan")
def list_scan_api(状态: str | None = None, status: str | None = None):
    with db() as conn:
        return list_scans(conn, 状态 or status)


@router.get("/扫描/{scan_id}")
@router.get("/scans/{scan_id}")
@router.get("/scan/{scan_id}")
def get_scan_api(scan_id: int):
    with db() as conn:
        scan = get_scan(conn, scan_id)
        if not scan:
            raise HTTPException(404, "扫描不存在")
        return scan


@router.post("/扫描")
@router.post("/scans")
@router.post("/scan")
def create_scan(body: ScanIn):
    barcode = body.条码 or body.barcode or body.bar_code or body.scan_code or body.code
    if not barcode:
        raise HTTPException(status_code=422, detail="必须提供条码/barcode")
    dest_code = (
        body.目的地码
        or body.dest_code
        or body.destination
        or body.destination_code
        or ""
    )
    line_id = body.分拣线编号 or body.line_id or body.lineId
    note = body.备注 or body.note or ""
    with write_db() as conn:
        require_unsealed(conn)
        shift = conn.execute(
            "SELECT * FROM shifts WHERE sealed=0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not shift:
            raise HTTPException(400, "无未封班班次")
        line = None
        if line_id:
            line = conn.execute("SELECT * FROM lines WHERE id=?", (line_id,)).fetchone()
        else:
            line = conn.execute("SELECT * FROM lines WHERE active=1 ORDER BY id LIMIT 1").fetchone()
        if not line:
            raise HTTPException(400, "无可用分拣线")
        if not line["active"]:
            raise HTTPException(400, "分拣线已停用")
        dup = conn.execute(
            "SELECT id FROM scans WHERE barcode=? AND shift_id=?",
            (barcode, shift["id"]),
        ).fetchone()
        if dup:
            raise HTTPException(400, "本班次条码已过机")
        created_at = now_str()
        cur = conn.execute(
            """
            INSERT INTO scans(barcode, dest_code, shift_id, line_id, status, scanned_at, note)
            VALUES (?, ?, ?, ?, '待路由', ?, ?)
            """,
            (barcode, dest_code, shift["id"], line["id"], created_at, note),
        )
        scan_id = int(cur.lastrowid)
        append_domain_event(
            conn,
            "扫描装载",
            scan_id,
            {
                "变更": {"status": "待路由", "dest_code": dest_code},
                "barcode": barcode,
                "dest_code": dest_code,
                "shift_id": shift["id"],
                "line_id": line["id"],
            },
        )
        audit(conn, "扫描", scan_id, "过机装载", barcode)
        return get_scan(conn, scan_id)


def route_scan_command(scan_id: int, body: RouteIn | None = None):
    body = body or RouteIn()
    requested_dest = (
        body.目的地码
        or body.dest_code
        or body.destination
        or body.destination_code
        or body.dest
        or ""
    ).strip()
    requested_chute = (
        body.口门编号
        or body.chute_id
        or body.chuteId
        or body.target_chute_id
        or body.targetChuteId
    )
    requested_chute_code = (body.口门码 or body.chute_code or body.chuteCode or "").strip()
    note = body.备注 or body.note or ""

    with write_db() as conn:
        require_unsealed(conn)
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        if not scan:
            raise HTTPException(404, "扫描不存在")
        old_status = scan["status"]
        if old_status not in ("待路由", "错分回流"):
            raise HTTPException(400, f"当前状态不可路由/再裁决: {old_status}")

        chute = None
        if requested_chute is not None:
            chute = conn.execute("SELECT * FROM chutes WHERE id=?", (requested_chute,)).fetchone()
            if not chute:
                raise HTTPException(404, "口门不存在")
            if requested_dest:
                dest_code = requested_dest
            else:
                dest_code = scan["dest_code"]
        elif requested_chute_code:
            chute = conn.execute("SELECT * FROM chutes WHERE code=?", (requested_chute_code,)).fetchone()
            if not chute:
                raise HTTPException(404, "口门不存在")
            dest_code = requested_dest or scan["dest_code"]
        else:
            dest_code = requested_dest or scan["dest_code"]
            if not dest_code:
                raise HTTPException(400, "缺少目的地码")
            rule = conn.execute(
                "SELECT * FROM route_rules WHERE dest_code=?", (dest_code,)
            ).fetchone()
            if not rule:
                raise HTTPException(400, "无匹配路由")
            chute = conn.execute("SELECT * FROM chutes WHERE id=?", (rule["chute_id"],)).fetchone()
            if not chute:
                raise HTTPException(400, "口门不存在")

        if chute["status"] != "口门开":
            raise HTTPException(400, "口门已闭，禁止落入")

        created_at = now_str()
        readjudication = old_status == "错分回流"
        if requested_dest:
            cur = conn.execute(
                """
                UPDATE scans
                SET status='已路由', chute_id=?, dest_code=?,
                    readjudicated_at=CASE WHEN ?='错分回流' THEN ? ELSE readjudicated_at END,
                    version=version+1
                WHERE id=? AND status IN ('待路由', '错分回流')
                """,
                (chute["id"], requested_dest, old_status, created_at, scan_id),
            )
        else:
            cur = conn.execute(
                """
                UPDATE scans
                SET status='已路由', chute_id=?,
                    readjudicated_at=CASE WHEN ?='错分回流' THEN ? ELSE readjudicated_at END,
                    version=version+1
                WHERE id=? AND status IN ('待路由', '错分回流')
                """,
                (chute["id"], old_status, created_at, scan_id),
            )
        if cur.rowcount != 1:
            latest = conn.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
            raise HTTPException(
                409,
                f"路由/再裁决与另一写操作冲突，当前状态：{latest['status'] if latest else '不存在'}",
            )

        action = "再裁决" if readjudication else "路由落入"
        event_uid = f"readjudicate-{scan_id}-{__import__('uuid').uuid4().hex}" if readjudication else f"route-{scan_id}-{__import__('uuid').uuid4().hex}"
        conn.execute(
            """
            INSERT INTO route_events(scan_id, chute_id, action, created_at, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (scan_id, chute["id"], action, created_at, note or chute["code"]),
        )
        add_occupancy_event(
            conn,
            shift_id=scan["shift_id"],
            chute_id=chute["id"],
            scan_id=scan_id,
            delta=1,
            reason=action,
            note=chute["code"],
            event_uid=f"{event_uid}-occupancy",
        )
        append_domain_event(
            conn,
            action,
            scan_id,
            {
                "变更": {
                    "status": "已路由",
                    "chute_id": chute["id"],
                    "dest_code": dest_code,
                },
                "from_status": old_status,
                "to_status": "已路由",
                "chute_id": chute["id"],
                "chute_code": chute["code"],
                "occupancy_delta": 1,
                "divert_batch_id": scan["divert_batch_id"],
                "note": note,
            },
            event_uid=event_uid,
        )
        audit(
            conn,
            "扫描",
            scan_id,
            "再裁决" if readjudication else "路由裁决",
            f"{chute['code']};目的地={dest_code};回流批次={scan['divert_batch_id'] or ''}",
        )
        result = get_scan(conn, scan_id)
        from app.services.ledger import get_chute

        chute_snapshot = get_chute(conn, chute["id"])
        result["回执"] = {
            "ok": True,
            "动作": action,
            "扫描编号": scan_id,
            "扫描状态": "已路由",
            "口门编号": chute["id"],
            "口门码": chute["code"],
            "在口件数": chute_snapshot["在口件数"] if chute_snapshot else None,
            "occupancy": chute_snapshot["在口件数"] if chute_snapshot else None,
            "保留回流批次编号": scan["divert_batch_id"],
        }
        result["口门快照"] = chute_snapshot
        result["口门"] = chute_snapshot
        result["chute"] = chute_snapshot
        result["总览"] = overview_snapshot(conn)
        result["overview"] = result["总览"]
        return result


def _merge_route_query(body: RouteIn | None, request: Request) -> RouteIn:
    body = body or RouteIn()
    q = request.query_params
    updates: dict[str, object] = {}
    dest = q.get("目的地码") or q.get("dest_code") or q.get("destination") or q.get("destination_code")
    if dest and not (body.目的地码 or body.dest_code or body.destination or body.destination_code):
        updates["目的地码"] = dest
    chute_id = q.get("口门编号") or q.get("chute_id") or q.get("chuteId")
    if chute_id and not (body.口门编号 or body.chute_id or body.chuteId):
        updates["口门编号"] = int(chute_id)
    chute_code = q.get("口门码") or q.get("chute_code") or q.get("chuteCode")
    if chute_code and not (body.口门码 or body.chute_code or body.chuteCode):
        updates["口门码"] = chute_code
    note = q.get("备注") or q.get("note")
    if note and not (body.备注 or body.note):
        updates["备注"] = note
    return body.model_copy(update=updates) if updates else body


@router.post("/扫描/{scan_id}/路由")
@router.post("/scans/{scan_id}/route")
@router.post("/scan/{scan_id}/route")
def route_scan(scan_id: int, request: Request, body: RouteIn | None = None):
    body = _merge_route_query(body, request)
    return route_scan_command(scan_id, body)


@router.post("/扫描/{scan_id}/再裁决")
@router.post("/扫描/{scan_id}/裁决")
@router.post("/scans/{scan_id}/readjudicate")
@router.post("/scans/{scan_id}/rejudge")
@router.post("/scan/{scan_id}/readjudicate")
@router.post("/scan/{scan_id}/rejudge")
def readjudicate_scan(scan_id: int, request: Request, body: RouteIn | None = None):
    body = _merge_route_query(body, request)
    return route_scan_command(scan_id, body)


@router.post("/再裁决")
@router.post("/裁决")
@router.post("/readjudicate")
def readjudicate_by_body(body: AdjudicateIn):
    scan_id = body.扫描编号 or body.scan_id or body.scanId or body.id
    if scan_id is None:
        raise HTTPException(status_code=422, detail="必须提供扫描编号")
    return route_scan_command(scan_id, body)

def _surface_router_scans() -> dict:
    return {"panel": "scans", "ready": True}

