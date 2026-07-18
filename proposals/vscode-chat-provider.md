# Modal Qwen Provider for VS Code Chat

> STATUS: DRAFT PROPOSAL (agent-scribed 2026-07-17). This proposes a sharp
> descope; it does not modify the human-owned specification.

## Outcome

Run one Qwen model on Modal and select it as a **Custom Endpoint** model in VS
Code Chat. The experiment is complete when the model can answer a streamed
chat request and complete an agent-mode request using an existing MCP tool.

This repository would answer one question: *do I like using this model in VS
Code Chat enough to build it into something larger?*

## One path

```text
VS Code Chat Custom Endpoint
  -> authenticated Chat Completions request
  -> public Modal web-server Function
  -> vLLM OpenAI-compatible server
  -> one Qwen model
```

### VS Code

Configure the model through **Chat: Manage Language Models** -> **Add Models**
-> **Custom Endpoint**:

- API type: `chat-completions`.
- URL: the full Modal-hosted `/v1/chat/completions` endpoint.
- API key: the key checked by vLLM.
- Model id: the id served by vLLM.
- `toolCalling: true` and `streaming: true`.
- `maxInputTokens` and `maxOutputTokens`: measured values whose sum does not
  exceed the served context window.

The deprecated `github.copilot.chat.customOAIModels` setting is not used.

### Tools

VS Code owns tools. It discovers configured MCP servers, sends their tool
schemas in chat requests, invokes selected tools, and returns tool results in
later requests. localmodal does not host MCP servers or execute tools.

The provider must correctly preserve OpenAI Chat Completions tool fields and
stream valid tool-call deltas from Qwen. Tool calling is required for the model
to appear in VS Code agent mode.

### Modal and inference

- One loadable **Qwen3-Coder 80B-A3B 4-bit** artifact. Resolve and record the
  canonical artifact id before implementation; do not add alternate models.
- **vLLM** serving OpenAI-compatible Chat Completions with streaming and tool
  calling enabled.
- vLLM checks one `Authorization: Bearer` API key stored as a Modal Secret.
- A Modal Function exposed with `@modal.web_server(...,
  requires_proxy_auth=False)`. Modal proxy auth is not used because it requires
  two nonstandard headers; vLLM authenticates the otherwise-public URL.
- One **RTX PRO 6000 (96 GB)** container.
- One Modal Volume for model and vLLM compile caches.
- Prefix caching enabled.
- Initially one warm container (`min_containers=1`, `max_containers=1`,
  `buffer_containers=0`) so a chat request does not wait through model load.
  Turn the deployment off manually when the trial is not running.

## Acceptance

1. A direct authenticated Chat Completions request streams a Qwen response
   from the deployed Modal URL; a missing or incorrect key is rejected.
2. The Custom Endpoint model appears in the VS Code Chat model picker and can
   be selected.
3. A normal VS Code Chat prompt streams a complete response from Qwen.
4. In agent mode, a prompt requiring Scout's `docs_search` causes Qwen to emit
   a valid tool call, VS Code invokes the existing MCP tool, Qwen receives the
   result, and the final answer uses it.
5. The served context limits are measured and copied into the VS Code model
   configuration without overstating them.

## Out of scope

- A localmodal CLI, MCP server, dispatcher, or general client library.
- DevDev integration.
- Journals, execution identity, sessions, conversations, turns, or replay.
- Evaluation harnesses, A/B allocation, counterfactuals, or quality judgment.
- LoRA adapters, runtime adapter swapping, fine-tuning, or multiple models.
- Host abstraction, local-hardware support, or engines other than vLLM.
- Server-side tools, browsing, filesystem access, or sandboxing.
- VS Code inline suggestions, embeddings, and GitHub-hosted utility features.
- Production availability, distributed operation, generalized telemetry, or
  autoscaling beyond the single trial container.
- Coven integration. That decision follows the usage experiment.

## Remaining gaps

| Gap | Binds | Evidence required |
|---|---|---|
| Canonical Qwen artifact and compatible 4-bit format | before implementation | Hub artifact resolves and loads in pinned vLLM on the target GPU. |
| Served input/output token limits | before VS Code configuration | Successful load plus measured memory headroom. |
| Qwen + vLLM streamed tool-call compatibility | acceptance test | The `docs_search` MCP witness completes end to end in VS Code agent mode. |

Official VS Code ground is pinned by
[`resources/fetch_vscode_docs.py`](../resources/fetch_vscode_docs.py): the
language-model page defines Custom Endpoint configuration and the MCP page
states that VS Code discovers and invokes MCP tools.
