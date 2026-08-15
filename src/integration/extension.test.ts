import assert from "node:assert/strict";
import { createServer, type Server } from "node:http";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import * as vscode from "vscode";
import type { LocalmodalExtensionApi } from "../extension";

const TEST_PORT = 43123;
const live = process.env.LOCALMODAL_LIVE_TEST === "1";
let fixture: Server;
let extension: vscode.Extension<LocalmodalExtensionApi>;
let api: LocalmodalExtensionApi;

suite("localmodal Extension Host", () => {
  suiteSetup(async () => {
    if (!live) {
      fixture = createServer((request, response) => {
      if (request.url === "/v1/models" && request.method === "GET") {
        response.writeHead(200, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ data: [{ id: "Qwen/Qwen3.8-27B" }] }));
        return;
      }

      if (request.url === "/v1/chat/completions" && request.method === "POST") {
        request.resume();
        request.on("end", () => {
          response.writeHead(200, {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
          });
          response.write('data: {"choices":[{"delta":{"content":"extension-host fixture response"}}]}\n\n');
          response.end("data: [DONE]\n\n");
        });
        return;
      }

      response.writeHead(404);
      response.end();
      });
      await new Promise<void>((resolveListen) => fixture.listen(TEST_PORT, "127.0.0.1", resolveListen));
    }

    extension = vscode.extensions.getExtension<LocalmodalExtensionApi>("goldenwitch.localmodal")!;
    assert.ok(extension, "localmodal extension must be loaded in the Extension Host");
    api = await extension.activate();
  });

  suiteTeardown(async () => {
    await vscode.commands.executeCommand("localmodal.stop");
    if (fixture) {
      await new Promise<void>((resolveClose, rejectClose) => {
        fixture.close((error) => (error ? rejectClose(error) : resolveClose()));
      });
    }
  });

  test("registers the real Copilot model and commands", async () => {
    const models = await vscode.lm.selectChatModels({ vendor: "localmodal" });
    assert.equal(models.length, 1);
    assert.equal(models[0].id, "Qwen/Qwen3.8-27B");
    assert.equal(models[0].name, "Qwen3.8-27B (Modal)");

    const commands = await vscode.commands.getCommands(true);
    assert.ok(commands.includes("localmodal.start"));
    assert.ok(commands.includes("localmodal.setup"));
    assert.ok(commands.includes("localmodal.showOutput"));
    assert.equal((await api.getStatus()).state, "stopped");
  });

  test("runs the real start command and resolves the real MCP provider", async () => {
    if (live) {
      await vscode.commands.executeCommand("localmodal.setup");
    } else {
      await vscode.commands.executeCommand("localmodal.start");
    }
    assert.equal((await api.getStatus()).state, "deployed");

    const definition = await api.resolveInferenceMcpServer();
    const environment = Object.fromEntries(
      Object.entries({ ...process.env, ...definition.env })
        .filter(([, value]) => typeof value === "string"),
    ) as Record<string, string>;
    const transport = new StdioClientTransport({
      command: definition.command,
      args: definition.args,
      cwd: definition.cwd?.fsPath,
      env: environment,
      stderr: "pipe",
    });
    const client = new Client(
      { name: "localmodal-extension-host-test", version: "0.0.1" },
      { capabilities: {} },
    );

    try {
      await client.connect(transport);
      const tools = await client.listTools();
      assert.deepEqual(
        tools.tools.map((tool) => tool.name).sort(),
        ["inference_probe", "inference_status"],
      );

      const status = await client.callTool({ name: "inference_status", arguments: {} });
      assert.match(JSON.stringify(status), /HTTP 200/);

      const probe = await client.callTool({
        name: "inference_probe",
        arguments: { prompt: "prove the Extension Host path" },
      });
      assert.doesNotMatch(JSON.stringify(probe), /isError/);
      if (live) {
        assert.match(JSON.stringify(probe), /model=Qwen\/Qwen3\.8-27B/);
      } else {
        assert.match(JSON.stringify(probe), /extension-host fixture response/);
      }
    } finally {
      await client.close();
    }
  });
});