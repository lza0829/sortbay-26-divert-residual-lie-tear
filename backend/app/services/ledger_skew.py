"""Identity shim.

The previous skew helpers overwrote truthful scan/overview readings with
painted ``surface_last`` values.  Read models now derive everything from the
live ledger and the reflow same-fate verdict, so these helpers intentionally
return their input unchanged.
"""
from __future__ import annotations


def skew_scan_row(row: dict, conn=None) -> dict:
    return dict(row or {})


def skew_list(rows, conn=None):
    return [skew_scan_row(r, conn) for r in (rows or [])]


def skew_overview(base: dict, conn=None) -> dict:
    return base or {}
