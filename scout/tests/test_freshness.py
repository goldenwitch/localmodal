from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def test_freshness() -> bool:
    print("freshness ledger:")
    from datetime import date

    fresh = _resource_module("freshness")
    today = date(2026, 7, 17)
    entries = {
        "young": {"corpus": "docs", "fetched": "2026-07-10", "ttl_days": 30,
                   "artifact": ".", "refresh": "refetch-young"},
        "old": {"corpus": "docs", "fetched": "2026-06-01", "ttl_days": 30,
                 "artifact": ".", "refresh": "refetch-old"},
        "immutable": {"corpus": "papers", "fetched": "2020-01-01", "ttl_days": None,
                       "artifact": ".", "refresh": "refetch-imm"},
        "gone": {"corpus": "papers", "fetched": "2026-07-17", "ttl_days": None,
                  "artifact": "no-such-dir-xyz", "refresh": "refetch-gone"},
    }
    docs_w = fresh.warnings_for("docs", entries, today)
    papers_w = fresh.warnings_for("papers", entries, today)
    ok = _check("within TTL is silent", not any("young" in w for w in docs_w))
    ok &= _check("past TTL screams with fix",
                 any("STALE source 'old'" in w and "refetch-old" in w and "16d overdue" in w
                     for w in docs_w))
    ok &= _check("immutable never screams", not any("immutable" in w for w in papers_w))
    ok &= _check("absent artifact screams with fix",
                 any("ABSENT source 'gone'" in w and "refetch-gone" in w for w in papers_w))
    ok &= _check("corpora isolated", not any("gone" in w for w in docs_w))
    return ok
