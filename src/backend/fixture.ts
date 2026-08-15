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

  public async deploy(spec: DeploymentSpec): Promise<Endpoint> {
    this.options.onEvent?.(`fixture deploy: ${spec.model.id} profile=${spec.profile.id}`);
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

  public async ensureReady(endpoint: Endpoint, token: string): Promise<void> {
    if (!token) {
      throw new Error("Fixture backend requires a token");
    }
    this.options.onEvent?.(`fixture ready: ${endpoint.baseUrl}`);
  }

  public async stop(): Promise<void> {
    this.options.onEvent?.("fixture stop");
    this.deployed = undefined;
  }
}