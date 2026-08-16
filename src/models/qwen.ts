import type { ModelDefinition } from "./types";

const profiles = {
  "32k": {
    id: "32k",
    maxModelLen: 32768,
    maxInputTokens: 28672,
    maxOutputTokens: 4096,
    measured: true,
  },
  "128k": {
    id: "128k",
    maxModelLen: 131072,
    maxInputTokens: 122880,
    maxOutputTokens: 8192,
    measured: true,
  },
  "262k": {
    id: "262k",
    maxModelLen: 262144,
    maxInputTokens: 253952,
    maxOutputTokens: 8192,
    measured: true,
  },
} as const;

export const QWEN38_27B: ModelDefinition = {
  id: "Qwen/Qwen3.8-27B",
  name: "Qwen3.8-27B (Modal)",
  family: "Qwen3.8",
  version: "27B",
  revision: "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0",
  architecture: "Qwen3_5ForConditionalGeneration",
  contextWindow: 262144,
  capabilities: {
    streaming: true,
    toolCalling: true,
    imageInput: true,
    reasoning: true,
  },
  profiles,
};