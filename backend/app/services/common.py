from __future__ import annotations
import re
from datetime import datetime
from fastapi import HTTPException
from app.db import db, row_to_dict


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def current_shift(conn):
    row = conn.execute(
        "SELECT * FROM shifts WHERE sealed=0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise HTTPException(400, "无未封班班次")
    return row


def audit(conn, kind: str, ref_id: int | None, action: str, note: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_events(kind, ref_id, action, created_at, note) VALUES (?, ?, ?, ?, ?)",
        (kind, ref_id, action, now_str(), note),
    )


def require_unsealed(conn) -> None:
    s = current_shift(conn)
    if s["sealed"]:
        raise HTTPException(400, "班次已封班，禁止写口")


def scan_audit_clause(scan_id: int, *, note_column: str = "note") -> tuple[str, list[object]]:
    """Match audit rows for one scan without treating id 1 as a prefix of 10."""
    patterns = [
        f"%扫描{scan_id};%",
        f"%扫描{scan_id} %",
        f"%扫描{scan_id}，%",
        f"%扫描{scan_id})%",
        f"%扫描{scan_id}",
    ]
    clause = (
        "((ref_id=? AND kind IN ('扫描', '错分检出', 'scan', 'divert')) OR "
        f"{note_column}=? OR "
        + " OR ".join(f"{note_column} LIKE ?" for _ in patterns)
        + ")"
    )
    params: list[object] = [scan_id, str(scan_id), *patterns]
    return clause, params
