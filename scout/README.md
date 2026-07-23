# scout — search MCP server

Exposes the repo's search surfaces as agent tools over stdio MCP:

| tool | surface | trust |
|---|---|---|
| `scout_search` | validated unified index over the active source-bound publication | all source bindings, artifact digests, and the referenced index generation validate before hits are ranked |
| `source_read` | resolve a source or VINE citation against its committed snapshot | lets a caller inspect the exact material that produced a search hit |
| `source_propose` | explicit ordered `add`/`remove` source batch | all rows preflight before runtime work; an accepted batch publishes atomically or not at all |
| `refresh_stale` | stale/absent registered-source maintenance | re-materializes only selected existing sources through the same publication path |
| `workspace_search`, `papers_search`, `docs_search` | legacy compatibility aliases | before activation they use routed legacy indexes; after activation they resolve `scout_search` |
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

## Source Onboarding

`web_search` is steered to surface **verifiable handles explicitly**: a Hugging
Face repo id, an arXiv id, or a canonical docs URL. Its answer is a lead, never
a citation. Resolve a load-bearing handle, then add one explicit source row.

The first activation imports the checked-in explicit manifest:

```powershell
python resources/source_cli.py migrate
```

That privately stages every retained source, builds one unified index, and
atomically activates the first master publication. The generated source ledger,
artifacts, index generations, and master pointer are local runtime state.
The runtime ledger is `resources/.scout-ledger.json`; the tracked
`resources/sources.json` file remains pre-activation freshness metadata and is
not rewritten by source control.

After activation, use one of these paths:

```powershell
python resources/source_cli.py propose rows.json
python resources/source_cli.py refresh-stale
python resources/source_cli.py search "your query"
```

`rows.json` is an array of complete `add` rows or target-only `remove` rows.
The canonical terms and exact payload boundaries live in
[scout-vocabulary.md](../proposals/scout-vocabulary.md). Legacy vendor fetch
scripts and `resources/search.py --update/--rebuild` reject activation rather
than mutating material behind the master publication.

A `repo-file` add may name only an exact normalized repository-relative path
listed in `resources/scout.json` under `repo_files.publishable_paths`. Add a
path there before proposing it; hidden paths, VCS metadata, traversal, and
symlink/reparse escapes are rejected. `migrate` and `bootstrap` are private
first-publication commands and reject once source control has activated.

## Worker Model

Loading OpenBLAS-backed DLLs inside a threaded async server deadlocks on the
Windows loader lock (observed live: py-spy showed `LoadLibraryExW` wedged
against anyio's stdio reader). So the server talks line-JSON to a
source worker (`resources/source_worker.py`) that validates the master
publication, then loads its referenced unified index generation. The master
pointer under `resources/.scout-publications/CURRENT` is the only active reader
truth after activation; the worker never follows route-specific index pointers.
The sibling `ACTIVATED` marker is written before the first pointer flip and is
one-way: if `CURRENT` is later missing or malformed, public routes return the
source-store diagnostics rather than falling back to a legacy index.
Legacy readers recheck that marker under the same transition barrier used by
activation, and legacy fetch writers hold the barrier for their process; a
cutover therefore waits for an in-flight compatibility operation rather than
allowing a legacy mutation to cross the activation boundary.
Its stdin is its lifeline, so a dead server cannot orphan it. Worker stderr is
inherited and lands in the VS Code MCP log.

## Smoke tests

```
python -m scout.smoke                  # sanitize unit checks + corpus round-trip
python -m scout.smoke --mcp            # + real stdio handshake and tool call
python -m scout.smoke --web            # + one live grounded query (needs key)
```

The corpus and `--mcp` legs each pay one cold index load — minutes on a small
machine, deterministic (no internal clocks to race).
