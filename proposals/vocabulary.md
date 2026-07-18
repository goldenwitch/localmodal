# Vocabulary

> STATUS: DRAFT (agent-scribed 2026-07-17 from owner rulings; owner review pending).
> Terms become binding when the spec uses them. Goal numbers refer to the
> nine-goal stone (owner, 2026-07-17); goals 1, 2, 4, 5, 6, 8 land in this
> repo, goals 3, 7, 9 land in DevDev.

## The role side (what a caller may rely on)

- **Executor** — the role: where inference requests go. **Effectless by
  definition**: tokens in, tokens out — no server-side tools, no browsing, no
  filesystem, no state between requests. Tool execution and sandboxing happen
  on the client side of the contract (goals 7, 9 → DevDev).
- **Caller** — anything holding a credential that sends a request. Agents
  never self-identify (they don't know who they are or who they work with);
  identity enters with the *execution*, not the caller's self-knowledge.
- **Execution** — one dispatched unit of work against the executor: a request,
  its response, and its provenance. The unit that telemetry records, evals
  judge, and counterfactuals replay (goals 6, 8).
- **Execution identity** — the provenance an execution carries, minted at
  dispatch by the composition (never asserted by an agent): at minimum an
  execution id; optionally the arm it belongs to and a parent reference
  (plan/task). Goal 8.
- **Dispatch / Dispatcher** — the act and the component (CLI or MCP, goal 1)
  that sends work to the executor, streams the response back, and mints
  execution identity.
- **Surface** — the wire shape: routes + request/response schemas. Instance in
  play: an OpenAI-compatible *subset* the contract pins (chat completions,
  streaming, tool-call fields; exact list = contract work).
- **Credential** — what a caller presents and *which component checks it*
  (platform / engine / shim).
- **Availability semantics** — the promise about not-ready: queue, or fail
  fast with retry as the caller's duty, or block.
- **Model name** — what the `model` field means: an alias we define, bound to
  an artifact or an adapter route by composition wiring (goal 5). Never caller
  identity.
- **Provider** — the executor exposed in the shape a third-party harness
  expects: base URL + credential + model names. Goal 2's target: GH Copilot
  (CLI/SDK/Chat — disambiguation pending).
- **Contract** — the sum a caller may rely on: Surface + Credential +
  Availability semantics + Model-name semantics + Execution-identity carriage
  + any promised floors. Survives every swap on the fill side.

## The fill side (what we may swap without callers noticing)

- **Engine** — the inference process (vLLM / SGLang / TRT-LLM class). May
  *incidentally provide* Surface and Credential-checking; the vocabulary
  exists so that incidental provision never blurs into contract.
- **Artifact** — the weights as pinned bytes: checkpoint + quantization, named
  by a resolvable handle.
- **Adapter** — a runtime-swappable weight delta (LoRA-class) attached to the
  engine and selected per-execution via model name (goal 5). *Reserved word:*
  "adapter" always means this; the hosting abstraction is a **host binding**,
  never an adapter.
- **Host** — what gives the engine lifecycle and a network identity: Modal
  Function+`@web_server`, Modal Server, someday systemd on the rig. Owns the
  mechanics behind availability semantics.
- **Host binding** — the declarative statement of *which* host fills the role
  and with what resources (goal 4). Swapping bindings must not touch the
  contract.
- **Platform** — where the host runs: Modal now, owned hardware later.
- **Shim** — any layer we author between host and engine (credential
  translation, identity fallback, telemetry capture). A fill may have none.
- **Policy** — the numeric knobs: concurrency, autoscaler values, served
  context length. Config we change freely — unless a number is promised to
  callers, which graduates it across the razor into the contract as a floor.

## Evaluation terms (goal 6)

- **Arm** — a named, dispatchable variant: (artifact, adapter, policy) tuple.
  Model names route to arms.
- **Assignment** — how an execution gets its arm: explicit at dispatch, or
  allocated by the dispatcher for A/B.
- **Counterfactual replay** — re-running a stored execution against a
  different arm; requires execution records complete enough to re-dispatch.
- **Quality judgment** — the comparison of executions across arms. Eval design
  is owner-led; this vocabulary only names the slot.

## The razor

**Contract vs Policy** is the whole game: anything a caller may depend on is
Contract and survives swaps; everything else is Policy/fill and swaps
silently. Every design fork resolves to: *which side of the razor, and which
component checks or provides it.*
