#!/usr/bin/env python3
"""Pre-activation compatibility freshness ledger (resources/sources.json).

After source-control activation the master publication and source ledger own
freshness; direct compatibility writers fail before changing this file.
"""
from __future__ import annotations

import atexit
import json
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from activation import TransitionLock, is_source_control_active, transition_lock

ROOT = Path(__file__).parent
LEDGER = ROOT / "sources.json"
_WRITER_LOCK: TransitionLock | None = None


def source_control_active() -> bool:
    return is_source_control_active(ROOT)


def load() -> dict:
    """The ledger's entries, {} before the first stamp."""
    if not LEDGER.exists():
        return {}
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def stamp(key: str, *, corpus: str, origin: str, ttl_days: int | None,
          artifact: str, refresh: str, files: int | None = None) -> None:
    """Record a completed fetch of source `key`, dated today."""
    require_legacy_writer()
    entries = load()
    entry = {"corpus": corpus, "origin": origin,
             "fetched": date.today().isoformat(),
             "ttl_days": ttl_days, "artifact": artifact, "refresh": refresh}
    if files is not None:
        entry["files"] = files
    entries[key] = entry
    LEDGER.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")


def require_legacy_writer() -> None:
    """Reject pre-control-plane writers after the master publication activates."""
    global _WRITER_LOCK
    if _WRITER_LOCK is None:
        lock = transition_lock(ROOT)
        lock.__enter__()
        if source_control_active():
            lock.__exit__(None, None, None)
            raise RuntimeError(
                "legacy source writer is disabled after activation; use "
                "python resources/source_cli.py propose or refresh-stale"
            )
        _WRITER_LOCK = lock
    elif source_control_active():
        raise RuntimeError(
            "legacy source writer is disabled after activation; use "
            "python resources/source_cli.py propose or refresh-stale"
        )


def _release_writer_lock() -> None:
    global _WRITER_LOCK
    if _WRITER_LOCK is not None:
        _WRITER_LOCK.__exit__(None, None, None)
        _WRITER_LOCK = None


atexit.register(_release_writer_lock)


@contextmanager
def legacy_reader_session():
    """Serve one compatibility read only while the one-way cutover is absent."""
    with transition_lock(ROOT):
        if source_control_active():
            raise RuntimeError(
                "source control is active; use python resources/source_cli.py search, "
                "propose, or refresh-stale instead of the legacy routed index."
            )
        yield


def warnings_for(corpus: str, entries: dict | None = None,
                 today: date | None = None) -> list[str]:
    """The corpus's freshness screams: STALE pins and ABSENT artifacts.
    Empty list = all pins current. `entries`/`today` are injectable for
    tests; production callers pass neither."""
    entries = load() if entries is None else entries
    today = today or date.today()
    out: list[str] = []
    for key, e in sorted(entries.items()):
        if e.get("corpus") != corpus:
            continue
        artifact = ROOT / e["artifact"]
        absent = not artifact.exists() or (artifact.is_dir() and not any(artifact.iterdir()))
        if absent:
            out.append(f"ABSENT source '{key}': ledger says fetched {e['fetched']} "
                       f"but resources/{e['artifact']} is missing. Fetch: {e['refresh']}")
            continue
        ttl = e.get("ttl_days")
        if ttl is None:
            continue
        overdue = (today - date.fromisoformat(e["fetched"])).days - ttl
        if overdue > 0:
            out.append(f"STALE source '{key}': pinned {e['fetched']}, TTL {ttl}d "
                       f"— {overdue}d overdue. Refresh: {e['refresh']}")
    return out
