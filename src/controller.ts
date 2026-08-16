import type {
  DeploymentSpec,
  Endpoint,
  LifecycleBackend,
  LifecycleStatus,
} from "./backend/types";
import type { ModelCatalog } from "./models/types";
import type { SecretStore, StateStore } from "./state/types";

const ENDPOINT_KEY = "localmodal.endpoint";

export class ModelController {
  public constructor(
    private readonly catalog: ModelCatalog,
    private readonly backend: LifecycleBackend,
    private readonly state: StateStore,
    private readonly secrets: SecretStore,
    private readonly onReady?: () => PromiseLike<void> | void,
  ) {}

  public get currentEndpoint(): Endpoint | undefined {
    return this.state.get<Endpoint | undefined>(ENDPOINT_KEY, undefined);
  }

  public async start(modelId: string, profileId: string): Promise<Endpoint> {
    const model = this.catalog.get(modelId);
    if (!model) {
      throw new Error(`Unknown localmodal model: ${modelId}`);
    }
    const profile = model.profiles[profileId as keyof typeof model.profiles];
    if (!profile) {
      throw new Error(`Unknown context profile for ${modelId}: ${profileId}`);
    }

    const spec: DeploymentSpec = { model, profile };
    const endpoint = await this.backend.deploy(spec);
    await this.state.update(ENDPOINT_KEY, endpoint);
    return endpoint;
  }

  public async ensureDeployed(modelId: string, profileId: string): Promise<Endpoint> {
    const saved = this.state.get<Endpoint | undefined>(ENDPOINT_KEY, undefined);
    const status = await this.backend.status();
    if (
      saved &&
      saved.modelId === modelId &&
      saved.profile.id === profileId &&
      (status.state === "deployed" || status.state === "deploying")
    ) {
      return saved;
    }
    return this.start(modelId, profileId);
  }

  public async ensureReady(
    modelId: string,
    profileId: string,
    signal?: AbortSignal,
  ): Promise<Endpoint> {
    const token = await this.secrets.get("modalProxyToken");
    if (!token) {
      throw new Error("A Modal Proxy Token is required to use localmodal.");
    }

    let endpoint = this.state.get<Endpoint | undefined>(ENDPOINT_KEY, undefined);
    const status = await this.backend.status();
    if (
      !endpoint ||
      endpoint.modelId !== modelId ||
      endpoint.profile.id !== profileId ||
      status.state === "stopped" ||
      status.state === "error"
    ) {
      endpoint = await this.start(modelId, profileId);
    }

    await this.backend.ensureReady(endpoint, token, signal);
    await this.onReady?.();
    return endpoint;
  }

  public async stop(): Promise<void> {
    await this.backend.stop();
    await this.state.update(ENDPOINT_KEY, undefined);
  }

  public async status(): Promise<LifecycleStatus> {
    return this.backend.status();
  }
}