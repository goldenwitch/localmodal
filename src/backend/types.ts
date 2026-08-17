import type { ContextProfile, ModelDefinition } from "../models/types";

export type LifecycleState =
  | "unknown"
  | "stopped"
  | "deploying"
  | "deployed"
  | "warming"
  | "ready"
  | "error";

export interface DeploymentSpec {
  model: ModelDefinition;
  profile: ContextProfile;
}

export interface Endpoint {
  baseUrl: string;
  modelId: string;
  profile: ContextProfile;
}

export interface LifecycleStatus {
  state: LifecycleState;
  endpoint?: Endpoint;
  detail?: string;
}

export interface LifecycleBackend {
  deploy(spec: DeploymentSpec, signal?: AbortSignal, onProgress?: (message: string) => void): Promise<Endpoint>;
  status(): Promise<LifecycleStatus>;
  ensureReady(
    endpoint: Endpoint,
    token: string,
    signal?: AbortSignal,
    onProgress?: (message: string) => void,
  ): Promise<void>;
  stop(signal?: AbortSignal): Promise<void>;
}