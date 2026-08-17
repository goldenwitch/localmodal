import type { Endpoint, LifecycleStatus } from "./backend/types";
import { ModelController } from "./controller";
import type { SecretStore } from "./state/types";

export interface ChatMessage {
  role: "user" | "assistant" | "tool";
  content?: unknown;
  tool_calls?: unknown[];
  tool_call_id?: string;
}

export interface ChatTool {
  type: "function";
  function: {
    name: string;
    description: string;
    parameters: object;
  };
}

export interface ChatRequest {
  messages: readonly ChatMessage[];
  tools?: readonly ChatTool[];
  toolChoice?: "auto" | "required";
  modelOptions?: Readonly<Record<string, unknown>>;
  maxTokens?: number;
}

export interface ChatDelta {
  text?: string;
  toolCalls: readonly {
    index: number;
    id: string;
    name: string;
    arguments: string;
  }[];
}

export class LocalmodalRuntime {
  private operationTail = Promise.resolve();

  public constructor(
    private readonly controller: ModelController,
    private readonly secrets: SecretStore,
    private readonly onDeploymentStateChange?: (active: boolean) => void,
    private readonly configureCredential?: () => Promise<boolean>,
  ) {}

  public get currentEndpoint(): Endpoint | undefined {
    return this.controller.currentEndpoint;
  }

  public resolveCredential(signal?: AbortSignal): Promise<boolean> {
    return this.runExclusive(
      async () => Boolean(await this.secrets.get("modalProxyToken")),
      signal,
    );
  }

  public configureProxyToken(signal?: AbortSignal): Promise<boolean> {
    if (!this.configureCredential) {
      throw new Error("Proxy Token configuration is unavailable in this runtime.");
    }
    return this.runExclusive(this.configureCredential, signal);
  }

  public async ensureDeployed(
    modelId: string,
    profileId: string,
    signal?: AbortSignal,
    onProgress?: (message: string) => void,
  ): Promise<Endpoint> {
    return this.runExclusive(async () => {
      const endpoint = await this.controller.ensureDeployed(modelId, profileId, signal, onProgress);
      this.onDeploymentStateChange?.(true);
      return endpoint;
    }, signal);
  }

  public async ensureReady(
    modelId: string,
    profileId: string,
    signal?: AbortSignal,
    onProgress?: (message: string) => void,
  ): Promise<Endpoint> {
    return this.runExclusive(async () => {
      const token = await this.requireToken();
      const endpoint = await this.controller.ensureReady(
        modelId,
        profileId,
        token,
        signal,
        onProgress,
      );
      this.onDeploymentStateChange?.(true);
      return endpoint;
    }, signal);
  }

  public async stop(signal?: AbortSignal): Promise<void> {
    await this.runExclusive(async () => {
      await this.controller.stop(signal);
      this.onDeploymentStateChange?.(false);
    }, signal);
  }

  public status(): Promise<LifecycleStatus> {
    return this.controller.status();
  }

  public async streamChat(
    modelId: string,
    profileId: string,
    request: ChatRequest,
    onDelta: (delta: ChatDelta) => void,
    signal?: AbortSignal,
    onProgress?: (message: string) => void,
  ): Promise<void> {
    await this.runExclusive(async () => {
      const proxyToken = await this.requireToken();
      const endpoint = await this.controller.ensureReady(
        modelId,
        profileId,
        proxyToken,
        signal,
        onProgress,
      );
      this.onDeploymentStateChange?.(true);
      onProgress?.("inference: waiting for model output");
      const heartbeat = onProgress
        ? setInterval(() => onProgress("inference: waiting for model output"), 15_000)
        : undefined;
      try {
        const response = await fetch(`${endpoint.baseUrl}/v1/chat/completions`, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${proxyToken}`,
            Accept: "text/event-stream",
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            ...(request.modelOptions ?? {}),
            model: endpoint.modelId,
            messages: request.messages,
            tools: request.tools,
            tool_choice: request.toolChoice,
            stream: true,
            stream_options: { include_usage: true },
            max_tokens: request.maxTokens,
            extra_body: {
              chat_template_kwargs: {
                enable_thinking: true,
                preserve_thinking: true,
              },
            },
          }),
          signal,
        });
        if (!response.ok) {
          throw new Error(`Qwen endpoint returned HTTP ${response.status}: ${await response.text()}`);
        }
        if (!response.body) {
          throw new Error("Qwen endpoint returned no streaming body.");
        }

        await consumeSse(response.body, onDelta);
      } finally {
        if (heartbeat) {
          clearInterval(heartbeat);
        }
      }
    }, signal);
  }

  private async requireToken(): Promise<string> {
    const token = await this.secrets.get("modalProxyToken");
    if (!token) {
      throw new Error("A Modal Proxy Token is required to use localmodal.");
    }
    return token;
  }

  private async runExclusive<T>(
    operation: () => Promise<T>,
    signal?: AbortSignal,
  ): Promise<T> {
    const predecessor = this.operationTail;
    let release: () => void = () => {};
    this.operationTail = new Promise<void>((resolve) => {
      release = resolve;
    });
    let acquired = false;
    try {
      await waitForTurn(predecessor, signal);
      acquired = true;
      signal?.throwIfAborted();
      return await operation();
    } finally {
      if (acquired) {
        release();
      } else {
        void predecessor.then(release, release);
      }
    }
  }
}

async function consumeSse(
  body: ReadableStream<Uint8Array>,
  onDelta: (delta: ChatDelta) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let completed = false;

  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const events = buffer.split(/\r?\n\r?\n/);
      buffer = events.pop() ?? "";
      for (const event of events) {
        processSseEvent(event, onDelta);
      }
      if (done) {
        if (buffer.trim()) {
          processSseEvent(buffer, onDelta);
          buffer = "";
        }
        completed = true;
        break;
      }
    }
  } finally {
    if (!completed) {
      await reader.cancel().catch(() => {});
    }
    reader.releaseLock();
  }
}

function processSseEvent(
  event: string,
  onDelta: (delta: ChatDelta) => void,
): void {
  const data = event
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""))
    .join("\n");
  if (!data || data === "[DONE]") {
    return;
  }
  const payload = JSON.parse(data) as {
    choices?: Array<{
      delta?: {
        content?: string;
        reasoning?: string;
        reasoning_content?: string;
        tool_calls?: Array<{
          index?: number;
          id?: string;
          function?: { name?: string; arguments?: string };
        }>;
      };
    }>;
  };
  const delta = payload.choices?.[0]?.delta;
  if (!delta) {
    return;
  }
  onDelta({
    text: delta.content ?? delta.reasoning ?? delta.reasoning_content,
    toolCalls: (delta.tool_calls ?? []).map((call) => ({
      index: call.index ?? 0,
      id: call.id ?? "",
      name: call.function?.name ?? "",
      arguments: call.function?.arguments ?? "",
    })),
  });
}

function waitForTurn(predecessor: Promise<void>, signal?: AbortSignal): Promise<void> {
  if (!signal) {
    return predecessor;
  }
  signal.throwIfAborted();
  return new Promise<void>((resolve, reject) => {
    const onAbort = (): void => reject(signal.reason);
    signal.addEventListener("abort", onAbort, { once: true });
    void predecessor.then(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    });
  });
}