import type {
  DeploymentSpec,
  Endpoint,
  LifecycleBackend,
  LifecycleStatus,
} from "./types";

export interface FixtureBackendOptions {
  endpoint: string;
  onEvent?: (message: string) => void;
}

export class FixtureLifecycleBackend implements LifecycleBackend {
  private deployed: Endpoint | undefined;

  public constructor(private readonly options: FixtureBackendOptions) {}

  public async deploy(
    spec: DeploymentSpec,
    signal?: AbortSignal,
    onProgress?: (message: string) => void,
  ): Promise<Endpoint> {
    signal?.throwIfAborted();
    const message = `fixture deploy: ${spec.model.id} profile=${spec.profile.id}`;
    this.options.onEvent?.(message);
    onProgress?.(message);
    this.deployed = {
      baseUrl: this.options.endpoint,
      modelId: spec.model.id,
      profile: spec.profile,
    };
    return this.deployed;
  }

  public async status(): Promise<LifecycleStatus> {
    return this.deployed
      ? { state: "deployed", endpoint: this.deployed }
      : { state: "stopped" };
  }

  public async ensureReady(
    endpoint: Endpoint,
    token: string,
    signal?: AbortSignal,
    onProgress?: (message: string) => void,
  ): Promise<void> {
    signal?.throwIfAborted();
    if (!token) {
      throw new Error("Fixture backend requires a token");
    }
    const message = `fixture ready: ${endpoint.baseUrl}`;
    this.options.onEvent?.(message);
    onProgress?.(message);
  }

  public async stop(signal?: AbortSignal): Promise<void> {
    signal?.throwIfAborted();
    this.options.onEvent?.("fixture stop");
    this.deployed = undefined;
  }
}