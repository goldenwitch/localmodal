# Executor Service — Implementation Spec

## 1. System context
- Two-tier agent architecture. A hosted frontier model does planning (low token volume, high value) and emits plans as `.vines` files. A self-hosted open-weight model executes those plans via subagents (high token volume, low unit value).
- The executor is self-hosted to (a) cut execution-token cost by orders of magnitude vs. frontier APIs and (b) allow custom per-role adapters later.
- Phase 1 (this spec): serve the executor on Modal. Phase 2 (out of scope): migrate to owned local hardware. The Phase 2 decision is made from Phase 1 telemetry, so telemetry is a first-class deliverable, not an add-on.

## 2. Serving stack
- Model: **Qwen3-Coder-80B-A3B**, 4-bit quantization (NVFP4 preferred; AWQ/Q4 fallback). ~48 GB weights.
- Runtime: **vLLM**, exposing an **OpenAI-compatible HTTP endpoint** behind Modal proxy auth; subagents authenticate with a key.
- GPU: Modal **RTX PRO 6000 (96 GB)**. Why this card: cheapest ≥80 GB option ($3.03/hr); 4-bit weights leave ~40 GB for KV cache, required for long agentic contexts.
- Enable **prefix caching** — subagents share long prompt scaffolding (system prompts, repo context, the plan itself), so a warm server compounds cache value across a session.
- Enable **multi-LoRA serving flags now**, even with zero adapters. Per-role adapters attach later without changing the API surface subagents code against.
- Storage: model weights and vLLM compile/graph caches persist on a **Modal Volume**. Why: drops warm-cache cold start to ~2–4 min (vs. 5–10 uncached).

## 3. Topology and static autoscaler config
- **Singleton.** Exactly one container. Parallel subagents are absorbed by vLLM continuous batching + high per-container input concurrency (allow ≥32 concurrent inputs). A second container doubles cost with no benefit at this load.
- Decorator (static) config encodes the **cold** state:
  - `min_containers=0`
  - `buffer_containers=0` — with a server that is always "active" during sessions, any buffer is a second full-price GPU running the whole time
  - `scaledown_window=300`
- Why cold-by-default: redeploys reset dynamic autoscaler settings to the decorator values, so the decorator must be the safe state. All warmth is applied dynamically (§4).
- Why `scaledown_window` cannot implement holds: it is a ceiling (containers may exit earlier), and it caps at 20 min. `min_containers` is the only reliable warmth primitive.

## 4. Warmth thermostat
- State: `modal.Dict` named `executor-state`, key `warm_until` (epoch seconds).
- Reconciler: cron every 10 min sets `min_containers = 1 if now < warm_until else 0` via `update_autoscaler()`. Idempotent desired-state loop; also self-heals within one tick after any redeploy.
- `request_hold(ttl_seconds)`: set `warm_until = now + ttl`, then invoke the reconciler immediately. Raising the floor pre-provisions the container — this **is** the pre-warm; no dummy request needed.
- Properties this design must preserve:
  - Expiry by default: doing nothing decays to off.
  - Bounded leak: worst case after a crashed caller = `ttl` + one tick.
  - Extension = timestamp overwrite (no release/re-acquire race).

## 5. Initial policy: 30-day calibration latch
- On first deploy: `warm_until = now + 30 days` → effectively 24/7 warm.
- Why: current demand is unobservable (the capability doesn't exist yet). Any scale-to-zero policy tuned now would be tuned on guesses and would suppress the demand signal it's supposed to measure. Remove supply friction first; observe unconstrained demand; then tune.
- Cost of calibration: $3.03/hr GPU → ~$2,213/mo; ~$2.5k/mo all-in with CPU/RAM metering. Treated as a bounded, one-time instrumentation expense — the latch expires unless deliberately renewed.
- During calibration, cold starts occur only at redeploys (~2–4 min each). The snapshot/sleep-mode wake-optimization stack is intentionally **not** built in this phase.

## 6. Telemetry
- Request middleware logs per call, appended as JSONL to a Volume: timestamp, subagent role, prompt tokens, completion tokens, TTFT, total latency, concurrent-streams gauge.
- Retain vLLM Prometheus metrics (prefix-cache hit rate, queue depth, throughput).
- Day-30 report computes three numbers:
  1. **Duty cycle** — fraction of clock-hours containing ≥1 request.
  2. **Gap CDF** — distribution of inter-request gaps; extract the smallest hold TTL that would have kept 95% and 99% of requests warm.
  3. **$/Mtok** — month all-in cost ÷ tokens served, compared against hosted-API pricing for the same open model (standing sanity check that self-hosting + adapters beats renting the same weights per-token).

## 7. Day-30 decision rule (pre-committed)
- **Duty cycle high (>~40% of hours, or trending up):** buy the local rig; the calibration data is the justification. Owned marginal cost ≈ electricity (~30–40× under rental rate). Keep Modal only until the hardware lands.
- **Duty cycle low:** switch to thermostat mode. `ttl = TTL95` from the gap CDF. The planner-start event (frontier model begins writing a `.vines` plan) fires `request_hold(ttl)`; plan generation time covers the wake, so perceived cold-start latency ≈ 0.
- **Intermediate / clearly diurnal:** cron floor — `min_containers=1` during working hours, 0 otherwise.
- All branches terminate the 24/7 state. Continuing it requires an explicit re-latch.

## 8. Out of scope, on deck
- Wake optimization (vLLM sleep mode + Modal CPU/GPU memory snapshots) — build only if the thermostat branch wins; target ~30–90 s wakes.
- Per-role adapter training and attachment (qLoRA / MiCA) via the already-enabled multi-LoRA path.
- Migration of this same stack (vLLM config, thermostat semantics, telemetry) to the local rig.