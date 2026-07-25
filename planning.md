# Planning

Notes on what makes a plan hold up, written from this repository's own record.
Every case below has a commit or a measurement behind it, including the ones we
got wrong while writing the plan that found them.

## 1. What a plan needs

A plan is a set of commitments made before the evidence is in. Its quality is
how well it survives contact with measurement — so the useful question is not
"is this plan good?" but "what would show this plan was wrong, and how early?"

Three things carry that weight. A plan works when each item in it states:

- **Necessity** — what this exists for. An item that cannot name the requirement
  it serves is a candidate for deletion, and deletion is the cheapest move
  available at any point in a plan.
- **Price** — what it would cost if this were simply absent. A property
  protected without a stated cost-of-loss will grow machinery to defend it, and
  nothing in the plan will ever say stop.
- **Falsifiability** — how you would know. A claim carries the measurement that
  produced it, so a later reader can re-run it rather than re-argue it.

Two preconditions make those three statable at all. Each thing needs **one
name, and each name one thing**, or the plan spends its rules compensating for
its vocabulary. And the **nouns get counted before mechanisms are compared** —
choosing between implementations of a thing you have not finished counting
produces a confident answer to the wrong question.

Necessity is upstream of everything else. Arranging, decomposing, migrating, and
hardening all multiply the cost of whatever they touch, so they are worth doing
after survival is settled and not before.

Remedies belong on a ladder, strongest first: **impossible** — the structure
leaves no way to express the mistake; **screaming** — a check fails loudly and
early; **discipline** — someone remembers. Discipline is where a remedy lands
when the first two are unavailable, not where it starts. Five of the six
pitfalls below reach the first rung. One reaches the second and no further, and
says so.

## 2. A zoo of pitfalls

Six distinct animals. The first is the most common — three sightings in one
repository — and the last two are ours from a single afternoon.

### The unpriced invariant

*Shape:* a property is protected without measuring what losing it would cost.
The defense then grows without a stopping condition, because no one wrote down
what it was worth.

*Sighting 1.* `proposals/scout-source-management.vine` required that no public
query ever see a mixed answer while the store cut over from the legacy indexer
to source-bound publication. That one requirement produced two coexisting reader
models, a one-way `ACTIVATED` marker, a shared transition lock, compatibility
aliases, an adopt-local-bytes import path, and two members of the closed
diagnostic union — 69 reference sites across the tree at `07b36b0`, plus
`activation.py`, which later measured as the only module reachable from all six
public capabilities. The material being protected was 285 re-fetchable public
document URLs. The cost of losing it was one command.

*Sighting 2.* `source_migration.py` (208 lines) exists for its `imports` map,
which binds each declared row to its already-fetched local file so bytes can be
adopted instead of re-fetched. The premise — that this material must survive the
change intact — was never tested. Re-fetching is also *more* correct: current
content, real digests, real TTL semantics.

*Sighting 3.* `proposals/scout-organization.vine`, in the same session that
documented the first two, gated deletion of the legacy readers on re-declaring
the corpus first, "so the legacy readers become genuinely dead before they are
deleted" — preserving search continuity in a repository whose store was already
empty. The plan that named the pattern reproduced it.

*The question that catches it:* what happens if this is simply absent, and what
does that cost?

*Remedy, rung 1.* Let the outgoing and incoming implementations share no state:
no common lock, no marker, no routing consulted by both. With no shared surface,
"a mixed answer" has nowhere to be expressed, and the switch becomes a one-line
change to what gets started. The apparatus at `07b36b0` was *entirely* shared
surface — a lock both sides held, a marker both sides read, aliases that
consulted it. The coupling existed to guarantee the transition property, and the
property existed because of the coupling.

*Remedy, rung 2, where both must genuinely coexist.* State the price as a number
in the node, and have the verifying node compare the size of the defense against
it. Sixty-nine sites against a stated price of one command is a ratio that
screams without anyone needing to notice.

### Arrangement before necessity

*Shape:* effort is spent organizing, splitting, or migrating artifacts whose
survival has not been ruled. The work is real and the result is discarded.

*Sighting.* `bc7e4d6` split two god files into packages: 4823 insertions
against 4537 deletions across 31 files, a net increase of 286 lines of
description. Among the carefully relocated files were four test modules and
several code paths that the next plan scheduled for deletion. The decomposition
was competent; it was applied to code whose necessity had not been settled.

*The question that catches it:* is this staying?

*Remedy, rung 1.* Express the ordering as a dependency edge rather than as
advice. When each arranging node depends on the necessity ruling for the
artifacts it touches, the execution frontier will not offer it early. The plan
format already computes the frontier, so this is enforcement rather than memory —
and it was available at the time, simply unused.

### One word, two referents

*Shape:* a name carries two meanings, so a rule gets written to compensate.
Rules cost more than names, and they keep costing.

*Sighting 1.* `CURRENT` names both the master publication pointer and
`search.py`'s route-specific index pointer. The design then had to rule, in
prose, that one of them must never be read as truth — a rule that exists only
because a name was reused.

*Sighting 2.* `bootstrap` means "stage a first publication from explicit rows"
on the control plane and "import legacy material" in the migration module. The
CLI imports the second as `bootstrap_migration`, applying a rename at the point
of confusion rather than at the point of naming.

*Sighting 3.* `proposals/scout-vocabulary.md` exists because `papers`, `docs`,
and `workspace` had been read as source categories when they named routes. A
whole document was needed to say what the names did not.

*The question that catches it:* does this word already mean something here?

*Remedy, rung 1.* One module owns each on-disk name and each shared term; others
take it from that owner instead of constructing their own. A second `CURRENT`
becomes unwritable rather than merely forbidden.

*Remedy, rung 2.* Scan for identifiers bound in more than one module, and for
filename literals constructed in more than one place. This is already working:
the ownership scan in `proposals/scout-organization.md` §6 found the second
`CURRENT` without being told to look for it.

### The proxy that ranked the work

*Shape:* a measurement is correct but measures a stand-in, and the stand-in
orders the work. Nothing is wrong until a second measurement disagrees.

*Sighting.* The first organization draft ranked targets by file size. A later
permission lens and a description-length lens ranked the same targets and no two
orderings agreed: size put a 1365-line test file first, where necessity leaves
it untouched; size dismissed `activation.py` at 96 lines, where entanglement
made it the most-reached module in the repository. The full disagreement is
tabulated in `proposals/scout-organization.md` §7.

*The question that catches it:* what is this a proxy for, and what would a
different lens rank first?

*Remedy, rung 2 — and there is no rung 1 here.* A ranking becomes actionable only
once a second independent measure has ranked the same targets, with the
disagreement published rather than resolved. No structure can prevent a
measurement from standing in for the thing you care about; the best available
move is to make a single lens insufficient to authorize work.

### The unvalidated instrument

*Shape:* a number is reported before the tool that produced it has been checked
against a case whose answer is already known.

*Sighting.* Twice in one session, an import-graph script produced confident
structure from silently dropped edges — first because bare-name imports naming a
package went unresolved, then because relative imports inside a package
`__init__` resolved against the parent directory. The corrected run moved the
graph from 6 strata to 8 and relocated a module by three levels. Both wrong
versions had already been quoted in conversation. A related over-approximation
survives, flagged in place: module-level reachability cannot separate read from
mutate through a facade.

*The question that catches it:* what does this tool say about a case I already
know the answer to?

*Remedy, rung 1.* An instrument carries a fixture whose answer is known by hand
and reports nothing when the fixture fails. A four-module fixture containing one
deliberate cycle and one package `__init__` would have caught both errors above
before either was spoken aloud.

### Mechanism before noun-count

*Shape:* a fork is offered between implementations of a thing before the thing
has been counted, so the answer is confident and addresses the wrong question.

*Sighting.* "Where does the root come from — environment variable, entrypoint
argument, or discovered marker file?" was put up for ruling before noticing that
Scout has at least three roots: the repository root that `repo-file` origins and
VINE citations resolve against, the state root holding ledger, publications and
artifacts, and the location of the checked-in configuration. At extraction these
stop coinciding, since the workspace being served belongs to the consumer. The
mechanism question is real but downstream.

*The question that catches it:* how many of these are there?

*Remedy, rung 1.* Make the count its own node and let the design node depend on
it. Same mechanism as arrangement-before-necessity: the graph carries the
ordering, so the fork cannot be offered before the cardinality is known.

## 3. Rubric

Ordered by rung. Build the first two wherever they are available, and let
discipline carry only what is left.

**Make it impossible**

1. Outgoing and incoming implementations share no state, so a transition
   property has no surface on which to be stated.
2. Ordering lives in dependency edges — pruning ahead of arranging, counting
   ahead of choosing — so the frontier enforces it and nobody has to recall it.
3. Each name has one owning module, and everyone else takes it from there.
4. Instruments carry a known-answer fixture and stay silent when it fails.

**Make it scream**

5. Each protected property states its price as a number, and verification
   compares the size of its defense against that number.
6. Duplicate identifiers and duplicate on-disk literals are scanned for.
7. A ranking authorizes work once a second independent measure has ranked the
   same targets, and the disagreement is published.
8. A node that names no requirement fails validation.

**Leave to discipline**

Judging whether a stated price is honest, and whether a chosen lens is the right
one. Both stay human — which is the argument for stating them in the open, where
they can be disagreed with early and cheaply.

A plan that answers these is still likely to be wrong somewhere. It will be wrong
in a way the next measurement can find, which is the property worth having.
