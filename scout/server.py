#!/usr/bin/env python3
"""scout MCP server: the repo's local corpus search + Gemini-grounded web
search, as agent tools over stdio.

Run (normally via .vscode/mcp.json, but works standalone):
    python -m scout.server

Tools:
    scout_search(query, k)    validated hits from the active source-bound publication.
    source_read(citation)     exact committed source snapshot or VINE task/ref block.
    source_propose(rows)      explicit atomic source add/remove batch.
    refresh_stale()           stale/absent source maintenance.
    workspace_search, papers_search, docs_search
                                                        compatibility aliases before activation; they route
                                                        to scout_search after the master pointer exists.
  web_search(question)      Gemini + Google Search grounding. Answer text is
                            sanitized (scout.sanitize) and wrapped UNTRUSTED;
                            the source list comes from the API's grounding
                            metadata, so a hallucinated citation cannot
                            appear in it.

Process isolation: this server NEVER imports numpy/scipy/txtai. Loading
OpenBLAS-backed DLLs inside a threaded async server deadlocks on the Windows
loader lock (observed: LoadLibraryExW on libscipy_openblas64_ wedged against
anyio's stdio reader; py-spy stack dump). The corpus tools
instead talk line-JSON to a resident worker (resources/search.py --serve)
that loads the index once per server lifetime. No timeouts in that
conversation: every pipe read returns a line or EOF, both handled — and the
worker exits on stdin EOF, so a dead server cannot orphan a worker and a
dead worker cannot hang the server.
"""
from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from resources.activation import is_source_control_active
from . import creds, sanitize

ROOT = Path(__file__).resolve().parents[1]
# gemini-3.1-pro-preview = the confidence pick (billing enabled; pro-class is
# paid-tier only — free keys get 429 limit:0). Fallback if billing ever
# lapses: gemini-2.5-flash (best free grounded model). Override: GEMINI_MODEL.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-pro-preview")
MAX_K = 20

# Steer every grounded answer to surface verifiable artifact handles. The
# prompt states the job in positive terms and trusts the model with it;
# distrust of the OUTPUT is structural, outside the prompt (sanitizer,
# UNTRUSTED wrap, citations from grounding metadata, handles verified by
# fetching). Domain context arrives with each caller's question.
_WEB_SYSTEM = (
    "You locate primary sources. For each model, dataset, paper, or claim in "
    "your answer, give its handle inline: a Hugging Face repo id as "
    "hf:<org>/<repo> with the https://huggingface.co/<org>/<repo> link, an "
    "arXiv id as arXiv:XXXX.XXXXX with the https://arxiv.org/abs/<id> link, "
    "or the canonical docs URL. When unsure of an id, name the artifact and "
    "mark the id unconfirmed. Handles are the deliverable; keep prose brief."
)

app = FastMCP(
    "scout",
    # Always in callers' context — kept minimal (an always-on prior is an
    # unconfined one). Routing detail lives on each tool's own description.
    instructions=(
        "Validated search over the active Scout source publication "
        "(scout_search), citation resolution (source_read), explicit source "
        "mutation (source_propose), stale maintenance (refresh_stale), and "
        "live-web primary-source leads "
        "(web_search). Trust a web lead once its handle resolves — the fetch "
        "is the verification."
    ),
)

# Resident search worker. FastMCP runs sync tools inline on the event loop
# thread (verified by stack dump), so calls are serialized — no lock needed.
_PROC: subprocess.Popen | None = None
_READY = False
_SOURCE_PROC: subprocess.Popen | None = None
_SOURCE_READY = False


def _spawn() -> None:
    """Start the worker without waiting for it (the index/model load proceeds
    concurrently in the child). Called at server startup so the load overlaps
    the session, never the MCP handshake — VS Code times that out."""
    global _PROC, _READY
    if _PROC is not None and _PROC.poll() is None:
        return
    _READY = False
    _PROC = subprocess.Popen(
        [sys.executable, str(ROOT / "resources" / "search.py"), "--serve"],
        cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=None,  # inherit: chatter lands in the MCP server log, can't fill a pipe
        encoding="utf-8", errors="replace", bufsize=1,
    )


def _spawn_source() -> None:
    """Start the source-control worker only when a source tool is requested."""
    global _SOURCE_PROC, _SOURCE_READY
    if _SOURCE_PROC is not None and _SOURCE_PROC.poll() is None:
        return
    _SOURCE_READY = False
    _SOURCE_PROC = subprocess.Popen(
        [sys.executable, str(ROOT / "resources" / "source_worker.py")],
        cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=None, encoding="utf-8", errors="replace", bufsize=1,
    )


def _ensure_ready() -> subprocess.Popen:
    """Block until the worker's ready line (one-time load; minutes cold).
    EOF instead of a line means it died — report, don't guess."""
    global _READY
    _spawn()
    if not _READY:
        if not _PROC.stdout.readline():
            _shutdown()
            raise RuntimeError(
                "search worker died during startup (see MCP server log)")
        _READY = True
    return _PROC


def _ensure_source_ready() -> subprocess.Popen:
    """Block for the source worker's one-line ready protocol."""
    global _SOURCE_READY
    _spawn_source()
    if not _SOURCE_READY:
        line = _SOURCE_PROC.stdout.readline()
        if not line:
            _shutdown_source()
            raise RuntimeError("source worker died during startup (see MCP server log)")
        try:
            ready = json.loads(line)
        except json.JSONDecodeError as exc:
            _shutdown_source()
            raise RuntimeError(f"source worker emitted invalid ready output: {exc}") from exc
        if ready.get("ready") is not True:
            _shutdown_source()
            raise RuntimeError("source worker did not acknowledge readiness")
        _SOURCE_READY = True
    return _SOURCE_PROC


def _shutdown() -> None:
    """Close the worker's stdin (its EOF lifeline) and reap it. atexit covers
    normal server exit; a hard-killed server closes the pipe anyway, and the
    worker exits itself on EOF."""
    global _PROC, _READY
    if _PROC is not None and _PROC.poll() is None:
        try:
            _PROC.stdin.close()
            _PROC.wait(timeout=10)
        except Exception:
            _PROC.kill()
    _PROC, _READY = None, False


def _shutdown_source() -> None:
    """Close and reap the source worker without affecting legacy search worker state."""
    global _SOURCE_PROC, _SOURCE_READY
    if _SOURCE_PROC is not None and _SOURCE_PROC.poll() is None:
        try:
            _SOURCE_PROC.stdin.close()
            _SOURCE_PROC.wait(timeout=10)
        except Exception:
            _SOURCE_PROC.kill()
    _SOURCE_PROC, _SOURCE_READY = None, False


atexit.register(_shutdown)
atexit.register(_shutdown_source)


SNIPPET_CHARS = 320  # per-hit preview cap: enough to judge relevance; the citation is the
                     # handle to the full source. Keeps a k-hit reply small enough to
                     # return inline instead of overflowing into a dumped, re-truncated file.


def _search(source: str, label: str, query: str, k: int) -> str:
    """Send one query to the resident worker for the named index and format the hits.
    Each hit is a compact preview (citation + a capped snippet); the citation
    self-cites the source to read in full, so the preview is the contract, not a loss."""
    k = max(1, min(int(k), MAX_K))
    try:
        proc = _ensure_ready()
        proc.stdin.write(json.dumps({"source": source, "query": query, "k": k}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
    except (RuntimeError, OSError) as exc:
        return f"{label} failed: {exc}"
    if not line:  # EOF mid-conversation: worker crashed; next call respawns
        _shutdown()
        return f"{label} failed: search worker exited (see MCP server log)."
    try:
        reply = json.loads(line)
    except json.JSONDecodeError as exc:
        return f"{label} failed: bad worker output: {exc}"
    if "error" in reply:
        return f"{label} failed: {reply['error']}"
    # Freshness screams ride ahead of the results, outside the UNTRUSTED wrap:
    # they are this instrument's own voice, and consulting a rotten pin should
    # look different from consulting ground.
    banner = "".join(f"!! {w}\n" for w in reply.get("warnings", []))
    hits = reply.get("hits", [])
    if not hits:
        return banner + f"no hits in {source}"
    lines = []
    for h in hits:
        snippet = " ".join(h["text"].split())
        if len(snippet) > SNIPPET_CHARS:
            snippet = snippet[:SNIPPET_CHARS].rsplit(" ", 1)[0] + " ..."
        score = h.get("score")
        tag = f"{score:.3f}" if isinstance(score, float) else "  -  "
        lines.append(f"[{tag}] {h['citation']}\n  {snippet}")
    # Search hit text is data either way -- same hygiene, same labeling.
    return banner + sanitize.wrap(label.upper(), sanitize.clean("\n".join(lines), max_chars=12000))


def _source_request(payload: dict[str, object]) -> dict[str, object]:
    """Send one structured request to the isolated source-control worker."""
    try:
        worker = _ensure_source_ready()
        worker.stdin.write(json.dumps(payload) + "\n")
        worker.stdin.flush()
        line = worker.stdout.readline()
    except (RuntimeError, OSError) as exc:
        return {"error": f"source worker failed: {exc}"}
    if not line:
        _shutdown_source()
        return {"error": "source worker exited (see MCP server log)"}
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        return {"error": f"source worker emitted invalid JSON: {exc}"}


def _source_activation_present() -> bool:
    return is_source_control_active(ROOT / "resources")


def _diagnostic_text(diagnostics: object) -> str:
    if not isinstance(diagnostics, list):
        return "source control returned malformed diagnostics"
    return json.dumps({"diagnostics": diagnostics}, ensure_ascii=False, indent=2)


def _warning_banner(warnings: object) -> str:
    if not isinstance(warnings, list):
        return ""
    lines = []
    for item in warnings:
        if not isinstance(item, dict):
            continue
        code = item.get("code", "WARNING")
        repair = item.get("repair", "")
        evidence = item.get("evidence", {})
        lines.append(f"!! {code}: {repair} {json.dumps(evidence, ensure_ascii=True, sort_keys=True)}")
    return "\n".join(lines) + ("\n" if lines else "")


def scout_search(query: str, k: int = 6) -> str:
    """Validated search over the active unified Scout source publication.

    The source-control worker validates the master publication, every source
    binding, artifact digest, and index generation before it ranks any hit. A
    missing or invalid store returns typed diagnostics and no hit text.
    """
    k = max(1, min(int(k), MAX_K))
    reply = _source_request({"op": "search", "query": query, "k": k})
    if "error" in reply:
        return f"scout_search failed: {reply['error']}"
    if "diagnostics" in reply:
        return _diagnostic_text(reply["diagnostics"])
    result = reply.get("result")
    if not isinstance(result, dict):
        return "scout_search failed: malformed worker result"
    diagnostics = result.get("diagnostics")
    if diagnostics:
        return _diagnostic_text(diagnostics)
    banner = _warning_banner(result.get("warnings"))
    hits = result.get("hits")
    if not isinstance(hits, list) or not hits:
        return banner + "no hits in active Scout publication"
    lines = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        text = " ".join(str(hit.get("text", "")).split())
        if len(text) > SNIPPET_CHARS:
            text = text[:SNIPPET_CHARS].rsplit(" ", 1)[0] + " ..."
        score = hit.get("score")
        score_text = f"{score:.3f}" if isinstance(score, float) else "  -  "
        lines.append(f"[{score_text}] {hit.get('citation', '<missing citation>')}\n  {text}")
    return banner + sanitize.wrap("SCOUT_SEARCH", sanitize.clean("\n".join(lines), max_chars=12000))


def source_propose(rows: list[dict]) -> str:
    """Apply an explicit ordered batch of source add/remove rows through one publication."""
    reply = _source_request({"op": "propose", "rows": rows})
    if "error" in reply:
        return f"source_propose failed: {reply['error']}"
    return json.dumps(reply.get("result", {"diagnostics": reply.get("diagnostics", [])}), ensure_ascii=False, indent=2)


def refresh_stale() -> str:
    """Re-materialize every stale or absent registered source; never registers a new source."""
    reply = _source_request({"op": "refresh-stale"})
    if "error" in reply:
        return f"refresh_stale failed: {reply['error']}"
    return json.dumps(reply.get("result", {"diagnostics": reply.get("diagnostics", [])}), ensure_ascii=False, indent=2)


def source_read(citation: str) -> str:
    """Resolve one search citation to its exact committed source snapshot or VINE block."""
    reply = _source_request({"op": "read", "citation": citation})
    if "error" in reply:
        return f"source_read failed: {reply['error']}"
    result = reply.get("result")
    if not isinstance(result, dict):
        return "source_read failed: malformed worker result"
    diagnostics = result.get("diagnostics")
    if diagnostics:
        return _diagnostic_text(diagnostics)
    text = result.get("text")
    if not isinstance(text, str):
        return "source_read failed: worker omitted source text"
    return sanitize.wrap("SOURCE_READ", sanitize.clean(text, max_chars=12000))


def workspace_search(query: str, k: int = 6) -> str:
    """Semantic search over our own writing: the human-owned spec, notes,
    proposals, root docs, and structural VINE task/ref chunks. Prefer this
    first — it is the ground for what we've already established. Hits self-cite
    (for example, 'initial-spec#spec#c2' or
    'proposals/scout-source-management.vine#ssm#vine'); read the cited source
    before building on it. The first call after server start may wait on a
    one-time index load; later calls are sub-second."""
    if _source_activation_present():
        return scout_search(query, k)
    return _search("workspace", "workspace_search", query, k)


def papers_search(query: str, k: int = 6) -> str:
    """Semantic search over the indexed third-party papers (resources/pdf/).
    Use it for the literature; workspace_search covers our own writing. Hits
    self-cite as paper#page#chunk (e.g. 'some-paper#p15#c2'); read the cited
    page before building on it. The first call after server start may wait on
    a one-time index load; later calls are sub-second."""
    if _source_activation_present():
        return scout_search(query, k)
    return _search("papers", "papers_search", query, k)


def docs_search(query: str, k: int = 6) -> str:
    """Semantic search over locally pinned Modal and VS Code documentation.
    Hits self-cite as path#vendor#chunk (for example,
    'guide-scale#modal#c2' or 'language-models#vscode#c3'); the matching
    mirror under resources/ holds the full text. Pins carry a date + TTL: a
    stale or missing pin announces itself at the top of results together
    with its refresh command. The first call after server start may wait on
    a one-time index load; later calls are sub-second."""
    if _source_activation_present():
        return scout_search(query, k)
    return _search("docs", "docs_search", query, k)


def web_search(question: str) -> str:
    """Search the live web for primary sources (Gemini + Google Search
    grounding), steered to answer with verifiable handles: Hugging Face repo
    ids, arXiv ids, canonical docs URLs. Returns a sanitized summary, the
    queries run, and a source list from grounding metadata. Treat the reply
    as leads; trust a handle once you resolve it yourself. Turn a
     load-bearing lead into declared material through `source_propose`; one
     explicit source row maps to one origin and one publication outcome.
    Hugging Face ids resolve on the hub page; cite the fetched source."""
    key = creds.load_key()
    if not key:
        return ("web_search unavailable: no credential. Run "
                "`python -m scout.creds set` (DPAPI store) or set GEMINI_API_KEY.")
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return "web_search unavailable: `pip install google-genai`."

    try:
        client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=90_000))
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=question,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.2,
                system_instruction=_WEB_SYSTEM,
            ),
        )
    except Exception as exc:
        return f"web_search failed: {type(exc).__name__}: {exc}"

    answer = sanitize.clean(getattr(resp, "text", None) or "(no answer text)")

    # Citations come from grounding metadata — structurally not model prose.
    queries: list[str] = []
    sources: list[str] = []
    cand = (getattr(resp, "candidates", None) or [None])[0]
    gm = getattr(cand, "grounding_metadata", None)
    if gm is not None:
        queries = list(getattr(gm, "web_search_queries", None) or [])
        for i, chunk in enumerate(getattr(gm, "grounding_chunks", None) or [], start=1):
            web = getattr(chunk, "web", None)
            if web is None:
                continue
            title = sanitize.clean(getattr(web, "title", None) or "untitled", max_chars=200)
            uri = (getattr(web, "uri", None) or "").strip()
            if uri.startswith("https://"):
                sources.append(f"  {i}. {title} — {uri}")
            else:
                sources.append(f"  {i}. {title} — (non-https uri withheld)")

    parts = [answer]
    if queries:
        parts.append("SEARCHES RUN: " + "; ".join(sanitize.clean(q, max_chars=200) for q in queries))
    parts.append("SOURCES (from grounding metadata):\n" + ("\n".join(sources) if sources
                 else "  none returned — treat the answer as UNGROUNDED model text"))
    return sanitize.wrap("WEB", "\n\n".join(parts))


app.tool()(workspace_search)
app.tool()(papers_search)
app.tool()(docs_search)
app.tool()(scout_search)
app.tool()(source_read)
app.tool()(source_propose)
app.tool()(refresh_stale)
app.tool()(web_search)


if __name__ == "__main__":
    print("scout MCP server on stdio", file=sys.stderr)
    if not _source_activation_present():
        _spawn()  # start legacy compatibility load now; never block handshake
    app.run()
