import type { ModelCatalog, ModelDefinition } from "./types";

export interface PresentedModel {
  definition: ModelDefinition;
  profileId: string;
  version: string;
  detail: string;
  maxInputTokens: number;
  maxOutputTokens: number;
}

export function presentModels(catalog: ModelCatalog, profileId: string): PresentedModel[] {
  return catalog.list().flatMap((definition) => {
    const profile = definition.profiles[profileId as keyof typeof definition.profiles];
    if (!profile) {
      return [];
    }
    return [{
      definition,
      profileId: profile.id,
      version: `${definition.version}-${profile.id}`,
      detail: `Modal / ${profile.id}${profile.measured ? "" : " / unmeasured"}`,
      maxInputTokens: profile.maxInputTokens,
      maxOutputTokens: profile.maxOutputTokens,
    }];
  });
}