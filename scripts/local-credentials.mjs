import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const STORE_ENVIRONMENT_VARIABLE = "LOCALMODAL_CREDENTIALS_FILE";
const CREDENTIAL_NAME_PATTERN = /^[A-Z][A-Z0-9_]*$/;
const POWERSHELL_SCRIPT = [
  "$ErrorActionPreference = 'Stop'",
  "Add-Type -AssemblyName System.Security",
  "$plaintext = [Text.Encoding]::UTF8.GetBytes([Console]::In.ReadToEnd())",
  "$protected = [Security.Cryptography.ProtectedData]::Protect($plaintext, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)",
  "[Console]::Out.Write([Convert]::ToBase64String($protected))",
].join("; ");
const POWERSHELL_DECRYPT_SCRIPT = [
  "$ErrorActionPreference = 'Stop'",
  "Add-Type -AssemblyName System.Security",
  "$protected = [Convert]::FromBase64String(([Console]::In.ReadToEnd()).Trim())",
  "$plaintext = [Security.Cryptography.ProtectedData]::Unprotect($protected, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)",
  "[Console]::Out.Write([Text.Encoding]::UTF8.GetString($plaintext))",
].join("; ");

export function credentialStorePath(environment = process.env) {
  const configured = environment[STORE_ENVIRONMENT_VARIABLE]?.trim();
  if (configured) {
    return path.resolve(configured);
  }

  const localAppData = environment.LOCALAPPDATA ?? path.join(os.homedir(), "AppData", "Local");
  return path.join(localAppData, "localmodal", "credentials.dpapi");
}

export function validateCredentialName(name) {
  if (!CREDENTIAL_NAME_PATTERN.test(name)) {
    throw new Error("Credential names must be uppercase environment variable names, for example MODAL_PROXY_TOKEN.");
  }
}

export async function readCredentials(file = credentialStorePath()) {
  assertWindows();
  let encrypted;
  try {
    encrypted = await readFile(file, "utf8");
  } catch (error) {
    if (error?.code === "ENOENT") {
      return {};
    }
    throw error;
  }

  let parsed;
  try {
    parsed = JSON.parse(await runPowerShell(POWERSHELL_DECRYPT_SCRIPT, encrypted));
  } catch (error) {
    throw new Error(`Could not decrypt the local credential store at ${file}.`, { cause: error });
  }
  if (parsed?.version !== 1 || !parsed.credentials || typeof parsed.credentials !== "object") {
    throw new Error(`The local credential store at ${file} has an unsupported format.`);
  }

  const credentials = {};
  for (const [name, value] of Object.entries(parsed.credentials)) {
    validateCredentialName(name);
    if (typeof value !== "string") {
      throw new Error(`The local credential store contains a non-text value for ${name}.`);
    }
    credentials[name] = value;
  }
  return credentials;
}

export async function writeCredentials(credentials, file = credentialStorePath()) {
  assertWindows();
  for (const [name, value] of Object.entries(credentials)) {
    validateCredentialName(name);
    if (typeof value !== "string" || value.length === 0) {
      throw new Error(`Credential ${name} must have a non-empty text value.`);
    }
  }

  const encrypted = await runPowerShell(
    POWERSHELL_SCRIPT,
    JSON.stringify({ version: 1, credentials }),
  );
  await mkdir(path.dirname(file), { recursive: true });
  const temporaryFile = `${file}.${process.pid}.tmp`;
  try {
    await writeFile(temporaryFile, `${encrypted.trim()}\n`, { encoding: "utf8", mode: 0o600 });
    await rename(temporaryFile, file);
  } finally {
    await rm(temporaryFile, { force: true });
  }
}

async function main(argumentsList) {
  const [command, ...argumentsAfterCommand] = argumentsList;
  switch (command) {
    case "set":
      await setCredential(argumentsAfterCommand);
      return;
    case "list":
      await listCredentials();
      return;
    case "remove":
      await removeCredential(argumentsAfterCommand);
      return;
    case "run":
      await runWithCredentials(argumentsAfterCommand);
      return;
    case "help":
    case undefined:
      printUsage();
      return;
    default:
      throw new Error(`Unknown command: ${command}`);
  }
}

async function setCredential(argumentsAfterCommand) {
  const name = argumentsAfterCommand[0];
  if (!name || argumentsAfterCommand.length !== 1) {
    throw new Error("Usage: npm run credentials -- set NAME");
  }
  validateCredentialName(name);

  const value = await readSecret(`Value for ${name}: `);
  if (!value) {
    throw new Error("Credential value cannot be empty.");
  }
  const file = credentialStorePath();
  const credentials = await readCredentials(file);
  credentials[name] = value;
  await writeCredentials(credentials, file);
  console.log(`Stored ${name} in ${file}.`);
}

async function listCredentials() {
  const credentials = await readCredentials();
  const names = Object.keys(credentials).sort();
  if (names.length === 0) {
    console.log("No local credentials are stored.");
    return;
  }
  for (const name of names) {
    console.log(name);
  }
}

async function removeCredential(argumentsAfterCommand) {
  const name = argumentsAfterCommand[0];
  if (!name || argumentsAfterCommand.length !== 1) {
    throw new Error("Usage: npm run credentials -- remove NAME");
  }
  validateCredentialName(name);

  const file = credentialStorePath();
  const credentials = await readCredentials(file);
  if (!(name in credentials)) {
    throw new Error(`No local credential is stored for ${name}.`);
  }
  delete credentials[name];
  if (Object.keys(credentials).length === 0) {
    await rm(file, { force: true });
  } else {
    await writeCredentials(credentials, file);
  }
  console.log(`Removed ${name}.`);
}

async function runWithCredentials(argumentsAfterCommand) {
  const separator = argumentsAfterCommand.indexOf("--");
  const command = separator >= 0 ? argumentsAfterCommand[separator + 1] : argumentsAfterCommand[0];
  const commandArguments = separator >= 0
    ? argumentsAfterCommand.slice(separator + 2)
    : argumentsAfterCommand.slice(1);
  if (!command) {
    throw new Error("Usage: npm run credentials -- run -- COMMAND [ARGUMENT ...]");
  }

  const credentials = await readCredentials();
  const resolvedCommand = resolveCommand(command);
  const child = spawn(resolvedCommand.executable, [...resolvedCommand.arguments, ...commandArguments], {
    env: { ...process.env, ...credentials },
    stdio: "inherit",
    windowsHide: false,
  });
  await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", (code, signal) => {
      if (signal) {
        reject(new Error(`Credential-wrapped command terminated with ${signal}.`));
      } else {
        resolve(code ?? 1);
      }
    });
  }).then((code) => {
    process.exitCode = code;
  });
}

function resolveCommand(command) {
  if (process.platform === "win32" && ["npm", "npm.cmd"].includes(command.toLowerCase())) {
    const npmCli = process.env.npm_execpath?.trim()
      || path.join(path.dirname(process.execPath), "node_modules", "npm", "bin", "npm-cli.js");
    if (!existsSync(npmCli)) {
      throw new Error(`Could not find the npm CLI entry point at ${npmCli}.`);
    }
    return { executable: process.execPath, arguments: [npmCli] };
  }
  if (process.platform === "win32" && /\.(cmd|bat)$/i.test(command)) {
    throw new Error("Windows batch commands are not supported by the credential wrapper; use the executable directly.");
  }
  return { executable: command, arguments: [] };
}

function readSecret(prompt) {
  if (!process.stdin.isTTY) {
    return readPipedSecret();
  }

  return new Promise((resolve, reject) => {
    let value = "";
    const onData = (chunk) => {
      for (const character of chunk.toString()) {
        if (character === "\u0003") {
          cleanup();
          reject(new Error("Credential entry cancelled."));
          return;
        }
        if (character === "\r" || character === "\n") {
          cleanup();
          process.stdout.write("\n");
          resolve(value);
          return;
        }
        if (character === "\u007f" || character === "\b") {
          value = value.slice(0, -1);
        } else {
          value += character;
        }
      }
    };
    const cleanup = () => {
      process.stdin.off("data", onData);
      process.stdin.setRawMode?.(false);
      process.stdin.pause();
    };

    process.stdout.write(prompt);
    process.stdin.setEncoding("utf8");
    process.stdin.setRawMode?.(true);
    process.stdin.resume();
    process.stdin.on("data", onData);
  });
}

function readPipedSecret() {
  return new Promise((resolve, reject) => {
    let value = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      value += chunk;
    });
    process.stdin.once("end", () => resolve(value.replace(/\r?\n$/, "")));
    process.stdin.once("error", reject);
  });
}

function runPowerShell(script, input) {
  return new Promise((resolve, reject) => {
    const child = spawn(powerShellCommand(), [
      "-NoLogo",
      "-NoProfile",
      "-NonInteractive",
      "-ExecutionPolicy",
      "Bypass",
      "-Command",
      script,
    ], {
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
        reject(new Error(stderr.trim() || `PowerShell exited with code ${code}.`));
      } else {
        resolve(stdout.trim());
      }
    });
    child.stdin.end(input, "utf8");
  });
}

function powerShellCommand() {
  if (process.platform !== "win32") {
    throw new Error("The local credential store requires Windows DPAPI.");
  }
  return process.env.SystemRoot
    ? path.join(process.env.SystemRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    : "powershell.exe";
}

function assertWindows() {
  if (process.platform !== "win32") {
    throw new Error("The local credential store requires Windows DPAPI.");
  }
}

function printUsage() {
  console.log([
    "Localmodal encrypted credentials (Windows DPAPI)",
    "",
    "  npm run credentials -- set MODAL_TOKEN_ID",
    "  npm run credentials -- set MODAL_TOKEN_SECRET",
    "  npm run credentials -- set MODAL_PROXY_TOKEN",
    "  npm run credentials -- list",
    "  npm run credentials -- remove NAME",
    "  npm run credentials -- run -- npm run test:integration:live",
    "",
    `Store: ${credentialStorePath()}`,
  ].join("\n"));
}

const isMain = process.argv[1] && path.resolve(fileURLToPath(import.meta.url)) === path.resolve(process.argv[1]);
if (isMain) {
  main(process.argv.slice(2)).catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}