#!/usr/bin/env python3
"""Source freshness ledger (resources/sources.json).

Every pinned source set is stamped by the fetcher that pulls it:
{corpus, origin, fetched, ttl_days, artifact, refresh}. ttl_days null =
immutable (arXiv PDFs never change); a number = the pin goes STALE that many
days after `fetched`. Staleness and missing artifacts SCREAM: the search
worker attaches the warnings to every reply from the affected corpus, so
consulting a rotten pin and seeing the rot are the same event. Each warning
carries its own fix (the source's `refresh` command).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent
LEDGER = ROOT / "sources.json"


def load() -> dict:
    """The ledger's entries, {} before the first stamp."""
    if not LEDGER.exists():
        return {}
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def stamp(key: str, *, corpus: str, origin: str, ttl_days: int | None,
          artifact: str, refresh: str, files: int | None = None) -> None:
    """Record a completed fetch of source `key`, dated today."""
    entries = load()
    entry = {"corpus": corpus, "origin": origin,
             "fetched": date.today().isoformat(),
             "ttl_days": ttl_days, "artifact": artifact, "refresh": refresh}
    if files is not None:
        entry["files"] = files
    entries[key] = entry
    LEDGER.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")


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
