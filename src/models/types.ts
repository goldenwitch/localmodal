export type ContextProfileId = "32k" | "128k" | "262k";

export interface ContextProfile {
  id: ContextProfileId;
  maxModelLen: number;
  maxInputTokens: number;
  maxOutputTokens: number;
  measured: boolean;
}

export interface ModelCapabilities {
  streaming: boolean;
  toolCalling: boolean;
  imageInput: boolean;
  reasoning: boolean;
}

export interface ModelDefinition {
  id: string;
  name: string;
  family: string;
  version: string;
  revision: string;
  architecture: string;
  contextWindow: number;
  capabilities: ModelCapabilities;
  profiles: Readonly<Record<ContextProfileId, ContextProfile>>;
}

export interface ModelCatalog {
  list(): readonly ModelDefinition[];
  get(id: string): ModelDefinition | undefined;
}

export class StaticModelCatalog implements ModelCatalog {
  private readonly byId: ReadonlyMap<string, ModelDefinition>;

  public constructor(models: readonly ModelDefinition[]) {
    this.byId = new Map(models.map((model) => [model.id, model]));
  }

  public list(): readonly ModelDefinition[] {
    return [...this.byId.values()];
  }

  public get(id: string): ModelDefinition | undefined {
    return this.byId.get(id);
  }
}