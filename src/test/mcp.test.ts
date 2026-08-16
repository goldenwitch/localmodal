import assert from "node:assert/strict";
import { createServer } from "node:http";
import { resolve } from "node:path";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

test("the packed MCP server exposes delegate, up, and down tools", async () => {
  const httpServer = createServer((request, response) => {
    if (request.url === "/v1/models" && request.method === "GET") {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ data: [{ id: "fixture/model" }] }));
      return;
    }

    if (request.url === "/v1/chat/completions" && request.method === "POST") {
      request.resume();
      request.on("end", () => {
        response.writeHead(200, {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
        });
        response.write('data: {"choices":[{"delta":{"content":"fixture streamed response"}}]}\n\n');
        response.end("data: [DONE]\n\n");
      });
      return;
    }

    response.writeHead(404);
    response.end();
  });

  await new Promise<void>((resolveListen) => httpServer.listen(0, "127.0.0.1", resolveListen));
  const address = httpServer.address();
  assert.ok(address && typeof address !== "string");

  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [resolve(process.cwd(), "dist", "mcp.js")],
    env: {
      ...(process.env as Record<string, string>),
      LOCALMODAL_ENDPOINT: `http://127.0.0.1:${address.port}`,
      LOCALMODAL_MODEL_ID: "fixture/model",
      MODAL_PROXY_TOKEN: "fixture-token",
    },
    stderr: "pipe",
  });
  const client = new Client({ name: "localmodal-test", version: "0.0.1" }, { capabilities: {} });

  try {
    await client.connect(transport);
    const tools = await client.listTools();
    assert.deepEqual(
      tools.tools.map((tool) => tool.name).sort(),
      ["delegate", "down", "up"],
    );

    const upResult = await client.callTool({ name: "up", arguments: {} });
    assert.match(JSON.stringify(upResult), /Endpoint ready/);
    assert.doesNotMatch(JSON.stringify(upResult), /isError/);

    const delegateResult = await client.callTool({
      name: "delegate",
      arguments: { task: "prove the fixture path" },
    });
    assert.match(JSON.stringify(delegateResult), /fixture streamed response/);
    assert.doesNotMatch(JSON.stringify(delegateResult), /isError/);

    const downResult = await client.callTool({ name: "down", arguments: {} });
    assert.match(JSON.stringify(downResult), /Localmodal stopped/);
    assert.doesNotMatch(JSON.stringify(downResult), /isError/);
  } finally {
    await client.close();
    await new Promise<void>((resolveClose, rejectClose) => {
      httpServer.close((error) => (error ? rejectClose(error) : resolveClose()));
    });
  }
});