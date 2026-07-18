# DevDev handoff

> STATUS: DRAFT (agent-scribed 2026-07-17; owner review pending). Seams only —
> nothing here designs DevDev internals. Goals 3, 7, 9 land in DevDev; this
> document states what localmodal holds stable for them and where the joints
> are. Vocabulary per [vocabulary.md](vocabulary.md).

## Boundary principle

localmodal's executor is **effectless**: tokens in, tokens out. Agent hosting,
tool execution, filesystem mediation, and sandboxing are DevDev's
jurisdiction. Agent-hosting-shaped code (ACP, sessions, workspaces, approval
flows) stays out of this repo — the same discipline geas ruled for itself.
The seam between the projects is exactly the executor contract, plus the
handoff points below.

## What localmodal holds stable for DevDev

1. **The provider triple** — base URL + credential + model names
   ([strawman-architecture.md](strawman-architecture.md), goal 2). Anything
   DevDev hosts that speaks OpenAI-compatible chat completions can consume
   the executor with zero DevDev-specific accommodation on our side.
2. **Tool-call fields in the surface** — the contract's OpenAI subset includes
   `tools` / `tool_calls`, so a DevDev-side harness can round-trip tool use
   through our executor. Tool *execution* never crosses to our side (goal 7).
3. **The effectless invariant** — sandbox guarantees (goal 9) compose from
   DevDev's side alone; the executor adds no effects that would need
   re-sandboxing. Inference calls leaving a DevDev sandbox reach one known
   endpoint with one known credential — an auditable egress point.
4. **Boundary-minted execution identity** — executions originating in
   DevDev-hosted agents are recorded even when the harness carries no
   identity (fallback minting at the contract boundary, goal 8), so DevDev
   runs appear in eval data from day one.

## Handoff points by goal

### Goal 3 — MCP-dispatched GH Copilot CLI agents (DevDev)
DevDev already hosts Copilot (`copilot --acp`) and owns agent lifecycle. The
joint: those agents' *inference* is pointed at our executor via the provider
triple. Known mechanics on the DevDev side: its Copilot child processes
inherit the parent environment (verified in devdev-acp client spawning), so
provider configuration can arrive by environment without DevDev code changes.
Richer identity propagation (per-run ids injected by DevDev at spawn) is a
DevDev-side enhancement; until then, boundary-minted fallback identity covers
recording.

### Goal 7 — custom sandboxed tools (DevDev)
Tools are declared to the model through the request's `tools` field and
executed wherever the agent runs — inside DevDev's sandbox. localmodal's whole
obligation is surface support (tool-call fields, streamed tool-call deltas).
Tool registries, sandboxing of tool effects, and approval flows are DevDev
design space.

### Goal 9 — executions stay sandboxed (DevDev)
Sandboxing composes entirely client-side: DevDev virtualizes the workspace
and mediates actions; the executor contributes by being effectless and by
having exactly one egress shape (the contract). No localmodal work item
exists for this goal beyond keeping the invariant true.

## Sequencing

Nothing in localmodal blocks on DevDev. The shared dependency is goal 2 (the
provider integration): once Copilot-as-caller works anywhere, DevDev-hosted
Copilot inherits it by configuration. Candidate first joint test, when both
sides are ready: one DevDev-hosted agent run whose inference lands on the
executor and appears in execution records with a boundary-minted identity.
