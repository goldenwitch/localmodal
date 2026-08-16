import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { FixtureLifecycleBackend } from "../backend/fixture";
import { ModalLifecycleBackend } from "../backend/modal";
import type { Endpoint, LifecycleBackend } from "../backend/types";
import { ModelController } from "../controller";
import { QWEN38_27B } from "../models/qwen";
import {
  type ContextProfileId,
  type ModelDefinition,
  StaticModelCatalog,
} from "../models/types";
import { DEPLOYMENT_DEFAULTS } from "../product";
import type { SecretStore, StateStore } from "../state/types";

class SimpleStateStore implements StateStore {
  private readonly store = new Map<string, unknown>();

  public constructor(initialEndpoint?: Endpoint) {
    if (initialEndpoint) {
      this.store.set("localmodal.endpoint", initialEndpoint);
    }
  }

  public get<T>(key: string, defaultValue: T): T {
    return (this.store.get(key) as T | undefined) ?? defaultValue;
  }

  public async update<T>(key: string, value: T): Promise<void> {
    if (value === undefined) {
      this.store.delete(key);
    } else {
      this.store.set(key, value);
    }
  }
}

class SimpleSecretStore implements SecretStore {
  private token: string | undefined;

  public constructor(initialToken?: string) {
    this.token = initialToken;
  }

  public async get(key: string): Promise<string | undefined> {
    if (key === "modalProxyToken") {
      return this.token;
    }
    return undefined;
  }

  public async store(key: string, value: string): Promise<void> {
    if (key === "modalProxyToken") {
      this.token = value;
    }
  }

  public async delete(key: string): Promise<void> {
    if (key === "modalProxyToken") {
      this.token = undefined;
    }
  }
}

const modelId = process.env.LOCALMODAL_MODEL_ID ?? QWEN38_27B.id;
const defaultProfile: ContextProfileId = (process.env.LOCALMODAL_CONTEXT_PROFILE as ContextProfileId) ?? "128k";
const initialToken = process.env.MODAL_PROXY_TOKEN;
const initialEndpointUrl = process.env.LOCALMODAL_ENDPOINT?.replace(/\/$/, "");

const customModel: ModelDefinition = modelId === QWEN38_27B.id
  ? QWEN38_27B
  : {
    ...QWEN38_27B,
    id: modelId,
    name: modelId,
  };

const catalog = new StaticModelCatalog([QWEN38_27B, customModel]);
const state = new SimpleStateStore(
  initialEndpointUrl
    ? {
      baseUrl: initialEndpointUrl,
      modelId,
      profile: customModel.profiles[defaultProfile] ?? customModel.profiles["128k"],
    }
    : undefined,
);
const secrets = new SimpleSecretStore(initialToken);

const emitProgress = (message: string): void => {
  try {
    process.stderr.write(`[localmodal] ${message}\n`);
    void server.sendLoggingMessage({
      level: "info",
      data: message,
    }).catch(() => {});
  } catch {}
};

const isTestMode = process.env.LOCALMODAL_TEST_MODE === "1"
  || Boolean(process.env.LOCALMODAL_TEST_ENDPOINT)
  || Boolean(process.env.LOCALMODAL_ENDPOINT && !process.env.LOCALMODAL_DEPLOYMENT_FILE);

const backend: LifecycleBackend = isTestMode
  ? new FixtureLifecycleBackend({
    endpoint: process.env.LOCALMODAL_TEST_ENDPOINT ?? initialEndpointUrl ?? "http://127.0.0.1:43123",
    onEvent: emitProgress,
  })
  : new ModalLifecycleBackend({
    deploymentRoot: process.env.LOCALMODAL_DEPLOYMENT_ROOT ?? process.cwd(),
    appName: process.env.LOCALMODAL_APP_NAME ?? DEPLOYMENT_DEFAULTS.appName,
    deploymentFile: process.env.LOCALMODAL_DEPLOYMENT_FILE ?? DEPLOYMENT_DEFAULTS.deploymentFile,
    gpu: process.env.LOCALMODAL_GPU ?? DEPLOYMENT_DEFAULTS.gpu,
    modalCommand: process.env.LOCALMODAL_MODAL_COMMAND ?? DEPLOYMENT_DEFAULTS.modalCommand,
    onEvent: emitProgress,
  });

const controller = new ModelController(catalog, backend, state, secrets);

const server = new McpServer(
  {
    name: "localmodal",
    version: "0.0.1",
  },
  {
    capabilities: {
      logging: {},
    },
  },
);

server.registerTool(
  "delegate",
  {
    title: "Delegate task to model",
    description: "Pass a task and optional context to the model for execution, deploying and warming on demand if stopped.",
    inputSchema: {
      task: z.string().min(1).describe("The task, prompt, or directive for the model to execute."),
      context: z.string().optional().describe("Optional context, code snippets, or documentation relevant to the task."),
      profile: z.enum(["32k", "128k", "262k"]).optional().describe("Context profile to use (default: 128k)."),
    },
  },
  async ({ task, context, profile }) => {
    try {
      const selectedProfile = profile ?? defaultProfile;
      emitProgress(`Ensuring model readiness for delegation (profile=${selectedProfile})...`);
      const endpoint = await controller.ensureReady(modelId, selectedProfile);
      const token = await secrets.get("modalProxyToken");
      if (!token) {
        throw new Error("A Modal Proxy Token is required to execute delegation.");
      }

      emitProgress(`Executing inference request against ${endpoint.baseUrl}...`);
      const messages = context
        ? [{ role: "user", content: `Context:\n${context}\n\nTask:\n${task}` }]
        : [{ role: "user", content: task }];

      const response = await fetch(`${endpoint.baseUrl}/v1/chat/completions`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "text/event-stream",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          model: endpoint.modelId,
          messages,
          stream: true,
          stream_options: { include_usage: true },
          extra_body: {
            chat_template_kwargs: {
              enable_thinking: true,
              preserve_thinking: true,
            },
          },
        }),
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${await response.text()}`);
      }

      const text = await readStream(response);
      return {
        content: [{
          type: "text" as const,
          text,
        }],
      };
    } catch (error) {
      return failure(error);
    }
  },
);

server.registerTool(
  "up",
  {
    title: "Start and warm model",
    description: "Explicitly deploy and warm the Modal endpoint, returning readiness and timing.",
    inputSchema: {
      profile: z.enum(["32k", "128k", "262k"]).optional().describe("Context profile to deploy (default: 128k)."),
    },
  },
  async ({ profile }) => {
    try {
      const selectedProfile = profile ?? defaultProfile;
      const startTime = Date.now();
      emitProgress(`Starting deployment / readiness check (profile=${selectedProfile})...`);
      const endpoint = await controller.ensureReady(modelId, selectedProfile);
      const elapsedSeconds = ((Date.now() - startTime) / 1000).toFixed(1);
      return {
        content: [{
          type: "text" as const,
          text: `Endpoint ready in ${elapsedSeconds}s\nmodel: ${endpoint.modelId}\nprofile: ${endpoint.profile.id}\nurl: ${endpoint.baseUrl}`,
        }],
      };
    } catch (error) {
      return failure(error);
    }
  },
);

server.registerTool(
  "down",
  {
    title: "Stop model deployment",
    description: "Explicitly stop the active Modal deployment to halt compute billing while preserving cache volumes.",
  },
  async () => {
    try {
      emitProgress("Stopping model deployment...");
      await controller.stop();
      return {
        content: [{
          type: "text" as const,
          text: "Localmodal stopped. Model caches were preserved.",
        }],
      };
    } catch (error) {
      return failure(error);
    }
  },
);

async function main(): Promise<void> {
  await server.connect(new StdioServerTransport());
}

async function readStream(response: Response): Promise<string> {
  const body = await response.text();
  const output: string[] = [];
  for (const line of body.split(/\r?\n/)) {
    if (!line.startsWith("data: ")) {
      continue;
    }
    const data = line.slice(6).trim();
    if (!data || data === "[DONE]") {
      continue;
    }
    try {
      const payload = JSON.parse(data) as {
        choices?: Array<{ delta?: { content?: string; reasoning?: string; reasoning_content?: string } }>;
      };
      const delta = payload.choices?.[0]?.delta;
      const text = delta?.content ?? delta?.reasoning ?? delta?.reasoning_content;
      if (text) {
        output.push(text);
      }
    } catch {}
  }
  return output.join("");
}

function failure(error: unknown): { isError: true; content: Array<{ type: "text"; text: string }> } {
  return {
    isError: true,
    content: [{
      type: "text" as const,
      text: error instanceof Error ? error.message : String(error),
    }],
  };
}

void main().catch((error) => {
  process.stderr.write(`[localmodal] fatal error: ${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
  process.exitCode = 1;
});