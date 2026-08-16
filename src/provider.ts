import * as vscode from "vscode";
import type { Endpoint } from "./backend/types";
import { ModelController } from "./controller";
import type { ModelCatalog, ModelDefinition } from "./models/types";
import { presentModels } from "./models/presentation";
import type { SecretStore } from "./state/types";

interface ProviderModel extends vscode.LanguageModelChatInformation {
  readonly definition: ModelDefinition;
  readonly profileId: string;
}

interface OpenAIMessage {
  role: "user" | "assistant" | "tool";
  content?: unknown;
  tool_calls?: unknown[];
  tool_call_id?: string;
}

export class LocalmodalLanguageModelProvider
  implements vscode.LanguageModelChatProvider<ProviderModel>
{
  private readonly changeEmitter = new vscode.EventEmitter<void>();

  public constructor(
    private readonly catalog: ModelCatalog,
    private readonly controller: ModelController,
    private readonly secrets: SecretStore,
    private readonly profileId: () => string,
  ) {}

  public get onDidChangeLanguageModelChatInformation(): vscode.Event<void> {
    return this.changeEmitter.event;
  }

  public refresh(): void {
    this.changeEmitter.fire();
  }

  public provideLanguageModelChatInformation(
    _options: vscode.PrepareLanguageModelChatModelOptions,
    _token: vscode.CancellationToken,
  ): ProviderModel[] {
    return presentModels(this.catalog, this.profileId()).map((presented) => ({
      id: presented.definition.id,
      name: presented.definition.name,
      family: presented.definition.family,
      version: presented.version,
      detail: presented.detail,
      tooltip: `${presented.definition.id} at Hub revision ${presented.definition.revision}`,
      maxInputTokens: presented.maxInputTokens,
      maxOutputTokens: presented.maxOutputTokens,
      capabilities: {
        imageInput: presented.definition.capabilities.imageInput,
        toolCalling: presented.definition.capabilities.toolCalling,
      },
      definition: presented.definition,
      profileId: presented.profileId,
    }));
  }

  public async provideLanguageModelChatResponse(
    model: ProviderModel,
    messages: readonly vscode.LanguageModelChatRequestMessage[],
    options: vscode.ProvideLanguageModelChatResponseOptions,
    progress: vscode.Progress<vscode.LanguageModelResponsePart>,
    token: vscode.CancellationToken,
  ): Promise<void> {
    const abortController = new AbortController();
    const cancellation = token.onCancellationRequested(() => abortController.abort());
    try {
      const endpoint = await this.controller.ensureReady(
        model.definition.id,
        model.profileId,
        abortController.signal,
      );
      const proxyToken = await this.secrets.get("modalProxyToken");
      if (!proxyToken) {
        throw new Error("Configure the Modal Proxy Token before using localmodal.");
      }

      const response = await fetch(`${endpoint.baseUrl}/v1/chat/completions`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${proxyToken}`,
          Accept: "text/event-stream",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          model: model.definition.id,
          messages: toOpenAIMessages(messages),
          tools: options.tools?.map((tool) => ({
            type: "function",
            function: {
              name: tool.name,
              description: tool.description,
              parameters: tool.inputSchema,
            },
          })),
          tool_choice: options.tools?.length
            ? options.toolMode === vscode.LanguageModelChatToolMode.Required
              ? "required"
              : "auto"
            : undefined,
          stream: true,
          stream_options: { include_usage: true },
          ...(options.modelOptions ?? {}),
          max_tokens: model.maxOutputTokens,
          extra_body: {
            chat_template_kwargs: {
              enable_thinking: true,
              preserve_thinking: true,
            },
          },
        }),
        signal: abortController.signal,
      });
      if (!response.ok) {
        throw new Error(`Qwen endpoint returned HTTP ${response.status}: ${await response.text()}`);
      }
      if (!response.body) {
        throw new Error("Qwen endpoint returned no streaming body.");
      }

      await consumeSse(response.body, progress);
    } finally {
      cancellation.dispose();
    }
  }

  public provideTokenCount(
    _model: ProviderModel,
    text: string | vscode.LanguageModelChatRequestMessage,
    _token: vscode.CancellationToken,
  ): Promise<number> {
    const value = typeof text === "string" ? text : textToString(text.content);
    return Promise.resolve(Math.ceil(value.length / 4));
  }
}

function toOpenAIMessages(
  messages: readonly vscode.LanguageModelChatRequestMessage[],
): OpenAIMessage[] {
  const output: OpenAIMessage[] = [];
  for (const message of messages) {
    const text = textToString(message.content);
    const images = message.content
      .filter((part): part is vscode.LanguageModelDataPart => part instanceof vscode.LanguageModelDataPart)
      .filter((part) => part.mimeType.startsWith("image/"))
      .map((part) => ({
        type: "image_url",
        image_url: {
          url: `data:${part.mimeType};base64,${Buffer.from(part.data).toString("base64")}`,
        },
      }));
    const toolCalls = message.content.filter(
      (part): part is vscode.LanguageModelToolCallPart => part instanceof vscode.LanguageModelToolCallPart,
    );
    const toolResults = message.content.filter(
      (part): part is vscode.LanguageModelToolResultPart => part instanceof vscode.LanguageModelToolResultPart,
    );

    if (message.role === vscode.LanguageModelChatMessageRole.Assistant) {
      output.push({
        role: "assistant",
        content: text || undefined,
        tool_calls: toolCalls.map((part) => ({
          id: part.callId,
          type: "function",
          function: { name: part.name, arguments: JSON.stringify(part.input) },
        })),
      });
      continue;
    }

    if (text || images.length > 0) {
      output.push({ role: "user", content: images.length > 0 ? [{ type: "text", text }, ...images] : text });
    }
    for (const result of toolResults) {
      output.push({ role: "tool", tool_call_id: result.callId, content: textToString(result.content) });
    }
  }
  return output;
}

function textToString(parts: readonly unknown[]): string {
  return parts
    .map((part) => {
      if (typeof part === "string") {
        return part;
      }
      if (part instanceof vscode.LanguageModelTextPart) {
        return part.value;
      }
      return "";
    })
    .join("");
}

async function consumeSse(
  body: ReadableStream<Uint8Array>,
  progress: vscode.Progress<vscode.LanguageModelResponsePart>,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const toolCalls = new Map<number, { id: string; name: string; arguments: string }>();

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const events = buffer.split(/\r?\n\r?\n/);
    buffer = events.pop() ?? "";
    for (const event of events) {
      const data = event
        .split(/\r?\n/)
        .filter((line) => line.startsWith("data: "))
        .map((line) => line.slice(6))
        .join("\n");
      if (!data || data === "[DONE]") {
        continue;
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
        continue;
      }
      const text = delta.content ?? delta.reasoning ?? delta.reasoning_content;
      if (text) {
        progress.report(new vscode.LanguageModelTextPart(text));
      }
      for (const call of delta.tool_calls ?? []) {
        const index = call.index ?? 0;
        const current = toolCalls.get(index) ?? { id: "", name: "", arguments: "" };
        current.id += call.id ?? "";
        current.name += call.function?.name ?? "";
        current.arguments += call.function?.arguments ?? "";
        toolCalls.set(index, current);
      }
    }
    if (done) {
      break;
    }
  }

  for (const call of toolCalls.values()) {
    const input = JSON.parse(call.arguments || "{}");
    progress.report(new vscode.LanguageModelToolCallPart(call.id, call.name, input));
  }
}