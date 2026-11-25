from __future__ import annotations
from app.services import divert_surface

import json
from uuid import uuid4

from fastapi import HTTPException

from app.db import row_to_dict
from app.services.common import audit, current_shift, now_str, scan_audit_clause
from app.services.ledger import (
    POLICY,
    add_occupancy_event,
    append_domain_event,
    augment_bag_relation,
    augment_scan,
    get_chute,
    get_scan,
    list_scans,
    overview_snapshot,
    void_bag_relation,
)


def _batch_code(shift_id: int, scan_id: int) -> str:
    stamp = now_str().replace("-", "").replace(" ", "").replace(":", "")
    return f"回流-{shift_id}-{stamp}-s{scan_id}-{uuid4().hex[:6]}"


def detect_scan(
    conn,
    *,
    scan_id: int | None = None,
    barcode: str | None = None,
    note: str = "",
    idempotency_key: str | None = None,
):
    if idempotency_key:
        old_event = conn.execute(
            "SELECT * FROM domain_events WHERE event_uid=?",
            (idempotency_key,),
        ).fetchone()
        if old_event:
            event = row_to_dict(old_event)
            return build_detect_result(
                conn,
                event["aggregate_id"],
                event_uid=idempotency_key,
                idempotent=True,
            )

    scan = None
    if scan_id is not None:
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
    elif barcode:
        scan = conn.execute(
            "SELECT * FROM scans WHERE barcode=? ORDER BY id DESC LIMIT 1",
            (barcode,),
        ).fetchone()
    if not scan:
        raise HTTPException(status_code=404, detail="扫描不存在")

    scan_id = int(scan["id"])
    if scan["status"] == "错分回流":
        raise HTTPException(
            status_code=400,
            detail="该件已错分检出并挂回流批次；请重放结局或执行再裁决，不能重复检出",
        )
    if scan["status"] not in ("已路由", "已入袋"):
        raise HTTPException(
            status_code=400,
            detail=f"仅已路由或已入袋件可错分检出，当前状态：{scan['status']}",
        )

    # 与写操作在同一事务内校验班次，避免封班和检出交叉。
    shift = current_shift(conn)
    if shift["sealed"]:
        raise HTTPException(status_code=400, detail="班次已封班，禁止错分检出")

    old_status = scan["status"]
    old_chute_id = scan["chute_id"]
    old_bag_id = scan["bag_id"]
    preserved_original_bag_id = old_bag_id or scan["original_bag_id"]
    batch_code = _batch_code(shift["id"], scan_id)
    created_at = now_str()
    event_uid = idempotency_key or f"detect-{scan_id}-{uuid4().hex}"

    cur = conn.execute(
        """
        INSERT INTO divert_batches(shift_id, code, status, created_at, note)
        VALUES (?, ?, '回流中', ?, ?)
        """,
        (shift["id"], batch_code, created_at, note or f"扫描 {scan_id} 错分检出"),
    )
    batch_id = int(cur.lastrowid)

    # 条件更新钉住合法前置态；并发入袋/封袋失败者不得继续写第二终态。
    cur = conn.execute(
        """
        UPDATE scans
        SET status='错分回流',
            divert_batch_id=?,
            bag_id=NULL,
            original_bag_id=COALESCE(?, original_bag_id),
            original_chute_id=COALESCE(original_chute_id, chute_id),
            detected_at=?,
            version=version+1
        WHERE id=? AND status IN ('已路由', '已入袋')
        """,
        (batch_id, old_bag_id, created_at, scan_id),
    )
    if cur.rowcount != 1:
        latest = conn.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
        raise HTTPException(
            status_code=409,
            detail=f"错分检出与另一写操作冲突，当前状态：{latest['status'] if latest else '不存在'}",
        )

    relation = void_bag_relation(
        conn,
        scan_id=scan_id,
        bag_id=old_bag_id,
        reason="错分检出：活动袋关联作废，原袋编号留痕",
        event_uid=event_uid,
    )
    occupancy_event = add_occupancy_event(
        conn,
        shift_id=shift["id"],
        chute_id=old_chute_id,
        scan_id=scan_id,
        delta=-1,
        reason="错分检出",
        note=batch_code,
        event_uid=f"{event_uid}-occupancy",
    )

    if old_chute_id is not None:
        conn.execute(
            """
            INSERT INTO route_events(scan_id, chute_id, action, created_at, note)
            VALUES (?, ?, '错分检出', ?, ?)
            """,
            (scan_id, old_chute_id, created_at, batch_code),
        )

    payload = {
        "变更": {
            "status": "错分回流",
            "bag_id": None,
            "original_bag_id": preserved_original_bag_id,
            "original_chute_id": old_chute_id,
            "divert_batch_id": batch_id,
            "detected_at": created_at,
        },
        "from_status": old_status,
        "to_status": "错分回流",
        "old_chute_id": old_chute_id,
        "occupancy_delta": -1,
        "occupancy_event_id": occupancy_event["id"] if occupancy_event else None,
        "batch_id": batch_id,
        "batch_code": batch_code,
        "old_bag_id": old_bag_id,
        "original_bag_id": preserved_original_bag_id,
        "preserved_original_bag_id": preserved_original_bag_id,
        "bag_relation": relation,
        "readjudication_open": True,
        "policy": POLICY["架构"],
        "note": note,
    }
    event = append_domain_event(
        conn,
        "错分检出",
        scan_id,
        payload,
        event_uid=event_uid,
    )

    audit(conn, "回流", batch_id, "错分检出/建回流批次", f"扫描{scan_id} {batch_code}")
    audit(
        conn,
        "错分检出",
        scan_id,
        "错分检出",
        f"批次={batch_code};原状态={old_status};原口门={old_chute_id};在口-1",
    )
    if old_chute_id is not None:
        audit(conn, "口门", old_chute_id, "在口扣减", f"扫描{scan_id};delta=-1;批次={batch_code}")
    if old_bag_id is not None:
        audit(
            conn,
            "原袋关联",
            old_bag_id,
            "检出作废活动袋关联",
            f"扫描{scan_id};原袋留痕可审计;未再裁决前禁止原袋入口收入;事件={event_uid}",
        )
        audit(
            conn,
            "封袋",
            old_bag_id,
            "错分检出关联作废",
            f"扫描{scan_id};活动件数扣减;原关联可审计",
        )

    return build_detect_result(conn, scan_id, event_uid=event["event_uid"], idempotent=False)


def build_detect_result(conn, scan_id: int, *, event_uid: str, idempotent: bool) -> dict:
    scan = get_scan(conn, scan_id)
    if not scan:
        raise HTTPException(404, "扫描不存在")
    batch = None
    if scan.get("divert_batch_id"):
        batch = row_to_dict(
            conn.execute(
                "SELECT * FROM divert_batches WHERE id=?",
                (scan["divert_batch_id"],),
            ).fetchone()
        )
        if batch:
            batch["回流批次号"] = batch["code"]
            batch["批次号"] = batch["code"]
    chute = get_chute(conn, scan.get("chute_id")) if scan.get("chute_id") else None
    relation = None
    if scan.get("original_bag_id"):
        relation = augment_bag_relation(
            conn.execute(
                """
                SELECT * FROM bag_relations
                WHERE scan_id=? AND bag_id=?
                ORDER BY id DESC LIMIT 1
                """,
                (scan_id, scan["original_bag_id"]),
            ).fetchone()
        )
    event = row_to_dict(
        conn.execute("SELECT * FROM domain_events WHERE event_uid=?", (event_uid,)).fetchone()
    )
    event_payload: dict = {}
    if event and event.get("payload_json"):
        try:
            event_payload = json.loads(event["payload_json"])
            event["payload"] = event_payload
        except (json.JSONDecodeError, TypeError):
            event_payload = {}
    detected_chute_id = (
        event_payload.get("old_chute_id")
        or scan.get("chute_id")
        or scan.get("original_chute_id")
    )
    if detected_chute_id:
        chute = get_chute(conn, detected_chute_id) or chute
    audit_clause, audit_params = scan_audit_clause(scan_id)
    audit_rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT * FROM audit_events
            WHERE {audit_clause}
            ORDER BY id DESC LIMIT 30
            """,
            audit_params,
        ).fetchall()
    ]
    overview = overview_snapshot(conn)
    scan_book = list_scans(conn)
    chute_occupancy_after = chute.get("在口件数") if chute else 0
    chute_code = (chute or {}).get("code") or scan.get("口门码") or scan.get("原口门码")
    detected_event_status = (
        event_payload.get("to_status")
        or (event_payload.get("变更") or {}).get("status")
        or "错分回流"
    )
    receipt = {
        "ok": True,
        "success": True,
        "动作": "错分检出",
        "action": "错分检出",
        "message": "错分检出成功，原口门在口已扣减，回流批次已挂载",
        "回执号": event_uid,
        "receipt_id": event_uid,
        "幂等重放": idempotent,
        "idempotent": idempotent,
        "扫描编号": scan_id,
        "scan_id": scan_id,
        "status": detected_event_status,
        "扫描状态": detected_event_status,
        "scan_status": detected_event_status,
        "检出结局状态": detected_event_status,
        "当前扫描状态": scan["status"],
        "current_scan_status": scan["status"],
        "原口门编号": detected_chute_id,
        "chute_id": detected_chute_id,
        "原口门": detected_chute_id,
        "本次扣减口门编号": detected_chute_id,
        "最初原口门编号": scan.get("original_chute_id"),
        "原口门码": chute_code,
        "chute_code": chute_code,
        "口门": chute,
        "chute": chute,
        "扣减在口": 1,
        "occupancy_delta": -1,
        "原口门在口": chute_occupancy_after,
        "口门在口": chute_occupancy_after,
        "在口件数": chute_occupancy_after,
        "occupancy": chute_occupancy_after,
        "occupancy_after": chute_occupancy_after,
        "chute_occupancy": chute_occupancy_after,
        "回流批次编号": scan.get("divert_batch_id"),
        "batch_id": scan.get("divert_batch_id"),
        "回流批次号": scan.get("回流批次号") or (batch or {}).get("code"),
        "批次码": (batch or {}).get("code"),
        "batch_code": scan.get("回流批次号") or (batch or {}).get("code"),
        "原袋编号": scan.get("original_bag_id"),
        "original_bag_id": scan.get("original_bag_id"),
        "source_bag_id": scan.get("original_bag_id"),
        "voided_bag_id": scan.get("original_bag_id"),
        "active_bag_id": scan.get("bag_id"),
        "bag_id": scan.get("bag_id"),
        "原袋码": scan.get("原袋码"),
        "原袋关联": relation or scan.get("原袋关联状态"),
        "bag_relation": relation or scan.get("原袋关联状态"),
        "原袋关联状态": scan.get("原袋关联状态"),
        "relation_state": (relation or {}).get("state") if isinstance(relation, dict) else None,
        "再裁决入口": scan.get("再裁决入口"),
        "readjudication_url": scan.get("再裁决入口"),
        "readjudication_endpoint": scan.get("再裁决入口"),
        "重放入口": f"/api/错分检出/{scan_id}/重放",
        "replay_url": f"/api/diverts/{scan_id}/replay",
        "审计重放入口": f"/api/审计/重放?扫描编号={scan_id}",
        "audit_replay_url": f"/api/audit/replay?scan_id={scan_id}",
        "可再裁决": scan.get("可再裁决"),
        "readjudication_open": scan.get("可再裁决"),
        "事件": event,
        "event": event,
        "event_uid": event_uid,
    }
    return {
        **scan,
        **receipt,
        "ok": True,
        "success": True,
        "scan_id": scan_id,
        "扫描编号": scan_id,
        "id": scan_id,
        "status": scan["status"],
        "扫描状态": scan["status"],
        "scan_status": scan["status"],
        "当前状态": scan["status"],
        "state": scan["status"],
        "扫描当前状态": scan["status"],
        "divert_batch_id": scan.get("divert_batch_id"),
        "batch_id": scan.get("divert_batch_id"),
        "回执": receipt,
        "receipt": receipt,
        "receipt": receipt,
        "扫描": scan,
        "scan": scan,
        "扫描册": scan_book,
        "scans": scan_book,
        "批次": batch,
        "batch": batch,
        "口门": chute,
        "chute": chute,
        "原袋关联": relation,
        "relation": relation,
        "事件": event,
        "event": event,
        "event_id": (event or {}).get("id"),
        "event_uid": event_uid,
        "审计": audit_rows,
        "audit": audit_rows,
        "总览": overview,
        "overview": overview,
        "口径": POLICY,
        "policy": POLICY,
        "idempotent": idempotent,
    }

_build_detect_result_impl = build_detect_result


def build_detect_result(*args, **kwargs):
    out = _build_detect_result_impl(*args, **kwargs)
    return divert_surface.after_divert(out, *args, **kwargs)

