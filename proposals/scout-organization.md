# Scout organization — three lenses

Status: DRAFT for the owner. Measurements taken 2026-07-24 at `e751fec`. This
reports data and two candidate principles; it settles nothing. The plan graph
that consumes it is `proposals/scout-encapsulation.vine`.

## 0. Three lenses, and the order they have to run in

**Size.** *How big is this file?* Answers by splitting. Size is a proxy — it
correlates with trouble without naming it. It ranked the two god files, both of
which are now split (largest engine module 433 lines, largest test 1365).

**Permission.** *What is each module allowed to know?* Organization becomes a
statement about permissions and the tree becomes where they're enforced. Three
are measurable: what a module may import, whether it may know where it is,
whether it may cross a process boundary.

**Description length.** *Which features are necessary, and what is the
minimal-description factorization of them?* Minimize
$L(\text{structure}) + L(\text{behavior} \mid \text{structure})$ over
factorizations, restricted to necessary features, preferring factorizations
whose factors vary independently — where changing one feature does not require
re-describing another. A boundary earns its place only by buying back more than
the cost of describing it.

The third runs first. The other two take the feature set as given and optimize
its arrangement; the cheapest description of an unnecessary feature is zero, so
deletion dominates any layout improvement available. Settle necessity before
layout, or pay to relocate dead weight and pay again to remove it.

Owner rulings recorded for this draft: the unit of "feature" is the **public
capability surface** (MCP tools + CLI subcommands); necessity is judged against
**Scout-as-extracted**, the standalone tool it becomes next.

## 1. Method, and what it got wrong

AST import graph over `resources/**.py` and `scout/**.py`; bare-name imports
resolved against `resources/` (the flat namespace) and packages beneath it;
longest-path strata and cycle detection over import edges only, and
per-capability reachability over import edges plus `subprocess.Popen` targets.
Location census counts `Path(__file__)` / `__file__ =` sites against modules
taking a `root: Path` parameter. Both instruments now live in `tools/` with a
known-answer fixture; they were working copies when the figures below were
taken.

Two bugs found and fixed mid-measurement, recorded because the first numbers
were wrong and may be quoted from elsewhere:

1. Bare-name imports naming a *package* (`from source_control import ...`) went
   unresolved, silently dropping the CLI and worker edges.
2. Relative imports inside a package `__init__` resolved against the parent
   directory instead of the package. This mislocated `source_control/__init__`
   by three strata and understated the graph depth as 6 rather than 8.

One blind spot remains, and it is not fixable by this method: see §3.

## 2. Measurement A — capability reachability, and what nothing needs

Modules grouped by the set of public capabilities that can reach them.

| Serves | Modules | Lines |
| --- | --- | --- |
| `web_search` only | `scout/server`, `scout/sanitize`, `scout/creds`, `scout/__init__` | 661 |
| legacy aliases only (`workspace/papers/docs_search`, pre-activation) | `search` | 640 |
| vendor fetch scripts only | `fetch_modal_docs`, `fetch_papers`, `fetch_vscode_docs` | 281 |
| legacy aliases + vendor scripts | `freshness` | 114 |
| `cli: migrate` only | `source_migration` | 208 |
| all three source-capability groups | 17 modules (`config`, `diagnostics`, `durable`, `ledger`, `materializer`, `publication`, `source_index`, `source_model`, `attempts`, all of `source_control/`) | 4737 |
| the above + legacy aliases | `vine` | 348 |
| **all six capabilities** | `activation` | 96 |
| dispatch only | `source_cli` (76), `source_worker` (68) | 144 |

The `tools/` reconstruction reproduces every figure above except that last row:
counting each entry point as served by its own capability puts `source_cli` and
`source_worker` under `cli` and the source tools rather than in a group of their
own. It also reports 11 modules as told their location rather than 9, marking
the two mixed derivers as both. Recorded as measured deltas rather than
retrofitted into the table.

Observations:

- **The most entangled module in the repository exists to retire a feature.**
  `activation` is the only module reachable from every capability, at 96 lines
  and fan-in 6. Its whole job is sequencing the legacy→source cutover so a
  legacy reader cannot fall back. For Scout-as-extracted there is no legacy
  reader to fence out. What survives that deletion is a real question, not a
  formality: "has this store ever published?" may still be a necessary state
  even when "has the cutover happened?" is not. Sized gap, §9.
- **The unnecessary set is measurable.** Against Scout-as-extracted:
  `search` (640), `freshness` (114), the three vendor fetchers (281), and
  `source_migration` (208) serve only pre-activation compatibility or the
  one-shot legacy import — 1243 lines, 17% of the 7278 non-test module lines.
  Add four transition-only test modules (174 lines) and the legacy leg of
  `test_machine_output`. `activation` (96) sits on top of that, pending the
  question above.
- **`web_search` is already a clean factor.** Four modules, 661 lines, touching
  the engine at zero points. Its only tie to the rest is that `scout/server`
  imports `activation` — to route the *legacy aliases*. Delete legacy and
  `web_search` becomes fully separable.
- **`vine` is the one genuine straddle.** 348 lines serving both the legacy
  workspace route and the source pipeline's `vine-task` adapter. It survives
  extraction; only its legacy caller doesn't.
- **Over-approximation, flagged.** Module-level reachability cannot separate
  `scout_search` from `source_propose`: both enter through the `SourceControl`
  facade, which imports the whole engine, so all 17 core modules appear shared.
  In truth `search` needs publication + index + model; `propose` additionally
  needs materializer + ledger journal + recovery. Separating them needs
  method-level tracing, which I have not done — §10.

## 3. Measurement B — imports: clean layering, one invisible cycle

Eight strata, fan-in in parentheses:

| Stratum | Modules |
| --- | --- |
| 0 | `diagnostics` (18), `activation` (6), `durable` (5), `vine` (3) |
| 1 | `source_model` (13), `config` (5), `freshness` (4), `scout/server` |
| 2 | `source_control/outcomes` (6), `ledger` (5), `source_index` (5), `materializer` (2), `attempts` (1), `source_control/citations` (1), `search`, `fetch_*` |
| 3 | `publication` (3), `source_control/inputs` (2), `source_control/refresh` (1) |
| 4 | `source_control/execution`, `source_control/recovery` |
| 5 | `source_control/plane` (fan-out 14) |
| 6 | `source_control/__init__` (fan-out 8) |
| 7 | `source_migration`, `source_worker` |
| 8 | `source_cli` |

Zero **static** cycles. The dependency structure the source-management design
ruled is genuinely in the code — `diagnostics` is a true leaf with 18
dependants, so the closed error union is the foundation it was designed to be
and not merely on paper.

But `plane.py` and `execution.py` each contain `return sys.modules[__package__]`.
The package `__init__` imports `plane`; `plane` resolves the package object back
at call time. At runtime that is a cycle. No static measurement I ran can see
it — I found it by grep, after the corrected graph made me ask how the seam in
§5 actually works. Recorded as a method limit as much as a finding: the import
graph is evidence about imports, not about coupling.

And the layering is **invisible in the tree**. `resources/` holds stratum 0 and
stratum 8 side by side, so directory membership predicts nothing about what a
module may import, and no illegal edge is harder to write than a legal one. The
layering is a property of the current code, not of the structure.

## 4. Measurement C — location: 29 of 53 modules deduce where they are

| Population | Count | Who |
| --- | --- | --- |
| Derive location from `__file__` | 29 | 22 test modules, `search`, `freshness`, three `fetch_*`, `config`, `scout/server`, plus mixed `source_migration` and `source_control/plane` |
| Told their location (`root: Path`) | 9 | `source_index` (8 params), `vine` (5), `activation` (4), `materializer` (3), `source_model` (2), `attempts`, `durable`, `ledger`, `publication` |
| Location-free | 15 | `source_control/*` except `plane`, plus `diagnostics`, `source_cli`, `source_worker`, `scout/sanitize`, `scout/creds`, `scout/smoke` |

**The good pattern already exists, unevenly applied.** The engine core is
injected or location-free; the design's boundary modules take `resources_root`.
This is not a missing idea — it is an idea applied to what was written under the
source-management design and not to what was written before or beside it.

**The split made location load-bearing, then hid it.** Eighteen test modules
open with:

```python
# Preserve the original smoke entrypoint anchor for path-sensitive fixtures.
__file__ = str(Path(__file__).resolve().parents[1] / "smoke.py")
```

Each rebinds `__file__` to a file that is not itself, so `parents[1]` still
lands on the repo root. `scout/tests/harness.py` does the same and says so:
*"Keep moved smoke helpers anchored to the original entrypoint path."* The tests
pass — because 18 modules assert the same falsehood in unison. A pure file-move
refactor was blocked by location-dependence and unblocked by lying about
location. The next move pays the same toll.

A quieter instance: `config.py` sets
`CONFIG_PATH = Path(__file__).with_name("scout.json")`. The checked-in
configuration is half the store-validity boundary, and its address is defined by
the code position of the module that reads it.

## 5. Measurement D — four ways in, and one feature described twice

`scout/server.py` holds exactly one import edge into the engine
(`resources.activation`). Everything else crosses by spawn: two long-lived
`Popen` children (`search.py --serve`, `source_worker.py`), launched by absolute
path with `cwd=ROOT`, speaking line-JSON over pipes. At runtime `scout/` and
`resources/` are close to two programs talking over pipes, while looking like
two folders of one program.

Four disciplines currently reach the same code:

1. bare-name flat namespace inside `resources/`,
2. package-relative inside `source_control/` and `scout/`,
3. `importlib.util.spec_from_file_location` by path in `scout/tests/harness.py`,
   with a fallback branch detecting whether a name has become a package,
4. `subprocess.Popen` by absolute path.

Discipline 3 exists because of 1; discipline 4 is why 1 was survivable.

**One feature, two descriptions.** `source_cli.main` and `source_worker._reply`
are two dispatch tables over the same five control-plane operations — 76 and 68
lines, each marshalling `propose`, `refresh-stale`, `search`, `read`, `bootstrap`
into the same methods with separately-written validation and error rendering.
The design ruled that both surfaces must exist. It did not rule that the mapping
be written twice. Under the description-length lens this is the clearest
redundancy in the tree, and it is invisible to both other lenses.

Related, from §2 of the earlier draft: `source_control/__init__.py` states that
it re-exports the surface and owns the runtime seams *"reproducing the original
single-file namespace semantics."* The 1438-line class became seven modules; the
test coupling still addresses the pre-split module object, and the package
carries the `sys.modules[__package__]` machinery of §3 to keep that address
valid. File size moved; the patch surface did not.

## 6. Measurement E — entanglement through the filesystem

Invisible to the import graph, so worth checking separately: every runtime name
has exactly one constructing owner. `ledger.py` owns `.scout-ledger*`,
`publication.py` owns `.scout-publications` and `CURRENT`, `source_index.py`
owns `.scout-index*`, `materializer.py` owns `.scout-staging`, `source_model.py`
owns `scout-source--<name>`, `activation.py` owns `ACTIVATED`.

One violator: `search.py` constructs a second, independent `CURRENT`. That is
precisely the pointer the design ruled must never be read as truth — and it
lives in the module §2 marks unnecessary. The necessary features are already
well factored on disk; the entanglement that exists is concentrated in the
feature with a known expiry.

## 7. Where the lenses disagree

| Target | Size says | Permission says | Description length says |
| --- | --- | --- | --- |
| `test_source_control.py` (1365) | top remaining target | ordinary — dependent like every sibling | untouched: it tests necessary features |
| `search.py` (640, legacy) | second-largest file | 5 location sites, an island | **delete** — serves no necessary capability |
| `activation.py` (96) | trivial, ignore | injected, well-behaved | **most entangled thing measured** — reached by all six capabilities, exists to retire a feature |
| `config.py` (219) | fine | high — validity boundary addressed by code position | necessary, keep |
| `source_cli` + `source_worker` (144) | trivial | clean, location-free | one feature described twice |
| `source_control/` split (landed) | done | done | patch surface unchanged (§5) |

No two orderings agree. They are different questions, and the third reorders the
other two rather than competing with them.

## 8. Candidate principles

Two, on different axes, each stated once.

**Necessity precedes layout.** Nothing moves until it is known whether it
survives extraction. The measured deletion set is 1243 lines plus four test
modules; the arithmetic of moving them is strictly wasted.

**A module may not know where it is.** Location is a fact entrypoints inject and
modules receive. Testable consequences: a named, countable set of entrypoints
derives a root (candidates: `source_cli`, `source_worker`, `scout/server`, the
test runner) and every other module takes it; `Path(__file__)` outside that set
becomes a lint failure, for which the §4 census script is already the lint;
tests receive a root from a fixture and the 18 rebinds delete. Only then does
moving a file cost nothing, which is the precondition for the tree encoding
anything at all.

Neither settles the package layout, `src/` or not, whether `resources/` and
`scout/` merge, or how corpus data ships. Those become cheap to decide, rather
than answered.

## 9. Open questions — the owner's

- **Does `activation` survive?** Its cutover job ends with legacy. Whether "has
  ever published" remains a necessary state, and where it would live, decides
  the fate of the repository's most-reached module. Binds first, because it is
  upstream of the deletion set.
- **Is `web_search` a necessary capability of the extracted tool, or
  localmodal's paying customer?** §2 shows it is already separable at zero cost
  either way — which makes this a cheap decision to defer, and the only
  capability with that property.
- **Where does the root come from?** Environment variable, entrypoint argument,
  or a discovered marker file. Everything in the second principle depends on it.
- **What root do tests get** — a fixture, or the real repo as today via the
  rebind? Different testing postures, not just different plumbing.
- **Are the two workers one program or two?** §5 says they behave like two. If
  intended, `resources/` and `scout/` are peers and should not merge; if
  incidental, they should.
- Under this lens the earlier pytest question reframes as *who tells a test
  where it is*; the runner choice may fall out of that rather than the reverse.

## 10. Next measurements, if this is worth continuing

- **Method-level tracing** to split the 17-module core by capability, retiring
  the over-approximation flagged in §2. This is the measurement that would show
  whether read and mutate are one factor or two — the largest open entanglement
  question in the necessary set.
- Whether any current import edge would become illegal under a stratum fence —
  i.e. does tree-as-permission cost anything today, or is it free because the
  DAG is already clean.
- Change-coupling from git history. The branch is ~15 commits, so the signal may
  be too thin to read; cheap to check before trusting.

## 11. Other refactors spotted while measuring

Found incidentally, not by any of the three lenses. Recorded as evidence; the
work items they imply live in the plan graph.

**One fact, three declarations.** `search.py`, `source_index.py`, and `vine.py`
each independently declare
`MODEL = "sentence-transformers/all-MiniLM-L6-v2"`. This is load-bearing: the
VINE windower derives its token budget from the identity it declares for
itself, so a silent divergence between its copy and the index's copy produces
segments that exceed the encoder limit the index actually uses — the precise
failure the bounded-chunking design was written to prevent.

**The error union knows the file tree.** Diagnostic repair strings name
invocation paths (`"Use source_cli.py propose or refresh-stale..."`). The closed
diagnostic union is part of the store-validity boundary, and it currently
carries knowledge of where scripts sit — which goes stale at extraction, the
one event this whole document is preparing for.

**The legacy worker starts whether or not it is wanted.** `scout/server.py`
calls `_spawn()` unconditionally at startup, launching `search.py --serve` and
paying an index and model load — minutes cold, by the suite's own note — on
every server start. Post-activation the aliases it serves delegate to
`scout_search` instead. Worth confirming against a live start before acting;
the call site is unconditional, but the cost is an assumption.

**One word, two meanings.** `SourceControl.bootstrap(rows)` stages a first
publication from explicit rows; `source_migration.bootstrap(manifest)` imports
legacy material. The CLI imports the second as `bootstrap_migration` to
disambiguate at the call site — a rename applied at the point of confusion
rather than at the point of naming. `scout-vocabulary.md` governs terms in the
design; these are two code-level meanings under one word.

Three smaller ones sharing a root cause with §4:

- `smoke.py` lists its 17 test functions twice — once as imports, once in the
  results list — so adding a test is a two-site edit in one file.
- `harness._resource_module` carries a fallback branch that detects whether a
  name has become a package. A test helper encoding the refactor history of the
  code it loads.
- `test_ledger.py` computes a subprocess `PYTHONPATH` from the rebound
  `__file__`. It resolves correctly only because the rebind is correct; the
  value is derived from a deliberate falsehood two lines above it.
