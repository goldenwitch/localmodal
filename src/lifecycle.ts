export type LifecyclePolicy = "on-demand" | "workspace";

export function shouldDeployOnActivation(policy: LifecyclePolicy): boolean {
  return policy === "workspace";
}

export function shouldStopOnDeactivation(
  policy: LifecyclePolicy,
  deploymentManaged: boolean,
): boolean {
  return deploymentManaged && policy === "workspace";
}