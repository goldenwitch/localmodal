# Scout public surface — overlap map

Status: DRAFT for the owner. Measured 2026-07-24 at `cb71c31`. This reports what
each public entry does in source-model terms and which entries do the same
thing; it settles nothing and deletes nothing.

Vocabulary is `proposals/scout-vocabulary.md` throughout. Where an entry's own
help text uses a word differently, the difference is recorded rather than
translated away.

## 0. What counts as the surface, and how it was enumerated

The unit is the **public capability**: MCP tools plus CLI subcommands. Two
process entry points are included as well — `python -m scout.server` and
`resources/source_worker.py` — because each publishes a wire protocol that a
caller addresses directly, and one of them (§2, G7) is where a duplicate
dispatch lives.

Enumeration is a single re-runnable pattern over `resources/*.py` and
`scout/*.py`:

```powershell
Select-String -Path resources\*.py, scout\*.py -Pattern 'add_parser\("|app\.tool\(\)\(|choices=\(|cmd == "|add_argument\("--(serve|rebuild|update|json|force|mcp|web)"'
```

**Known-answer check on the enumeration, and what it caught.** The pattern's
MCP result must equal the eight tools named by hand in `scout/server.py`'s
module docstring; it does. Its CLI result must equal the five operations named
by hand in `source_worker._reply`'s error message ("op must be bootstrap,
propose, refresh-stale, search, or read"); it **did not** — the worker's
dispatch is an `if operation == ...` chain and the pattern sees none of it.
Those five entries are in the table below because the hand-known case disagreed
with the instrument, not because the instrument found them. Treat the pattern as
a floor on the surface, not a census of it.

Counted surface: **35 addressable entries** — 8 MCP tools, 21 CLI subcommands
and mode flags (7 `source_cli`, 2 `source_migration`, 5 `search.py`, 3 fetchers,
3 `creds`, 1 `smoke`), 5 worker protocol ops, and the server process entry. In
§1 one row stands for the three `creds` verbs and one row for the five worker
ops.

## 1. Every public entry, stated as a source-model operation

| # | Entry | Where | Operation in source-model terms |
| --- | --- | --- | --- |
| 1 | `scout_search(query,k)` | MCP | Validate the current publication, rank chunks of its index generation, return citations + capped previews + warnings. |
| 2 | `source_read(citation)` | MCP | Resolve one citation against the live snapshot it names; return the artifact text, capped. |
| 3 | `source_propose(rows)` | MCP | Apply one ordered batch of source rows against a valid current publication; emit one publication and one outcome per row. |
| 4 | `refresh_stale()` | MCP | Select registered sources whose live snapshot is absent or TTL-expired, materialize each, publish. |
| 5 | `workspace_search(query,k)` | MCP | Post-activation: entry 1 verbatim. Pre-activation: semantic query over the legacy `workspace` route. |
| 6 | `papers_search(query,k)` | MCP | Post-activation: entry 1 verbatim. Pre-activation: legacy `papers` route. |
| 7 | `docs_search(query,k)` | MCP | Post-activation: entry 1 verbatim. Pre-activation: legacy `docs` route. |
| 8 | `web_search(question)` | MCP | No source-model operation. Grounded remote query; returns leads, declares nothing, publishes nothing. |
| 9 | `source_cli.py search QUERY [--k]` | CLI | Entry 1, rendered as JSON with an exit code. |
| 10 | `source_cli.py read CITATION` | CLI | Entry 2, rendered as JSON with an exit code. |
| 11 | `source_cli.py propose ROWS.json` | CLI | Entry 3, rows read from a file. |
| 12 | `source_cli.py refresh-stale` | CLI | Entry 4. |
| 13 | `source_cli.py bootstrap ROWS.json` | CLI | Entry 3 with no current publication as its base, refused once the store has activated. |
| 14 | `source_cli.py migrate [--manifest]` | CLI | Entry 13 whose rows come from the checked-in inventory, binding each row to already-fetched local bytes instead of materializing. |
| 15 | `source_cli.py write-migration-manifest` | CLI | Discovery: walk existing local material, emit the row array + import map. Declares nothing. |
| 16 | `source_migration.py bootstrap [--manifest]` | CLI | Entry 14, same function, different spelling. |
| 17 | `source_migration.py write-manifest` | CLI | Entry 15, same function, different spelling. |
| 18 | `search.py QUERY [--k]` | CLI | Legacy: rank chunks of the route-local `CURRENT` index of all three routes, print for humans. Not publication-validated. |
| 19 | `search.py QUERY --json` | CLI | Entry 18, machine rendering, semantic hits only, corpus name per hit. |
| 20 | `search.py --rebuild` | CLI | Build a legacy index generation from scratch over route inputs and move the route-local pointer. |
| 21 | `search.py --update` | CLI | Entry 20, incremental over changed chunks only. |
| 22 | `search.py --serve` | CLI/protocol | Resident legacy reader: `{source,query,k}` in, `{hits,warnings}` out. |
| 23 | `fetch_papers.py [--force]` | CLI | Discovery + materialization outside the ledger: resolve arXiv/OpenReview ids from a code-level manifest, write PDFs, stamp a freshness pin. |
| 24 | `fetch_modal_docs.py` | CLI | As 23: expand `llms.txt` into a page set, mirror markdown, stamp a pin. |
| 25 | `fetch_vscode_docs.py` | CLI | As 23, fixed page set. |
| 26 | `scout/creds.py set\|check\|clear` | CLI (3) | No source-model operation. Local credential store for entry 8. |
| 27 | `scout/smoke.py [--mcp] [--web]` | CLI | No source-model operation. Runs the suite; the two flags add legs. |
| 28 | `python -m scout.server` | process | Hosts entries 1–8; spawns the legacy reader unconditionally when the activation marker is absent. |
| 29 | `source_worker.py` | process | Line-JSON protocol carrying exactly `bootstrap`, `propose`, `refresh-stale`, `search`, `read` — entries 13, 3, 4, 1, 2 under a fifth spelling. |

## 2. Groups — entries whose operations are the same modulo an argument or a default

Eight groups. Each states the collapsed form and what a caller could no longer
express after the collapse.

### G1. Query — 9 entries, one operation

Entries 1, 5, 6, 7, 9, 18, 19, 22, and the worker's `search` op.

Post-activation, entries 5–7 are `return scout_search(query, k)` verbatim; the
route name they carry is dead argument. Entries 18–22 are the same ranking
against the legacy route-local pointer instead of the master pointer.

*Collapsed form:* one operation `search(query, k)` over the current
publication, with one clamp.

*What a caller could no longer express:* (a) which route to search — post-
activation nothing can express this already, since the three aliases ignore it;
(b) ranking against material that is not in a validated publication, which is
what entries 18–19 do and which the health gate exists to prevent; (c) the
human-readable rendering of entry 18, distinct from the JSON of 19 and the
capped-preview text of 1.

*Noted in passing:* the clamp `max(1, min(k, 20))` is written three times —
`scout/server.py` (`MAX_K`), `source_cli.py`, `source_worker.py`. One rule,
three declarations, no owner.

### G2. Resolve — 3 entries, one operation

Entries 2, 10, and the worker's `read` op. Byte-identical operation; only the
rendering differs (MCP wraps in the sanitizer, CLI prints JSON).

*Collapsed form:* `read(citation)`.

*What a caller could no longer express:* nothing about the operation. Only the
choice of rendering, which is the transport's business.

*Internal structure worth recording, not a collapse:* `read_citation` dispatches
on two citation grammars — `source:<name>#<snapshot>#<chunk>` and
`<quoted-path>#<target>#vine`. That is one entry over two grammars, not two
entries.

### G3. Declare and publish — 10 entries, one operation with three bases

Entries 3, 11, 13, 14, 16, the worker's `propose` and `bootstrap` ops, and
(outside the ledger entirely) 23–25.

`bootstrap` and `propose` reach the same `_run(rows, bootstrap=...)`. The flag
does exactly three things: it refuses when the activation marker exists; it lets
`_base` return an empty record map instead of raising `PUBLICATION_MISSING`
when no current publication exists; and it tolerates a
`LEGACY_MIGRATION_REQUIRED` ledger read. Two of those three are legacy-facing.

*Collapsed form:* `propose(rows)` whose base is the current publication if one
exists and the empty map otherwise.

*What a caller could no longer express:* the refusal itself — "this store has
already published, so a first publication is a mistake." Today that refusal is a
typed diagnostic (`BOOTSTRAP_AFTER_ACTIVATION`); after the collapse, proposing
rows against an already-published store is simply a proposal, which is correct
if and only if there is nothing special about the first one.

Entry 14/16 (`migrate`) additionally carries `import_paths`: bind a row to
already-fetched local bytes rather than materialize its origin. *What is lost
with it:* the ability to publish without contacting an origin — which is the
same capability an offline or air-gapped first publication would need, and the
only current caller of it is a one-shot legacy import.

Entries 23–25 do not touch the ledger at all: they materialize a code-level
manifest into local files and stamp a freshness pin. Their materialization
collapses into `propose`(add rows) exactly. Their *discovery* does not — see G6.

### G4. Select and materialize — 3 entries, one operation with a predicate

Entries 4, 12, and the worker's `refresh-stale` op — together with the re-fetch
case of G3 (an `add` row naming an existing source), which is a case rather than
an entry.

`_refresh_stale_with_lease` builds `AddRow(record.declaration)` per selected
record and hands them to the same `_preflight`/`_execute` as `propose`. So the
row construction genuinely is "propose with a predicate instead of caller rows".

*But the failure policy differs, and not by a default.* When materialization
fails, `propose` fails the batch. `refresh-stale` stages a `refresh_failure`
into the journal, keeps the prior live snapshot, and records an attempt record
that later surfaces as a warning on every search. That is a second axis, not a
second argument.

*Collapsed form:* `propose(select, on_failure)` where `select` is either an
explicit row list or a staleness predicate, and `on_failure` is either
fail-batch or keep-snapshot-and-warn.

*What a caller could no longer express:* today, nothing chooses between the two
failure policies — the choice is welded to the entry. After the collapse, a
caller could express "refresh these named sources, failing the batch", which is
new capability rather than lost capability. What *would* be lost is the
guarantee that an explicit proposal can never silently leave an old snapshot
live: that guarantee currently comes free from there being no way to ask.

### G5. Index generation — 2 entries against one internal step

Entries 20 and 21 build and publish a legacy index generation. In the source
engine there is no public entry for this at all: an index generation is produced
inside every batch and referenced by the publication it belongs to.

*Collapsed form:* none needed — the operation already has zero public entries on
the source side.

*What a caller could no longer express:* re-indexing without changing any
declaration (today: `--rebuild` to compact, `--update` after adding material).
`refresh-stale` is not that operation: it re-materializes origins. If compaction
is a use, it has no entry in the source engine; if it is not, entries 20–21 are
the whole cost of pretending it is.

### G6. Discovery — 2 entries plus half of 3 more, one operation Scout's vocabulary says it does not do

Entries 15, 17 (inventory → row array), and the manifest-expansion halves of
23–25 (`llms.txt` → page set; arXiv id → PDF URL).

The vocabulary is explicit: *source discovery — creating a new declaration from
content, a ref, a glob, or an inventory. Scout's normal commands do not do
this.* Every entry here does exactly that, and each is currently classified as
migration or as a legacy fetcher.

*Collapsed form:* one operation `discover(inventory) -> rows`, outside the
control plane, whose only output is a row array a caller may then propose.

*What a caller could no longer express:* nothing, if discovery keeps an entry.
Everything, if the collapse deletes discovery along with the legacy fetchers —
hand-writing the 292 rows the checked-in inventory currently generates is the
cost, and it is paid again at every vendor index change.

### G7. Dispatch — one feature described twice

`source_cli.main` (76 lines) and `source_worker._reply` (68 lines) are two
tables over the same five operations, with independently written argument
validation and error rendering. Confirmed line by line:

| Operation | `source_cli` | `source_worker` |
| --- | --- | --- |
| bootstrap | rows from a JSON file, list-checked in `_rows` | rows from the request, list-checked inline |
| propose | same | same |
| refresh-stale | no argument | no argument |
| search | `--k` default 6, clamped 1..20 | `k` default 6, type-checked, clamped 1..20 |
| read | positional citation | citation, nonempty-checked |
| migrate, write-migration-manifest | present | absent |

*Collapsed form:* one dispatch table over the five operations, with two thin
transports (argv → request, line → request) and two renderers.

*What a caller could no longer express:* the CLI's exit-code semantics, which
exist nowhere else — `source_cli` inspects every row outcome and returns 1
unless each is `published`, `removed`, or `not_found`. That is a real feature of
the CLI transport and it needs a home in the collapsed form.

### G8. Entries outside the source model — 5, already separate

Entry 8 (`web_search`), entries 26 (`creds set|check|clear`), entry 27
(`smoke`). None declares, materializes, publishes, or queries a source. The
root's ruling — web search separates completely — is already true of the code:
these entries touch the engine at zero import points.

*Collapsed form:* unchanged; they are not part of the source surface.

*What a caller could no longer express:* n/a. Recorded here so the coverage
claim in §4 is honest about them rather than silent.

## 3. The four starting points, checked

| Claim | Verdict | Evidence |
| --- | --- | --- |
| With activation dead, `bootstrap` is `propose` against an empty store | Confirmed, with one residue | The flag does three things (G3); two are legacy-facing, the third is the `PUBLICATION_MISSING` refusal, which is a live design choice rather than dead weight |
| `refresh-stale` is `propose` with a selection predicate instead of caller rows | Half confirmed | Row construction and execution are literally shared; the failure policy is a second axis (G4) |
| CLI and MCP are two spellings of one dispatch | Confirmed | Table in G7; the CLI carries two entries the worker does not, and the worker carries none the CLI does not |
| `source_read` resolves what `scout_search` returns | Confirmed, and total | Search emits citations in exactly the two grammars `read_citation` accepts (G2); no third grammar exists on either side |

## 4. Coverage, and where this map is thin

Coverage claim: all 35 entries appear in §1, and each appears in a group in §2 —
9 in G1, 3 in G2, 10 in G3, 3 in G4, 2 in G5, 2 in G6, 5 in G8, and the server
process entry, which hosts operations rather than performing one. The tallies
sum to 35 because entries 23–25 are counted once, in G3; each of them also
performs the G6 operation, so they are the one place a single entry does two
things. G7 describes a duplication among entries already counted and adds none.

Known limits, stated rather than hidden:

- The instrument is a grep plus reading. An entry declared by an idiom neither
  covers would be invisible; the worker ops already proved this can happen.
- "Same operation" here is judged from code paths and returned shapes, not from
  a behavioral test. Two entries called identical in §2 have not been run
  side-by-side against the same store.
- Rendering differences (sanitizer wrap, exit codes, human vs JSON) are recorded
  as losses where they exist, but no ruling is offered on which transport should
  own them.
- Route arguments on entries 5–7 are dead only *post-activation*. Pre-activation
  they select genuinely different corpora, so the G1 collapse is conditional on
  the legacy readers being gone, not on the aliases being renamed.

## 5. What survives every collapse

Reported as a count, not a proposal. Distinct source-model operations remaining
after G1–G7:

| Operation | Entries collapsing into it |
| --- | --- |
| `search(query, k)` | 9 |
| `read(citation)` | 3 |
| `propose(rows)` — base is current publication or empty | 10 |
| `propose(select=predicate, on_failure=keep)` | 3, plus the re-fetch case |
| `discover(inventory) -> rows` | 2, plus half of entries 23–25 |
| index maintenance with no declaration change | 2, currently legacy-only |
| outside the source model (web, credentials, suite) | 5 |

Thirty-five counted entries; six source-model operations, one of which (index
maintenance) has no source-side entry today and one of which (discovery) the
vocabulary places outside the control plane — plus five entries that carry no
source-model operation at all.

## 6. Questions this map raises for the owner

- Is the first publication special? The `BOOTSTRAP_AFTER_ACTIVATION` refusal is
  the only thing `bootstrap` has that `propose` does not once legacy is gone.
- Is keep-snapshot-and-warn a property of refreshing, or the right failure
  policy for every batch? G4 is the only place the surface hides a policy behind
  an entry name.
- Does discovery keep an entry? The vocabulary says Scout does not discover;
  five current entries do, and one of them produced the 292-row manifest.
- Is re-indexing without re-declaring a use? If yes it needs an entry; if no,
  `--rebuild`/`--update` are pure legacy cost.
- Where do exit codes live once the CLI and worker share one dispatch?
