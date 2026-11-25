from __future__ import annotations

import os
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app import config

DB_PATH = config.DB_PATH
_INITIAL_DB_PATH = DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS shifts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL,
  name TEXT NOT NULL,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  UNIQUE(business_day, name)
);
CREATE TABLE IF NOT EXISTS chutes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  line_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '口门开',
  soft_cap INTEGER NOT NULL DEFAULT 200,
  UNIQUE(line_id, code),
  FOREIGN KEY(line_id) REFERENCES lines(id)
);
CREATE TABLE IF NOT EXISTS route_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dest_code TEXT NOT NULL UNIQUE,
  chute_id INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  barcode TEXT NOT NULL,
  dest_code TEXT NOT NULL DEFAULT '',
  shift_id INTEGER NOT NULL,
  line_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT '待路由',
  chute_id INTEGER,
  bag_id INTEGER,
  divert_batch_id INTEGER,
  scanned_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  original_chute_id INTEGER,
  original_bag_id INTEGER,
  detected_at TEXT,
  readjudicated_at TEXT,
  FOREIGN KEY(shift_id) REFERENCES shifts(id),
  FOREIGN KEY(line_id) REFERENCES lines(id),
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS route_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id INTEGER NOT NULL,
  chute_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(scan_id) REFERENCES scans(id),
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS divert_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shift_id INTEGER NOT NULL,
  code TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT '回流中',
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(shift_id) REFERENCES shifts(id)
);
CREATE TABLE IF NOT EXISTS bags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shift_id INTEGER NOT NULL,
  chute_id INTEGER NOT NULL,
  code TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT '集包中',
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(shift_id) REFERENCES shifts(id),
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  ref_id INTEGER,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_uid TEXT NOT NULL UNIQUE,
  aggregate_type TEXT NOT NULL,
  aggregate_id INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_domain_events_aggregate
  ON domain_events(aggregate_type, aggregate_id, id);
CREATE TABLE IF NOT EXISTS occupancy_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_uid TEXT NOT NULL UNIQUE,
  shift_id INTEGER,
  chute_id INTEGER NOT NULL,
  scan_id INTEGER,
  delta INTEGER NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_occupancy_chute
  ON occupancy_events(chute_id, id);
CREATE TABLE IF NOT EXISTS bag_relations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id INTEGER NOT NULL,
  bag_id INTEGER NOT NULL,
  chute_id INTEGER,
  state TEXT NOT NULL DEFAULT '有效',
  created_at TEXT NOT NULL,
  voided_at TEXT,
  reason TEXT NOT NULL DEFAULT '',
  event_uid TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bag_relations_scan ON bag_relations(scan_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bag_relations_active_scan
  ON bag_relations(scan_id) WHERE state='有效';
"""

SCAN_COLUMNS = {
    "version": "INTEGER NOT NULL DEFAULT 1",
    "original_chute_id": "INTEGER",
    "original_bag_id": "INTEGER",
    "detected_at": "TEXT",
    "readjudicated_at": "TEXT",
}


def _resolve_db_path() -> Path:
    """Resolve the path lazily so common test monkeypatch styles all work."""
    global_path = globals().get("DB_PATH")
    if global_path is not None and Path(global_path) != Path(_INITIAL_DB_PATH):
        return Path(global_path)

    config_path = getattr(config, "DB_PATH", None)
    if config_path is not None and Path(config_path) != Path(_INITIAL_DB_PATH):
        return Path(config_path)

    env_path = os.environ.get("SORTBAY_DB")
    if env_path and Path(env_path) != Path(_INITIAL_DB_PATH):
        return Path(env_path)

    return Path(global_path or config_path or _INITIAL_DB_PATH)


def connect() -> sqlite3.Connection:
    db_path = _resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_db():
    """Serialize write transactions and make the whole use case atomic."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(scans)")}
    for name, ddl in SCAN_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE scans ADD COLUMN {name} {ddl}")

    # Existing active bag links remain queryable after the upgrade.
    conn.execute(
        """
        UPDATE scans SET original_bag_id=bag_id
        WHERE bag_id IS NOT NULL AND original_bag_id IS NULL
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO bag_relations
          (scan_id, bag_id, chute_id, state, created_at, reason)
        SELECT s.id, s.bag_id, b.chute_id, '有效', COALESCE(b.sealed_at, s.scanned_at), '存量关联回填'
        FROM scans s JOIN bags b ON b.id=s.bag_id
        WHERE s.bag_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM bag_relations r
            WHERE r.scan_id=s.id AND r.bag_id=s.bag_id AND r.state='有效'
          )
        """
    )
    # Make the occupancy event ledger reconcile with pre-existing routed items.
    conn.execute(
        """
        INSERT OR IGNORE INTO occupancy_events
          (event_uid, shift_id, chute_id, scan_id, delta, reason, created_at, note)
        SELECT 'backfill-occupancy-' || s.id, s.shift_id, s.chute_id, s.id, 1,
               '存量在口回填', s.scanned_at, s.status
        FROM scans s
        WHERE s.chute_id IS NOT NULL
          AND s.status IN ('已路由', '已入袋')
        """
    )

    # Upgrade old rows without event history into a small deterministic chain.
    # This is a migration reconstruction, not an attempt to pretend the exact
    # historical timestamps/actions were known.
    eventless_scans = conn.execute(
        """
        SELECT s.* FROM scans s
        WHERE NOT EXISTS (
          SELECT 1 FROM domain_events e
          WHERE e.aggregate_type='扫描' AND e.aggregate_id=s.id
        )
        """
    ).fetchall()
    for scan in eventless_scans:
        sid = scan["id"]
        ts = scan["scanned_at"]

        def add_legacy_event(uid_suffix: str, event_type: str, changes: dict, extra: dict | None = None) -> None:
            payload = {"变更": changes, "迁移回填": True}
            if extra:
                payload.update(extra)
            conn.execute(
                """
                INSERT OR IGNORE INTO domain_events
                  (event_uid, aggregate_type, aggregate_id, event_type,
                   payload_json, created_at)
                VALUES (?, '扫描', ?, ?, ?, ?)
                """,
                (
                    f"legacy-{uid_suffix}-scan-{sid}",
                    sid,
                    event_type,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    ts,
                ),
            )

        add_legacy_event(
            "load",
            "扫描装载",
            {"status": "待路由", "dest_code": scan["dest_code"]},
            {"barcode": scan["barcode"], "shift_id": scan["shift_id"], "line_id": scan["line_id"]},
        )
        if scan["status"] != "待路由" and scan["chute_id"] is not None:
            add_legacy_event(
                "route",
                "路由落入",
                {"status": "已路由", "chute_id": scan["chute_id"], "dest_code": scan["dest_code"]},
                {"chute_id": scan["chute_id"], "occupancy_delta": 1},
            )
        if scan["status"] in ("已入袋", "已封袋") and scan["bag_id"] is not None:
            add_legacy_event(
                "bag-add",
                "入袋",
                {"status": "已入袋", "bag_id": scan["bag_id"]},
                {"bag_id": scan["bag_id"], "occupancy_delta": 0},
            )
        if scan["status"] == "错分回流":
            add_legacy_event(
                "detect",
                "错分检出",
                {
                    "status": "错分回流",
                    "bag_id": None,
                    "original_bag_id": scan["original_bag_id"],
                    "original_chute_id": scan["original_chute_id"] or scan["chute_id"],
                    "divert_batch_id": scan["divert_batch_id"],
                },
                {
                    "old_chute_id": scan["chute_id"],
                    "original_bag_id": scan["original_bag_id"],
                    "batch_id": scan["divert_batch_id"],
                    "occupancy_delta": -1,
                },
            )
        elif scan["status"] == "已封袋":
            add_legacy_event(
                "seal",
                "封袋",
                {"status": "已封袋", "bag_id": scan["bag_id"]},
                {"bag_id": scan["bag_id"], "occupancy_delta": -1},
            )


def init_db() -> None:
    _resolve_db_path().parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            # Some constrained/network filesystems do not support WAL.
            pass
        conn.executescript(SCHEMA)
        _migrate(conn)


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]
