"""回流关联命运（全局唯一口径）。

一套事实源：回流批次 ∪ 扫描关联位 ∪ 口门清空 ∪ 裁决入口必须同命运可重放。

- 新写入走 ``write_db`` 单事务，天然同命运，失败整笔回滚（架构 A）。
- 对任何已经落账的撕裂残态（夹具直接改库造出的“回流绿/关联还在/批次成口门未清/
  关联已丢”等），这里给出可核的 ``关联缺口``、冻结“已回流/可裁决”宣称，
  并由调用方决定入账/拦截/回滚（架构 B 的缺口账）。

字段命名同时给中文键与英文别名，便于各定位面与重放/对账复用同一结论。
"""
from __future__ import annotations

import json
from typing import Any

from app.services.common import audit, now_str

CLAIM_STATUS = "错分回流"
ACTIVE_STATUSES = ("已路由", "已入袋")

# 会冻结裁决入口的核心关联缺口。
ADJUDICATION_BLOCKING_CODES = {
    "BATCH_MOUNT_MISSING",
    "BATCH_ROW_MISSING",
    "ACTIVE_BAG_LINK_REMAINS",
    "ACTIVE_RELATION_REMAINS",
    "DUAL_ACTIVE_RELATION",
    "GATE_NOT_CLEARED",
    "DETECT_DELTA_MISSING",
    "GHOST_REROUTABLE",
    "DETECT_EVENT_MISSING",
    "RECEIPT_BATCH_MISMATCH",
    "ASSOCIATION_EVIDENCE_MISSING",
}


def _gap(code: str, label: str, *, detail: str = "", **extra: Any) -> dict[str, Any]:
    row = {
        "code": code,
        "缺口码": code,
        "label": label,
        "缺口": label,
        "描述": detail or label,
        "detail": detail or label,
        "state": "未清",
        "状态": "未清",
    }
    row.update(extra)
    return row


def _latest_event(conn, scan_id: int, event_type: str):
    return conn.execute(
        """
        SELECT id, event_uid, payload_json, created_at
        FROM domain_events
        WHERE aggregate_type='扫描' AND aggregate_id=? AND event_type=?
        ORDER BY id DESC LIMIT 1
        """,
        (scan_id, event_type),
    ).fetchone()


def association_fate(conn, item: dict | None) -> dict[str, Any]:
    """Return the same-fate verdict for one scan row.

    ``item`` may be a raw ``scans`` row or an augmented scan dict.  Every
    lookup is defensive so a sparse fixture row still yields a verdict.
    """
    s: dict[str, Any] = dict(item or {})
    sid = s.get("id") or s.get("scan_id") or s.get("扫描编号")
    try:
        sid = int(sid) if sid is not None else None
    except (TypeError, ValueError):
        sid = None

    status = s.get("status") or s.get("状态")
    batch_id = s.get("divert_batch_id")
    if batch_id is None:
        batch_id = s.get("return_batch_id")
    bag_id = s.get("bag_id")
    joined_batch_code = s.get("回流批次号")

    checks: dict[str, bool] = {}
    gaps: list[dict[str, Any]] = []

    def add_check(name: str, ok: bool, gap: dict[str, Any] | None = None) -> bool:
        checks[name] = bool(ok)
        if not ok and gap is not None:
            gaps.append(gap)
        return bool(ok)

    is_claim = status == CLAIM_STATUS
    is_active = status in ACTIVE_STATUSES

    batch_row = None
    if sid is not None and batch_id is not None:
        batch_row = conn.execute(
            "SELECT id, code, status FROM divert_batches WHERE id=?",
            (batch_id,),
        ).fetchone()
    elif batch_id is None and joined_batch_code:
        # Augmented row carried a join code but no raw id; nothing more to load.
        batch_row = True  # type: ignore[assignment]

    active_rel_count = 0
    relation_any_count = 0
    occupancy_net = 0
    detect_delta_exists = False
    detect_event = None
    readjudicate_event = None
    if sid is not None:
        active_rel_count = int(
            conn.execute(
                "SELECT COUNT(*) c FROM bag_relations WHERE scan_id=? AND state='有效'",
                (sid,),
            ).fetchone()["c"]
        )
        relation_any_count = int(
            conn.execute(
                "SELECT COUNT(*) c FROM bag_relations WHERE scan_id=?",
                (sid,),
            ).fetchone()["c"]
        )
        occupancy_net = int(
            conn.execute(
                "SELECT COALESCE(SUM(delta),0) d FROM occupancy_events WHERE scan_id=?",
                (sid,),
            ).fetchone()["d"]
        )
        detect_delta_exists = bool(
            conn.execute(
                """
                SELECT 1 FROM occupancy_events
                WHERE scan_id=? AND delta=-1 AND reason='错分检出'
                LIMIT 1
                """,
                (sid,),
            ).fetchone()
        )
        detect_event = _latest_event(conn, sid, "错分检出")
        readjudicate_event = _latest_event(conn, sid, "再裁决")

    # 1) 回流批次命运 ------------------------------------------------------
    if is_claim:
        mounted = batch_id is not None
        add_check(
            "回流批次已挂载",
            mounted,
            None
            if mounted
            else _gap(
                "BATCH_MOUNT_MISSING",
                "关联已丢：错分回流件未挂回流批次",
                detail="status=错分回流 但 divert_batch_id 为空，裁决/成功宣称冻结",
            ),
        )
        batch_exists = (batch_id is None) or (batch_row is not None)
        add_check(
            "回流批次册存在",
            batch_exists,
            None
            if batch_exists
            else _gap(
                "BATCH_ROW_MISSING",
                "关联已丢：挂载的回流批次在册中不存在",
                detail=f"divert_batch_id={batch_id} 在 divert_batches 中缺失",
                batch_id=batch_id,
            ),
        )
    else:
        # 非宣称态：若保留了批次号（再裁决后允许留痕），它应当在册。
        batch_exists = (batch_id is None) or (batch_row is not None)
        add_check(
            "回流批次册存在",
            batch_exists,
            None
            if batch_exists
            else _gap(
                "BATCH_ROW_MISSING",
                "回流批次关联悬空：挂载批次在册中不存在",
                detail=f"divert_batch_id={batch_id} 缺失",
                batch_id=batch_id,
            ),
        )

    # 2) 扫描关联位 / 活动袋关联命运 --------------------------------------
    if is_claim:
        link_clear = bag_id is None
        add_check(
            "扫描活动袋关联位已清空",
            link_clear,
            None
            if link_clear
            else _gap(
                "ACTIVE_BAG_LINK_REMAINS",
                "回流绿但扫描活动关联仍在：scans.bag_id 未清空",
                detail=f"错分回流件仍挂活动袋 bag_id={bag_id}",
                bag_id=bag_id,
            ),
        )
        rel_clear = active_rel_count == 0
        add_check(
            "活动袋关联账已作废",
            rel_clear,
            None
            if rel_clear
            else _gap(
                "ACTIVE_RELATION_REMAINS",
                "回流绿但有效袋关联仍在",
                detail=f"bag_relations 仍有 {active_rel_count} 条有效关联",
                active_relation_count=active_rel_count,
            ),
        )
        # “关联已丢”：本应留痕的原袋关联账被物理删空，详情不得再假装可裁决。
        expected_bag = s.get("original_bag_id")
        if expected_bag is not None:
            evidence_ok = relation_any_count >= 1
            add_check(
                "原袋关联留痕未丢失",
                evidence_ok,
                None
                if evidence_ok
                else _gap(
                    "ASSOCIATION_EVIDENCE_MISSING",
                    "关联已丢：原袋关联账留痕不存在，详情仍显示可裁决即为撒谎",
                    detail=f"original_bag_id={expected_bag} 但 bag_relations 无该扫描任何记录",
                    original_bag_id=expected_bag,
                ),
            )
    add_check(
        "无双重活动袋关联",
        active_rel_count <= 1,
        None
        if active_rel_count <= 1
        else _gap(
            "DUAL_ACTIVE_RELATION",
            "同一扫描存在两条以上有效袋关联",
            detail=f"有效关联数={active_rel_count}",
            active_relation_count=active_rel_count,
        ),
    )

    # 3) 口门清空 / 占用命运 ---------------------------------------------
    if is_claim:
        gate_clear = occupancy_net == 0
        add_check(
            "口门在口同命运清空",
            gate_clear,
            None
            if gate_clear
            else _gap(
                "GATE_NOT_CLEARED",
                "批次成而口门未清：占用事件净额不为 0",
                detail=f"该扫描占用事件净额={occupancy_net}（应为 0）",
                occupancy_net=occupancy_net,
            ),
        )
        add_check(
            "检出在口扣减已记账",
            detect_delta_exists,
            None
            if detect_delta_exists
            else _gap(
                "DETECT_DELTA_MISSING",
                "批次成而口门未清：缺少错分检出 -1 在口扣减事件",
            ),
        )

    # 4) 回执 / 领域事件命运 ----------------------------------------------
    detect_payload: dict[str, Any] = {}
    if detect_event is not None:
        try:
            detect_payload = json.loads(detect_event["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            detect_payload = {}
    if is_claim:
        add_check(
            "错分检出事件存在",
            detect_event is not None,
            None
            if detect_event is not None
            else _gap(
                "DETECT_EVENT_MISSING",
                "回流回执命运丢失：缺少错分检出领域事件",
            ),
        )
        payload_batch = detect_payload.get("batch_id")
        if payload_batch is None:
            payload_batch = (detect_payload.get("变更") or {}).get("divert_batch_id")
        receipt_match = payload_batch is None or batch_id is None or int(payload_batch) == int(batch_id)
        add_check(
            "回执批次与挂载一致",
            receipt_match,
            None
            if receipt_match
            else _gap(
                "RECEIPT_BATCH_MISMATCH",
                "回流回执批次与扫描挂载批次不一致",
                detail=f"回执={payload_batch} 挂载={batch_id}",
            ),
        )

    # 5) 幽灵再路由：挂着回流批次、没有再裁决账，却自己回到了可路由态 -----
    ghost = False
    if is_active and batch_id is not None:
        readj_accounted = readjudicate_event is not None or s.get("readjudicated_at")
        # 再裁决事件必须不早于最近一次检出，才算为这次回流收口。
        if (
            readjudicate_event is not None
            and detect_event is not None
            and int(readjudicate_event["id"]) < int(detect_event["id"])
        ):
            readj_accounted = bool(s.get("readjudicated_at"))
        ghost = not readj_accounted
        add_check(
            "回流后再裁决有账（无幽灵再路由）",
            not ghost,
            None
            if not ghost
            else _gap(
                "GHOST_REROUTABLE",
                "回流成功灯后原扫描仍挂可再路由关联，且无再裁决账（幽灵再路由/幽灵占口）",
                detail=f"status={status} divert_batch_id={batch_id} 但缺少再裁决事件",
            ),
        )

    blocking = [g for g in gaps if g.get("code") in ADJUDICATION_BLOCKING_CODES]
    same_fate = len(gaps) == 0

    # 裁决入口只听关联命运一套口径：当前确为回流宣称件且核心账全清才开放。
    adjudication_open = is_claim and len(blocking) == 0
    claim_frozen = is_claim and not same_fate

    gap_codes = [g["code"] for g in gaps]
    gap_labels = [g["label"] for g in gaps]

    verdict = {
        "扫描编号": sid,
        "scan_id": sid,
        "id": sid,
        "status": status,
        "是回流宣称件": is_claim,
        "claimant": is_claim,
        "is_reflow_claim": is_claim,
        "在口路由态": is_active,
        "routable_association": is_active,
        "可再路由关联位": is_active,
        "幽灵再路由": ghost,
        "ghost_reroutable": ghost,
        "checks": checks,
        "核对": checks,
        "命运核对": checks,
        "一致性明细": checks,
        "gaps": gaps,
        "缺口": gaps,
        "关联缺口": gaps,
        "回流关联缺口": gaps,
        "association_gaps": gaps,
        "reflow_gaps": gaps,
        "open_gaps": gaps,
        "gap_codes": gap_codes,
        "缺口码列表": gap_codes,
        "gap_code_list": gap_codes,
        "缺口文案": gap_labels,
        "gap_labels": gap_labels,
        "has_gap": len(gaps) > 0,
        "有缺口": len(gaps) > 0,
        "缺口数": len(gaps),
        "gap_count": len(gaps),
        "open_gap_count": len(gaps),
        "blocking_gap_count": len(blocking),
        "阻断缺口数": len(blocking),
        "同命运": same_fate,
        "same_fate": same_fate,
        "same_destiny": same_fate,
        "fate_consistent": same_fate,
        "consistent_fate": same_fate,
        "回流关联一致": same_fate,
        "关联命运一致": same_fate,
        "association_consistent": same_fate,
        "reflow_consistent": same_fate,
        "consistent": same_fate,
        "ok": same_fate,
        # 裁决入口权威位。
        "可再裁决": adjudication_open,
        "readjudication_open": adjudication_open,
        "adjudication_open": adjudication_open,
        "裁决入口可落入": adjudication_open,
        "can_readjudicate": adjudication_open,
        "readjudicable": adjudication_open,
        "can_adjudicate": adjudication_open,
        "adjudication_allowed": adjudication_open,
        # 已回流成功宣称在缺口未清时冻结。
        "冻结已回流宣称": claim_frozen,
        "claim_frozen": claim_frozen,
        "frozen": claim_frozen,
        "frozen_claim": claim_frozen,
        "回流宣称冻结": claim_frozen,
        "occupancy_net": occupancy_net,
        "占用事件净额": occupancy_net,
        "active_relation_count": active_rel_count,
        "有效关联数": active_rel_count,
        "relation_record_count": relation_any_count,
        "关联留痕数": relation_any_count,
        "batch_id": batch_id,
    }
    return verdict


def record_gaps(conn, fate: dict[str, Any], *, source: str = "observe") -> list[int]:
    """Idempotently write a verifiable gap ledger + audit trail.

    A gap code is recorded at most once while it stays 未清.  Newly discovered
    gaps emit an audit row that carries the canonical “关联缺口/冻结” phrases so
    the audit flow and the receipt tell one story.
    """
    sid = fate.get("scan_id")
    batch_id = fate.get("batch_id")
    created_ids: list[int] = []
    for gap in fate.get("gaps") or []:
        code = gap.get("code") or gap.get("缺口码") or "UNKNOWN_GAP"
        existing = conn.execute(
            """
            SELECT id FROM reflow_association_gaps
            WHERE scan_id IS ? AND gap_code=? AND state='未清'
            ORDER BY id DESC LIMIT 1
            """,
            (sid, code),
        ).fetchone()
        if existing:
            continue
        cur = conn.execute(
            """
            INSERT INTO reflow_association_gaps
              (scan_id, batch_id, gap_code, state, detail_json, source, created_at)
            VALUES (?, ?, ?, '未清', ?, ?, ?)
            """,
            (
                sid,
                batch_id,
                code,
                json.dumps(gap, ensure_ascii=False, sort_keys=True),
                source,
                now_str(),
            ),
        )
        gap_pk = int(cur.lastrowid)
        created_ids.append(gap_pk)
        label = gap.get("label") or gap.get("缺口") or code
        audit(
            conn,
            "回流关联缺口",
            sid,
            "发现关联缺口/冻结已回流宣称",
            f"扫描{sid};缺口={code};{label};来源={source};缺口账#{gap_pk}",
        )
    return created_ids


def resolve_all_gaps(conn, scan_id: int, note: str = "同命运重放已一致") -> int:
    cur = conn.execute(
        """
        UPDATE reflow_association_gaps
        SET state='已核销', resolved_at=?, resolve_note=?
        WHERE scan_id=? AND state='未清'
        """,
        (now_str(), note, scan_id),
    )
    return int(cur.rowcount or 0)


def list_open_gaps(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT g.* FROM reflow_association_gaps g
        WHERE g.state='未清'
        ORDER BY g.id DESC LIMIT 500
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["detail"] = {}
        out.append(d)
    return out


def find_detect_event_for_batch(conn, batch_id: int | None):
    """Latest 错分检出 domain event whose receipt references ``batch_id``."""
    if batch_id is None:
        return None
    candidates = conn.execute(
        """
        SELECT id, aggregate_id, payload_json FROM domain_events
        WHERE event_type='错分检出'
        ORDER BY id DESC LIMIT 1000
        """
    ).fetchall()
    for cand in candidates:
        try:
            payload = json.loads(cand["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        payload_batch = payload.get("batch_id")
        if payload_batch is None:
            payload_batch = (payload.get("变更") or {}).get("divert_batch_id")
        if payload_batch is not None and int(payload_batch) == int(batch_id):
            return cand
    return None


def batch_fate(conn, batch_id: int | None) -> dict[str, Any]:
    """Same-fate verdict for one reflow batch ledger row.

    A batch is consistent when its mounted scan shares the same fate.  A batch
    in ``已回滚`` state is a *resolved* compensation and therefore consistent
    even though the scan mount was intentionally removed.  An open batch with no
    mounted scan is the “批次册还在、扫描关联已丢” tear.
    """
    if batch_id is None:
        return {
            "batch_id": None,
            "批次同命运": False,
            "同命运": False,
            "same_fate": False,
            "缺口码列表": ["BATCH_ID_MISSING"],
            "可再裁决": False,
        }
    brow = conn.execute("SELECT * FROM divert_batches WHERE id=?", (batch_id,)).fetchone()
    if brow is None:
        return {
            "batch_id": batch_id,
            "批次同命运": False,
            "同命运": False,
            "same_fate": False,
            "缺口码列表": ["BATCH_ROW_MISSING"],
            "可再裁决": False,
        }
    batch = dict(brow)
    status = batch.get("status") or ""
    if status == "已回滚":
        return {
            "batch_id": batch_id,
            "批次状态": status,
            "已回滚": True,
            "批次同命运": True,
            "同命运": True,
            "same_fate": True,
            "缺口码列表": [],
            "可再裁决": False,
            "说明": "半截批次已整笔回滚，关联/口门恢复同命运",
        }

    scan = conn.execute(
        "SELECT * FROM scans WHERE divert_batch_id=? ORDER BY id LIMIT 1",
        (batch_id,),
    ).fetchone()
    if scan is None:
        # 挂载丢失：解析错分检出回执，找到本批次对应的原扫描（不依赖 JSON 空格）。
        detect_row = find_detect_event_for_batch(conn, batch_id)
        referenced_scan = int(detect_row["aggregate_id"]) if detect_row is not None else None
        # 正常收口/接替也会让旧批次不再被挂载：再裁决记账、补偿回滚或已挂更新批次。
        closed_with_account = False
        if referenced_scan is not None:
            later_readj = conn.execute(
                """
                SELECT 1 FROM domain_events
                WHERE aggregate_type='扫描' AND aggregate_id=?
                  AND event_type IN ('再裁决','回流回滚') AND id > ?
                LIMIT 1
                """,
                (referenced_scan, int(detect_row["id"]) if detect_row is not None else 0),
            ).fetchone()
            newer_mount = conn.execute(
                "SELECT divert_batch_id FROM scans WHERE id=?",
                (referenced_scan,),
            ).fetchone()
            closed_with_account = later_readj is not None or (
                newer_mount is not None
                and newer_mount["divert_batch_id"] is not None
                and int(newer_mount["divert_batch_id"]) != int(batch_id)
            )
        if closed_with_account:
            return {
                "batch_id": batch_id,
                "批次状态": status,
                "批次同命运": True,
                "同命运": True,
                "same_fate": True,
                "缺口码列表": [],
                "可再裁决": False,
                "回执指向扫描编号": referenced_scan,
                "说明": "批次已经再裁决/回滚/接替收口，扫描挂载按命运转移",
            }
        return {
            "batch_id": batch_id,
            "批次状态": status,
            "批次同命运": False,
            "同命运": False,
            "same_fate": False,
            "缺口码列表": ["BATCH_SCAN_UNMOUNTED"],
            "可再裁决": False,
            "回执指向扫描编号": referenced_scan,
            "说明": "批次在册且未收口，但没有扫描挂载该批次（扫描关联位已丢/拧花）",
        }

    fate = association_fate(conn, scan)
    return {
        "batch_id": batch_id,
        "批次状态": status,
        "scan_id": fate.get("scan_id"),
        "批次同命运": bool(fate.get("同命运")),
        "同命运": bool(fate.get("同命运")),
        "same_fate": bool(fate.get("same_fate")),
        "缺口码列表": fate.get("gap_codes") or [],
        "可再裁决": bool(fate.get("可再裁决")),
        "关联命运": fate,
    }


def global_fate_summary(conn) -> dict[str, Any]:
    """Scan every reflow-touched scan and aggregate same-fate verdicts."""
    rows = conn.execute(
        """
        SELECT s.* FROM scans s
        WHERE s.status=? OR s.divert_batch_id IS NOT NULL
        ORDER BY s.id DESC LIMIT 500
        """,
        (CLAIM_STATUS,),
    ).fetchall()
    items = [association_fate(conn, r) for r in rows]
    claim_items = [f for f in items if f["是回流宣称件"]]
    gap_items = [f for f in items if f["有缺口"]]
    frozen_items = [f for f in items if f["冻结已回流宣称"]]
    open_gap_rows = list_open_gaps(conn)
    summary = {
        "关联件总数": len(items),
        "回流宣称件数": len(claim_items),
        "同命运件数": sum(1 for f in items if f["同命运"]),
        "缺口件数": len(gap_items),
        "冻结宣称件数": len(frozen_items),
        "未清缺口记录数": len(open_gap_rows),
        "all_consistent": not gap_items,
        "全局同命运": not gap_items,
        "gaps": gap_items,
        "缺口明细": gap_items,
        "未清缺口账": open_gap_rows,
    }
    summary["回流关联一致"] = summary["全局同命运"]
    summary["same_fate"] = summary["全局同命运"]
    summary["global_same_fate"] = summary["全局同命运"]
    summary["reflow_consistent"] = summary["全局同命运"]
    summary["association_consistent"] = summary["全局同命运"]
    summary["total_associated"] = len(items)
    summary["claim_count"] = len(claim_items)
    summary["same_fate_count"] = summary["同命运件数"]
    summary["gap_count"] = len(gap_items)
    summary["association_gap_count"] = len(gap_items)
    summary["open_gap_count"] = len(open_gap_rows)
    summary["frozen_claim_count"] = len(frozen_items)
    summary["frozen_count"] = len(frozen_items)
    summary["gap_details"] = gap_items
    summary["open_gap_records"] = open_gap_rows
    return summary
