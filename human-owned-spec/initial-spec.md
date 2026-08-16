# Localmodal Extension - Implementation Spec

## 1. Outcome

Make one current Qwen model selectable and usable in GitHub Copilot Chat from
VS Code. The extension is the control plane: it exposes configuration, model
selection, Modal lifecycle commands, the Copilot language-model provider, and an
MCP server for model delegation and lifecycle management (`delegate`, `up`, `down`).
The Modal deployment remains a small backend artifact under [deployment/](../deployment/).

This is a personal-use extension, not a general model marketplace, an engine
benchmark, an agent framework, or a replacement for GitHub inline suggestions.
There is one production model and one production backend in this first cut.

## 2. Current product target

- Model: **Qwen/Qwen3.8-27B**.
- Hub revision: `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0`.
- Architecture: `Qwen3_5ForConditionalGeneration`.
- Serving engine: vLLM `0.21.0` with Transformers `5.8.0`.
- Modal GPU: `RTX-PRO-6000`.
- Wire protocol: OpenAI-compatible Chat Completions at `/v1/chat/completions`.
- Deployment authentication: Modal Proxy Token in VS Code SecretStorage.
- Container bound: one container through `max_containers=1`.

The model natively supports 262,144 tokens. Context profiles are explicit:

- `32k`: measured cached profile.
- `128k`: measured cached profile and extension default.
- `262k`: measured cached profile at the native ceiling; experimental.

The extension advertises the selected profile, and the deployment receives the
same profile at deploy time. No profile is called measured merely because the
model card names it.

## 3. Polymorphic boundaries

The extension core depends on small interfaces rather than Modal or VS Code
objects:

1. `ModelCatalog` selects model definitions. Production uses
   `StaticModelCatalog` with Qwen3.8-27B; tests use a fixture catalog.
2. `LifecycleBackend` deploys, reports status, waits for readiness, and stops.
   Production uses `ModalLifecycleBackend`; contract tests use a fake backend.
3. `StateStore` and `SecretStore` persist endpoint state and credentials.
   Production uses VS Code workspace state and SecretStorage; tests use memory
   stores.

The test implementations are the second implementation of each seam. They are
not advertised as user-selectable products and do not create future production
paths. Adding another model, engine, or cloud backend requires a contract test
before it becomes a product option.

## 4. Configuration contract

- The only contributed VS Code settings are `localmodal.lifecycle` and
  `localmodal.contextProfile`.
- Deployment identity and infrastructure are product constants: Modal command,
  app name, bundled deployment file, GPU shape, model id, model revision, and
  serving engine are not user overrides.
- Model selection happens in the Copilot Chat picker. There is no second
  localmodal model selector while the production catalog contains one model.
- The Proxy Token is an authentication credential, not a setting. On the first request,
  SecretStorage is checked, then `MODAL_PROXY_TOKEN`, then a dashboard-linked
  wizard. The wizard accepts the separate `wk-...` ID and masked `ws-...`
  secret, a combined bearer token, or two whitespace-separated values; it
  validates prefixes and stores only the normalized combined value in
  SecretStorage.
- Credential lookup happens before deployment, so cancelling the wizard does
  not leave a newly deployed Modal app behind.
- On first extension activation, localmodal offers `Connect Qwen`. Choosing
  `Later` leaves Modal untouched; the same wizard is available from the
  `Localmodal: Connect Qwen` command.
- In `workspace` mode, successful onboarding deploys the app without warming
  inference. In `on-demand` mode, onboarding stores the Proxy Token and
  waits for the first request to deploy and warm it.

## 5. Extension behavior

- The provider registers the fixed Qwen model through
  `vscode.lm.registerLanguageModelChatProvider`; Copilot's picker is the sole
  model-selection UI.
- The extension dynamically provides a stdio MCP server with `delegate`, `up`,
  and `down` tools. No workspace MCP file is required.
- When the MCP server starts, the extension resolves configuration and Proxy
  Token without blocking on GPU readiness; the MCP server process starts
  immediately over stdio.
- `delegate` accepts a task and optional context to execute inference on the
  remote model; if the deployment is stopped or cold, it performs best-effort
  startup (`up`) and emits progress before fulfilling the request.
- `up` explicitly deploys and warms the endpoint, returning readiness timing.
- `down` explicitly stops the active Modal deployment to halt compute billing
  while preserving cache Volumes.
- Copilot requests are translated to Qwen Chat Completions requests.
- Text, streamed reasoning, images, tool definitions, tool results, and
  streamed tool calls are translated across the boundary.
- `localmodal.lifecycle` has two intentional policies:
  - `workspace`: deploy the app when the extension activates and stop it when
    the extension deactivates; the first request warms the GPU.
  - `on-demand`: deploy and warm only when Copilot sends the first request.
- The explicit Start and Stop commands remain available in both policies.
- The extension never writes a personal endpoint or token to tracked files.

Workspace shutdown cleanup is best-effort because VS Code extension
deactivation is not a guaranteed process-lifecycle hook. Modal's own
scale-to-zero behavior remains the compute safety net; Stop is the explicit
destructive app-control operation and preserves the cache Volumes.

## 6. Test obligations

The automated suite must keep these claims executable:

1. The package manifest exposes exactly the lifecycle and context settings.
2. Fixed deployment defaults cannot be supplied through workspace settings.
3. Stored, environment, dashboard-linked prompted, combined/separate-value, and
  cancelled token paths are deterministic.
4. A missing token is observed before backend status/deploy calls.
5. Deployment can occur without reading the token for workspace lifecycle setup.
6. The production catalog presents one Qwen model for each supported profile,
  with measurement state visible.
7. Workspace and on-demand policies make opposite activation/deactivation
  decisions.
8. The controller accepts fixture catalogs, fake backends, and memory stores
  through the same interfaces.
9. The bundled MCP server passes a real stdio client test, lists `delegate`,
  `up`, and `down` tools, and exercises streamed task delegation against a
  fixture endpoint.
10. The canonical VS Code Extension Host suite opens a separate VS Code
  instance, activates the real extension, observes the Copilot model
  registration, executes the Start command, resolves the dynamic MCP
  provider, and calls the packed MCP tools against a local HTTP fixture; it
  does not start Modal or consume GPU time.
11. The Modal child-process environment forces UTF-8 and a full local backend
  subprocess fixture captures Unicode CLI output and parses a deploy URL.
12. First activation offers Connect Qwen, cancellation leaves Modal untouched,
  and the command can rerun setup after a Later choice.
13. The opt-in Modal startup job records deploy-to-ready and ready-to-first-token
  timings and fails against explicit ceilings, with app cleanup in all paths.

## 7. Acceptance

The direction is complete when all of these are validated:

1. The extension starts with no workspace secret in source control.
2. Qwen appears in the Copilot Chat model picker with the selected profile.
3. A streamed text request completes through Modal.
4. A Copilot tool call round-trip completes through the same provider.
5. The extension-provided MCP server is discoverable without `.vscode/mcp.json`;
  `delegate` completes against the deployed endpoint and Qwen uses its returned
  result.
6. Start, status, and Stop work through the extension commands.
7. The 128K profile is measured and is the normal operating profile; the 262K
  profile remains clearly experimental.