import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import type {
  DeploymentSpec,
  Endpoint,
  LifecycleBackend,
  LifecycleStatus,
} from "../backend/types";
import { ModelController } from "../controller";
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

class RotatingSecretStore implements SecretStore {
  public getCalls = 0;

  public async get(): Promise<string> {
    this.getCalls += 1;
    return this.getCalls === 1 ? "first-token" : "second-token";
  }

  public async store(): Promise<void> {}
  public async delete(): Promise<void> {}
}

class RuntimeBackend implements LifecycleBackend {
  public endpoint: Endpoint | undefined;
  public readyToken: string | undefined;
  public stopCalls = 0;

  public constructor(private readonly baseUrl: string) {}

  public async deploy(spec: DeploymentSpec): Promise<Endpoint> {
    this.endpoint = {
      baseUrl: this.baseUrl,
      modelId: spec.model.id,
      profile: spec.profile,
    };
    return this.endpoint;
  }

  public async status(): Promise<LifecycleStatus> {
    return this.endpoint
      ? { state: "deployed", endpoint: this.endpoint }
      : { state: "stopped" };
  }

  public async ensureReady(_endpoint: Endpoint, token: string): Promise<void> {
    this.readyToken = token;
  }

  public async stop(): Promise<void> {
    this.stopCalls += 1;
    this.endpoint = undefined;
  }
}

test("one chat operation owns its credential and wire invariants", async () => {
  let authorization: string | undefined;
  let requestBody: Record<string, unknown> | undefined;
  const httpServer = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      authorization = request.headers.authorization;
      requestBody = JSON.parse(Buffer.concat(chunks).toString()) as Record<string, unknown>;
      response.writeHead(200, { "Content-Type": "text/event-stream" });
      response.end('data:{"choices":[{"delta":{"content":"runtime response"}}]}');
    });
  });
  await new Promise<void>((resolve) => httpServer.listen(0, "127.0.0.1", resolve));
  const address = httpServer.address();
  assert.ok(address && typeof address !== "string");

  const secrets = new RotatingSecretStore();
  const backend = new RuntimeBackend(`http://127.0.0.1:${address.port}`);
  const runtime = new LocalmodalRuntime(
    new ModelController(
      new StaticModelCatalog([QWEN38_27B]),
      backend,
      new MemoryStateStore(),
    ),
    secrets,
  );
  const output: string[] = [];

  try {
    await runtime.streamChat(
      QWEN38_27B.id,
      "128k",
      {
        messages: [{ role: "user", content: "authoritative message" }],
        modelOptions: {
          model: "wrong/model",
          messages: [{ role: "user", content: "wrong message" }],
          stream: false,
        },
      },
      (delta) => {
        if (delta.text) {
          output.push(delta.text);
        }
      },
    );

    assert.equal(secrets.getCalls, 1);
    assert.equal(backend.readyToken, "first-token");
    assert.equal(authorization, "Bearer first-token");
    assert.equal(requestBody?.model, QWEN38_27B.id);
    assert.deepEqual(requestBody?.messages, [{ role: "user", content: "authoritative message" }]);
    assert.equal(requestBody?.stream, true);
    assert.equal(output.join(""), "runtime response");
  } finally {
    await new Promise<void>((resolve, reject) => {
      httpServer.close((error) => (error ? reject(error) : resolve()));
    });
  }
});

test("queued lifecycle operations preserve order and observe cancellation", { timeout: 2000 }, async () => {
  let releaseResponse: () => void = () => {};
  const responseReleased = new Promise<void>((resolve) => {
    releaseResponse = resolve;
  });
  let markRequestStarted: () => void = () => {};
  const requestStarted = new Promise<void>((resolve) => {
    markRequestStarted = resolve;
  });
  const httpServer = createServer((request, response) => {
    request.resume();
    request.on("end", async () => {
      markRequestStarted();
      await responseReleased;
      response.writeHead(200, { "Content-Type": "text/event-stream" });
      response.end('data:{"choices":[{"delta":{"content":"done"}}]}\n\ndata:[DONE]\n\n');
    });
  });
  await new Promise<void>((resolve) => httpServer.listen(0, "127.0.0.1", resolve));
  const address = httpServer.address();
  assert.ok(address && typeof address !== "string");

  const backend = new RuntimeBackend(`http://127.0.0.1:${address.port}`);
  const runtime = new LocalmodalRuntime(
    new ModelController(
      new StaticModelCatalog([QWEN38_27B]),
      backend,
      new MemoryStateStore(),
    ),
    new RotatingSecretStore(),
  );

  try {
    const inference = runtime.streamChat(
      QWEN38_27B.id,
      "128k",
      { messages: [{ role: "user", content: "wait" }] },
      () => {},
    );
    await requestStarted;

    const cancellation = new AbortController();
    const cancelledStop = runtime.stop(cancellation.signal);
    cancellation.abort();
    await assert.rejects(cancelledStop, /abort/i);

    const stop = runtime.stop();
    await new Promise<void>((resolve) => setImmediate(resolve));
    assert.equal(backend.stopCalls, 0);

    releaseResponse();
    await inference;
    await stop;
    assert.equal(backend.stopCalls, 1);
  } finally {
    await new Promise<void>((resolve, reject) => {
      httpServer.close((error) => (error ? reject(error) : resolve()));
    });
  }
});

test("malformed SSE cancels the response body", { timeout: 2000 }, async () => {
  let markResponseClosed: () => void = () => {};
  const responseClosed = new Promise<void>((resolve) => {
    markResponseClosed = resolve;
  });
  const httpServer = createServer((request, response) => {
    request.resume();
    request.on("end", () => {
      response.writeHead(200, { "Content-Type": "text/event-stream" });
      response.write("data:{bad-json}\n\n");
      response.once("close", markResponseClosed);
    });
  });
  await new Promise<void>((resolve) => httpServer.listen(0, "127.0.0.1", resolve));
  const address = httpServer.address();
  assert.ok(address && typeof address !== "string");

  const backend = new RuntimeBackend(`http://127.0.0.1:${address.port}`);
  const runtime = new LocalmodalRuntime(
    new ModelController(
      new StaticModelCatalog([QWEN38_27B]),
      backend,
      new MemoryStateStore(),
    ),
    new RotatingSecretStore(),
  );

  try {
    await assert.rejects(
      runtime.streamChat(
        QWEN38_27B.id,
        "128k",
        { messages: [{ role: "user", content: "malformed" }] },
        () => {},
      ),
      /JSON|Unexpected/,
    );
    await responseClosed;
  } finally {
    await new Promise<void>((resolve, reject) => {
      httpServer.close((error) => (error ? reject(error) : resolve()));
    });
  }
});
