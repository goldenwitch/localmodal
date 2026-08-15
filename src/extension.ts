import * as vscode from "vscode";
import { FixtureLifecycleBackend } from "./backend/fixture";
import { ModalLifecycleBackend } from "./backend/modal";
import type { LifecycleBackend, LifecycleStatus } from "./backend/types";
import { ModelController } from "./controller";
import { StaticModelCatalog } from "./models/types";
import { QWEN38_27B } from "./models/qwen";
import { LocalmodalLanguageModelProvider } from "./provider";
import { LocalmodalMcpProvider } from "./mcp/provider";
import {
  needsOnboarding,
  ONBOARDING_STATE_KEY,
  shouldDeployAfterOnboarding,
  shouldStopAfterOnboarding,
} from "./onboarding";
import { DEPLOYMENT_DEFAULTS, USER_DEFAULTS } from "./product";
import { shouldDeployOnActivation, type LifecyclePolicy } from "./lifecycle";
import { VscodeSecretStore, VscodeStateStore } from "./state/vscode";

const catalog = new StaticModelCatalog([QWEN38_27B]);
let stopWorkspaceApp: (() => Promise<void>) | undefined;

export interface LocalmodalExtensionApi {
  getStatus(): Promise<LifecycleStatus>;
  resolveInferenceMcpServer(): Promise<vscode.McpStdioServerDefinition>;
}

export async function activate(context: vscode.ExtensionContext): Promise<LocalmodalExtensionApi> {
  const settings = readSettings();
  const output = vscode.window.createOutputChannel("localmodal");
  context.subscriptions.push(output);
  const report = (message: string): void => {
    output.appendLine(`[${new Date().toISOString()}] ${message}`);
  };
  const liveIntegration = process.env.LOCALMODAL_LIVE_TEST === "1";
  const backend: LifecycleBackend = process.env.LOCALMODAL_TEST_MODE === "1"
    ? new FixtureLifecycleBackend({
      endpoint: process.env.LOCALMODAL_TEST_ENDPOINT ?? "http://127.0.0.1:43123",
      onEvent: report,
    })
    : new ModalLifecycleBackend({
      deploymentRoot: context.extensionPath,
      ...DEPLOYMENT_DEFAULTS,
      ...(liveIntegration && process.env.LOCALMODAL_TEST_APP_NAME
        ? { appName: process.env.LOCALMODAL_TEST_APP_NAME }
        : {}),
      onEvent: report,
    });
  const state = new VscodeStateStore(context.workspaceState);
  const secrets = new VscodeSecretStore(context.secrets);
  let controller: ModelController;
  const markOnboarded = async (): Promise<void> => {
    await context.globalState.update(ONBOARDING_STATE_KEY, true);
    if (shouldStopAfterOnboarding(readSettings().lifecycle, true)) {
      stopWorkspaceApp = () => controller.stop();
    }
  };
  controller = new ModelController(
    catalog,
    backend,
    state,
    secrets,
    markOnboarded,
  );
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
  statusBar.text = "$(sync~spin) Qwen: starting";
  statusBar.show();
  report(`activated; lifecycle=${settings.lifecycle} profile=${settings.contextProfile}`);

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
    vscode.commands.registerCommand("localmodal.showOutput", () => output.show(true)),
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
    vscode.commands.registerCommand("localmodal.setup", async () => {
      await runFirstRun({
        context,
        backend,
        controller,
        secrets,
        provider,
        statusBar,
        output,
        report,
        prompt: false,
      });
    }),
  );

  const integrationTest = process.env.LOCALMODAL_TEST_MODE === "1";
  const onboardingComplete = integrationTest || liveIntegration || context.globalState.get<boolean>(ONBOARDING_STATE_KEY, false);
  if (!integrationTest && !liveIntegration && needsOnboarding(onboardingComplete)) {
    statusBar.text = "$(plug) Qwen: setup required";
    statusBar.show();
    void runCommand("Localmodal", async () => {
      await runFirstRun({
        context,
        backend,
        controller,
        secrets,
        provider,
        statusBar,
        output,
        report,
        prompt: true,
      });
    });
  } else if (!integrationTest && !liveIntegration && shouldDeployOnActivation(settings.lifecycle)) {
    void runCommand("Localmodal", async () => {
      await runWorkspaceDeployment(controller, backend, statusBar, settings.contextProfile, report);
    });
    stopWorkspaceApp = shouldStopAfterOnboarding(settings.lifecycle, true)
      ? () => controller.stop()
      : undefined;
  }

  return {
    getStatus: () => backend.status(),
    resolveInferenceMcpServer: async () => {
      const [server] = mcpProvider.provideMcpServerDefinitions();
      return mcpProvider.resolveMcpServerDefinition(
        server,
        new vscode.CancellationTokenSource().token,
      );
    },
  };
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

async function updateStatusBar(statusBar: vscode.StatusBarItem, backend: LifecycleBackend): Promise<void> {
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

interface FirstRunOptions {
  context: vscode.ExtensionContext;
  backend: LifecycleBackend;
  controller: ModelController;
  secrets: VscodeSecretStore;
  provider: LocalmodalLanguageModelProvider;
  statusBar: vscode.StatusBarItem;
  output: vscode.OutputChannel;
  report: (message: string) => void;
  prompt: boolean;
}

async function runFirstRun(options: FirstRunOptions): Promise<void> {
  await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: "Localmodal: connecting Qwen",
      cancellable: false,
    },
    async (progress) => {
      if (options.prompt) {
        const choice = await vscode.window.showInformationMessage(
          "Localmodal is ready to connect Qwen to Copilot Chat.",
          "Connect Qwen",
          "Later",
        );
        if (choice !== "Connect Qwen") {
          return;
        }
      }

      options.output.show(true);

      progress.report({ message: "checking Modal CLI" });
      options.report("onboarding: checking Modal CLI");
      const cliStatus = await options.backend.status();
      if (cliStatus.state === "error") {
        throw new Error(cliStatus.detail ?? "Modal CLI setup is required before connecting Qwen.");
      }

      progress.report({ message: "waiting for Proxy Token" });
      options.report("onboarding: resolving Proxy Token");
      const token = await options.secrets.get("modalProxyToken");
      if (!token) {
        options.report("onboarding: cancelled before deployment");
        return;
      }

      const settings = readSettings();
      if (shouldDeployAfterOnboarding(settings.lifecycle)) {
        progress.report({ message: "deploying Modal app; first build may take several minutes" });
        options.report("onboarding: deploying Modal app");
        await options.controller.ensureDeployed(QWEN38_27B.id, settings.contextProfile);
      } else {
        progress.report({ message: "connected; waiting for first Copilot request" });
      }
      await options.context.globalState.update(ONBOARDING_STATE_KEY, true);
      if (shouldStopAfterOnboarding(settings.lifecycle, true)) {
        stopWorkspaceApp = () => options.controller.stop();
      }
      options.provider.refresh();
      await updateStatusBar(options.statusBar, options.backend);
      vscode.window.showInformationMessage(
        "Localmodal is connected. Select Qwen3.8-27B in Copilot Chat and send a request.",
      );
    },
  );
}

async function runWorkspaceDeployment(
  controller: ModelController,
  backend: LifecycleBackend,
  statusBar: vscode.StatusBarItem,
  profile: string,
  report: (message: string) => void,
): Promise<void> {
  await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: "Localmodal: deploying Qwen",
      cancellable: false,
    },
    async (progress) => {
      progress.report({ message: "Modal is starting; first build may take several minutes" });
      report("workspace lifecycle: deploying Modal app");
      statusBar.text = "$(sync~spin) Qwen: deploying";
      await controller.ensureDeployed(QWEN38_27B.id, profile);
      progress.report({ message: "deployment registered" });
      report("workspace lifecycle: deployment registered");
      await updateStatusBar(statusBar, backend);
    },
  );
}