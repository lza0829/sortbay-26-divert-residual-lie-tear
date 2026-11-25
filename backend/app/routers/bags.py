from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from app.db import db, row_to_dict, rows_to_list, write_db
from app.services.common import audit, now_str, require_unsealed
from app.services.ledger import (
    add_bag_relation,
    add_occupancy_event,
    append_domain_event,
    get_scan,
    overview_snapshot,
)

router = APIRouter(tags=["封袋"])


class BagOpenIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    口门编号: int | None = None
    chute_id: int | None = None
    chuteId: int | None = None
    口门码: str | None = None
    chute_code: str | None = None
    chuteCode: str | None = None
    备注: str = ""
    note: str | None = None


class BagAddIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    扫描编号: int | None = None
    scan_id: int | None = None
    scanId: int | None = None
    id: int | None = None
    条码: str | None = None
    barcode: str | None = None


@router.get("/封袋")
@router.get("/bags")
@router.get("/bag")
def list_bags():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT b.*, c.code AS 口门码,
              (SELECT COUNT(*) FROM scans s WHERE s.bag_id=b.id) AS 件数,
              (
                SELECT COUNT(*) FROM bag_relations r
                WHERE r.bag_id=b.id AND r.state='已作废'
                  AND r.reason LIKE '错分检出%'
              ) AS 错分检出作废关联数
            FROM bags b JOIN chutes c ON c.id=b.chute_id
            ORDER BY b.id DESC LIMIT 100
            """
        ).fetchall()
        result = rows_to_list(rows)
        for row in result:
            row["bag_code"] = row.get("code")
        return result


@router.get("/封袋/{bag_id}")
@router.get("/bags/{bag_id}")
@router.get("/bag/{bag_id}")
def get_bag(bag_id: int):
    with db() as conn:
        row = conn.execute(
            """
            SELECT b.*, c.code AS 口门码
            FROM bags b JOIN chutes c ON c.id=b.chute_id
            WHERE b.id=?
            """,
            (bag_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "袋不存在")
        result = row_to_dict(row)
        scans = rows_to_list(
            conn.execute("SELECT * FROM scans WHERE bag_id=? ORDER BY id", (bag_id,)).fetchall()
        )
        relations = rows_to_list(
            conn.execute("SELECT * FROM bag_relations WHERE bag_id=? ORDER BY id", (bag_id,)).fetchall()
        )
        result["扫描"] = scans
        result["scans"] = scans
        result["关联"] = relations
        result["relations"] = relations
        return result


@router.post("/封袋/开袋")
@router.post("/封袋/open")
@router.post("/bags/open")
@router.post("/bag/open")
@router.post("/bags")
def open_bag(body: BagOpenIn):
    chute_pk = body.口门编号 or body.chute_id or body.chuteId
    chute_code = body.口门码 or body.chute_code or body.chuteCode
    note = body.备注 or body.note or ""
    with write_db() as conn:
        require_unsealed(conn)
        shift = conn.execute("SELECT * FROM shifts WHERE sealed=0 ORDER BY id DESC LIMIT 1").fetchone()
        if not shift:
            raise HTTPException(400, "无未封班班次")
        if chute_pk is not None:
            chute = conn.execute("SELECT * FROM chutes WHERE id=?", (chute_pk,)).fetchone()
        elif chute_code:
            chute = conn.execute("SELECT * FROM chutes WHERE code=?", (chute_code,)).fetchone()
            chute_pk = chute["id"] if chute else None
        else:
            raise HTTPException(422, "必须提供口门编号/chute_id或口门码/chute_code")
        if not chute:
            raise HTTPException(404, "口门不存在")
        code = f"袋-{chute['code']}-{shift['id']}-{now_str().replace(':', '').replace(' ', '')[-10:]}-{uuid4().hex[:4]}"
        cur = conn.execute(
            "INSERT INTO bags(shift_id, chute_id, code, status, note) VALUES (?, ?, ?, '集包中', ?)",
            (shift["id"], chute_pk, code, note),
        )
        bag_id = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone()
        audit(conn, "封袋", bag_id, "开袋", code)
        return row_to_dict(row)


@router.post("/封袋/{bag_id}/入袋")
@router.post("/封袋/{bag_id}/add")
@router.post("/bags/{bag_id}")
@router.post("/bags/{bag_id}/add")
@router.post("/bag/{bag_id}/add")
@router.post("/bag/{bag_id}")
@router.post("/bags/{bag_id}/scan")
@router.post("/bags/{bag_id}/scans")
@router.post("/bags/{bag_id}/items")
def add_to_bag(bag_id: int, body: BagAddIn):
    scan_id = body.扫描编号 or body.scan_id or body.scanId or body.id
    barcode = body.条码 or body.barcode
    with write_db() as conn:
        if scan_id is None and barcode:
            row = conn.execute(
                "SELECT id FROM scans WHERE barcode=? ORDER BY id DESC LIMIT 1",
                (barcode,),
            ).fetchone()
            scan_id = row["id"] if row else None
        if scan_id is None:
            raise HTTPException(422, "必须提供扫描编号/scan_id或条码")
        require_unsealed(conn)
        bag = conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone()
        if not bag:
            raise HTTPException(404, "袋不存在")
        if bag["status"] != "集包中":
            raise HTTPException(400, "袋已封，禁止再入")
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        if not scan:
            raise HTTPException(404, "扫描不存在")
        if scan["status"] == "错分回流":
            raise HTTPException(
                400,
                "该件已错分检出，原袋关联已作废留痕；请先再裁决，不能由原袋入口直接收入",
            )
        if scan["status"] != "已路由":
            raise HTTPException(400, "仅已路由件可入袋")
        # 幽灵再路由残态（回流批次仍挂载、无再裁决账却自己回到已路由）不得从
        # 封袋入口被静默收成已入袋，否则又造一条与回流命运撕裂的关联。
        if scan["divert_batch_id"] is not None:
            from app.services.reflow_fate import association_fate, record_gaps

            fate = association_fate(conn, scan)
            if fate.get("幽灵再路由") or not fate.get("同命运"):
                record_gaps(conn, fate, source="封袋入口拦截")
                conn.commit()
                codes = "、".join(fate.get("gap_codes") or []) or "ASSOCIATION_FATE_TORN"
                raise HTTPException(
                    409,
                    "该件回流关联账未清（幽灵再路由/同命运不一致），封袋入口不得假装可正常入袋："
                    f"缺口={codes}；请先再裁决记账或回滚半截回流",
                )
        if scan["chute_id"] != bag["chute_id"]:
            raise HTTPException(400, "扫描口门与袋口门不一致")

        event_uid = f"bag-add-{bag_id}-scan-{scan_id}-{uuid4().hex}"
        cur = conn.execute(
            """
            UPDATE scans
            SET status='已入袋', bag_id=?, version=version+1
            WHERE id=? AND status='已路由' AND bag_id IS NULL AND chute_id=?
            """,
            (bag_id, scan_id, bag["chute_id"]),
        )
        if cur.rowcount != 1:
            latest = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
            raise HTTPException(
                409,
                f"入袋与另一写操作冲突，当前状态：{latest['status'] if latest else '不存在'}",
            )

        add_bag_relation(
            conn,
            scan_id=scan_id,
            bag_id=bag_id,
            chute_id=bag["chute_id"],
            event_uid=event_uid,
        )
        append_domain_event(
            conn,
            "入袋",
            scan_id,
            {
                "变更": {"status": "已入袋", "bag_id": bag_id},
                "bag_id": bag_id,
                "chute_id": bag["chute_id"],
                "occupancy_delta": 0,
            },
            event_uid=event_uid,
        )
        audit(conn, "封袋", bag_id, "入袋", f"扫描{scan_id}")
        result = get_scan(conn, scan_id)
        overview = overview_snapshot(conn)
        result["总览"] = overview
        result["overview"] = overview
        result["回执"] = {
            "ok": True,
            "动作": "入袋",
            "扫描编号": scan_id,
            "袋编号": bag_id,
            "扫描状态": result.get("status"),
        }
        return result


@router.post("/封袋/{bag_id}/封口")
@router.post("/封袋/{bag_id}/seal")
@router.post("/bags/{bag_id}/seal")
@router.post("/bags/{bag_id}/close")
@router.post("/bag/{bag_id}/seal")
@router.post("/bag/{bag_id}/close")
def seal_bag(bag_id: int):
    with write_db() as conn:
        require_unsealed(conn)
        bag = conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone()
        if not bag:
            raise HTTPException(404, "袋不存在")
        if bag["status"] != "集包中":
            raise HTTPException(400, "袋状态不可封口")
        items = conn.execute(
            "SELECT * FROM scans WHERE bag_id=? AND status='已入袋' ORDER BY id",
            (bag_id,),
        ).fetchall()
        if not items:
            n = conn.execute("SELECT COUNT(*) c FROM scans WHERE bag_id=?", (bag_id,)).fetchone()["c"]
            if n == 0:
                raise HTTPException(400, "空袋不可封口")
            raise HTTPException(400, "袋内没有可封口的已入袋件")

        sealed_at = now_str()
        for scan in items:
            event_uid = f"seal-{bag_id}-scan-{scan['id']}-{uuid4().hex}"
            add_occupancy_event(
                conn,
                shift_id=scan["shift_id"],
                chute_id=scan["chute_id"] or bag["chute_id"],
                scan_id=scan["id"],
                delta=-1,
                reason="封袋",
                note=f"袋{bag['code']}",
                event_uid=f"{event_uid}-occupancy",
            )
            append_domain_event(
                conn,
                "封袋",
                scan["id"],
                {
                    "变更": {"status": "已封袋"},
                    "bag_id": bag_id,
                    "occupancy_delta": -1,
                },
                event_uid=event_uid,
            )
            audit(
                conn,
                "封袋",
                bag_id,
                "单件封口扣减在口",
                f"扫描{scan['id']};delta=-1;袋={bag['code']}",
            )

        cur = conn.execute(
            "UPDATE bags SET status='已封袋', sealed_at=? WHERE id=? AND status='集包中'",
            (sealed_at, bag_id),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "封袋与另一写操作冲突")
        conn.execute(
            "UPDATE scans SET status='已封袋' WHERE bag_id=? AND status='已入袋'",
            (bag_id,),
        )
        n = len(items)
        audit(conn, "封袋", bag_id, "封口", f"件数{n}")
        return row_to_dict(conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone())

def _surface_router_bags() -> dict:
    return {"panel": "bags", "ready": True}

