from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def test_index_metadata() -> bool:
    print("index metadata migration:")
    import contextlib
    import json
    import tempfile
    from pathlib import Path

    search = _resource_module("search")
    old_index_root = search.INDEX_ROOT
    old_freshness_root = search.freshness.ROOT
    state = {"citation": "fixture#one", "include_vine": False}

    def chunks():
        yield search._chunk("fixture-id", "fixture alpha beta", state["citation"], fixture=True)
        if state["include_vine"]:
            yield search._chunk("fixture.vine#root#vine#s0", "fixture vine addition",
                                "fixture.vine#root#vine", vine_kind="task", vine_segment=0)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        search.INDEX_ROOT = Path(temporary) / ".index"
        search.freshness.ROOT = Path(temporary)
        corpus = search.Corpus("fixture", chunks)
        opened = []
        try:
            opened.append(search.build(corpus))
            current = search._read_current(corpus)
            loaded = search._load(current)
            opened.append(loaded)
            first = search.semantic(loaded, "fixture", 1)[0]
            ok = True
            ok &= _check("tags persist after save/load",
                         first["id"] == "fixture-id" and first["citation"] == "fixture#one")

            state["citation"] = "fixture#two"
            opened.append(search.update(corpus))
            current = search._read_current(corpus)
            loaded = search._load(current)
            opened.append(loaded)
            second = search.semantic(loaded, "fixture", 1)[0]
            ok &= _check("metadata-only upsert", second["citation"] == "fixture#two")

            state["include_vine"] = True
            opened.append(search.update(corpus))
            current = search._read_current(corpus)
            loaded = search._load(current)
            opened.append(loaded)
            vine_hit = search.semantic(loaded, "fixture vine addition", 1)[0]
            ok &= _check("incremental VINE chunk addition",
                         vine_hit["id"] == "fixture.vine#root#vine#s0" and
                         vine_hit["citation"] == "fixture.vine#root#vine")

            legacy = current
            search._manifest(legacy).write_text(json.dumps({"fixture-id": "legacy"}), encoding="utf-8")
            rebuilt = search._ensure_current_schema(corpus)
            loaded = search._load(rebuilt)
            opened.append(loaded)
            third = search.semantic(loaded, "fixture", 1)[0]
            ok &= _check("legacy current hot-rebuild", rebuilt.name != legacy.name and
                         search._read_sigs(rebuilt) is not None and
                         third["citation"] == "fixture#two")
            return ok
        finally:
            for embedding in opened:
                with contextlib.suppress(Exception):
                    embedding.close()
            search.INDEX_ROOT = old_index_root
            search.freshness.ROOT = old_freshness_root
