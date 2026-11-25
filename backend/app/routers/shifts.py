from __future__ import annotations
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from app.db import db, rows_to_list, row_to_dict, write_db
from app.services.common import audit, now_str
from app.services.reflow_fate import global_fate_summary, record_gaps

router = APIRouter(tags=["班次"])


class ShiftIn(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    营业日: str | None = Field(default=None, min_length=8)
    business_day: str | None = Field(default=None, min_length=8)
    班次名: str | None = Field(default=None, min_length=1)
    name: str | None = Field(default=None, min_length=1)
    shift_name: str | None = Field(default=None, min_length=1)


@router.get("/班次")
@router.get("/shifts")
@router.get("/shift")
def list_shifts():
    with db() as conn:
        return rows_to_list(conn.execute("SELECT * FROM shifts ORDER BY id DESC").fetchall())


@router.get("/班次/对账")
@router.get("/班次/reconcile")
@router.get("/shifts/reconcile")
@router.get("/shifts/reconciliation")
@router.get("/shift/reconcile")
@router.get("/shift/reconciliation")
def reconcile(
    shift_id: int | None = None,
    shiftId: int | None = Query(default=None),
    id: int | None = Query(default=None),
    班次编号: int | None = Query(default=None),
    班次: int | None = Query(default=None),
):
    shift_id = shift_id or shiftId or id or 班次编号 or 班次
    with db() as conn:
        if shift_id is None:
            shift = conn.execute("SELECT * FROM shifts ORDER BY id DESC LIMIT 1").fetchone()
        else:
            shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
        if not shift:
            raise HTTPException(404, "班次不存在")
        sid = shift["id"]
        scan_n = conn.execute("SELECT COUNT(*) c FROM scans WHERE shift_id=?", (sid,)).fetchone()["c"]
        routed = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE shift_id=? AND status IN ('已路由','已入袋','已封袋')",
            (sid,),
        ).fetchone()["c"]
        bag_n = conn.execute("SELECT COUNT(*) c FROM bags WHERE shift_id=?", (sid,)).fetchone()["c"]
        sealed_bag = conn.execute(
            "SELECT COUNT(*) c FROM bags WHERE shift_id=? AND status='已封袋'", (sid,)
        ).fetchone()["c"]
        divert_n = conn.execute(
            "SELECT COUNT(*) c FROM divert_batches WHERE shift_id=?", (sid,)
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE shift_id=? AND status IN ('待路由','错分回流')",
            (sid,),
        ).fetchone()["c"]
        wrong_n = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE shift_id=? AND status='错分回流'",
            (sid,),
        ).fetchone()["c"]
        detect_events = conn.execute(
            """
            SELECT COUNT(*) c FROM domain_events e
            JOIN scans s ON s.id=e.aggregate_id
            WHERE e.aggregate_type='扫描' AND e.event_type='错分检出'
              AND s.shift_id=?
            """,
            (sid,),
        ).fetchone()["c"]
        readjudicate_events = conn.execute(
            """
            SELECT COUNT(*) c FROM domain_events e
            JOIN scans s ON s.id=e.aggregate_id
            WHERE e.aggregate_type='扫描' AND e.event_type='再裁决'
              AND s.shift_id=?
            """,
            (sid,),
        ).fetchone()["c"]
        occupancy_delta = conn.execute(
            "SELECT COALESCE(SUM(delta), 0) d FROM occupancy_events WHERE shift_id=?",
            (sid,),
        ).fetchone()["d"]
        current_occupancy = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE shift_id=? AND status IN ('已路由','已入袋')",
            (sid,),
        ).fetchone()["c"]
        dual_state_n = conn.execute(
            """
            SELECT COUNT(*) c FROM scans
            WHERE shift_id=? AND status='错分回流' AND bag_id IS NOT NULL
            """,
            (sid,),
        ).fetchone()["c"]
        detected_without_batch_n = conn.execute(
            """
            SELECT COUNT(*) c FROM scans
            WHERE shift_id=? AND status='错分回流' AND divert_batch_id IS NULL
            """,
            (sid,),
        ).fetchone()["c"]
        missing_detect_event_n = conn.execute(
            """
            SELECT COUNT(*) c FROM scans s
            WHERE s.shift_id=? AND s.status='错分回流'
              AND NOT EXISTS (
                SELECT 1 FROM domain_events e
                WHERE e.aggregate_type='扫描' AND e.aggregate_id=s.id
                  AND e.event_type='错分检出'
              )
            """,
            (sid,),
        ).fetchone()["c"]
        barcode_dest_rows = conn.execute(
            """
            SELECT s.id AS 扫描编号, s.barcode AS 条码, s.dest_code AS 目的地码,
                   s.status AS 状态, s.version AS 版本,
                   l.name AS 分拣线名,
                   c.code AS 口门码,
                   rrc.code AS 目的地规则口门码,
                   b.code AS 袋码,
                   bc.code AS 袋口门码,
                   db.code AS 回流批次号,
                   s.original_bag_id AS 原袋编号,
                   (SELECT COUNT(*) FROM domain_events e
                    WHERE e.aggregate_type='扫描' AND e.aggregate_id=s.id) AS 事件数,
                   (SELECT GROUP_CONCAT(e.event_type, ' | ')
                    FROM domain_events e
                    WHERE e.aggregate_type='扫描' AND e.aggregate_id=s.id) AS 事件类型列表
            FROM scans s
            LEFT JOIN lines l ON l.id=s.line_id
            LEFT JOIN chutes c ON c.id=s.chute_id
            LEFT JOIN route_rules rr ON rr.dest_code=s.dest_code
            LEFT JOIN chutes rrc ON rrc.id=rr.chute_id
            LEFT JOIN bags b ON b.id=s.bag_id
            LEFT JOIN chutes bc ON bc.id=b.chute_id
            LEFT JOIN divert_batches db ON db.id=s.divert_batch_id
            WHERE s.shift_id=?
            ORDER BY s.id DESC
            LIMIT 300
            """,
            (sid,),
        ).fetchall()
        barcode_dest_reconciliation = rows_to_list(barcode_dest_rows)
        for row in barcode_dest_reconciliation:
            expected = row.get("目的地规则口门码")
            actual = row.get("口门码")
            bag_chute = row.get("袋口门码")
            event_types = row.get("事件类型列表") or ""
            row["目的地路由一致"] = bool(expected and actual and expected == actual)
            row["集包口门一致"] = bool(not row.get("袋码") or not actual or bag_chute == actual)
            row["检出事件未丢失"] = row.get("状态") != "错分回流" or "错分检出" in event_types
            row["对账行一致"] = (
                int(row.get("事件数") or 0) > 0
                and row["集包口门一致"]
                and row["检出事件未丢失"]
            )
        barcode_dest_mismatch_n = sum(1 for r in barcode_dest_reconciliation if not r["对账行一致"])
        fate_summary = global_fate_summary(conn)
        for fate in fate_summary.get("缺口明细") or []:
            record_gaps(conn, fate, source="班次对账")
        fate_gap_n = int(fate_summary.get("缺口件数") or 0)
        fate_frozen_n = int(fate_summary.get("冻结宣称件数") or 0)
        differences = []
        if int(occupancy_delta) != int(current_occupancy):
            differences.append("口门占用事件净额与当前在口件数不一致")
        if dual_state_n:
            differences.append("存在错分回流仍挂活动袋的双态件")
        if detected_without_batch_n:
            differences.append("存在错分检出未挂回流批次的扫描")
        if missing_detect_event_n:
            differences.append("存在缺少错分检出事件的扫描")
        if barcode_dest_mismatch_n:
            differences.append("分拣线集包条码目的地对账存在丢事件或集包口门不一致")
        if fate_gap_n:
            differences.append("存在回流批次/扫描关联位/口门清空/裁决入口不同命运的回流关联缺口件")
        return {
            "班次": row_to_dict(shift),
            "扫描总数": scan_n,
            "已路由及以上": routed,
            "袋数": bag_n,
            "已封袋数": sealed_bag,
            "回流批次数": divert_n,
            "未清件数": pending,
            "错分桶数": wrong_n,
            "错分检出事件数": detect_events,
            "再裁决事件数": readjudicate_events,
            "口门占用事件净额": int(occupancy_delta),
            "当前在口件数": current_occupancy,
            "双态件数": dual_state_n,
            "检出未挂批次件数": detected_without_batch_n,
            "缺检出事件件数": missing_detect_event_n,
            "条码目的地对账": barcode_dest_reconciliation,
            "barcode_destination_reconciliation": barcode_dest_reconciliation,
            "条码目的地差异数": barcode_dest_mismatch_n,
            "回流关联对账": fate_summary,
            "reflow_association": fate_summary,
            "回流关联缺口件数": fate_gap_n,
            "关联缺口件数": fate_gap_n,
            "冻结回流宣称件数": fate_frozen_n,
            "全局同命运": fate_gap_n == 0,
            "差异": differences,
            "对账一致": not differences,
        }


@router.post("/班次")
@router.post("/shifts")
def create_shift(body: ShiftIn):
    business_day = body.营业日 or body.business_day
    name = body.班次名 or body.name or body.shift_name
    if not business_day or not name:
        raise HTTPException(422, "必须提供营业日/business_day 与 班次名/name")
    with write_db() as conn:
        open_one = conn.execute("SELECT id FROM shifts WHERE sealed=0").fetchone()
        if open_one:
            raise HTTPException(400, "仍有未封班班次，先封班再开新班")
        try:
            conn.execute(
                "INSERT INTO shifts(business_day, name, sealed) VALUES (?, ?, 0)",
                (business_day, name),
            )
        except Exception as e:
            raise HTTPException(400, f"开班失败: {e}")
        row = conn.execute(
            "SELECT * FROM shifts WHERE business_day=? AND name=?",
            (business_day, name),
        ).fetchone()
        audit(conn, "班次", row["id"], "开班", name)
        return row_to_dict(row)


@router.post("/班次/{shift_id}/封班")
@router.post("/shifts/{shift_id}/seal")
@router.post("/shifts/{shift_id}/close")
def seal_shift(shift_id: int):
    with write_db() as conn:
        shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
        if not shift:
            raise HTTPException(404, "班次不存在")
        if shift["sealed"]:
            raise HTTPException(400, "已封班")
        pending = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE shift_id=? AND status IN ('待路由','错分回流','已路由','已入袋')",
            (shift_id,),
        ).fetchone()["c"]
        if pending:
            raise HTTPException(400, f"仍有未封袋/未路由件 {pending}，禁止封班")
        conn.execute(
            "UPDATE shifts SET sealed=1, sealed_at=? WHERE id=?",
            (now_str(), shift_id),
        )
        audit(conn, "班次", shift_id, "封班", "")
        return row_to_dict(conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone())

def _surface_router_shifts() -> dict:
    return {"panel": "shifts", "ready": True}

