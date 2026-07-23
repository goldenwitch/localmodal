#!/usr/bin/env python3
"""Pre-activation compatibility fetcher for the legacy Modal mirror.

After `python resources/source_cli.py migrate` activates the source control
plane, this command fails before mutating files. Use `source_propose` or
`refresh_stale` instead.
"""
from __future__ import annotations

import re
import shutil
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

import freshness

try:
    import certifi

    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    # Fall back to the system trust store. Verification stays enabled.
    SSL_CONTEXT = ssl.create_default_context()

INDEX_URL = "https://modal.com/llms.txt"
# Every docs page named by llms.txt has a raw markdown twin; this matches them.
_MD_URL = re.compile(r"https://modal\.com/docs/[^)\s#\"']+\.md")
USER_AGENT = "Mozilla/5.0 (compatible; localmodal-resource-fetch/1.0)"
OUT_DIR = Path(__file__).resolve().parent / "modal-docs"
# Draft default: Modal's docs move steadily but not daily. Owner-tunable.
TTL_DAYS = 30
REFRESH_CMD = "python resources/source_cli.py refresh-stale"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
        return resp.read()


def main() -> int:
    try:
        freshness.require_legacy_writer()
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    index = _get(INDEX_URL).decode("utf-8", errors="replace")
    urls = list(dict.fromkeys(_MD_URL.findall(index)))  # dedup, order kept
    if not urls:
        print("FATAL: no .md links found in llms.txt — format change?", file=sys.stderr)
        return 1
    print(f"llms.txt names {len(urls)} markdown pages")

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)  # a pin, not a merge: dead pages must not linger

    failures: list[str] = []
    for i, url in enumerate(urls, start=1):
        rel = url.split("modal.com/docs/", 1)[1]
        dest = OUT_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = _get(url)
            if data.lstrip().startswith(b"<"):
                raise ValueError("response is HTML, expected markdown")
            dest.write_bytes(data)
            if i % 50 == 0 or i == len(urls):
                print(f"  {i}/{len(urls)} fetched ...")
        except (urllib.error.URLError, ValueError, TimeoutError, OSError) as exc:
            print(f"FAIL  {rel}  ({exc})", file=sys.stderr)
            failures.append(rel)

    fetched = len(urls) - len(failures)
    print(f"{fetched}/{len(urls)} pages mirrored into {OUT_DIR}")
    if failures:
        print(f"NOT STAMPED: {len(failures)} failure(s) — the ledger keeps the "
              f"previous pin date. Failed: {', '.join(failures[:10])}"
              + (" ..." if len(failures) > 10 else ""), file=sys.stderr)
        return 1
    freshness.stamp("modal-docs", corpus="docs", origin=INDEX_URL,
                    ttl_days=TTL_DAYS, artifact="modal-docs",
                    refresh=REFRESH_CMD, files=fetched)
    print(f"stamped: modal-docs pinned today, TTL {TTL_DAYS}d "
          f"(next: python resources/search.py --update)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
