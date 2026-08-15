export const DEPLOYMENT_DEFAULTS = {
  modalCommand: "modal",
  appName: "localmodal-qwen",
  deploymentFile: "deployment/qwen.py",
  gpu: "RTX-PRO-6000",
} as const;

export const USER_DEFAULTS = {
  lifecycle: "workspace",
  contextProfile: "128k",
} as const;