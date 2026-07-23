from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def test_legacy_activation_guard() -> bool:
    print("legacy index activation guard:")
    import tempfile
    from pathlib import Path

    search = _resource_module("search")
    old_index_root = search.INDEX_ROOT
    old_freshness_root = search.freshness.ROOT
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        search.INDEX_ROOT = root / ".index"
        search.freshness.ROOT = root
        marker = root / ".scout-publications" / "ACTIVATED"
        marker.parent.mkdir(parents=True)
        marker.write_text("active\n", encoding="utf-8")
        corpus = search.Corpus("blocked", lambda: iter(()))
        try:
            search.load_or_build(corpus, rebuild=False)
        except RuntimeError as exc:
            blocked = "source control is active" in str(exc)
            pointer_absent = not corpus.current.exists()
        else:
            blocked = False
            pointer_absent = not corpus.current.exists()
        finally:
            search.INDEX_ROOT = old_index_root
            search.freshness.ROOT = old_freshness_root
    return _check(
        "direct legacy builder is blocked after activation",
        blocked and pointer_absent,
    )
