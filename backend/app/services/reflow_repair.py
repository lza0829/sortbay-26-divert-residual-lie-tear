"""对已经落账的回流撕裂残态做补偿回滚/核销。

新写入路径由 ``write_db`` 单事务保证“要么整笔同命运、要么整笔回滚”；本模块只
处理夹具/历史原因造成的、已经 commit 的“半截批次成/关联未清”等残态，把它补偿
回检出之前的路由/入袋命运，并补齐占用净额，杜绝幽灵再路由与幽灵占口。
"""
from __future__ import annotations

import json
from uuid import uuid4

from fastapi import HTTPException

from app.services.common import audit, now_str
from app.services.ledger import add_occupancy_event, append_domain_event, get_scan
from app.services.reflow_fate import association_fate, resolve_all_gaps


def rollback_half_reflow(conn, scan_id: int) -> dict:
    scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
    if not scan:
        raise HTTPException(404, "扫描不存在")
    scan_id = int(scan_id)

    detect_event = conn.execute(
        """
        SELECT payload_json FROM domain_events
        WHERE aggregate_type='扫描' AND aggregate_id=? AND event_type='错分检出'
        ORDER BY id DESC LIMIT 1
        """,
        (scan_id,),
    ).fetchone()
    payload: dict = {}
    if detect_event:
        try:
            payload = json.loads(detect_event["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}

    old_bag_id = payload.get("old_bag_id") or payload.get("original_bag_id") or scan["original_bag_id"]
    old_chute_id = payload.get("old_chute_id") or scan["original_chute_id"] or scan["chute_id"]
    # 夹具可能只留了关联账、没留回执/原袋列：从最近一条袋关联补回袋号。
    if old_bag_id is None:
        rel = conn.execute(
            """
            SELECT bag_id FROM bag_relations
            WHERE scan_id=? ORDER BY id DESC LIMIT 1
            """,
            (scan_id,),
        ).fetchone()
        if rel is not None:
            old_bag_id = rel["bag_id"]
    if old_chute_id is None:
        bag_chute = conn.execute(
            "SELECT chute_id FROM bags WHERE id=?", (old_bag_id,)
        ).fetchone() if old_bag_id is not None else None
        if bag_chute is not None:
            old_chute_id = bag_chute["chute_id"]
    from_status = payload.get("from_status") or payload.get("old_status")
    if from_status not in ("已路由", "已入袋"):
        from_status = "已入袋" if old_bag_id else "已路由"
    # 没有可恢复的袋就不要落在“已入袋但 bag_id 为空”的二次撕裂态。
    if from_status == "已入袋" and old_bag_id is None:
        from_status = "已路由"

    batch_id = scan["divert_batch_id"]
    created_at = now_str()
    event_uid = f"reflow-rollback-{scan_id}-{uuid4().hex}"

    # 1) 扫描关联位回到检出前命运，并卸下半截回流批次。
    conn.execute(
        """
        UPDATE scans
        SET status=?,
            bag_id=?,
            chute_id=COALESCE(?, chute_id),
            divert_batch_id=NULL,
            version=version+1
        WHERE id=?
        """,
        (from_status, old_bag_id if from_status == "已入袋" else None, old_chute_id, scan_id),
    )

    # 2) 袋关联账回到同一命运：入袋态恰有一条有效关联；路由态全部作废。
    conn.execute(
        "UPDATE bag_relations SET state='已作废', voided_at=?, reason=? "
        "WHERE scan_id=? AND state='有效'",
        (created_at, "半截回流补偿回滚：先清空活动关联", scan_id),
    )
    if from_status == "已入袋" and old_bag_id is not None:
        prior = conn.execute(
            """
            SELECT id FROM bag_relations
            WHERE scan_id=? AND bag_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (scan_id, old_bag_id),
        ).fetchone()
        if prior:
            conn.execute(
                """
                UPDATE bag_relations
                SET state='有效', voided_at=NULL,
                    reason='半截回流补偿回滚：恢复原活动袋关联', event_uid=?
                WHERE id=?
                """,
                (event_uid, prior["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO bag_relations
                  (scan_id, bag_id, chute_id, state, created_at, reason, event_uid)
                VALUES (?, ?, ?, '有效', ?, '半截回流补偿回滚：补回活动袋关联', ?)
                """,
                (scan_id, old_bag_id, old_chute_id, created_at, event_uid),
            )

    # 3) 半截批次册标记已回滚（留痕，不物理删除）。
    if batch_id is not None:
        conn.execute(
            "UPDATE divert_batches SET status='已回滚', note=COALESCE(note,'') || ? WHERE id=?",
            ("；补偿回滚半截回流", batch_id),
        )
        audit(conn, "回流", batch_id, "回滚半截回流批次", f"扫描{scan_id};批次{batch_id}")

    # 4) 占用净额对齐恢复后的在口命运（路由/入袋均在口，目标净额 1）。
    net = int(
        conn.execute(
            "SELECT COALESCE(SUM(delta),0) d FROM occupancy_events WHERE scan_id=?",
            (scan_id,),
        ).fetchone()["d"]
    )
    target_net = 1 if from_status in ("已路由", "已入袋") and old_chute_id is not None else 0
    correction = target_net - net
    if correction != 0 and old_chute_id is not None:
        add_occupancy_event(
            conn,
            shift_id=scan["shift_id"],
            chute_id=old_chute_id,
            scan_id=scan_id,
            delta=correction,
            reason="回流回滚占口对齐",
            note=f"补偿 delta={correction}",
            event_uid=f"{event_uid}-occ",
        )

    conn.execute(
        """
        INSERT INTO route_events(scan_id, chute_id, action, created_at, note)
        VALUES (?, ?, '回流回滚', ?, ?)
        """,
        (scan_id, old_chute_id, created_at, f"恢复为{from_status}"),
    )
    append_domain_event(
        conn,
        "回流回滚",
        scan_id,
        {
            "变更": {
                "status": from_status,
                "bag_id": old_bag_id if from_status == "已入袋" else None,
                "divert_batch_id": None,
            },
            "from_fate": "错分回流(撕裂)",
            "to_status": from_status,
            "restored_bag_id": old_bag_id,
            "restored_chute_id": old_chute_id,
            "rolled_back_batch_id": batch_id,
            "occupancy_correction": correction,
        },
        event_uid=event_uid,
    )
    audit(
        conn,
        "回流关联",
        scan_id,
        "回滚半截回流/清除幽灵再路由",
        f"扫描{scan_id};恢复状态={from_status};原袋={old_bag_id};原口门={old_chute_id};占用补偿={correction}",
    )
    resolve_all_gaps(conn, scan_id, "半截回流已补偿回滚，命运重放一致")

    result = get_scan(conn, scan_id)
    result["回滚回执"] = {
        "ok": True,
        "动作": "回流关联回滚",
        "扫描编号": scan_id,
        "恢复状态": from_status,
        "restored_status": from_status,
        "恢复袋编号": old_bag_id,
        "恢复口门编号": old_chute_id,
        "回滚批次编号": batch_id,
        "占用补偿": correction,
    }
    result["rollback_receipt"] = result["回滚回执"]
    result["关联命运"] = association_fate(conn, result)
    return result
