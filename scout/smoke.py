#!/usr/bin/env python3
"""Smoke tests for scout. $0, no server needed — calls the tool functions
directly (they are plain functions; FastMCP registration doesn't wrap them).

    python -m scout.smoke          # sanitize + DPAPI + corpus round-trip
    python -m scout.smoke --mcp    # also a real stdio handshake + tool call
    python -m scout.smoke --web    # also one live grounded web query

The corpus and --mcp legs each pay one index+model load in a fresh worker
(minutes on a small machine). Deterministic: there are no internal timeouts
to race — each leg finishes or reports EOF.
"""
from __future__ import annotations

import sys

from .tests.test_sanitize import test_sanitize
from .tests.test_dpapi import test_dpapi
from .tests.test_freshness import test_freshness
from .tests.test_durable import test_durable
from .tests.test_activation_marker import test_activation_marker
from .tests.test_config import test_config
from .tests.test_source_model import test_source_model
from .tests.test_ledger import test_ledger
from .tests.test_materializer import test_materializer
from .tests.test_publication import test_publication
from .tests.test_source_control import test_source_control
from .tests.test_source_migration import test_source_migration
from .tests.test_vine import test_vine
from .tests.test_index_metadata import test_index_metadata
from .tests.test_legacy_activation_guard import test_legacy_activation_guard
from .tests.test_machine_output import test_machine_output
from .tests.test_corpus import test_corpus
from .tests.test_web import test_web
from .tests.test_mcp import test_mcp

def main() -> int:
    results = [test_sanitize(), test_dpapi(), test_freshness(), test_durable(), test_activation_marker(), test_config(), test_source_model(), test_ledger(), test_materializer(), test_publication(), test_source_control(), test_source_migration(), test_vine(),
               test_index_metadata(), test_legacy_activation_guard(), test_machine_output(), test_corpus()]
    if "--mcp" in sys.argv:
        results.append(test_mcp())
    if "--web" in sys.argv:
        results.append(test_web())
    print("PASS" if all(results) else "FAIL")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
