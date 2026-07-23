from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def test_activation_marker() -> bool:
    print("source-control activation marker:")
    import tempfile
    from pathlib import Path

    from resources.activation import activation_path
    from . import server

    fresh = _resource_module("freshness")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        resources_root = root / "resources"
        marker = activation_path(resources_root)
        marker.parent.mkdir(parents=True)
        marker.write_text("source-control-v1\n", encoding="utf-8")
        original_freshness_root = fresh.ROOT
        original_server_root = server.ROOT
        fresh.ROOT = resources_root
        server.ROOT = root
        try:
            try:
                fresh.require_legacy_writer()
            except RuntimeError:
                writer_blocked = True
            else:
                writer_blocked = False
            try:
                with fresh.legacy_reader_session():
                    pass
            except RuntimeError:
                reader_blocked = True
            else:
                reader_blocked = False
            ok = _check(
                "marker blocks legacy routing without CURRENT",
                fresh.source_control_active()
                and server._source_activation_present()
                and writer_blocked
                and reader_blocked,
            )
        finally:
            fresh.ROOT = original_freshness_root
            server.ROOT = original_server_root
    return ok
