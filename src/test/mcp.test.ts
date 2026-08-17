import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { FixtureLifecycleBackend } from "../backend/fixture";
import { ModelController } from "../controller";
import { LocalmodalMcpHost } from "../mcp/host";
import { QWEN38_27B } from "../models/qwen";
import { StaticModelCatalog } from "../models/types";
import { LocalmodalRuntime } from "../runtime";
import type { SecretStore, StateStore } from "../state/types";

class MemoryStateStore implements StateStore {
  private readonly values = new Map<string, unknown>();

  public get<T>(key: string, defaultValue: T): T {
    return (this.values.get(key) as T | undefined) ?? defaultValue;
  }

  public async update<T>(key: string, value: T): Promise<void> {
    if (value === undefined) {
      this.values.delete(key);
    } else {
      this.values.set(key, value);
    }
  }
}

class MemorySecretStore implements SecretStore {
  private token: string | undefined;

  public async get(key: string): Promise<string | undefined> {
    return key === "modalProxyToken" ? this.token : undefined;
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

test("the extension-hosted MCP control surface shares one live runtime", async () => {
  let releaseCancelledResponse: (() => void) | undefined;
  let markCancellationStarted: (() => void) | undefined;
  const cancellationStarted = new Promise<void>((resolve) => {
    markCancellationStarted = resolve;
  });
  const cancellationObserved = new Promise<void>((resolve) => {
    releaseCancelledResponse = resolve;
  });
  const httpServer = createServer((request, response) => {
    if (request.url === "/v1/models" && request.method === "GET") {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ data: [{ id: QWEN38_27B.id }] }));
      return;
    }

    if (request.url === "/v1/chat/completions" && request.method === "POST") {
      const chunks: Buffer[] = [];
      request.on("data", (chunk: Buffer) => chunks.push(chunk));
      request.on("end", () => {
        const body = Buffer.concat(chunks).toString();
        response.writeHead(200, {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
        });
        if (body.includes("malformed response")) {
          response.end("data: {bad-json}\n\ndata: [DONE]\n\n");
          return;
        }
        if (body.includes("wait for cancellation")) {
          markCancellationStarted?.();
          response.once("close", () => releaseCancelledResponse?.());
          return;
        }
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

  const secrets = new MemorySecretStore();
  const managedStates: boolean[] = [];
  const backend = new FixtureLifecycleBackend({
    endpoint: `http://127.0.0.1:${address.port}`,
  });
  const runtime = new LocalmodalRuntime(
    new ModelController(
      new StaticModelCatalog([QWEN38_27B]),
      backend,
      new MemoryStateStore(),
    ),
    secrets,
    (active) => managedStates.push(active),
  );
  const host = await LocalmodalMcpHost.start(
    runtime,
    QWEN38_27B.id,
    () => "128k",
    () => {},
  );
  const transport = new StreamableHTTPClientTransport(host.url, {
    requestInit: { headers: { Authorization: host.authorizationHeader } },
  });
  const client = new Client({ name: "localmodal-test", version: "0.0.1" }, { capabilities: {} });

  try {
    const unauthorized = await fetch(host.url, { method: "POST" });
    assert.equal(unauthorized.status, 401);

    await client.connect(transport);
    const tools = await client.listTools();
    assert.deepEqual(
      tools.tools.map((tool) => tool.name).sort(),
      ["delegate", "down", "up"],
    );

    const missingToken = await client.callTool({ name: "up", arguments: {} });
    assert.equal(missingToken.isError, true);
    await secrets.store("modalProxyToken", "fixture-token");

    let progressCount = 0;
    const upResult = await client.callTool({ name: "up", arguments: {} }, undefined, {
      onprogress: () => {
        progressCount += 1;
      },
      resetTimeoutOnProgress: true,
    });
    assert.match(JSON.stringify(upResult), /Endpoint ready/);
    assert.equal(upResult.isError, undefined);
    assert.ok(progressCount > 0);

    const delegateResult = await client.callTool({
      name: "delegate",
      arguments: { task: "prove the fixture path" },
    });
    assert.match(JSON.stringify(delegateResult), /fixture streamed response/);
    assert.equal(delegateResult.isError, undefined);

    const malformedResult = await client.callTool({
      name: "delegate",
      arguments: { task: "return a malformed response" },
    });
    assert.equal(malformedResult.isError, true);
    assert.match(JSON.stringify(malformedResult), /JSON/);

    const cancellation = new AbortController();
    const cancelledCall = client.callTool({
      name: "delegate",
      arguments: { task: "wait for cancellation" },
    }, undefined, { signal: cancellation.signal });
    await cancellationStarted;
    cancellation.abort();
    await assert.rejects(cancelledCall, /abort/i);
    await cancellationObserved;

    const downResult = await client.callTool({ name: "down", arguments: {} });
    assert.match(JSON.stringify(downResult), /Localmodal stopped/);
    assert.equal(downResult.isError, undefined);
    assert.deepEqual(managedStates.slice(-2), [true, false]);
  } finally {
    await client.close();
    await host.close();
    await new Promise<void>((resolveClose, rejectClose) => {
      httpServer.close((error) => (error ? rejectClose(error) : resolveClose()));
    });
  }
});
