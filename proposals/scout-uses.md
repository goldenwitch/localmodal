# Scout uses — inventory

Status: DATA for the owner. Taken 2026-07-24 at `cb71c31` (branch
`scout-source-management-execution`, clean tree). This enumerates every public
entry point Scout exposes and every use in this repository and its practice that
reaches one. Nothing here is ranked, promoted, or retired; §3 lists entry points
against which no use was found, and that listing is a measurement, not a verdict.

Owned by `sen/measure/use-inventory`. The unit of "entry point" is the public
capability surface already ruled for this work — MCP tools plus CLI subcommands —
extended to the process entry points those two are built on (`scout.server`,
`source_worker`, `search.py --serve`), because a use that starts a process is a
use even when it invokes no subcommand.

## 0. Method, and what it cannot see

Sources of evidence, in the order they were read:

1. `scout/server.py` tool registrations (lines 437–444) — the authoritative MCP
   surface.
2. `argparse` and `__main__` blocks across `resources/**.py` and `scout/**.py`.
3. `.vscode/mcp.json`, `scout/README.md`, `README.md`, `resources/papers.md` —
   documented invocations.
4. `scout/smoke.py` and `scout/tests/**` — mechanized invocations.
5. `localmodal.vine` — the research workflows that cite Scout.
6. The live tool list of the Scout MCP server as exposed to the agent session
   that produced this file.

Three things this method cannot see, recorded so a later reader does not mistake
absence for evidence:

- **Uses outside the repository.** An owner running a command by hand and never
  writing it down leaves no trace here. §5 flags the two places that gap is most
  likely to bind.
- **Call frequency.** Every use below is a *capability* record. Nothing in the
  tree counts invocations, so "used often" is not a statement this inventory can
  make about any row.
- **Intent behind a documented command.** Where a command appears only in a
  README, the record says so; a documented command with no mechanized caller and
  no vine citation is reported as exactly that.

## 1. Entry-point census

Every public entry point in the tree, against the named uses (§2) that reach it.

### MCP tools — `scout/server.py`

| Entry point | Registered | Named uses |
| --- | --- | --- |
| `scout_search(query, k)` | line 440 | U6, U8 |
| `source_read(citation)` | line 441 | U7, U8 |
| `source_propose(rows)` | line 442 | U5 |
| `refresh_stale()` | line 443 | U9 |
| `workspace_search(query, k)` | line 437 | U1, U8 |
| `papers_search(query, k)` | line 438 | U2 |
| `docs_search(query, k)` | line 439 | U3, U4 |
| `web_search(question)` | line 444 | U5 |

### CLI subcommands — `resources/source_cli.py`

| Entry point | Named uses |
| --- | --- |
| `bootstrap <rows>` | U7 |
| `migrate [--manifest]` | U7 |
| `write-migration-manifest [--manifest]` | U7 |
| `propose <rows>` | U5 |
| `refresh-stale` | U9 |
| `search <query> [--k]` | U8 (`test_machine_output`), U11 |
| `read <citation>` | none found — §3 |

### CLI subcommands — `resources/search.py` (legacy reader)

| Entry point | Named uses |
| --- | --- |
| `<query>... [--k]` | U8 (`test_machine_output`, human-output leg) |
| `--json <query>` | U8 (`test_machine_output`, machine-output leg) |
| `--serve` | U1, U10 |
| `--rebuild` | U12 |
| `--update` | U2, U12 |

### Other process entry points

| Entry point | Named uses |
| --- | --- |
| `python -m scout.server` | U1, U10 (VS Code, via `.vscode/mcp.json`) |
| `python resources/source_worker.py` (ops `bootstrap`, `propose`, `refresh-stale`, `search`, `read`) | U6, U7, U8, U10 |
| `python resources/source_migration.py write-manifest\|bootstrap` | none found — §3 |
| `python resources/fetch_modal_docs.py` | U4, U12 |
| `python resources/fetch_vscode_docs.py` | U3, U12 |
| `python resources/fetch_papers.py [--force]` | U2, U12 |
| `python -m scout.creds set\|check\|clear` | U13 |
| `python -m scout.smoke [--mcp] [--web]` | U8 |

### Configuration surfaces that bind a use

Not entry points, but a use that names one fails without it. Listed so the
inventory is complete against "what does this use need back".

| Surface | Bound use | What it carries |
| --- | --- | --- |
| `.vscode/mcp.json` | U1, U10 | stdio server registration, `cwd=${workspaceFolder}` |
| `resources/scout.json` | U5 | `repo_files.publishable_paths` — a `repo-file` add outside this list is rejected |
| `resources/sources.json` | U12 | pre-activation freshness metadata (pin dates, TTLs) |
| `resources/papers.md` | U2 | the paper manifest `fetch_papers.py` reads |
| `GEMINI_API_KEY` / DPAPI store | U5, U13 | web credential; env var takes precedence |
| `GEMINI_MODEL` | U5 | model override, default `gemini-3.1-pro-preview` |

## 2. Named uses

Each record: **who** invokes, **through which** entry point, **what it needs
back**, **cost if absent** — cost stated as what breaks and what the repair
would be, with a number wherever one was measurable.

---

### U1 — Agent design search over our own writing

**Who.** Any Copilot agent session in this workspace, including the session that
produced this file.

**Entry point.** `workspace_search` over MCP; `python -m scout.server` started by
VS Code from `.vscode/mcp.json`; served by `search.py --serve` before activation.

**Needs back.** Ranked hits over the human-owned spec, proposals, root docs, and
VINE task/ref chunks, each self-citing a handle (`initial-spec#spec#c2`,
`proposals/scout-source-management.vine#ssm#vine`) that resolves to the exact
source.

**Cost if absent.** The agent falls back to `semantic_search` / `grep_search`
over the workspace, which return file regions rather than citation handles; the
self-citation contract that lets a reader re-open the cited chunk is what is
lost, not the ability to find text. Two documents state the "read the cited
source before building on it" discipline that depends on the handle:
`scout/README.md` and the server's own tool description.

---

### U2 — Third-party literature search and paper onboarding

**Who.** Agent sessions answering questions from the indexed papers; whoever adds
a paper.

**Entry points.** `papers_search` over MCP; `fetch_papers.py` then
`search.py --update` for onboarding (`resources/papers.md` lines 8–9 states this
two-step workflow verbatim).

**Needs back.** Hits citing `paper#page#chunk`; after onboarding, a new paper's
chunks present in the index without a full re-embed.

**Cost if absent.** Measured at this commit: `resources/pdf/` holds **0 files**,
so `papers_search` currently has no material to return, and the onboarding
workflow has been run zero times against the present tree. The manifest
(`resources/papers.md`) and the fetcher (`fetch_papers.py`, part of the 281
fetcher lines) survive as the recipe. Cost of losing the *tool* today is zero
observed answers; cost of losing the *workflow* is the documented path by which a
load-bearing paper becomes citable ground.

---

### U3 — The MCP tool round-trip that the Qwen trial verifies against

**Who.** `localmodal.vine`, node `lm/verify/tools` (notstarted).

**Entry point.** `docs_search` over MCP, invoked by VS Code agent mode against
the Modal-hosted model.

**Needs back.** A tool call that VS Code can invoke and whose result the endpoint
accepts. The node's acceptance text names the tool literally: *"the endpoint
emits a valid streamed tool call, VS Code invokes `docs_search`, the endpoint
accepts the tool result, and the final answer uses it"* (localmodal.vine line
52).

**Cost if absent.** The acceptance criterion of a `lm/requires/tools` verifier
becomes unexecutable as written. Repair is one vine edit naming a different tool.
Note the shape of the dependency: this use needs *a* tool with a stable name and
a cheap deterministic result — it does not need the docs corpus specifically.

---

### U4 — Pinned vendor documentation as design evidence

**Who.** `localmodal.vine` research nodes `lm/research/modal-runtime`,
`lm/research/vscode-contract`, `lm/research/engines`, `lm/research/auth`, and the
three engine nodes; each carries `@guidance text/html <vendor URL>` attachments.

**Entry points.** `docs_search` over MCP for retrieval; `fetch_modal_docs.py` and
`fetch_vscode_docs.py` for the mirrors it reads.

**Needs back.** Retrieval of the pinned vendor text with a citation
(`guide-scale#modal#c2`, `language-models#vscode#c3`), plus the freshness banner
that rides ahead of hits when a pin is past TTL.

**Cost if absent.** Measured mirror size at this commit: **283** Modal doc files,
**2** VS Code doc files. Those nodes already carry dozens of dated `Known
2026-07-1x:` findings quoted from this material; without the pin, re-checking a
quoted claim means re-fetching the live page, which is the same operation the
research discipline already requires for a load-bearing claim. The freshness
banner — a pin announcing its own staleness — has no substitute in the workspace.

---

### U5 — Turning a lead into declared, citable ground

**Who.** Any agent session following the source-onboarding discipline stated in
`scout/README.md` §"Source Onboarding" and in the `web_search` tool description.

**Entry points.** `web_search` for the lead; `source_propose(rows)` over MCP, or
`source_cli.py propose rows.json`, for the declaration.

**Needs back.** From `web_search`: a sanitized answer, the queries run, and a
source list drawn from grounding metadata (structurally not model prose). From
`source_propose`: one publication outcome per row, atomic across the batch.

**Cost if absent.** `web_search` is the only live-web path in the tree — 4
modules, 661 lines, zero import edges into the engine
(`proposals/scout-organization.md` §2). Absent it, an agent has no in-repo way to
locate a primary source and the repo's own instruction *"trust a web lead once
its handle resolves"* addresses nothing. Absent `source_propose`, a resolved
handle can still be fetched by hand but cannot become indexed, citable material;
`repo-file` adds additionally require the target path to be listed in
`resources/scout.json`.

---

### U6 — Validated search over the source publication

**Who.** Agent sessions after activation; `scout/tests/test_mcp.py` and
`test_machine_output.py` unconditionally.

**Entry points.** `scout_search` over MCP → `source_worker.py` op `search`;
`source_cli.py search` for the terminal path.

**Needs back.** Hits only when the master publication, every source binding,
every artifact digest, and the referenced index generation validate; typed
diagnostics and *no hit text* otherwise.

**Cost if absent.** This is the tool the source-management work exists to
produce; its absence is the absence of the current engine, not a feature.
Recorded state at this commit: `resources/` holds `.scout-transition.lock` and
nothing else — no `ACTIVATED` marker, no `.scout-ledger*`, no
`.scout-publications`, no `.scout-index*`. So `scout_search` today returns
`PUBLICATION_MISSING`, which is exactly what `test_mcp` asserts for the inactive
branch. There is no live store in this tree.

---

### U7 — First publication, and legacy import

**Who.** Whoever activates a Scout store. `scout/README.md` documents
`source_cli.py migrate` as the first activation step; `test_source_migration`
exercises the module.

**Entry points.** `source_cli.py migrate`, `bootstrap`,
`write-migration-manifest`; `source_worker.py` op `bootstrap`; `source_read` for
verifying what landed.

**Needs back.** A staged first publication built from an explicit manifest,
activated atomically, with the marker written before the pointer flip.

**Cost if absent.** The material at stake is 292 declared rows / 285 imports
(repo memory, verified 2026-07-22 at `07b36b0`). `proposals/scout-organization.md`
§2 sizes the module at 208 lines; `planning.md` §"The unpriced invariant"
sighting 2 records that the adopt-local-bytes premise was never tested and that
re-fetching is also more correct. Both figures are prior measurements quoted, not
re-derived here. Note the ordering fact this inventory can add: at this commit
there is nothing published, so `migrate` has zero completed uses against the
present tree.

---

### U8 — Development validation

**Who.** Any agent or owner changing Scout; repo memory names
`python -m scout.smoke` as the validation ritual.

**Entry points.** `python -m scout.smoke` (17 tests), `--mcp` (adds a real stdio
handshake), `--web` (adds one live grounded query). Through them, mechanized use
of: `python -m scout.server`, `scout_search`, `source_read`, `workspace_search`,
`source_cli.py search --k`, `search.py <query>`, `search.py --json`,
`search.py --serve`, `source_worker.py`.

**Needs back.** PASS/FAIL, and — for `test_mcp` — the assertion that the server
lists exactly eight tools: `docs_search`, `papers_search`, `refresh_stale`,
`scout_search`, `source_propose`, `source_read`, `web_search`,
`workspace_search`.

**Cost if absent.** This is the only executable check in the tree; there is no
pytest configuration and no CI workflow (`.github/` does not exist). Its
`test_mcp` leg is also the only place the public tool list is written down as an
assertion rather than as prose, so it is the mechanism by which a change to the
MCP surface fails loudly.

---

### U9 — Pin maintenance

**Who.** Documented in `scout/README.md`; the refresh command string is embedded
in the fetchers themselves (`REFRESH_CMD = "python resources/source_cli.py
refresh-stale"`, `fetch_modal_docs.py` line 35) and stamped into freshness
metadata.

**Entry points.** `refresh_stale()` over MCP; `source_cli.py refresh-stale`;
`source_worker.py` op `refresh-stale`.

**Needs back.** Re-materialization of stale or absent registered sources, and
nothing else — the surface never registers a new source.

**Cost if absent.** The freshness contract loses its remedy: a stale pin can
still announce itself but nothing can act on the announcement without a full
`propose`. The repair path advertised by every stamped source and by the
diagnostic repair strings would name a command that does not exist.

---

### U10 — Server and worker lifecycle

**Who.** VS Code, on demand, per `.vscode/mcp.json`; `scout/server.py` itself,
which spawns children.

**Entry points.** `python -m scout.server`; `search.py --serve` spawned
unconditionally at startup when the activation marker is absent
(`scout/server.py` line 450); `source_worker.py` spawned lazily on the first
source tool call.

**Needs back.** A server that completes the MCP handshake without blocking on an
index load, and workers whose stdin is their lifeline so a dead server cannot
orphan one.

**Cost if absent.** No MCP surface at all — U1, U2, U3, U4, U5, U6, U7, U9 all
route through this process. Two observations belong to this row rather than to
any tool: the legacy worker's spawn is unconditional pre-activation and pays an
index and model load ("minutes cold", per the suite's own note), and the
`cwd=${workspaceFolder}` in `mcp.json` plus `ROOT = parents[1]` in `server.py`
are what make the one-workspace assumption operative today.

---

### U11 — Terminal search by a human

**Who.** Documented in `scout/README.md` §"Source Onboarding":
`python resources/source_cli.py search "your query"`.

**Entry point.** `source_cli.py search`.

**Needs back.** JSON on stdout, indented, sorted keys.

**Cost if absent.** Evidence for this use is a README line and one mechanized
caller (`test_machine_output` invokes it with `--k 3` to assert the machine
contract). No vine node cites it. Whether a human has ever run it outside the
test is not observable from the tree — flagged in §5.

---

### U12 — Legacy corpus maintenance

**Who.** The pre-activation workflow: `resources/papers.md` for papers, the two
vendor fetchers for docs, then a re-index.

**Entry points.** `fetch_modal_docs.py`, `fetch_vscode_docs.py`,
`fetch_papers.py [--force]`, then `search.py --update` (incremental) or
`--rebuild` (full re-embed).

**Needs back.** Refreshed mirrors, a freshness stamp per source, and an index
generation containing the new chunks.

**Cost if absent.** Each fetcher calls `freshness.require_legacy_writer()` and
fails before mutating files once source control has activated
(`fetch_modal_docs.py` lines 45–49) — so this use is defined to end at
activation, and `scout/README.md` says so directly: *"Legacy vendor fetch scripts
and `resources/search.py --update/--rebuild` reject activation rather than
mutating material behind the master publication."* The material it maintains is
the 283 + 2 mirror files of U4, whose successor path is U5/U9.

---

### U13 — Credential lifecycle

**Who.** The owner, per `scout/README.md` §"Setup".

**Entry points.** `python -m scout.creds set` / `check` / `clear` (`check` is the
default when no subcommand is given).

**Needs back.** A DPAPI round-trip bound to the current Windows account, and a
fingerprint that identifies the stored key without printing it.

**Cost if absent.** `web_search` (U5) falls back to `GEMINI_API_KEY` in the
environment, which is already the documented non-Windows path and always takes
precedence. `test_dpapi` exercises the primitive directly rather than the CLI.

## 3. Entry points against which no use was found

Reported as measurements. No conclusion is drawn about any of them here.

| Entry point | What the search covered |
| --- | --- |
| `source_cli.py read <citation>` | Not in `scout/README.md`'s command lists, not in any vine node, not invoked by the smoke suite. The *capability* is used through `source_read` over MCP (U7, U8); the **CLI subcommand** has no located caller. |
| `python resources/source_migration.py write-manifest\|bootstrap` | A second `__main__` onto the two operations `source_cli.py` already exposes as `write-migration-manifest` and `migrate`. `test_source_migration` imports the module and calls it as a library, not as a CLI. No document names this path. |
| `python -m scout.creds clear` | Documented in the module docstring; absent from `scout/README.md`'s setup block, which lists only `set` and `check`. No mechanized caller. |
| `fetch_papers.py --force` | The flag has no located caller; the unflagged script is named by `resources/papers.md`. |
| `search.py --k` | Reachable only with a positional query; `test_machine_output` passes `--k` to `source_cli.py search`, not to `search.py`. |

## 4. Observed environment at this commit

State facts, so a later reader knows what the inventory was taken against.

| Observation | Value |
| --- | --- |
| Scout runtime state under `resources/` | `.scout-transition.lock` only — no `ACTIVATED`, no ledger, no publications, no index |
| `resources/pdf/` | 0 files |
| `resources/modal-docs/` | 283 files |
| `resources/vscode-docs/` | 2 files |
| MCP tools registered by `scout/server.py` | 8 |
| MCP tools exposed to the agent session that wrote this file | 4 — `workspace_search`, `papers_search`, `docs_search`, `web_search` |
| Test entry points in `scout/smoke.py` | 17 default, +1 with `--mcp`, +1 with `--web` |
| CI / pytest configuration | none — no `.github/`, no pytest config |

The 8-versus-4 row is the one that most wants a second look before it is used
for anything: it is a reading of the tool list this session was given, and this
inventory cannot distinguish "the running server predates the source tools" from
"the host filtered the list". `test_mcp` asserts 8 against a freshly spawned
server, so the 8 is the property of the code and the 4 is a property of some
running instance.

## 5. What this inventory could not establish

Sized gaps, not voids — each states when it binds.

- **Uses that leave no repository trace.** The two entry points most likely to
  have an unrecorded human user are `source_cli.py search`/`read` (U11) and
  `scout.creds clear`. Binds when a surface is proposed for removal on the
  strength of "no use found"; the cheap closing move is one question to the
  owner, not a further measurement.
- **Which tool `lm/verify/tools` actually needs.** U3 names `docs_search`
  literally, but the requirement it serves is "a tool round-trip", and the vine
  node does not say whether the corpus matters. Binds at the first change to the
  legacy alias set.
- **Frequency and value per use.** Nothing in the tree counts invocations, so
  every row above is capability-shaped. Binds if any decision downstream wants
  "how much is this used" rather than "is this used".
- **The `web_search` question, restated as a use question.** This inventory can
  say U5 is the only live-web path in the tree; it cannot say whether the use
  belongs to Scout or to localmodal, which is the owner question already open in
  `proposals/scout-organization.md` §9.
