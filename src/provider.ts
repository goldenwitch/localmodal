import * as vscode from "vscode";
import type { ModelCatalog, ModelDefinition } from "./models/types";
import { presentModels } from "./models/presentation";
import { type ChatMessage, LocalmodalRuntime } from "./runtime";

interface ProviderModel extends vscode.LanguageModelChatInformation {
  readonly definition: ModelDefinition;
  readonly profileId: string;
}

export class LocalmodalLanguageModelProvider
  implements vscode.LanguageModelChatProvider<ProviderModel>
{
  private readonly changeEmitter = new vscode.EventEmitter<void>();

  public constructor(
    private readonly catalog: ModelCatalog,
    private readonly runtime: LocalmodalRuntime,
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
      const toolCalls = new Map<number, { id: string; name: string; arguments: string }>();
      await this.runtime.streamChat(
        model.definition.id,
        model.profileId,
        {
          messages: toOpenAIMessages(messages),
          tools: options.tools?.map((tool) => ({
            type: "function",
            function: {
              name: tool.name,
              description: tool.description,
              parameters: tool.inputSchema ?? {},
            },
          })),
          toolChoice: options.tools?.length
            ? options.toolMode === vscode.LanguageModelChatToolMode.Required
              ? "required"
              : "auto"
            : undefined,
          modelOptions: options.modelOptions,
          maxTokens: model.maxOutputTokens,
        },
        (delta) => {
          if (delta.text) {
            progress.report(new vscode.LanguageModelTextPart(delta.text));
          }
          for (const call of delta.toolCalls) {
            const current = toolCalls.get(call.index) ?? { id: "", name: "", arguments: "" };
            current.id += call.id;
            current.name += call.name;
            current.arguments += call.arguments;
            toolCalls.set(call.index, current);
          }
        },
        abortController.signal,
      );
      for (const call of toolCalls.values()) {
        const input = JSON.parse(call.arguments || "{}");
        progress.report(new vscode.LanguageModelToolCallPart(call.id, call.name, input));
      }
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
): ChatMessage[] {
  const output: ChatMessage[] = [];
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