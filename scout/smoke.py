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

from . import sanitize


def _check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail else ""))
    return ok


def test_sanitize() -> bool:
    print("sanitize:")
    dirty = (
        "A\u200bB\u202egood\u202c text"            # zero-width + bidi override
        "\n![beacon](https://evil.example/x.png)"   # image exfil beacon
        "\n<script>alert(1)</script>"               # html
        "\n[click](https://ok.example/a) and [bad](javascript:alert(1))"
        "\n\n\n\n\nend"
    )
    out = sanitize.clean(dirty)
    ok = True
    ok &= _check("zero-width stripped", "\u200b" not in out)
    ok &= _check("bidi stripped", "\u202e" not in out and "\u202c" not in out)
    ok &= _check("image removed", "beacon" not in out and "evil.example" not in out)
    ok &= _check("html removed", "<script>" not in out)
    ok &= _check("https link demoted", "click (https://ok.example/a)" in out)
    ok &= _check("non-https target dropped", "javascript:" not in out and "bad" in out)
    ok &= _check("blank lines collapsed", "\n\n\n" not in out)
    capped = sanitize.clean("x" * 9000)
    ok &= _check("length capped", capped.endswith("[truncated]") and len(capped) < 8100)
    wrapped = sanitize.wrap("T", "body")
    ok &= _check("wrapper labels untrusted", "UNTRUSTED" in wrapped and wrapped.endswith("<<<END T>>>"))
    return ok


def test_dpapi() -> bool:
    print("dpapi:")
    if sys.platform != "win32":
        return _check("skipped (not Windows; GEMINI_API_KEY is the path here)", True)
    from .creds import _dpapi
    secret = b"correct horse battery staple \xf0\x9f\x90\x8e"
    blob = _dpapi(secret, protect=True)
    ok = _check("blob is opaque", secret not in blob)
    ok &= _check("round-trip", _dpapi(blob, protect=False) == secret)
    return ok


def test_freshness() -> bool:
    print("freshness ledger:")
    import importlib.util as iu
    from datetime import date
    from pathlib import Path
    spec = iu.spec_from_file_location(
        "freshness", Path(__file__).resolve().parents[1] / "resources" / "freshness.py")
    fresh = iu.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    today = date(2026, 7, 17)
    entries = {
        "young": {"corpus": "docs", "fetched": "2026-07-10", "ttl_days": 30,
                   "artifact": ".", "refresh": "refetch-young"},
        "old": {"corpus": "docs", "fetched": "2026-06-01", "ttl_days": 30,
                 "artifact": ".", "refresh": "refetch-old"},
        "immutable": {"corpus": "papers", "fetched": "2020-01-01", "ttl_days": None,
                       "artifact": ".", "refresh": "refetch-imm"},
        "gone": {"corpus": "papers", "fetched": "2026-07-17", "ttl_days": None,
                  "artifact": "no-such-dir-xyz", "refresh": "refetch-gone"},
    }
    docs_w = fresh.warnings_for("docs", entries, today)
    papers_w = fresh.warnings_for("papers", entries, today)
    ok = _check("within TTL is silent", not any("young" in w for w in docs_w))
    ok &= _check("past TTL screams with fix",
                 any("STALE source 'old'" in w and "refetch-old" in w and "16d overdue" in w
                     for w in docs_w))
    ok &= _check("immutable never screams", not any("immutable" in w for w in papers_w))
    ok &= _check("absent artifact screams with fix",
                 any("ABSENT source 'gone'" in w and "refetch-gone" in w for w in papers_w))
    ok &= _check("corpora isolated", not any("gone" in w for w in docs_w))
    return ok


def test_corpus() -> bool:
    print("workspace_search + papers_search:")
    from . import server
    ws = server.workspace_search("warmth thermostat min_containers reconciler", k=3)
    ok = _check("workspace returns hits", "#" in ws and "UNTRUSTED" in ws, ws[:80].replace("\n", " "))
    ok &= _check("workspace id self-cites", "#spec" in ws or "#note" in ws or "#proposal" in ws or "#doc" in ws)
    longest = max((len(l) for l in ws.splitlines()), default=0)
    ok &= _check("previews capped (not full chunks)", longest <= 500, f"longest={longest}")
    # The papers corpus is empty until the first web_search lead is fetched;
    # "no hits" is that state's honest answer, a failure string is not. Same
    # contract for the docs corpus before its first pin.
    pp = server.papers_search("joint embedding predictive architecture", k=3)
    ok &= _check("papers answers without failing", "failed" not in pp,
                 pp[:80].replace("\n", " "))
    dd = server.docs_search("container autoscaler min_containers warm", k=3)
    ok &= _check("docs answers without failing", "failed" not in dd,
                 dd[:80].replace("\n", " "))
    vscode = server.docs_search("VS Code Custom Endpoint MCP tools in chat", k=5)
    ok &= _check("VS Code docs are indexed", "#vscode#" in vscode,
                 vscode[:80].replace("\n", " "))
    second = server.workspace_search("telemetry duty cycle gap CDF", k=2)  # warm path: no reload
    ok &= _check("second query on warm worker", "UNTRUSTED" in second)
    server._shutdown()  # free the worker's RAM before test_mcp spawns its own
    return ok


def test_web() -> bool:
    print("web_search (live):")
    from .server import web_search
    out = web_search("What is the canonical Hugging Face repo id for the "
                     "Qwen3-Coder-80B-A3B model, if it exists?")
    print("  ---\n" + "\n".join("  " + l for l in out.splitlines()[:12]) + "\n  ---")
    ok = _check("wrapped untrusted", "UNTRUSTED" in out)
    ok &= _check("did not error", "web_search failed" not in out and "unavailable" not in out)
    return ok


def test_mcp() -> bool:
    """End-to-end over real stdio: handshake, list tools, call one. Proves the
    txtai/faiss log chatter lands on stderr, not the protocol stream."""
    print("mcp stdio round-trip:")
    import asyncio
    from pathlib import Path

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _run() -> bool:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "scout.server"],
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                names = [t.name for t in (await sess.list_tools()).tools]
                ok = _check("tools listed",
                            sorted(names) == ["docs_search", "papers_search",
                                              "web_search", "workspace_search"],
                            ", ".join(names))
                res = await sess.call_tool("workspace_search",
                                           {"query": "singleton container continuous batching", "k": 2})
                text = res.content[0].text if res.content else ""
                ok &= _check("tool call over protocol", "UNTRUSTED" in text and "#" in text,
                             text[:60].replace("\n", " "))
                return ok

    return asyncio.run(_run())


def main() -> int:
    results = [test_sanitize(), test_dpapi(), test_freshness(), test_corpus()]
    if "--mcp" in sys.argv:
        results.append(test_mcp())
    if "--web" in sys.argv:
        results.append(test_web())
    print("PASS" if all(results) else "FAIL")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
