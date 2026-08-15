import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "@vscode/test-cli";

const root = path.dirname(fileURLToPath(import.meta.url));

const common = {
  files: "dist/integration/**/*.test.js",
  version: process.env.VSCODE_TEST_VERSION ?? "insiders",
  extensionDevelopmentPath: root,
  workspaceFolder: root,
  launchArgs: ["--disable-extensions"],
  mocha: {
    ui: "tdd",
    timeout: 120000,
  },
};

const live = {
  ...common,
  mocha: {
    ...common.mocha,
    timeout: 25 * 60 * 1000,
  },
};

export default defineConfig([
  {
    ...common,
    label: "extension-host",
    env: {
      LOCALMODAL_TEST_MODE: "1",
      LOCALMODAL_TEST_ENDPOINT: "http://127.0.0.1:43123",
      MODAL_PROXY_TOKEN: "fixture-token",
    },
  },
  {
    ...live,
    label: "extension-live",
    env: {
      LOCALMODAL_LIVE_TEST: "1",
      LOCALMODAL_TEST_APP_NAME: process.env.LOCALMODAL_TEST_APP_NAME,
      MODAL_PROXY_TOKEN: process.env.MODAL_PROXY_TOKEN,
    },
  },
]);