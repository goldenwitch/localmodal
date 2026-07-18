#!/usr/bin/env python3
"""scout MCP server: the repo's local corpus search + Gemini-grounded web
search, as agent tools over stdio.

Run (normally via .vscode/mcp.json, but works standalone):
    python -m scout.server

Tools:
  workspace_search(query, k) semantic hits from our own writing (spec,
                            notes, proposals); hits self-cite the file.
  papers_search(query, k)   semantic hits from the third-party papers
                            (resources/pdf/*.pdf), self-citing paper#page#chunk.
  docs_search(query, k)     semantic hits from the pinned vendor docs
                            (Modal and VS Code), freshness-stamped:
                            a stale pin screams at the top of its results.
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
        "Local search over this repo's own writing (workspace_search), the "
        "indexed third-party literature (papers_search), pinned vendor docs "
        "(docs_search), and live-web primary-source leads (web_search). "
        "Trust a web lead once its handle resolves — the fetch is the "
        "verification."
    ),
)

# Resident search worker. FastMCP runs sync tools inline on the event loop
# thread (verified by stack dump), so calls are serialized — no lock needed.
_PROC: subprocess.Popen | None = None
_READY = False


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


def _ensure_ready() -> subprocess.Popen:
    """Block until the worker's ready line (one-time load; minutes cold).
    EOF instead of a line means it died — report, don't guess."""
    global _READY
    _spawn()
    if not _READY:
        if not _PROC.stdout.readline():
            _shutdown()
            raise RuntimeError(
                "search worker died during startup (see MCP server log; "
                "if the index is missing: python resources/search.py --rebuild)")
        _READY = True
    return _PROC


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


atexit.register(_shutdown)


SNIPPET_CHARS = 320  # per-hit preview cap: enough to judge relevance; the id is the
                     # handle to the full source. Keeps a k-hit reply small enough to
                     # return inline instead of overflowing into a dumped, re-truncated file.


def _search(source: str, label: str, query: str, k: int) -> str:
    """Send one query to the resident worker for the named index and format the hits.
    Each hit is a compact preview (id + a capped snippet); the id self-cites the source
    to read in full, so the preview is the contract, not a loss."""
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
        return (f"{label} failed: search worker exited (see MCP server log). "
                "Retry once; if it repeats: python resources/search.py --rebuild")
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
        lines.append(f"[{tag}] {h['id']}\n  {snippet}")
    # Third-party text either way (papers, or our own .md) -- same hygiene, same labeling.
    return banner + sanitize.wrap(label.upper(), sanitize.clean("\n".join(lines), max_chars=12000))


def workspace_search(query: str, k: int = 6) -> str:
    """Semantic search over our own writing: the human-owned spec, notes, and
    proposals. Prefer this first — it is the ground for what we've already
    established. Hits self-cite (e.g. 'initial-spec#spec#c2'); read the cited
    file before building on it. The first call after server start may wait on
    a one-time index load; later calls are sub-second."""
    return _search("workspace", "workspace_search", query, k)


def papers_search(query: str, k: int = 6) -> str:
    """Semantic search over the indexed third-party papers (resources/pdf/).
    Use it for the literature; workspace_search covers our own writing. Hits
    self-cite as paper#page#chunk (e.g. 'some-paper#p15#c2'); read the cited
    page before building on it. The first call after server start may wait on
    a one-time index load; later calls are sub-second."""
    return _search("papers", "papers_search", query, k)


def docs_search(query: str, k: int = 6) -> str:
    """Semantic search over locally pinned Modal and VS Code documentation.
    Hits self-cite as path#vendor#chunk (for example,
    'guide-scale#modal#c2' or 'language-models#vscode#c3'); the matching
    mirror under resources/ holds the full text. Pins carry a date + TTL: a
    stale or missing pin announces itself at the top of results together
    with its refresh command. The first call after server start may wait on
    a one-time index load; later calls are sub-second."""
    return _search("docs", "docs_search", query, k)


def web_search(question: str) -> str:
    """Search the live web for primary sources (Gemini + Google Search
    grounding), steered to answer with verifiable handles: Hugging Face repo
    ids, arXiv ids, canonical docs URLs. Returns a sanitized summary, the
    queries run, and a source list from grounding metadata. Treat the reply
    as leads; trust a handle once you resolve it yourself. Turn a
    load-bearing paper lead into ground the same way every time:
      1. add `key: "arxiv:<id>"` to PAPERS in resources/fetch_papers.py and a
         row to resources/papers.md (the manifest);
      2. `python resources/fetch_papers.py` (PDF into resources/pdf/);
      3. `python resources/search.py --update` (embeds just the new chunks;
         `--rebuild` is the full re-embed);
      4. papers_search it — now it self-cites and is safe to build on.
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
app.tool()(web_search)


if __name__ == "__main__":
    print("scout MCP server on stdio", file=sys.stderr)
    _spawn()  # start the index load now; never block the handshake on it
    app.run()
