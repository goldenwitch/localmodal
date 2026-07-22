# scout — search MCP server

Exposes the repo's search surfaces as agent tools over stdio MCP:

| tool | surface | trust |
|---|---|---|
| `workspace_search` | local txtai index ([resources/search.py](../resources/search.py)) over **our own writing**: the human-owned spec, notes, proposals, root docs, and structural VINE tasks/refs | our results; prefer it first, hits self-cite, read the file/task before building on it |
| `papers_search` | local txtai index over the **third-party papers** (`resources/pdf/`) | the literature; hits self-cite as `paper#page#chunk` |
| `docs_search` | local txtai index over the **pinned vendor docs**: Modal (`resources/modal-docs/`) and VS Code (`resources/vscode-docs/`) | pinned ground with a date + TTL; hits self-cite as `path#vendor#chunk`; a stale or absent pin screams at the top of its results |
| `web_search` | Gemini + Google Search grounding | **untrusted leads** — sanitized, delimited, citations from grounding metadata |

Registered in [.vscode/mcp.json](../.vscode/mcp.json); VS Code starts it on demand.
Named `scout` because the MCP SDK's pip package is literally `mcp` — a top-level
`mcp/` dir would shadow it. Same trap class as `modal/`: this repo's core
dependency is the Modal SDK, so a top-level `modal/` dir is equally forbidden.

## Setup

```
pip install -r requirements.txt
python -m scout.creds set              # paste Gemini API key (hidden input)
python -m scout.creds check            # verify decryption round-trip
```

The key is DPAPI-encrypted bound to the current Windows account (plus app
entropy) at `%LOCALAPPDATA%/localmodal-scout/`. **Credentials never live in
the repo.** `GEMINI_API_KEY` env var overrides the store (and is the
non-Windows path). Model defaults to `gemini-3.1-pro-preview`; override with
`GEMINI_MODEL`.

## Sanitization (the part that matters)

Web output is an injection surface. [sanitize.py](sanitize.py) is deterministic —
no guard-LLM arms race:

1. **Unicode hygiene** — NFC, drop all format chars (zero-width, bidi
   overrides, tag chars = the ASCII-smuggling alphabet) and controls except `\n\t`.
2. **Structure stripping** — markdown images removed whole (exfiltration
   beacons), HTML tags removed, links demoted to `text (url)`, https only.
3. **Budget** — hard length caps, blank-line collapse.
4. **Labeling** — everything wrapped in `<<<… UNTRUSTED …>>>` delimiters.
5. **Citations are structural** — `web_search` source lists come from the
   grounding API's metadata, never from model-written text; a hallucinated
   citation cannot appear there.

The real guarantee sits outside this folder: irreversible actions in this repo
go through a human gate regardless of what fetched text says.

## Provenance rule

`web_search` is steered to surface **verifiable handles explicitly** — a
Hugging Face repo id, an arXiv id, a canonical docs URL — and that handle is
the only part of its (untrusted) output worth trusting, because it's verified
by being fetched/resolved, not by Gemini's say-so. The answer is a *lead*,
never a citation. Turn a load-bearing paper lead into ground the same way
every time:

1. add `key: "arxiv:<id>"` to `PAPERS` in [fetch_papers.py](../resources/fetch_papers.py) and a row to [papers.md](../resources/papers.md);
2. `python resources/fetch_papers.py` — PDF into `resources/pdf/`;
3. `python resources/search.py --update` — incremental, embeds just the new PDF's chunks (`--rebuild` for a full re-embed);
4. `papers_search` it — now it self-cites and is safe to build on.

Hugging Face ids are verified by resolving the hub page. Take no one's word —
including Gemini's.

## Freshness ledger

Pinned sources are stamped in [resources/sources.json](../resources/sources.json)
by the fetcher that pulls them: fetch date + TTL (`ttl_days: null` = immutable,
like arXiv PDFs). Once a pin outlives its TTL — or its files go missing — every
search reply from that corpus opens with a `!!` warning naming the source, the
overdue days, and the exact refresh command. Consulting a rotten pin and seeing
the rot are the same event; nothing goes quietly stale.

## Worker model (why the corpus tools never import txtai here)

Loading OpenBLAS-backed DLLs inside a threaded async server deadlocks on the
Windows loader lock (observed live: py-spy showed `LoadLibraryExW` wedged
against anyio's stdio reader). So the server talks line-JSON to a
resident worker (`resources/search.py --serve`) that loads all three corpora at
startup and **hot-swaps** the routed one to a new version whenever a rebuild
publishes one. Each corpus is independent: `resources/.index/papers/`,
`resources/.index/docs/`, and `resources/.index/workspace/` each hold versioned
`v<ns>/` dirs behind their own atomic `CURRENT` pointer, so rebuilds never
fight the worker for an open file and one corpus rebuild never disturbs the
others. The
conversation has no timeouts: every pipe read returns a line
or EOF, both handled. The worker exits on stdin EOF — its stdin is its
lifeline — so a dead server cannot orphan a worker, even when hard-killed.
Worker stderr is inherited, landing in the VS Code MCP log.

## Smoke tests

```
python -m scout.smoke                  # sanitize unit checks + corpus round-trip
python -m scout.smoke --mcp            # + real stdio handshake and tool call
python -m scout.smoke --web            # + one live grounded query (needs key)
```

The corpus and `--mcp` legs each pay one cold index load — minutes on a small
machine, deterministic (no internal clocks to race).
