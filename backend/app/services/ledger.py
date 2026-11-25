from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4

from app.db import row_to_dict, rows_to_list
from app.services.common import now_str

# 全局唯一口径：检出立即扣减原口门在口；活动袋关联在同一事务内作废，
# 但原袋/批次/事件留痕保留，并继续开放“再裁决”。
POLICY = {
    "架构": "DEDUCT_OCCUPANCY_PRESERVE_READJUDICATION",
    "口径": "扣减在口，保留原袋与回流批次留痕并开放再裁决",
    "检出扣减原口门在口": True,
    "清空活动袋关联": True,
    "原袋关联静默消失": False,
    "保留原袋编号": True,
    "允许原活动袋直接再收": False,
    "作废后开放再裁决": True,
    "所有结局可审计重放": True,
}

ACTIVE_OCCUPANCY_STATUSES = ("已路由", "已入袋")

SCAN_SELECT = """
SELECT s.*,
       c.code AS 口门码,
       COALESCE(oc.code, c.code) AS 原口门码,
       b.code AS 当前袋码,
       ob.code AS 原袋码,
       db.code AS 回流批次号,
       db.status AS 回流批次状态
FROM scans s
LEFT JOIN chutes c ON c.id = s.chute_id
LEFT JOIN chutes oc ON oc.id = s.original_chute_id
LEFT JOIN bags b ON b.id = s.bag_id
LEFT JOIN bags ob ON ob.id = s.original_bag_id
LEFT JOIN divert_batches db ON db.id = s.divert_batch_id
"""

CHUTE_SELECT = """
SELECT c.*, l.name AS 分拣线名,
       (
         SELECT COUNT(*) FROM scans s
         WHERE s.chute_id = c.id
           AND s.status IN ('已路由', '已入袋')
       ) AS 在口件数,
       COALESCE((
         SELECT SUM(o.delta) FROM occupancy_events o
         WHERE o.chute_id = c.id
       ), 0) AS 占用事件余额
FROM chutes c
JOIN lines l ON l.id = c.line_id
"""


def augment_scan(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    item = row_to_dict(row) if isinstance(row, sqlite3.Row) else dict(row)
    if not item:
        return None
    status = item.get("status")
    active = status in ACTIVE_OCCUPANCY_STATUSES
    original_bag_id = item.get("original_bag_id")
    bag_id = item.get("bag_id")
    if status == "错分回流":
        relation = "活动袋关联已作废；原袋留痕可审计，须先再裁决" if original_bag_id else "无袋关联；可再裁决"
    elif bag_id:
        relation = "有效"
    else:
        relation = "无活动袋关联"
    item["在口"] = active
    item["occupancy_active"] = active
    item["state"] = status
    item["scan_status"] = status
    item["原袋编号"] = original_bag_id
    item["活动袋编号"] = bag_id
    item["source_bag_id"] = original_bag_id
    item["originalBagId"] = original_bag_id
    item["former_bag_id"] = original_bag_id
    item["voided_bag_id"] = original_bag_id if status == "错分回流" and bag_id is None else None
    item["return_batch_id"] = item.get("divert_batch_id")
    item["divert_batch_code"] = item.get("回流批次号")
    item["原袋关联状态"] = relation
    item["关联状态"] = relation
    item["可再裁决"] = status == "错分回流"
    item["readjudication_open"] = status == "错分回流"
    item["错分检出入口"] = f"/api/扫描/{item['id']}/错分检出" if status in ("已路由", "已入袋") else None
    item["再裁决入口"] = f"/api/扫描/{item['id']}/再裁决" if status == "错分回流" else None
    item["readjudication_endpoint"] = item["再裁决入口"]
    item["重放入口"] = f"/api/错分检出/{item['id']}/重放" if item.get("divert_batch_id") else None
    item["replay_url"] = f"/api/diverts/{item['id']}/replay" if item.get("divert_batch_id") else None
    item["replay_endpoint"] = item["重放入口"]
    return item


def augment_bag_relation(row: sqlite3.Row | dict | None) -> dict | None:
    item = row_to_dict(row) if isinstance(row, sqlite3.Row) else (dict(row) if row is not None else None)
    if item is None:
        return None
    state = item.get("state")
    item["state_code"] = {"有效": "active", "已作废": "voided"}.get(state, state)
    item["active"] = state == "有效"
    item["voided"] = state == "已作废"
    return item


def list_scans(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    if status:
        rows = conn.execute(
            SCAN_SELECT + " WHERE s.status=? ORDER BY s.id DESC LIMIT 500",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(SCAN_SELECT + " ORDER BY s.id DESC LIMIT 500").fetchall()
    return [augment_scan(r) for r in rows]


def get_scan(conn: sqlite3.Connection, scan_id: int) -> dict | None:
    row = conn.execute(SCAN_SELECT + " WHERE s.id=?", (scan_id,)).fetchone()
    return augment_scan(row)


def find_scan_by_barcode(conn: sqlite3.Connection, barcode: str) -> dict | None:
    row = conn.execute(
        SCAN_SELECT
        + """
        WHERE s.barcode=?
        ORDER BY s.id DESC LIMIT 1
        """,
        (barcode,),
    ).fetchone()
    return augment_scan(row)


def list_chutes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(CHUTE_SELECT + " ORDER BY c.code").fetchall()
    result = rows_to_list(rows)
    for item in result:
        count = int(item.get("在口件数") or 0)
        balance = int(item.get("占用事件余额") or 0)
        item["在口数"] = count
        item["在口数量"] = count
        item["occupancy"] = count
        item["occupancy_count"] = count
        item["current_occupancy"] = count
        item["at_chute_count"] = count
        item["chute_occupancy"] = count
        item["occupied_count"] = count
        item["在口一致"] = count == balance
        item["占用事件一致"] = count == balance
    return result


def get_chute(conn: sqlite3.Connection, chute_id: int) -> dict | None:
    row = conn.execute(CHUTE_SELECT + " WHERE c.id=?", (chute_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    count = int(item.get("在口件数") or 0)
    item["在口数"] = count
    item["在口数量"] = count
    item["occupancy"] = count
    item["occupancy_count"] = count
    item["current_occupancy"] = count
    item["at_chute_count"] = count
    item["chute_occupancy"] = count
    item["occupied_count"] = count
    item["在口一致"] = count == int(item.get("占用事件余额") or 0)
    item["占用事件一致"] = count == int(item.get("占用事件余额") or 0)
    return item


def append_domain_event(
    conn: sqlite3.Connection,
    event_type: str,
    aggregate_id: int,
    payload: dict[str, Any],
    aggregate_type: str = "扫描",
    event_uid: str | None = None,
) -> dict:
    uid = event_uid or f"{event_type}-{aggregate_type}-{aggregate_id}-{uuid4().hex}"
    existing = conn.execute(
        "SELECT * FROM domain_events WHERE event_uid=?", (uid,)
    ).fetchone()
    if existing:
        return row_to_dict(existing)
    created_at = now_str()
    body = dict(payload)
    body.setdefault("事件类型", event_type)
    body.setdefault("聚合类型", aggregate_type)
    body.setdefault("聚合编号", aggregate_id)
    body.setdefault("发生时间", created_at)
    conn.execute(
        """
        INSERT INTO domain_events
          (event_uid, aggregate_type, aggregate_id, event_type, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            uid,
            aggregate_type,
            aggregate_id,
            event_type,
            json.dumps(body, ensure_ascii=False, sort_keys=True),
            created_at,
        ),
    )
    return row_to_dict(
        conn.execute("SELECT * FROM domain_events WHERE event_uid=?", (uid,)).fetchone()
    )


def add_occupancy_event(
    conn: sqlite3.Connection,
    *,
    chute_id: int | None,
    scan_id: int,
    delta: int,
    reason: str,
    shift_id: int | None = None,
    note: str = "",
    event_uid: str | None = None,
) -> dict | None:
    if chute_id is None or delta == 0:
        return None
    uid = event_uid or f"occupancy-{reason}-scan-{scan_id}-{uuid4().hex}"
    conn.execute(
        """
        INSERT INTO occupancy_events
          (event_uid, shift_id, chute_id, scan_id, delta, reason, created_at, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (uid, shift_id, chute_id, scan_id, delta, reason, now_str(), note),
    )
    return row_to_dict(
        conn.execute("SELECT * FROM occupancy_events WHERE event_uid=?", (uid,)).fetchone()
    )


def add_bag_relation(
    conn: sqlite3.Connection,
    *,
    scan_id: int,
    bag_id: int,
    chute_id: int | None,
    reason: str = "入袋",
    event_uid: str = "",
) -> int:
    created = now_str()
    cur = conn.execute(
        """
        INSERT INTO bag_relations
          (scan_id, bag_id, chute_id, state, created_at, reason, event_uid)
        VALUES (?, ?, ?, '有效', ?, ?, ?)
        """,
        (scan_id, bag_id, chute_id, created, reason, event_uid),
    )
    return int(cur.lastrowid)


def void_bag_relation(
    conn: sqlite3.Connection,
    *,
    scan_id: int,
    bag_id: int | None,
    reason: str,
    event_uid: str = "",
) -> dict | None:
    if bag_id is None:
        return None
    row = conn.execute(
        """
        SELECT * FROM bag_relations
        WHERE scan_id=? AND bag_id=? AND state='有效'
        ORDER BY id DESC LIMIT 1
        """,
        (scan_id, bag_id),
    ).fetchone()
    voided_at = now_str()
    if row:
        conn.execute(
            "UPDATE bag_relations SET state='已作废', voided_at=?, reason=?, event_uid=? WHERE id=?",
            (voided_at, reason, event_uid, row["id"]),
        )
        relation_id = row["id"]
    else:
        cur = conn.execute(
            """
            INSERT INTO bag_relations
              (scan_id, bag_id, chute_id, state, created_at, voided_at, reason, event_uid)
            VALUES (?, ?, NULL, '已作废', ?, ?, ?, ?)
            """,
            (scan_id, bag_id, voided_at, voided_at, reason, event_uid),
        )
        relation_id = cur.lastrowid
    return augment_bag_relation(conn.execute("SELECT * FROM bag_relations WHERE id=?", (relation_id,)).fetchone())


def overview_snapshot(conn: sqlite3.Connection) -> dict:
    shift = conn.execute("SELECT * FROM shifts ORDER BY id DESC LIMIT 1").fetchone()
    count_rows = conn.execute("SELECT status, COUNT(*) c FROM scans GROUP BY status").fetchall()
    counts = {r["status"]: r["c"] for r in count_rows}
    for status in ("待路由", "已路由", "错分回流", "已入袋", "已封袋"):
        counts.setdefault(status, 0)
    chutes = list_chutes(conn)
    wrong_count = int(counts.get("错分回流", 0))
    wrong_items = [
        augment_scan(r)
        for r in conn.execute(
            SCAN_SELECT + " WHERE s.status='错分回流' ORDER BY s.id DESC LIMIT 50"
        ).fetchall()
    ]
    bucket = {
        "键": "错分回流",
        "标签": "错分检出/回流桶",
        "数量": wrong_count,
        "count": wrong_count,
        "亮": wrong_count > 0,
        "lit": wrong_count > 0,
        "active": wrong_count > 0,
        "扫描": wrong_items,
    }
    return {
        "班次": row_to_dict(shift),
        "分布": counts,
        "口门": chutes,
        "错分桶": bucket,
        "divert_bucket": bucket,
        "错分桶数量": wrong_count,
        "divert_count": wrong_count,
        "错分桶亮": wrong_count > 0,
        "divert_bucket_lit": wrong_count > 0,
        "policy": POLICY,
        "口径": POLICY["口径"],
    }


def replay_scan(conn: sqlite3.Connection, scan_id: int) -> dict:
    scan = get_scan(conn, scan_id)
    events = rows_to_list(
        conn.execute(
            """
            SELECT * FROM domain_events
            WHERE aggregate_type='扫描' AND aggregate_id=?
            ORDER BY id ASC
            """,
            (scan_id,),
        ).fetchall()
    )
    parsed_events = []
    folded: dict[str, Any] = {
        "状态": "未装载",
        "status": "未装载",
        "chute_id": None,
        "bag_id": None,
        "original_bag_id": None,
        "divert_batch_id": None,
    }
    latest_detection = None
    for event in events:
        try:
            payload = json.loads(event.pop("payload_json") or "{}")
        except json.JSONDecodeError:
            payload = {}
        event["payload"] = payload
        parsed_events.append(event)
        changes = payload.get("变更") or {}
        for key, value in changes.items():
            folded[key] = value
        # Keep the Chinese fold key in sync as well; older events only carried
        # original_bag_id at the payload top level.
        if changes.get("status") is not None:
            folded["状态"] = changes["status"]
        if "original_bag_id" not in changes and payload.get("original_bag_id") is not None:
            folded["original_bag_id"] = payload.get("original_bag_id")
        if "original_chute_id" not in changes and payload.get("old_chute_id") is not None:
            folded["original_chute_id"] = payload.get("old_chute_id")
        if event["event_type"] == "错分检出":
            latest_detection = payload

    occupancy = rows_to_list(
        conn.execute(
            "SELECT * FROM occupancy_events WHERE scan_id=? ORDER BY id ASC",
            (scan_id,),
        ).fetchall()
    )
    relations = [
        augment_bag_relation(row)
        for row in conn.execute(
            "SELECT * FROM bag_relations WHERE scan_id=? ORDER BY id ASC",
            (scan_id,),
        ).fetchall()
    ]
    occupancy_total = sum(int(e["delta"]) for e in occupancy)
    current = scan or {}
    folded_original_bag = folded.get("original_bag_id")
    current_original_bag = current.get("original_bag_id")
    original_bag_consistent = folded_original_bag == current_original_bag or (
        folded_original_bag is None
        and current.get("bag_id") is not None
        and current_original_bag == current.get("bag_id")
    )
    consistency = {
        "事件存在": len(events) > 0,
        "状态一致": folded.get("status") == current.get("status") or folded.get("状态") == current.get("status"),
        "活动袋一致": folded.get("bag_id") == current.get("bag_id"),
        "回流批次一致": folded.get("divert_batch_id") == current.get("divert_batch_id"),
        "原袋留痕一致": original_bag_consistent,
        "占用事件净额等于当前在口": occupancy_total == (1 if current.get("在口") else 0),
        "无活动双袋关联": sum(1 for r in relations if r.get("state") == "有效") <= 1,
        "检出必有批次": current.get("status") != "错分回流" or current.get("divert_batch_id") is not None,
    }
    consistent = all(consistency.values()) and scan is not None
    result = {
        "扫描编号": scan_id,
        "scan_id": scan_id,
        "扫描": scan,
        "scan": scan,
        "事件": parsed_events,
        "events": parsed_events,
        "占用事件": occupancy,
        "occupancy_events": occupancy,
        "袋关联": relations,
        "bag_relations": relations,
        "折叠结局": folded,
        "folded_state": folded,
        "最新检出结局": latest_detection,
        "latest_detection": latest_detection,
        "占用事件净额": occupancy_total,
        "occupancy_total": occupancy_total,
        "重放一致": consistent,
        "consistent": consistent,
        "ok": consistent,
        "一致性": consistency,
        "consistency": consistency,
        "口径": POLICY,
        "policy": POLICY,
    }
    return result
