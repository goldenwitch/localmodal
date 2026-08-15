import assert from "node:assert/strict";
import test from "node:test";
import { normalizeModalProxyToken, resolveModalProxyToken } from "../state/token";

test("proxy token normalization accepts the dashboard's separate values", () => {
  assert.equal(
    normalizeModalProxyToken("wk-token-id", "ws-token-secret"),
    "wk-token-id.ws-token-secret",
  );
  assert.equal(
    normalizeModalProxyToken("wk-token-id.ws-token-secret"),
    "wk-token-id.ws-token-secret",
  );
  assert.equal(
    normalizeModalProxyToken("wk-token-id ws-token-secret"),
    "wk-token-id.ws-token-secret",
  );
});

test("proxy token normalization rejects malformed prefixes", () => {
  assert.throws(() => normalizeModalProxyToken("not-a-token", "ws-secret"), /Token ID/);
  assert.throws(() => normalizeModalProxyToken("wk-id", "not-a-secret"), /Token secret/);
});

test("stored token wins without prompting", async () => {
  let prompted = false;
  const token = await resolveModalProxyToken({
    stored: async () => "stored-token",
    environment: () => "environment-token",
    prompt: async () => {
      prompted = true;
      return "prompt-token";
    },
    store: async () => undefined,
  });

  assert.equal(token, "stored-token");
  assert.equal(prompted, false);
});

test("environment token avoids the wizard", async () => {
  let prompted = false;
  const token = await resolveModalProxyToken({
    stored: async () => undefined,
    environment: () => "  environment-token  ",
    prompt: async () => {
      prompted = true;
      return "prompt-token";
    },
    store: async () => undefined,
  });

  assert.equal(token, "environment-token");
  assert.equal(prompted, false);
});

test("missing token prompts once and stores the normalized value", async () => {
  let stored: string | undefined;
  const token = await resolveModalProxyToken({
    stored: async () => undefined,
    environment: () => undefined,
    prompt: async () => "  prompted-token  ",
    store: async (value) => {
      stored = value;
    },
  });

  assert.equal(token, "prompted-token");
  assert.equal(stored, "prompted-token");
});