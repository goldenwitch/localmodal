import { mkdir, writeFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { performance } from "node:perf_hooks";

const modelId = process.env.LOCALMODAL_MODEL_ID ?? "Qwen/Qwen3.8-27B";
const modelRevision = process.env.LOCALMODAL_MODEL_REVISION ?? "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0";
const appName = process.env.LOCALMODAL_APP_NAME ?? "localmodal-qwen-ci";
const maxModelLen = process.env.LOCALMODAL_MAX_MODEL_LEN ?? "131072";
const proxyToken = process.env.MODAL_PROXY_TOKEN?.trim();
const metricsFile = process.env.LOCALMODAL_METRICS_FILE ?? "artifacts/modal-startup.json";
const maxDeployToReadySeconds = Number(process.env.LOCALMODAL_MAX_DEPLOY_TO_READY_SECONDS ?? "1500");
const maxReadyToFirstTokenSeconds = Number(process.env.LOCALMODAL_MAX_READY_TO_FIRST_TOKEN_SECONDS ?? "180");
const modalCommand = process.platform === "win32" ? "modal.exe" : "modal";
const defaultRequestTimeoutMs = 60000;

if (!proxyToken) {
  fail("MODAL_PROXY_TOKEN is required for the live Modal startup measurement.");
}

let endpoint;
let deployExitCode;
const deployStartedAt = new Date().toISOString();
const deployStart = performance.now();

try {
  const deploy = await runModal([
    "deploy",
    "-m",
    "deployment.qwen",
  ], {
    LOCALMODAL_APP_NAME: appName,
    LOCALMODAL_MODEL_ID: modelId,
    LOCALMODAL_MODEL_REVISION: modelRevision,
    LOCALMODAL_MAX_MODEL_LEN: maxModelLen,
  });
  deployExitCode = deploy.code;
  if (deploy.code !== 0) {
    fail(`Modal deploy failed with exit code ${deploy.code}.`);
  }
  endpoint = [...deploy.output.matchAll(/https:\/\/[^\s"']+\.modal\.run/g)]
    .map((match) => match[0].replace(/[),.]+$/, ""))
    .find((candidate) => !candidate.includes("modal.com/"));
  if (!endpoint) {
    fail("Modal deploy completed without a modal.run endpoint URL.");
  }

  const ready = await waitForReady(endpoint, proxyToken, deployStart);
  const firstToken = await measureFirstToken(endpoint, proxyToken);
  const deployToReadySeconds = roundSeconds(ready.readyElapsedMs);
  const metrics = {
    measured_at: new Date().toISOString(),
    app_name: appName,
    model_id: modelId,
    model_revision: modelRevision,
    max_model_len: Number(maxModelLen),
    endpoint,
    deploy_started_at: deployStartedAt,
    deploy_exit_code: deployExitCode,
    endpoint_ready_at: ready.readyAt,
    first_token_at: firstToken.firstTokenAt,
    deploy_to_ready_seconds: deployToReadySeconds,
    ready_to_first_token_seconds: roundSeconds(firstToken.firstTokenElapsedMs),
    thresholds: {
      max_deploy_to_ready_seconds: maxDeployToReadySeconds,
      max_ready_to_first_token_seconds: maxReadyToFirstTokenSeconds,
    },
  };
  await mkdir(metricsFile.replace(/[\\/][^\\/]+$/, "") || ".", { recursive: true });
  await writeFile(metricsFile, `${JSON.stringify(metrics, null, 2)}\n`, "utf8");
  console.log(JSON.stringify(metrics, null, 2));

  if (metrics.deploy_to_ready_seconds > maxDeployToReadySeconds) {
    fail(`deploy-to-ready exceeded ${maxDeployToReadySeconds}s.`);
  }
  if (metrics.ready_to_first_token_seconds > maxReadyToFirstTokenSeconds) {
    fail(`ready-to-first-token exceeded ${maxReadyToFirstTokenSeconds}s.`);
  }
} finally {
  await runModal(["app", "stop", appName], {}).catch((error) => {
    console.error(`Modal cleanup failed: ${error instanceof Error ? error.message : String(error)}`);
  });
}

async function waitForReady(url, token, deployStart) {
  const deadline = deployStart + maxDeployToReadySeconds * 1000;
  while (performance.now() < deadline) {
    try {
      const response = await fetchWithTimeout(`${url}/v1/models`, token);
      if (response.ok) {
        return {
          readyAt: new Date().toISOString(),
          readyElapsedMs: performance.now() - deployStart,
        };
      }
      if (response.status === 401 || response.status === 403) {
        fail(`Modal endpoint rejected the Proxy Token with HTTP ${response.status}.`);
      }
      if (![502, 503, 504].includes(response.status)) {
        fail(`Modal readiness returned unexpected HTTP ${response.status}: ${await response.text()}`);
      }
    } catch (error) {
      if (!(error instanceof Error && error.name === "AbortError")) {
        throw error;
      }
    }
    await delay(5000);
  }
  fail(`Modal endpoint did not become ready within ${maxDeployToReadySeconds}s.`);
}

async function measureFirstToken(url, token) {
  const started = performance.now();
  const requestTimeoutMs = Math.max(
    defaultRequestTimeoutMs,
    (maxReadyToFirstTokenSeconds + 5) * 1000,
  );
  const response = await fetchWithTimeout(`${url}/v1/chat/completions`, token, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({
      model: modelId,
      messages: [{ role: "user", content: "Reply with one short sentence proving inference is live." }],
      stream: true,
      max_tokens: 128,
      extra_body: { chat_template_kwargs: { enable_thinking: false } },
    }),
  }, requestTimeoutMs);
  if (!response.ok || !response.body) {
    fail(`First-token request failed with HTTP ${response.status}: ${await response.text()}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const events = buffer.split(/\r?\n\r?\n/);
    buffer = events.pop() ?? "";
    for (const event of events) {
      for (const line of event.split(/\r?\n/)) {
        if (!line.startsWith("data: ") || line === "data: [DONE]") {
          continue;
        }
        const payload = JSON.parse(line.slice(6));
        const delta = payload.choices?.[0]?.delta;
        if (delta?.content || delta?.reasoning || delta?.reasoning_content) {
          return {
            firstTokenAt: new Date().toISOString(),
            firstTokenElapsedMs: performance.now() - started,
          };
        }
      }
    }
    if (done) {
      break;
    }
  }
  fail("The first-token stream ended without a text token.");
}

async function fetchWithTimeout(url, token, init = {}, timeoutMs = defaultRequestTimeoutMs) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, {
      ...init,
      signal: controller.signal,
      headers: { Authorization: `Bearer ${token}`, ...(init.headers ?? {}) },
    });
  } finally {
    clearTimeout(timeout);
  }
}

function runModal(args, extraEnvironment) {
  return new Promise((resolve, reject) => {
    const child = spawn(modalCommand, args, {
      env: {
        ...process.env,
        PYTHONIOENCODING: "utf-8",
        PYTHONUTF8: "1",
        ...extraEnvironment,
      },
      windowsHide: true,
    });
    let output = "";
    child.stdout.on("data", (chunk) => {
      const text = chunk.toString();
      output += text;
      process.stdout.write(text);
    });
    child.stderr.on("data", (chunk) => {
      const text = chunk.toString();
      output += text;
      process.stderr.write(text);
    });
    child.on("error", reject);
    child.on("close", (code) => resolve({ code: code ?? 1, output }));
  });
}

function roundSeconds(milliseconds) {
  return Math.round((milliseconds / 1000) * 1000) / 1000;
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function fail(message) {
  throw new Error(message);
}