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
  deploy(spec: DeploymentSpec): Promise<Endpoint>;
  status(): Promise<LifecycleStatus>;
  ensureReady(endpoint: Endpoint, token: string, signal?: AbortSignal): Promise<void>;
  stop(): Promise<void>;
}