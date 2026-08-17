import type { LifecyclePolicy } from "./lifecycle";

export const ONBOARDING_STATE_KEY = "localmodal.firstRun.v1";

export function needsOnboarding(completed: boolean): boolean {
  return !completed;
}

export function shouldDeployAfterOnboarding(policy: LifecyclePolicy): boolean {
  return policy === "workspace";
}