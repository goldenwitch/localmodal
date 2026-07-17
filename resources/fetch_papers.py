#!/usr/bin/env python3
"""Fetch the curated paper set into resources/pdf/.

Source of truth is papers.md; the list below mirrors it. Re-run anytime;
existing files are skipped unless --force is passed.

Usage:
    python fetch_papers.py [--force]
"""
from __future__ import annotations

import argparse
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

# key -> source id. "arxiv:<id>" or "openreview:<forum_id>".
# Empty at repo birth: entries are added when a web_search lead proves
# load-bearing (the every-time loop in scout/server.py's web_search doc),
# never speculatively. Each id is resolved by being fetched.
PAPERS: dict[str, str] = {}

USER_AGENT = "Mozilla/5.0 (compatible; localmodal-resource-fetch/1.0)"
OUT_DIR = Path(__file__).resolve().parent / "pdf"


def pdf_url(source: str) -> str:
    scheme, _, ident = source.partition(":")
    if scheme == "arxiv":
        return f"https://arxiv.org/pdf/{ident}"
    if scheme == "openreview":
        return f"https://openreview.net/pdf?id={ident}"
    raise ValueError(f"unknown source scheme: {source!r}")


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
        data = resp.read()
    if not data.startswith(b"%PDF"):
        raise ValueError(f"response from {url} is not a PDF (got {data[:16]!r})")
    dest.write_bytes(data)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download existing files")
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    for key, source in PAPERS.items():
        dest = OUT_DIR / f"{key}.pdf"
        if dest.exists() and not args.force:
            print(f"skip  {key} (exists)")
            continue
        url = pdf_url(source)
        try:
            download(url, dest)
            size_kb = dest.stat().st_size // 1024
            print(f"ok    {key:24s} {size_kb:>6} KB  <- {url}")
            # arXiv/OpenReview PDFs are immutable: dated for provenance, no TTL.
            freshness.stamp(key, corpus="papers", origin=source, ttl_days=None,
                            artifact=f"pdf/{key}.pdf",
                            refresh="python resources/fetch_papers.py")
        except (urllib.error.URLError, ValueError, TimeoutError) as exc:
            print(f"FAIL  {key:24s} {url}  ({exc})", file=sys.stderr)
            failures.append(key)

    print(f"\n{len(PAPERS) - len(failures)}/{len(PAPERS)} downloaded into {OUT_DIR}")
    if failures:
        print("failed: " + ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
