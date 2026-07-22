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


def test_config() -> bool:
    print("scout config:")
    import json
    import tempfile
    from pathlib import Path

    config = _resource_module("config")
    loaded = config.load_config()
    ok = _check(
        "checked-in config loads",
        loaded.schema_version == 1
        and loaded.ledger.lock_wait_seconds == 30
        and loaded.ledger.lock_poll_milliseconds == 100
        and loaded.fetch.request_timeout_seconds == 60
        and loaded.fetch.max_redirects == 5
        and loaded.fetch.max_response_bytes == 83_886_080,
    )
    diagnostic = config.foundation_diagnostic(config.DiagnosticCode.CONFIG_MISSING, path="fixture")
    ok &= _check(
        "foundation diagnostic shape",
        diagnostic.as_dict() == {
            "code": "CONFIG_MISSING",
            "evidence": {"path": "fixture"},
            "repair": "Create the checked-in Scout configuration at the reported path.",
        },
    )

    valid = {
        "schema_version": 1,
        "ledger": {"lock_wait_seconds": 30, "lock_poll_milliseconds": 100},
        "fetch": {
            "request_timeout_seconds": 60,
            "max_redirects": 5,
            "max_response_bytes": 83_886_080,
        },
    }

    def codes(path: Path) -> set[str]:
        try:
            config.load_config(path)
        except config.ScoutDiagnosticsError as exc:
            return {diagnostic.code.value for diagnostic in exc.diagnostics}
        return set()

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ok &= _check(
            "missing config is typed",
            codes(root / "missing.json") == {"CONFIG_MISSING"},
        )

        malformed = root / "malformed.json"
        malformed.write_text("{", encoding="utf-8")
        ok &= _check(
            "malformed config is typed",
            codes(malformed) == {"CONFIG_MALFORMED"},
        )

        unknown = root / "unknown.json"
        unknown_payload = {**valid, "unexpected": True}
        unknown.write_text(json.dumps(unknown_payload), encoding="utf-8")
        ok &= _check(
            "unknown config key is typed",
            codes(unknown) == {"CONFIG_UNKNOWN_KEY"},
        )

        invalid = root / "invalid.json"
        invalid_payload = {
            "schema_version": 2,
            "ledger": {"lock_wait_seconds": 1, "lock_poll_milliseconds": 1_001},
            "fetch": {
                "request_timeout_seconds": False,
                "max_redirects": -1,
                "max_response_bytes": 0,
            },
        }
        invalid.write_text(json.dumps(invalid_payload), encoding="utf-8")
        invalid_codes = codes(invalid)
        ok &= _check(
            "wrong types and numeric domains aggregate",
            {"CONFIG_WRONG_TYPE", "CONFIG_INVALID_VALUE"}.issubset(invalid_codes),
            ", ".join(sorted(invalid_codes)),
        )
    return ok


def _resource_module(name: str):
    import importlib.util as iu
    from pathlib import Path

    module_name = f"scout_smoke_{name}"
    resource_dir = Path(__file__).resolve().parents[1] / "resources"
    if str(resource_dir) not in sys.path:
        sys.path.insert(0, str(resource_dir))
    spec = iu.spec_from_file_location(
        module_name, resource_dir / f"{name}.py"
    )
    module = iu.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_vine() -> bool:
    print("vine parser + citations:")
    import subprocess
    import tempfile
    from pathlib import Path

    vine = _resource_module("vine")
    long_description = " ".join(f"token{index}" for index in range(900))
    long_ref_description = " ".join(f"ref{index}" for index in range(900))
    dense_description = "." * 10_000
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        path = root / "plan.vine"
        path.write_text(
            "vine 1.2.0\n"
            "delimiter: ===\n"
            "---\n"
            "[root] Root task (planning) @priority(high)\n"
            "> durable decision\n"
            "  -> prose that remains description\n"
            ">not a decision\n"
            "@guidanceful prose\n"
            " ===\n"
            f"{long_description}\n"
            "===\n"
            "[dense] Dense task (planning)\n"
            f"{dense_description}\n"
            "===\n"
            "[short] Short task (planning)\n"
            "Brief task description\n"
            "===\n"
            "ref [child] Child graph (https://example.test/a_(b)) @sprite(./sprites/child.svg)\n"
            "Local proxy description\n"
            "===\n"
            "ref [long-ref] Long child graph (child.vine)\n"
            f"{long_ref_description}\n",
            encoding="utf-8",
        )
        blocks = vine.parse_vine(path)
        task = next(block for block in blocks if block.kind == "task")
        ref = next(block for block in blocks if block.kind == "ref")
        dense = next(block for block in blocks if block.block_id == "dense")
        short_task = next(block for block in blocks if block.block_id == "short")
        long_ref = next(block for block in blocks if block.block_id == "long-ref")
        task_citation = vine.citation_for(root, path, task)
        ref_citation = vine.citation_for(root, path, ref)
        dense_citation = vine.citation_for(root, path, dense)
        short_task_citation = vine.citation_for(root, path, short_task)
        long_ref_citation = vine.citation_for(root, path, long_ref)
        ok = True
        ok &= _check("task citation resolves",
                     vine.resolve_citation(root, task_citation).block_id == "root")
        ok &= _check("ref citation resolves",
                     vine.resolve_citation(root, ref_citation).block_id == "child")
        ok &= _check("exact field prefixes preserve prose",
                     "  -> prose that remains description" in task.projection and
                     ">not a decision" in task.projection and
                     "@guidanceful prose" in task.projection and
                     " ===" in task.projection and
                     "Decision: durable decision" in task.projection)
        ok &= _check("ref projection stays local",
                     "Local proxy description" in ref.projection and "example.test" not in ref.projection)

        compatibility_paths = []
        for version in ("1.0.0", "1.1.0"):
            compatibility = root / f"compat-{version}.vine"
            compatibility.write_text(
                f"vine {version}\n---\n[compat] Compatibility ({'planning'})\ntext\n",
                encoding="utf-8",
            )
            compatibility_paths.append(compatibility)
        ok &= _check("prior VINE versions parse",
                     all(vine.parse_vine(compatibility)[0].block_id == "compat"
                         for compatibility in compatibility_paths))

        spaced_delimiter = root / "spaced.vine"
        spaced_delimiter.write_text(
            "vine 1.2.0\n delimiter : === \n---\n[root] Root (planning)\n===\n[child] Child (planning)\n",
            encoding="utf-8",
        )
        ok &= _check("delimiter metadata whitespace", len(vine.parse_vine(spaced_delimiter)) == 2)

        unknown_at_ref = root / "unknown-at.vine"
        unknown_at_ref.write_text(
            "vine 1.2.0\n---\nref [child] Child (child.vine)\n@local-note remains description\n",
            encoding="utf-8",
        )
        ok &= _check("unknown ref @ stays description",
                     "@local-note remains description" in vine.parse_vine(unknown_at_ref)[0].projection)

        reserved_dir = root / "dir#part"
        reserved_dir.mkdir()
        reserved_path = reserved_dir / "plan.vine"
        reserved_path.write_text("vine 1.2.0\n---\n[root] Root (planning)\ntext\n", encoding="utf-8")
        reserved_block = vine.parse_vine(reserved_path)[0]
        reserved_citation = vine.citation_for(root, reserved_path, reserved_block)
        ok &= _check("reserved path characters encode canonically",
                     "%23" in reserved_citation and
                     vine.resolve_citation(root, reserved_citation).block_id == "root")

        citation_root = root / "repository"
        (citation_root / "sub").mkdir(parents=True)
        outside_dir = root / "outside"
        outside_dir.mkdir()
        outside = outside_dir / "plan.vine"
        outside.write_text("vine 1.2.0\n---\n[root] Outside (planning)\ntext\n", encoding="utf-8")
        try:
            vine.resolve_citation(citation_root, "sub%5C..%5C..%5Coutside.vine#root#vine")
        except vine.CitationResolutionError as exc:
            encoded_separator_rejected = "invalid VINE citation path" in str(exc)
        else:
            encoded_separator_rejected = False
        ok &= _check("encoded Windows separator rejected before resolution",
                     encoded_separator_rejected)

        escape = citation_root / "escape"
        if sys.platform == "win32":
            result = subprocess.run(
                f'mklink /J "{escape}" "{outside_dir}"',
                shell=True,
                capture_output=True,
                text=True,
            )
            link_created = result.returncode == 0 and escape.is_dir()
        else:
            escape.symlink_to(outside_dir, target_is_directory=True)
            link_created = escape.is_dir()
        ok &= _check("outside directory link fixture", link_created)
        try:
            vine.resolve_citation(citation_root, "escape/plan.vine#root#vine")
        except vine.CitationResolutionError as exc:
            link_escape_rejected = "escapes repository root" in str(exc)
        else:
            link_escape_rejected = False
        ok &= _check("outside directory link rejected", link_escape_rejected)

        bad_citations = (
            "plan.vine#root",
            "./plan.vine#root#vine",
            "PLAN.vine#root#vine",
            "../plan.vine#root#vine",
            "sub%5C..%5C..%5Coutside.vine#root#vine",
            "plan//nested.vine#root#vine",
            "/plan.vine#root#vine",
            "plan#bad.vine#root#vine",
            "dir#part/plan.vine#root#vine",
            "missing.vine#root#vine",
            "plan.vine#missing#vine",
            "plan.vine#ref:root#vine",
            "plan.vine#child#vine",
        )
        failures = []
        for citation in bad_citations:
            try:
                vine.resolve_citation(root, citation)
            except vine.CitationResolutionError:
                failures.append(citation)
        ok &= _check("bad citations fail explicitly", len(failures) == len(bad_citations))

        invalid_headers = (
            "vine 1.2.0\n---\n[bad] Bad ()\n",
            "vine 1.2.0\n---\n[bad] Bad (unknown-status)\n",
            "vine 1.2.0\n---\n[bad] Bad (planning) @broken\n",
            "vine 1.2.0\n---\nref [bad] Bad ()\n",
            "vine 1.2.0\n---\nref [bad] Bad (https://example.test/a)\n@artifact forbidden\n",
            "vine 1.2.0\ndelimiter: ===\n---\n[a] First (planning)\n===\n[a] Duplicate (planning)\n",
            " vine 1.2.0\n---\n[bad] Bad (planning)\n",
            "vine nonsense\n---\n[bad] Bad (planning)\n",
        )
        invalid_failures = 0
        for index, fixture in enumerate(invalid_headers):
            invalid_path = root / f"invalid{index}.vine"
            invalid_path.write_text(fixture, encoding="utf-8")
            try:
                vine.parse_vine(invalid_path)
            except vine.VineError:
                invalid_failures += 1
        ok &= _check("invalid headers fail explicitly", invalid_failures == len(invalid_headers))

        segments = vine.segments_for_vine(root, path)
        root_segments = [segment for segment in segments if segment.citation == task_citation]
        dense_segments = [segment for segment in segments if segment.citation == dense_citation]
        short_task_segments = [segment for segment in segments if segment.citation == short_task_citation]
        long_ref_segments = [segment for segment in segments if segment.citation == long_ref_citation]
        tokenizer, limit = vine._tokenizer_settings()
        token_safe = all(
            len(tokenizer(segment.text, add_special_tokens=True,
                          truncation=False, verbose=False)["input_ids"]) <= limit
            for segment in segments
        )

        def ranges_cover(block, block_segments):
            token_count = len(tokenizer(block.projection, add_special_tokens=False,
                                        truncation=False, verbose=False)["input_ids"])
            ordered = sorted(block_segments, key=lambda segment: segment.ordinal)
            if not ordered or ordered[0].token_start != 0 or ordered[-1].token_end != token_count:
                return False
            return all(
                current.token_end > previous.token_end and
                0 <= previous.token_end - current.token_start <= 30
                for previous, current in zip(ordered, ordered[1:])
            )

        ok &= _check("long task segments", len(root_segments) > 1)
        ok &= _check("long ref segments", len(long_ref_segments) > 1)
        ok &= _check("dense fallback segments", len(dense_segments) > 1)
        ok &= _check("segment ids unique", len({segment.index_id for segment in segments}) == len(segments))
        ok &= _check("segments token-safe", token_safe, f"limit={limit}")
        ok &= _check("shared task citation", all(segment.citation == task_citation for segment in root_segments))
        ok &= _check("short task emits", len(short_task_segments) == 1)
        ok &= _check("short ref emits", any(segment.citation == ref_citation for segment in segments))
        ok &= _check("task token ranges cover projection", ranges_cover(task, root_segments))
        ok &= _check("ref token ranges cover projection", ranges_cover(long_ref, long_ref_segments))
        ok &= _check("dense token ranges cover projection", ranges_cover(dense, dense_segments))
        joined = "\n".join(segment.text for segment in root_segments)
        ok &= _check("selected task text retained", "durable decision" in joined and "token899" in joined)
        return ok


def test_index_metadata() -> bool:
    print("index metadata migration:")
    import contextlib
    import json
    import tempfile
    from pathlib import Path

    search = _resource_module("search")
    old_index_root = search.INDEX_ROOT
    state = {"citation": "fixture#one", "include_vine": False}

    def chunks():
        yield search._chunk("fixture-id", "fixture alpha beta", state["citation"], fixture=True)
        if state["include_vine"]:
            yield search._chunk("fixture.vine#root#vine#s0", "fixture vine addition",
                                "fixture.vine#root#vine", vine_kind="task", vine_segment=0)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        search.INDEX_ROOT = Path(temporary) / ".index"
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


def test_machine_output() -> bool:
    print("machine output schemas:")
    import contextlib
    import json
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    query = "source publication boundary automatic rebase"
    command = [sys.executable, "resources/search.py", "--json", query]
    cli = subprocess.run(command, cwd=root, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    ok = _check("json command succeeds", cli.returncode == 0, cli.stderr[:120].replace("\n", " "))
    try:
        payload = json.loads(cli.stdout)
        cli_hits = payload[0]["hits"]
        vine_hit = next(hit for hit in cli_hits if hit["citation"].endswith("#vine"))
        ok &= _check("json retains id and citation",
                     isinstance(vine_hit["id"], str) and vine_hit["id"] != vine_hit["citation"])
    except (KeyError, StopIteration, json.JSONDecodeError, IndexError, TypeError) as exc:
        ok &= _check("json retains id and citation", False, type(exc).__name__)

    worker = subprocess.Popen(
        [sys.executable, "resources/search.py", "--serve"], cwd=root,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    try:
        ready = json.loads(worker.stdout.readline())
        worker.stdin.write(json.dumps({"source": "workspace", "query": query, "k": 3}) + "\n")
        worker.stdin.flush()
        reply = json.loads(worker.stdout.readline())
        worker_hits = reply["hits"]
        worker_vine = next(hit for hit in worker_hits if hit["citation"].endswith("#vine"))
        ok &= _check("worker retains id and citation",
                     ready.get("ready") is True and isinstance(worker_vine["id"], str) and
                     worker_vine["id"] != worker_vine["citation"])
    except (KeyError, StopIteration, json.JSONDecodeError, OSError) as exc:
        ok &= _check("worker retains id and citation", False, type(exc).__name__)
    finally:
        if worker.stdin:
            worker.stdin.close()
        with contextlib.suppress(Exception):
            worker.wait(timeout=30)
        if worker.poll() is None:
            worker.kill()
    return ok


def test_corpus() -> bool:
    print("workspace_search + papers_search:")
    from . import server
    ws = server.workspace_search("warmth thermostat min_containers reconciler", k=3)
    ok = _check("workspace returns hits", "#" in ws and "UNTRUSTED" in ws, ws[:80].replace("\n", " "))
    ok &= _check("workspace id self-cites", "#spec" in ws or "#note" in ws or "#proposal" in ws or "#doc" in ws or "#vine" in ws)
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
    vines = server.workspace_search("source publication boundary automatic rebase", k=3)
    ok &= _check("VINE task citations are indexed", "#vine" in vines,
                 vines[:80].replace("\n", " "))
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
                res = await sess.call_tool(
                    "workspace_search",
                    {"query": "source publication boundary automatic rebase", "k": 3},
                )
                text = res.content[0].text if res.content else ""
                ok &= _check("tool call over protocol", "UNTRUSTED" in text and "#vine" in text,
                             text[:60].replace("\n", " "))
                return ok

    return asyncio.run(_run())


def main() -> int:
    results = [test_sanitize(), test_dpapi(), test_freshness(), test_config(), test_vine(),
               test_index_metadata(), test_machine_output(), test_corpus()]
    if "--mcp" in sys.argv:
        results.append(test_mcp())
    if "--web" in sys.argv:
        results.append(test_web())
    print("PASS" if all(results) else "FAIL")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
