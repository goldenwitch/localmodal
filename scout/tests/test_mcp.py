from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module, _source_control_active

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
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
                                              "refresh_stale", "scout_search", "source_propose", "source_read",
                                              "web_search", "workspace_search"],
                            ", ".join(names))
                res = await sess.call_tool(
                    "workspace_search",
                    {"query": "source publication boundary automatic rebase", "k": 3},
                )
                text = res.content[0].text if res.content else ""
                ok &= _check("tool call over protocol", "UNTRUSTED" in text and "#vine" in text,
                             text[:60].replace("\n", " "))
                source_result = await sess.call_tool("scout_search", {"query": "fixture", "k": 1})
                source_text = source_result.content[0].text if source_result.content else ""
                active = _source_control_active()
                ok &= _check(
                    "source tool follows activation state",
                    ("PUBLICATION_MISSING" in source_text) if not active else ("UNTRUSTED" in source_text or "no hits" in source_text),
                    source_text[:80].replace("\n", " "),
                )
                read_result = await sess.call_tool(
                    "source_read",
                    {"citation": "proposals/scout-source-management.vine#ssm#vine"},
                )
                read_text = read_result.content[0].text if read_result.content else ""
                ok &= _check(
                    "source read follows activation state",
                    ("PUBLICATION_MISSING" in read_text) if not active else ("SOURCE_READ" in read_text and "Generalize scout" in read_text),
                    read_text[:80].replace("\n", " "),
                )
                return ok

    return asyncio.run(_run())
