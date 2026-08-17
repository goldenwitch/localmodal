import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { RequestHandlerExtra } from "@modelcontextprotocol/sdk/shared/protocol.js";
import type { ServerNotification, ServerRequest } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import type { LocalmodalRuntime } from "../runtime";

interface LocalmodalMcpOptions {
  runtime: LocalmodalRuntime;
  modelId: string;
  profileId: () => string;
}

type ToolExtra = RequestHandlerExtra<ServerRequest, ServerNotification>;

export function createLocalmodalMcpServer(options: LocalmodalMcpOptions): McpServer {
  const server = new McpServer({
    name: "localmodal",
    version: "0.0.1",
  });

  server.registerTool(
    "delegate",
    {
      title: "Delegate task to model",
      description: "Pass a task and optional context to the model for execution, deploying and warming on demand if stopped.",
      inputSchema: {
        task: z.string().min(1).describe("The task, prompt, or directive for the model to execute."),
        context: z.string().optional().describe("Optional context, code snippets, or documentation relevant to the task."),
        profile: z.enum(["32k", "128k", "262k"]).optional().describe("Context profile to use."),
      },
    },
    async ({ task, context, profile }, extra) => {
      const progress = createProgressReporter(extra);
      try {
        const selectedProfile = profile ?? options.profileId();
        progress.report(`Ensuring model readiness for delegation (profile=${selectedProfile})`);
        const messages = context
          ? [{ role: "user" as const, content: `Context:\n${context}\n\nTask:\n${task}` }]
          : [{ role: "user" as const, content: task }];
        const output: string[] = [];

        await options.runtime.streamChat(
          options.modelId,
          selectedProfile,
          { messages },
          (delta) => {
            if (delta.text) {
              output.push(delta.text);
            }
          },
          extra.signal,
          progress.report,
        );

        const text = output.join("");
        if (!text) {
          throw new Error("The model returned no text for the delegated task.");
        }
        progress.report("Delegation complete");
        await progress.flush();
        return {
          content: [{ type: "text" as const, text }],
        };
      } catch (error) {
        await progress.flush();
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
        profile: z.enum(["32k", "128k", "262k"]).optional().describe("Context profile to deploy."),
      },
    },
    async ({ profile }, extra) => {
      const progress = createProgressReporter(extra);
      try {
        const selectedProfile = profile ?? options.profileId();
        const startTime = Date.now();
        progress.report(`Starting deployment and readiness check (profile=${selectedProfile})`);
        const endpoint = await options.runtime.ensureReady(
          options.modelId,
          selectedProfile,
          extra.signal,
          progress.report,
        );
        const elapsedSeconds = ((Date.now() - startTime) / 1000).toFixed(1);
        progress.report("Endpoint ready");
        await progress.flush();
        return {
          content: [{
            type: "text" as const,
            text: `Endpoint ready in ${elapsedSeconds}s\nmodel: ${endpoint.modelId}\nprofile: ${endpoint.profile.id}\nurl: ${endpoint.baseUrl}`,
          }],
        };
      } catch (error) {
        await progress.flush();
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
    async (extra) => {
      const progress = createProgressReporter(extra);
      try {
        progress.report("Stopping model deployment");
        await options.runtime.stop(extra.signal);
        progress.report("Localmodal stopped");
        await progress.flush();
        return {
          content: [{
            type: "text" as const,
            text: "Localmodal stopped. Model caches were preserved.",
          }],
        };
      } catch (error) {
        await progress.flush();
        return failure(error);
      }
    },
  );

  return server;
}

function createProgressReporter(extra: ToolExtra): {
  report(message: string): void;
  flush(): Promise<void>;
} {
  const progressToken = extra._meta?.progressToken;
  let progress = 0;
  let pending = Promise.resolve();

  return {
    report(message: string): void {
      if (progressToken === undefined) {
        return;
      }
      progress += 1;
      pending = pending.then(() => extra.sendNotification({
        method: "notifications/progress",
        params: { progressToken, progress, message },
      })).catch(() => {});
    },
    flush(): Promise<void> {
      return pending;
    },
  };
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
