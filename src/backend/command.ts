import { existsSync, readdirSync } from "node:fs";
import * as path from "node:path";

export interface CommandEnvironment {
  platform: NodeJS.Platform;
  env: NodeJS.ProcessEnv;
  exists: (candidate: string) => boolean;
  directories: (root: string) => string[];
}

export function resolveModalCommand(
  command: string,
  environment: Partial<CommandEnvironment> = {},
): string {
  const platform = environment.platform ?? process.platform;
  const env = environment.env ?? process.env;
  const exists = environment.exists ?? existsSync;
  const directories = environment.directories ?? listDirectories;

  if (command !== "modal" || isAbsolute(command, platform)) {
    return command;
  }

  for (const candidate of modalCandidates(platform, env, directories)) {
    if (exists(candidate)) {
      return candidate;
    }
  }
  return command;
}

export function modalCandidates(
  platform: NodeJS.Platform,
  env: NodeJS.ProcessEnv,
  directories: (root: string) => string[] = listDirectories,
): string[] {
  if (platform !== "win32") {
    return [];
  }

  const roots = [
    env.APPDATA ? path.join(env.APPDATA, "Python") : undefined,
    env.LOCALAPPDATA ? path.join(env.LOCALAPPDATA, "Programs", "Python") : undefined,
  ].filter((root): root is string => Boolean(root));

  const candidates: string[] = [];
  for (const root of roots) {
    for (const directory of directories(root).filter((entry) => /^Python\d+$/i.test(entry)).sort().reverse()) {
      candidates.push(path.join(root, directory, "Scripts", "modal.exe"));
    }
  }
  return candidates;
}

function isAbsolute(command: string, platform: NodeJS.Platform): boolean {
  return platform === "win32" ? path.win32.isAbsolute(command) : path.posix.isAbsolute(command);
}

function listDirectories(root: string): string[] {
  try {
    return readdirSync(root, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name);
  } catch {
    return [];
  }
}

export function modalProcessEnvironment(
  environment: NodeJS.ProcessEnv = process.env,
): NodeJS.ProcessEnv {
  const sanitized = { ...environment };
  delete sanitized.MODAL_PROXY_TOKEN;
  return {
    ...sanitized,
    PYTHONIOENCODING: "utf-8",
    PYTHONUTF8: "1",
  };
}