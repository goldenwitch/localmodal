# Executor Service — Implementation Spec

## 1. System context
- The executor is self-hosted to (a) cut execution-token cost by orders of magnitude vs. frontier APIs and (b) allow custom adapters later.
- Serve the executor on Modal.

## 2. Serving stack
- Model: **Qwen3-Coder-80B-A3B**, 4-bit quantization (NVFP4 preferred; AWQ/Q4 fallback). ~48 GB weights.
- Runtime: **vLLM**, exposing an **OpenAI-compatible HTTP endpoint** behind Modal proxy auth; subagents authenticate with a key.
- GPU: Modal **RTX PRO 6000 (96 GB)**. Why this card: cheapest ≥80 GB option ($3.03/hr); 4-bit weights leave ~40 GB for KV cache, required for long agentic contexts.
- Enable **prefix caching** — subagents share long prompt scaffolding (system prompts, repo context, the plan itself), so a warm server compounds cache value across a session.
- Storage: model weights and vLLM compile/graph caches persist on a **Modal Volume**. Why: drops warm-cache cold start to ~2–4 min (vs. 5–10 uncached).

## 3. Topology and autoscaler config
- **Singleton.** Exactly one container. Parallel subagents are absorbed by vLLM continuous batching + high per-container input concurrency (allow ≥32 concurrent inputs). A second container doubles cost with no benefit at this load.
- Decorator (static) config encodes the **on** state:
  - `min_containers=1`
  - `max_containers=1` — makes the singleton architectural rather than assumed
  - `buffer_containers=0` — with a server that is always active, any buffer is a second full-price GPU running the whole time
- **On until turned off.** The service runs warm from deploy until deliberately stopped. Redeploys reset autoscaler settings to the decorator values, so a redeploy converges back to warm — the decorator is the desired standing state. There is no automatic scale-down.