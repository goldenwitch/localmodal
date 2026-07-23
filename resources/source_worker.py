#!/usr/bin/env python3
"""Line-JSON worker for the source control plane; keeps txtai out of MCP's event loop."""
from __future__ import annotations

import contextlib
import json
import sys

from diagnostics import ScoutDiagnosticsError
from source_control import SourceControl


def _reply(control: SourceControl, request: object) -> dict[str, object]:
    if not isinstance(request, dict):
        raise ValueError("request must be an object")
    operation = request.get("op")
    if operation == "bootstrap":
        rows = request.get("rows")
        if not isinstance(rows, list):
            raise ValueError("bootstrap rows must be a list")
        return control.bootstrap(rows).as_dict()
    if operation == "propose":
        rows = request.get("rows")
        if not isinstance(rows, list):
            raise ValueError("propose rows must be a list")
        return control.propose(rows).as_dict()
    if operation == "refresh-stale":
        return control.refresh_stale().as_dict()
    if operation == "search":
        query = request.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search query must be nonempty text")
        k = request.get("k", 6)
        if isinstance(k, bool) or not isinstance(k, int):
            raise ValueError("search k must be an integer")
        return control.search(query, max(1, min(k, 20)))
    if operation == "read":
        citation = request.get("citation")
        if not isinstance(citation, str) or not citation:
            raise ValueError("read citation must be nonempty text")
        return control.read_citation(citation)
    raise ValueError("op must be bootstrap, propose, refresh-stale, search, or read")


def serve() -> int:
    with contextlib.redirect_stdout(sys.stderr):
        control = SourceControl()
    print(json.dumps({"ready": True}, ensure_ascii=True), flush=True)
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                payload = _reply(control, request)
                response = {"result": payload}
            except ScoutDiagnosticsError as exc:
                response = {"diagnostics": [item.as_dict() for item in exc.diagnostics]}
            except Exception as exc:
                response = {"error": f"{type(exc).__name__}: {exc}"}
            # The worker's stdout is a protocol pipe. ASCII JSON avoids a cp1252
            # child-console encoding from killing a response containing source text.
            print(json.dumps(response, ensure_ascii=True), flush=True)
    finally:
        control.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
