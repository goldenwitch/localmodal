import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const endpoint = process.env.LOCALMODAL_ENDPOINT?.replace(/\/$/, "");
const token = process.env.MODAL_PROXY_TOKEN;
const modelId = process.env.LOCALMODAL_MODEL_ID ?? "Qwen/Qwen3.8-27B";

const server = new McpServer({
  name: "localmodal-inference-validation",
  version: "0.0.1",
});

server.registerTool(
  "inference_status",
  {
    title: "Check localmodal inference status",
    description: "Check that the configured Modal Qwen endpoint is reachable and exposes the expected model.",
  },
  async () => {
    try {
      const response = await request("/v1/models", { method: "GET" });
      const body = await response.text();
      return {
        isError: !response.ok,
        content: [{
          type: "text" as const,
          text: `HTTP ${response.status}\n${body}`,
        }],
      };
    } catch (error) {
      return failure(error);
    }
  },
);

server.registerTool(
  "inference_probe",
  {
    title: "Stream a localmodal inference probe",
    description: "Send one bounded streamed Chat Completions request to the configured Modal Qwen endpoint.",
    inputSchema: {
      prompt: z.string().min(1).max(2000).optional(),
    },
  },
  async ({ prompt }) => {
    try {
      const response = await request("/v1/chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({
          model: modelId,
          messages: [{
            role: "user",
            content: prompt ?? "Reply with exactly one short sentence proving localmodal inference is reachable.",
          }],
          stream: true,
          max_tokens: 256,
          extra_body: { chat_template_kwargs: { enable_thinking: false } },
        }),
      });
      if (!response.ok) {
        return failure(`HTTP ${response.status}: ${await response.text()}`);
      }
      const text = await readStream(response);
      return {
        content: [{
          type: "text" as const,
          text: `model=${modelId}\n${text}`,
        }],
      };
    } catch (error) {
      return failure(error);
    }
  },
);

async function main(): Promise<void> {
  if (!endpoint || !token) {
    throw new Error("localmodal MCP server is missing LOCALMODAL_ENDPOINT or MODAL_PROXY_TOKEN");
  }
  await server.connect(new StdioServerTransport());
}

async function request(path: string, init: RequestInit): Promise<Response> {
  if (!endpoint || !token) {
    throw new Error("localmodal MCP server is missing endpoint configuration");
  }
  return fetch(`${endpoint}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      ...init.headers,
    },
  });
}

async function readStream(response: Response): Promise<string> {
  const body = await response.text();
  const output: string[] = [];
  for (const line of body.split(/\r?\n/)) {
    if (!line.startsWith("data: ")) {
      continue;
    }
    const data = line.slice(6);
    if (data === "[DONE]") {
      continue;
    }
    const payload = JSON.parse(data) as {
      choices?: Array<{ delta?: { content?: string; reasoning?: string; reasoning_content?: string } }>;
    };
    const delta = payload.choices?.[0]?.delta;
    const text = delta?.content ?? delta?.reasoning ?? delta?.reasoning_content;
    if (text) {
      output.push(text);
    }
  }
  return output.join("");
}

function failure(error: unknown) {
  return {
    isError: true,
    content: [{
      type: "text" as const,
      text: error instanceof Error ? error.message : String(error),
    }],
  };
}

void main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});