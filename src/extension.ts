import * as vscode from "vscode";
import { ModalLifecycleBackend } from "./backend/modal";
import { ModelController } from "./controller";
import { StaticModelCatalog } from "./models/types";
import { QWEN38_27B } from "./models/qwen";
import { LocalmodalLanguageModelProvider } from "./provider";
import { LocalmodalMcpProvider } from "./mcp/provider";
import { DEPLOYMENT_DEFAULTS, USER_DEFAULTS } from "./product";
import { shouldDeployOnActivation, shouldStopOnDeactivation, type LifecyclePolicy } from "./lifecycle";
import { VscodeSecretStore, VscodeStateStore } from "./state/vscode";

const catalog = new StaticModelCatalog([QWEN38_27B]);
let stopWorkspaceApp: (() => Promise<void>) | undefined;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  if (!workspaceRoot) {
    vscode.window.showWarningMessage("Localmodal needs a workspace folder to manage Modal.");
    return;
  }

  const settings = readSettings();
  const backend = new ModalLifecycleBackend({
    deploymentRoot: context.extensionPath,
    ...DEPLOYMENT_DEFAULTS,
  });
  const state = new VscodeStateStore(context.workspaceState);
  const secrets = new VscodeSecretStore(context.secrets);
  const controller = new ModelController(catalog, backend, state, secrets);
  const provider = new LocalmodalLanguageModelProvider(
    catalog,
    controller,
    secrets,
    () => readSettings().contextProfile,
  );
  const mcpProvider = new LocalmodalMcpProvider(
    context.extensionPath,
    controller,
    secrets,
    () => readSettings().contextProfile,
  );

  context.subscriptions.push(
    vscode.lm.registerLanguageModelChatProvider("localmodal", provider),
    vscode.lm.registerMcpServerDefinitionProvider("localmodal.mcp", mcpProvider),
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration("localmodal")) {
        provider.refresh();
        updateStatusBar(statusBar, backend);
      }
    }),
  );

  const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBar.command = "localmodal.status";
  statusBar.tooltip = "Localmodal status";
  context.subscriptions.push(statusBar);

  context.subscriptions.push(
    vscode.commands.registerCommand("localmodal.start", async () => {
      await runCommand("Localmodal", async () => {
        const config = readSettings();
        await controller.ensureReady(QWEN38_27B.id, config.contextProfile);
        await updateStatusBar(statusBar, backend);
        vscode.window.showInformationMessage("Localmodal Qwen is ready for Copilot Chat.");
      });
    }),
    vscode.commands.registerCommand("localmodal.stop", async () => {
      await runCommand("Localmodal", async () => {
        await controller.stop();
        await updateStatusBar(statusBar, backend);
        vscode.window.showInformationMessage("Localmodal stopped. Model caches were preserved.");
      });
    }),
    vscode.commands.registerCommand("localmodal.status", async () => {
      const status = await backend.status();
      await updateStatusBar(statusBar, backend);
      vscode.window.showInformationMessage(`Localmodal: ${status.state}${status.detail ? ` (${status.detail})` : ""}`);
    }),
    vscode.commands.registerCommand("localmodal.selectContextProfile", async () => {
      const selected = await vscode.window.showQuickPick(
        ["32k", "128k", "262k"].map((id) => ({ label: id, description: id === "128k" ? "Long-context default" : "" })),
        { placeHolder: "Select the Copilot context profile" },
      );
      if (selected) {
        await vscode.workspace.getConfiguration("localmodal").update(
          "contextProfile",
          selected.label,
          vscode.ConfigurationTarget.Workspace,
        );
        provider.refresh();
      }
    }),
    vscode.commands.registerCommand("localmodal.configureToken", async () => {
      const configured = await secrets.configureProxyToken();
      if (configured) {
        vscode.window.showInformationMessage("Localmodal Proxy Token stored.");
      }
    }),
  );

  await updateStatusBar(statusBar, backend);
  if (shouldDeployOnActivation(settings.lifecycle)) {
    void runCommand("Localmodal", async () => {
      await controller.ensureDeployed(QWEN38_27B.id, settings.contextProfile);
      await updateStatusBar(statusBar, backend);
    });
  }

  stopWorkspaceApp = shouldStopOnDeactivation(readSettings().lifecycle)
    ? () => controller.stop()
    : undefined;
}

export async function deactivate(): Promise<void> {
  if (stopWorkspaceApp) {
    await stopWorkspaceApp();
    stopWorkspaceApp = undefined;
  }
}

function readSettings() {
  const config = vscode.workspace.getConfiguration("localmodal");
  return {
    lifecycle: config.get<LifecyclePolicy>("lifecycle", USER_DEFAULTS.lifecycle),
    contextProfile: config.get<string>("contextProfile", USER_DEFAULTS.contextProfile),
  };
}

async function updateStatusBar(statusBar: vscode.StatusBarItem, backend: ModalLifecycleBackend): Promise<void> {
  const status = await backend.status();
  statusBar.text = `$(server) Qwen: ${status.state}`;
  statusBar.show();
}

async function runCommand(label: string, action: () => Promise<void>): Promise<void> {
  try {
    await action();
  } catch (error) {
    vscode.window.showErrorMessage(`${label}: ${error instanceof Error ? error.message : String(error)}`);
  }
}