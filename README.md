# localmodal

Use one Modal-hosted Qwen model in GitHub Copilot Chat from VS Code.

The extension owns configuration, encrypted credential storage, Modal
deployment lifecycle, model selection, the streamed Copilot Chat provider, and
a diagnostic MCP server for validating the inference path.
The current production target is
[Qwen/Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B) at Hub revision
`1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0`, served by vLLM on one Modal
`RTX-PRO-6000`.

## Prerequisites

- VS Code `1.133` or newer.
- GitHub Copilot Chat available in VS Code for the chat experience.
- The Modal CLI installed and available as `modal` to the VS Code extension
  host.
- A Modal account with access to the configured GPU and an authenticated CLI
  profile. Run `modal setup` once in a terminal.
- Node.js and npm only when developing or packaging this repository.

The extension does not store a Modal token in the workspace or repository.

## Store Local Test Credentials

On Windows, the repository includes a small local CLI for live testing. It
stores an encrypted credential bundle under
`%LOCALAPPDATA%\localmodal\credentials.dpapi` using Windows DPAPI for the
current Windows user. Credential values are entered without terminal echo,
and are injected only into the child process started by `run`.

Store the three values used by the live Modal paths:

```powershell
npm run credentials -- set MODAL_TOKEN_ID
npm run credentials -- set MODAL_TOKEN_SECRET
npm run credentials -- set MODAL_PROXY_TOKEN
```

The CLI never prints stored values. Inspect names or remove a value with:

```powershell
npm run credentials -- list
npm run credentials -- remove MODAL_PROXY_TOKEN
```

Run a live test with the decrypted values available only to that process tree:

```powershell
npm run credentials -- run -- npm run test:integration:live
```

The same wrapper can run the startup measurement:

```powershell
npm run credentials -- run -- node scripts/modal-startup.mjs
```

The store is tied to the current Windows user and machine protection context;
it is not a portable backup and is not used by the packaged extension. The
CLI is intentionally Windows-only because it relies on Windows DPAPI.

## Install The Extension

There are two supported installation paths.

### Packaged install

From a fresh clone, build the local VSIX:

```powershell
npm install
npm run package
```

This creates `localmodal-0.0.1.vsix` at the repository root. The file is a
generated, ignored artifact, so it is not present in a fresh clone until this
command runs.

Install it into the current VS Code profile from the Extensions view with
`Extensions: Install from VSIX...`, or from PowerShell:

```powershell
code --install-extension .\localmodal-0.0.1.vsix --force
```

Reload VS Code after installation. The VSIX contains the extension bundle and
the bundled `deployment/qwen.py`; do not copy deployment files into individual
workspaces.

### Development install

From this repository:

```powershell
npm install
npm test
```

Run the canonical VS Code Extension Host integration test:

```powershell
npm run test:integration
```

This downloads/launches a separate VS Code instance, opens this workspace,
activates the real extension, verifies the Copilot model registration, invokes
the real Start command, resolves the dynamic MCP provider, and calls the
packed MCP tools against a local HTTP fixture. It does not start Modal or
consume GPU time.

The same suite is available from the `Run localmodal Extension Tests` launch
configuration. For the real cloud witness, run:

```powershell
$env:MODAL_PROXY_TOKEN = "wk-<id>.ws-<secret>"
$env:LOCALMODAL_TEST_APP_NAME = "localmodal-qwen-live"
npm run test:integration:live
```

The live label deploys the real Modal app, resolves the real endpoint through
the extension, and invokes the packed MCP tools against it. It is opt-in and
incurs GPU cost; the scheduled/manual CI workflow runs the same label with
repository secrets and stops the unique test app afterward.

Run the full unit plus Extension Host suite with:

```powershell
npm run test:all
```

Package validation remains:

```powershell
npm run package
```

Open the repository in VS Code and press `F5`. The tracked
`.vscode/launch.json` starts an Extension Development Host and builds the
extension first.

## Measure Modal Startup

The live Modal measurement is intentionally opt-in because it allocates the
GPU. It records the complete cold path:

```text
modal deploy -> first successful /v1/models -> first streamed token
```

Run it locally only when the required Modal account and Proxy Token environment
variables are present:

```powershell
node scripts/modal-startup.mjs
```

It writes structured metrics to `artifacts/modal-startup.json`, prints live
Modal output, stops the CI app in a `finally` cleanup path, and fails if the
configured deploy-to-ready or ready-to-first-token ceilings are exceeded. The
scheduled/manual GitHub Actions workflow runs this same script with the
credentials supplied as repository secrets.

## First Run

1. Open any VS Code workspace folder.
2. On first activation, choose `Connect Qwen` in the localmodal notification.
3. The extension checks the Modal CLI, opens the Proxy Token settings page, and
  prompts for the `wk-...` ID and masked `ws-...` secret.
4. In `workspace` mode, the extension deploys the app after setup. In
  `on-demand` mode, deployment waits until the first model request.
5. Open GitHub Copilot Chat and choose `Qwen3.8-27B (Modal)` in the model
  picker, then send a normal request.

The model picker is the model-selection UI. There is currently one production
model in the catalog, so localmodal does not add a second model-selection step.
The context profile is an advanced setting; leave the default in place unless
you are measuring or diagnosing a specific context limit.

Choosing `Later` leaves Modal untouched. Rerun the setup with `Localmodal:
Connect Qwen` from the Command Palette.

During setup and deployment, the status bar shows a spinning Qwen state, a
progress notification describes the current phase, and Modal's live output is
shown in the `localmodal` Output channel. Use `Localmodal: Show Output` to
reopen it. A first image build can take several minutes; the output channel is
the authoritative indication that the subprocess is still producing progress.

## Validate The Inference Path

The extension dynamically provides an MCP server named `localmodal inference
validation`. No `.vscode/mcp.json` file or separate MCP installation is
needed. When Copilot starts the server, localmodal supplies the current Modal
endpoint and the SecretStorage token to the short-lived stdio process.

The server exposes two diagnostic tools:

- `inference_status`: checks `/v1/models` and reports the endpoint response.
- `inference_probe`: sends one bounded streamed Chat Completions request and
  returns the streamed text.

With `Qwen3.8-27B (Modal)` selected, ask Copilot Chat:

```text
Use the localmodal inference probe with the prompt "Reply with exactly one sentence proving the inference path is live." Then report the returned model and response.
```

That exercises the complete loop: Qwen emits a tool call, VS Code invokes the
extension-provided MCP server, the MCP server calls the Modal endpoint, and the
tool result returns to Qwen for the final response. The MCP tools are
diagnostic witnesses, not part of the model's production tool catalog.

## Lifecycle And Cost

The `localmodal.lifecycle` setting has two modes.

| Mode | Behavior |
| --- | --- |
| `workspace` | Deploy the Modal app when the extension activates; the first Copilot request warms the GPU; extension deactivation attempts to stop the app. |
| `on-demand` | Wait to deploy and warm the app until the first model request; stop explicitly with the Stop command. |

Deploying the app is not the same as running the GPU. Modal scales the web
function to zero when idle. `Localmodal: Stop Model` stops the deployed app and
preserves the Hugging Face and vLLM cache Volumes.

The context profiles are:

| Profile | Meaning |
| --- | --- |
| `32k` | Measured first-load fallback. Use this for the first diagnostic request if the long-context profile has not been witnessed on your account. |
| `128k` | Extension default and intended repository-work profile; still marked unmeasured until a real Modal load/request witness exists. |
| `262k` | Qwen's native context ceiling; experimental. |

The selected profile is advertised to Copilot and passed to the Modal
deployment. It is not silently upgraded.

## Commands And Settings

Commands are available from the Command Palette:

- `Localmodal: Configure Modal Proxy Token`
- `Localmodal: Connect Qwen`
- `Localmodal: Start Model`
- `Localmodal: Stop Model`
- `Localmodal: Show Status`
- `Localmodal: Show Output`
- `Localmodal: Select Context Profile`

The main settings are:

- `localmodal.lifecycle`: `workspace` or `on-demand`.
- `localmodal.contextProfile`: `32k`, `128k`, or `262k`.

The status bar shows the observed lifecycle state. `Localmodal: Show Status`
provides the backend detail when the state is `error` or `unknown`.

Use `Localmodal: Configure Modal Proxy Token` to rotate or re-enter the
credential. The wizard accepts separate ID/secret fields, a combined
`wk-...ws-...` value, or two whitespace-separated values. It stores the
normalized bearer value in VS Code SecretStorage, and also accepts
`MODAL_PROXY_TOKEN` from the extension host environment when present.

## Scope

This provides the Copilot Chat experience. It does not replace GitHub-backed
inline suggestions, semantic search, or other Copilot services outside chat.
The extension core has explicit `ModelCatalog`, `LifecycleBackend`,
`StateStore`, and `SecretStore` interfaces. Production uses Qwen, Modal, and
VS Code; contract tests use fixture, fake, and memory implementations to prove
those boundaries without adding unused production paths.

The design authority is [human-owned-spec/initial-spec.md](human-owned-spec/initial-spec.md).
The execution graph is [localmodal.vine](localmodal.vine).
