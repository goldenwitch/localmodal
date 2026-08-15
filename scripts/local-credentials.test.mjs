import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const script = fileURLToPath(new URL("./local-credentials.mjs", import.meta.url));

test("the local credentials CLI stores encrypted values and injects them into a child", {
  skip: process.platform !== "win32",
}, async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "localmodal-credentials-"));
  const store = path.join(directory, "credentials.dpapi");
  const environment = { ...process.env, LOCALMODAL_CREDENTIALS_FILE: store };
  try {
    const stored = await runCli(["set", "MODAL_PROXY_TOKEN"], environment, "wk-test.ws-test\n");
    assert.match(stored.stdout, /Stored MODAL_PROXY_TOKEN/);

    const encrypted = await readFile(store, "utf8");
    assert.doesNotMatch(encrypted, /wk-test|ws-test/);

    const second = await runCli(["set", "MODAL_TOKEN_ID"], environment, "wk-id\n");
    assert.match(second.stdout, /Stored MODAL_TOKEN_ID/);

    const listed = await runCli(["list"], environment);
    assert.deepEqual(listed.stdout.trim().split(/\r?\n/), ["MODAL_PROXY_TOKEN", "MODAL_TOKEN_ID"]);

    const child = await runCli([
      "run",
      "--",
      process.execPath,
      "-e",
      "process.stdout.write(process.env.MODAL_PROXY_TOKEN)",
    ], environment);
    assert.equal(child.stdout, "wk-test.ws-test");

    const npmCli = process.env.npm_execpath
      ?? path.join(path.dirname(process.execPath), "node_modules", "npm", "bin", "npm-cli.js");
    const wrappedNpm = await runCli(
      ["run", "--", "npm", "--version"],
      { ...environment, npm_execpath: npmCli },
    );
    assert.match(wrappedNpm.stdout.trim(), /^\d+\.\d+\.\d+$/);
    assert.doesNotMatch(wrappedNpm.stderr, /DEP0190/);

    const removed = await runCli(["remove", "MODAL_PROXY_TOKEN"], environment);
    assert.match(removed.stdout, /Removed MODAL_PROXY_TOKEN/);

    const remaining = await runCli(["list"], environment);
    assert.equal(remaining.stdout.trim(), "MODAL_TOKEN_ID");
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

function runCli(argumentsList, environment, input = "") {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [script, ...argumentsList], {
      env: environment,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.once("error", reject);
    child.once("close", (code) => {
      if (code !== 0) {
        reject(new Error(`CLI exited with ${code}: ${stderr}`));
      } else {
        resolve({ stdout, stderr });
      }
    });
    child.stdin.end(input, "utf8");
  });
}