from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def test_durable() -> bool:
    print("durable source state:")
    import tempfile
    from pathlib import Path

    durable = _resource_module("durable")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source"
        destination = root / "destination"
        source.write_text("new", encoding="utf-8")
        destination.write_text("old", encoding="utf-8")
        durable.replace(source, destination)
        ok = _check(
            "replace updates authoritative path",
            not source.exists() and destination.read_text(encoding="utf-8") == "new",
        )
        durable.unlink(destination)
        ok &= _check(
            "unlink removes authoritative path",
            not destination.exists() and not list(root.glob("*.deleted")),
        )
        if durable.os.name == "nt":
            from unittest.mock import patch

            cleanup_source = root / "cleanup-source"
            cleanup_source.write_text("cleanup", encoding="utf-8")
            try:
                with patch.object(Path, "unlink", side_effect=PermissionError("fixture cleanup failure")):
                    durable.unlink(cleanup_source)
            except OSError:
                cleanup_succeeds = False
            else:
                cleanup_succeeds = True
            cleanup_tombstones = list(root.glob(".cleanup-source.*.deleted"))
            ok &= _check(
                "Windows tombstone cleanup cannot undo unlink",
                cleanup_succeeds and not cleanup_source.exists() and len(cleanup_tombstones) == 1,
            )
            for tombstone in cleanup_tombstones:
                tombstone.unlink()
    return ok
