import assert from "node:assert/strict";
import test from "node:test";
import type {
  DeploymentSpec,
  Endpoint,
  LifecycleBackend,
  LifecycleStatus,
} from "../backend/types";
import { ModelController } from "../controller";
import type { ModelCatalog, ModelDefinition } from "../models/types";
import { QWEN38_27B } from "../models/qwen";
import { presentModels } from "../models/presentation";
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
  private readonly values = new Map<string, string>();
  public getCalls = 0;

  public async get(key: string): Promise<string | undefined> {
    this.getCalls += 1;
    return this.values.get(key);
  }

  public async store(key: string, value: string): Promise<void> {
    this.values.set(key, value);
  }

  public async delete(key: string): Promise<void> {
    this.values.delete(key);
  }
}

class FakeLifecycleBackend implements LifecycleBackend {
  public deployed: DeploymentSpec | undefined;
  public stopped = false;
  public ready = false;
  public statusCalls = 0;

  public async deploy(spec: DeploymentSpec): Promise<Endpoint> {
    this.deployed = spec;
    this.stopped = false;
    return {
      baseUrl: "https://fixture.invalid",
      modelId: spec.model.id,
      profile: spec.profile,
    };
  }

  public async status(): Promise<LifecycleStatus> {
    this.statusCalls += 1;
    return { state: this.stopped ? "stopped" : "deployed" };
  }

  public async ensureReady(_endpoint: Endpoint, token: string): Promise<void> {
    assert.equal(token, "fixture-token");
    this.ready = true;
  }

  public async stop(): Promise<void> {
    this.stopped = true;
    this.ready = false;
  }
}

class FixtureCatalog implements ModelCatalog {
  public constructor(private readonly models: readonly ModelDefinition[]) {}

  public list(): readonly ModelDefinition[] {
    return this.models;
  }

  public get(id: string): ModelDefinition | undefined {
    return this.models.find((model) => model.id === id);
  }
}

test("the controller accepts any model catalog implementation", async () => {
  const fixture: ModelDefinition = {
    ...QWEN38_27B,
    id: "fixture/model",
    name: "Fixture model",
  };
  const backend = new FakeLifecycleBackend();
  const secrets = new MemorySecretStore();
  await secrets.store("modalProxyToken", "fixture-token");
  const controller = new ModelController(
    new FixtureCatalog([fixture]),
    backend,
    new MemoryStateStore(),
    secrets,
  );

  await controller.ensureReady("fixture/model", "32k");

  assert.equal(backend.deployed?.model.id, "fixture/model");
  assert.equal(backend.ready, true);
});

test("the lifecycle boundary supports start, readiness, status, and stop", async () => {
  const backend = new FakeLifecycleBackend();
  const secrets = new MemorySecretStore();
  await secrets.store("modalProxyToken", "fixture-token");
  const controller = new ModelController(
    { list: () => [QWEN38_27B], get: (id) => (id === QWEN38_27B.id ? QWEN38_27B : undefined) },
    backend,
    new MemoryStateStore(),
    secrets,
  );

  await controller.ensureReady(QWEN38_27B.id, "128k");
  assert.equal(backend.deployed?.profile.maxModelLen, 131072);
  assert.equal((await controller.status()).state, "deployed");

  await controller.stop();
  assert.equal((await controller.status()).state, "stopped");
});

test("deployment does not read the wire token until readiness is requested", async () => {
  const backend = new FakeLifecycleBackend();
  const secrets = new MemorySecretStore();
  const controller = new ModelController(
    { list: () => [QWEN38_27B], get: (id) => (id === QWEN38_27B.id ? QWEN38_27B : undefined) },
    backend,
    new MemoryStateStore(),
    secrets,
  );

  await controller.ensureDeployed(QWEN38_27B.id, "128k");
  assert.equal(secrets.getCalls, 0);
  assert.equal(backend.deployed?.profile.id, "128k");
});

test("a missing wire token fails before touching the backend", async () => {
  const backend = new FakeLifecycleBackend();
  const secrets = new MemorySecretStore();
  const controller = new ModelController(
    { list: () => [QWEN38_27B], get: (id) => (id === QWEN38_27B.id ? QWEN38_27B : undefined) },
    backend,
    new MemoryStateStore(),
    secrets,
  );

  await assert.rejects(
    controller.ensureReady(QWEN38_27B.id, "128k"),
    /Modal Proxy Token is required/,
  );
  assert.equal(secrets.getCalls, 1);
  assert.equal(backend.statusCalls, 0);
  assert.equal(backend.deployed, undefined);
});

test("the production catalog presents one Qwen model per selected profile", () => {
  const catalog = new FixtureCatalog([QWEN38_27B]);

  const measured = presentModels(catalog, "32k");
  assert.equal(measured.length, 1);
  assert.equal(measured[0].definition.id, QWEN38_27B.id);
  assert.equal(measured[0].version, "27B-32k");
  assert.equal(measured[0].detail, "Modal / 32k");
  assert.equal(measured[0].maxInputTokens, 28672);

  const unmeasured = presentModels(catalog, "128k");
  assert.equal(unmeasured.length, 1);
  assert.equal(unmeasured[0].detail, "Modal / 128k / unmeasured");
});