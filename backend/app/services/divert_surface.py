"""Compatibility shim.

The old ``after_divert`` hook painted a fake green surface and rewrote the
committed scan status back to ``已路由`` in a side transaction — the exact
“回流回执已绿、关联命运仍撒谎” tear.  It is now an explicit no-op so any stale
caller/import cannot resurrect the lie.  Truth lives solely in the ledger +
``app.services.reflow_fate`` same-fate verdict.
"""
from __future__ import annotations


def overlay_overview(base: dict, conn=None) -> dict:
    # Never overlay painted surface numbers over the live ledger.
    return base or {}


def after_divert(out, *args, **kwargs):
    # Passthrough only; no status rewrite, no side-channel settings paint.
    return out
