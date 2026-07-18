#!/usr/bin/env python3
"""Pin the official VS Code language-model and MCP documentation locally.

The pinned Markdown feeds scout's docs corpus and carries the same freshness
contract as the Modal docs mirror. The ledger is stamped only after every
declared page is fetched successfully.

Usage:
    python resources/fetch_vscode_docs.py
"""
from __future__ import annotations

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
    SSL_CONTEXT = ssl.create_default_context()

PAGES = {
    "language-models.md": (
        "https://raw.githubusercontent.com/microsoft/vscode-docs/main/"
        "docs/agent-customization/language-models.md"
    ),
    "mcp-servers.md": (
        "https://raw.githubusercontent.com/microsoft/vscode-docs/main/"
        "docs/agent-customization/mcp-servers.md"
    ),
}
ORIGIN = (
    "https://github.com/microsoft/vscode-docs/tree/main/"
    "docs/agent-customization"
)
USER_AGENT = "Mozilla/5.0 (compatible; localmodal-resource-fetch/1.0)"
OUT_DIR = Path(__file__).resolve().parent / "vscode-docs"
TTL_DAYS = 30
REFRESH_CMD = ("python resources/fetch_vscode_docs.py, "
               "then python resources/search.py --update")


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60, context=SSL_CONTEXT) as response:
        return response.read()


def main() -> int:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)

    failures: list[str] = []
    for relative_path, url in PAGES.items():
        destination = OUT_DIR / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = _get(url)
            if data.lstrip().startswith(b"<"):
                raise ValueError("response is HTML, expected markdown")
            destination.write_bytes(data)
            print(f"fetched {relative_path}")
        except (urllib.error.URLError, ValueError, TimeoutError, OSError) as exc:
            print(f"FAIL  {relative_path}  ({exc})", file=sys.stderr)
            failures.append(relative_path)

    if failures:
        print("NOT STAMPED: the ledger keeps the previous pin date", file=sys.stderr)
        return 1

    freshness.stamp(
        "vscode-docs",
        corpus="docs",
        origin=ORIGIN,
        ttl_days=TTL_DAYS,
        artifact="vscode-docs",
        refresh=REFRESH_CMD,
        files=len(PAGES),
    )
    print(f"stamped: vscode-docs pinned today, TTL {TTL_DAYS}d "
          "(next: python resources/search.py --update)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
