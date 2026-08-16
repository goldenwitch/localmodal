import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { shouldDeployOnActivation, shouldStopOnDeactivation } from "../lifecycle";
import { needsOnboarding, shouldDeployAfterOnboarding, shouldStopAfterOnboarding } from "../onboarding";
import { DEPLOYMENT_DEFAULTS, USER_DEFAULTS } from "../product";

test("only lifecycle and context profile are user-facing settings", () => {
  const manifest = JSON.parse(readFileSync(resolve(process.cwd(), "package.json"), "utf8")) as {
    contributes: {
      configuration: { properties: Record<string, unknown> };
      mcpServerDefinitionProviders: Array<{ id: string; label: string }>;
      commands: Array<{ command: string }>;
    };
  };
  assert.deepEqual(Object.keys(manifest.contributes.configuration.properties).sort(), [
    "localmodal.contextProfile",
    "localmodal.lifecycle",
  ]);
  assert.deepEqual(manifest.contributes.mcpServerDefinitionProviders, [{
    id: "localmodal.mcp",
    label: "localmodal inference validation",
  }]);
  assert.equal(
    manifest.contributes.commands.some((command) => command.command === "localmodal.setup"),
    true,
  );
  assert.equal(
    manifest.contributes.commands.some((command) => command.command === "localmodal.reportIssue"),
    true,
  );
});

test("deployment choices are fixed product defaults", () => {
  assert.deepEqual(DEPLOYMENT_DEFAULTS, {
    modalCommand: "modal",
    appName: "localmodal-qwen",
    deploymentFile: "deployment/qwen.py",
    gpu: "RTX-PRO-6000",
  });
  assert.deepEqual(USER_DEFAULTS, {
    lifecycle: "workspace",
    contextProfile: "128k",
  });
});

test("lifecycle policy is the only activation policy choice", () => {
  assert.equal(shouldDeployOnActivation("workspace"), true);
  assert.equal(shouldStopOnDeactivation("workspace"), true);
  assert.equal(shouldDeployOnActivation("on-demand"), false);
  assert.equal(shouldStopOnDeactivation("on-demand"), false);
});

test("first-run setup gates deployment and follows the selected lifecycle policy", () => {
  assert.equal(needsOnboarding(false), true);
  assert.equal(needsOnboarding(true), false);
  assert.equal(shouldDeployAfterOnboarding("workspace"), true);
  assert.equal(shouldDeployAfterOnboarding("on-demand"), false);
  assert.equal(shouldStopAfterOnboarding("workspace", true), true);
  assert.equal(shouldStopAfterOnboarding("workspace", false), false);
  assert.equal(shouldStopAfterOnboarding("on-demand", true), false);
});