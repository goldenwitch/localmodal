import { spawn } from "node:child_process";
import { modalProcessEnvironment, resolveModalCommand } from "./command";
import type {
  DeploymentSpec,
  Endpoint,
  LifecycleBackend,
  LifecycleStatus,
} from "./types";

export interface ModalBackendOptions {
  deploymentRoot: string;
  modalCommand: string;
  appName: string;
  deploymentFile: string;
  gpu: string;
  commandPrefix?: string[];
  startupTimeoutMs?: number;
  onEvent?: (message: string) => void;
}

interface CommandResult {
  code: number;
  stdout: string;
  stderr: string;
}

interface ModalAppRecord {
  Description?: string;
  State?: string;
}

const DEFAULT_STARTUP_TIMEOUT_MS = 20 * 60 * 1000;
const RETRYABLE_STATUS_CODES = new Set([502, 503, 504]);

export class ModalLifecycleBackend implements LifecycleBackend {
  private lastEndpoint: Endpoint | undefined;
  private readonly modalCommand: string;

  public constructor(private readonly options: ModalBackendOptions) {
    this.modalCommand = resolveModalCommand(options.modalCommand);
  }

  public async deploy(
    spec: DeploymentSpec,
    signal?: AbortSignal,
    onProgress?: (message: string) => void,
  ): Promise<Endpoint> {
    this.emit(`deploy: ${spec.model.id} profile=${spec.profile.id}`, onProgress);
    const result = await this.runModal(
      ["deploy", "-m", this.moduleName(this.options.deploymentFile)],
      {
        LOCALMODAL_MODEL_ID: spec.model.id,
        LOCALMODAL_MODEL_REVISION: spec.model.revision,
        LOCALMODAL_MAX_MODEL_LEN: String(spec.profile.maxModelLen),
        LOCALMODAL_GPU: this.options.gpu,
        LOCALMODAL_APP_NAME: this.options.appName,
      },
      signal,
      onProgress,
    );
    if (result.code !== 0) {
      throw new Error(this.commandError("Modal deploy failed", result));
    }

    const url = [...result.stdout.matchAll(/https:\/\/[^\s"']+\.modal\.run/g)]
      .map((match) => match[0].replace(/[),.]+$/, ""))
      .find((candidate) => !candidate.includes("modal.com/"));
    if (!url) {
      throw new Error("Modal deploy completed without an endpoint URL.");
    }

    this.lastEndpoint = {
      baseUrl: url,
      modelId: spec.model.id,
      profile: spec.profile,
    };
    return this.lastEndpoint;
  }

  public async status(): Promise<LifecycleStatus> {
    this.emit("status: checking Modal app");
    let result: CommandResult;
    try {
      result = await this.runModal(["app", "list", "--json"]);
    } catch (error) {
      return {
        state: "error",
        detail: error instanceof Error ? error.message : String(error),
      };
    }
    if (result.code !== 0) {
      return { state: "error", detail: this.commandError("Modal status failed", result) };
    }

    let apps: ModalAppRecord[];
    try {
      apps = JSON.parse(result.stdout) as ModalAppRecord[];
    } catch (error) {
      return {
        state: "error",
        detail: `Modal returned invalid app status JSON: ${String(error)}`,
      };
    }

    const matchingApps = apps.filter((candidate) => candidate.Description === this.options.appName);
    const app = matchingApps.find((candidate) => candidate.State?.toLowerCase() === "deployed")
      ?? matchingApps.find((candidate) => candidate.State?.toLowerCase().includes("initial"))
      ?? matchingApps[0];
    if (!app) {
      return { state: "stopped" };
    }

    const state = app.State?.toLowerCase() ?? "unknown";
    if (state === "deployed") {
      return { state: "deployed", endpoint: this.lastEndpoint };
    }
    if (state.includes("initial")) {
      return { state: "deploying", endpoint: this.lastEndpoint };
    }
    if (state.includes("stopped")) {
      return { state: "stopped" };
    }
    return { state: "unknown", detail: app.State, endpoint: this.lastEndpoint };
  }

  public async ensureReady(
    endpoint: Endpoint,
    token: string,
    signal?: AbortSignal,
    onProgress?: (message: string) => void,
  ): Promise<void> {
    this.emit(`readiness: waiting for ${endpoint.baseUrl}/v1/models`, onProgress);
    const deadline = Date.now() + (this.options.startupTimeoutMs ?? DEFAULT_STARTUP_TIMEOUT_MS);
    while (Date.now() < deadline) {
      if (signal?.aborted) {
        throw new Error("The localmodal startup request was cancelled.");
      }

      try {
        const response = await fetch(`${endpoint.baseUrl}/v1/models`, {
          headers: { Authorization: `Bearer ${token}` },
          signal,
        });
        if (response.ok) {
          this.emit("readiness: endpoint is ready", onProgress);
          return;
        }
        if (!RETRYABLE_STATUS_CODES.has(response.status)) {
          throw new Error(`Modal endpoint readiness returned HTTP ${response.status}.`);
        }
        this.emit(`readiness: HTTP ${response.status}; retrying`, onProgress);
      } catch (error) {
        if (signal?.aborted) {
          throw new Error("The localmodal startup request was cancelled.");
        }
        if (error instanceof Error && error.message.startsWith("Modal endpoint readiness")) {
          throw error;
        }
        this.emit("readiness: endpoint unavailable; retrying", onProgress);
      }

      await sleep(5000, signal);
    }
    throw new Error("The Modal endpoint did not become ready before the startup deadline.");
  }

  public async stop(signal?: AbortSignal): Promise<void> {
    this.emit(`stop: ${this.options.appName}`);
    const result = await this.runModal(["app", "stop", this.options.appName], {}, signal);
    if (result.code !== 0 && !/not found|already stopped/i.test(result.stderr + result.stdout)) {
      throw new Error(this.commandError("Modal stop failed", result));
    }
    this.lastEndpoint = undefined;
  }

  private async runModal(
    args: string[],
    environment: Record<string, string> = {},
    signal?: AbortSignal,
    onProgress?: (message: string) => void,
  ): Promise<CommandResult> {
    if (signal?.aborted) {
      throw new Error("The Modal operation was cancelled.");
    }
    return new Promise((resolve, reject) => {
      const child = spawn(this.modalCommand, [...(this.options.commandPrefix ?? []), ...args], {
        cwd: this.options.deploymentRoot,
        env: modalProcessEnvironment({ ...process.env, ...environment }),
        signal,
        windowsHide: true,
      });
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (chunk: Buffer) => {
        const text = chunk.toString();
        stdout += text;
        this.emit(text.trimEnd(), onProgress);
      });
      child.stderr.on("data", (chunk: Buffer) => {
        const text = chunk.toString();
        stderr += text;
        this.emit(`[stderr] ${text.trimEnd()}`, onProgress);
      });
      child.on("error", (error: NodeJS.ErrnoException) => {
        if (error.name === "AbortError") {
          reject(new Error("The Modal operation was cancelled."));
          return;
        }
        if (error.code === "ENOENT") {
          reject(new Error(
            `Modal CLI was not found (tried '${this.modalCommand}'). ` +
            "Install Modal and run 'modal setup', or ensure modal is on the Extension Host PATH.",
          ));
          return;
        }
        reject(error);
      });
      child.on("close", (code) => resolve({ code: code ?? 1, stdout, stderr }));
    });
  }

  private moduleName(file: string): string {
    return file
      .replace(/\\/g, "/")
      .replace(/^\.\//, "")
      .replace(/\.py$/, "")
      .replace(/\//g, ".");
  }

  private commandError(prefix: string, result: CommandResult): string {
    const detail = (result.stderr || result.stdout).trim();
    return detail ? `${prefix}: ${detail}` : `${prefix} (exit code ${result.code}).`;
  }

  private emit(message: string, onProgress?: (message: string) => void): void {
    if (message) {
      this.options.onEvent?.(message);
      onProgress?.(message);
    }
  }
}

function sleep(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, milliseconds);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(new Error("The localmodal startup request was cancelled."));
      },
      { once: true },
    );
  });
}