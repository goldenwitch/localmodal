from __future__ import annotations

import sys
from pathlib import Path

from .harness import _check, _resource_module, _source_control_active

# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
def test_corpus() -> bool:
    print("workspace_search + papers_search:")
    from . import server
    from pathlib import Path

    active = _source_control_active()
    ws = server.workspace_search("warmth thermostat min_containers reconciler", k=3)
    ok = _check("workspace returns hits", "#" in ws and "UNTRUSTED" in ws, ws[:80].replace("\n", " "))
    expected_workspace_citation = "source:" if active else None
    ok &= _check(
        "workspace id self-cites",
        (expected_workspace_citation in ws)
        if active
        else ("#spec" in ws or "#note" in ws or "#proposal" in ws or "#doc" in ws or "#vine" in ws),
    )
    longest = max((len(l) for l in ws.splitlines()), default=0)
    ok &= _check("previews capped (not full chunks)", longest <= 500, f"longest={longest}")
    # Pre-activation queries use legacy routed indexes. After activation all
    # aliases resolve the one master publication and preserve source citations.
    pp = server.papers_search("joint embedding predictive architecture", k=3)
    ok &= _check("papers answers without failing", "failed" not in pp,
                 pp[:80].replace("\n", " "))
    dd = server.docs_search("container autoscaler min_containers warm", k=3)
    ok &= _check("docs answers without failing", "failed" not in dd,
                 dd[:80].replace("\n", " "))
    vscode = server.docs_search("VS Code Custom Endpoint MCP tools in chat", k=5)
    expected_vscode_citation = "source:" if active else "#vscode#"
    ok &= _check("VS Code docs are indexed", expected_vscode_citation in vscode,
                 vscode[:80].replace("\n", " "))
    second = server.workspace_search("telemetry duty cycle gap CDF", k=2)  # warm path: no reload
    ok &= _check("second query on warm worker", "UNTRUSTED" in second)
    vines = server.workspace_search("source publication boundary automatic rebase", k=3)
    ok &= _check("VINE task citations are indexed", "#vine" in vines,
                 vines[:80].replace("\n", " "))
    server._shutdown()  # free the worker's RAM before test_mcp spawns its own
    return ok
