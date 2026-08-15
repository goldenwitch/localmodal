import * as vscode from "vscode";
import type { SecretStore, StateStore } from "./types";
import { normalizeModalProxyToken, resolveModalProxyToken } from "./token";

const PROXY_TOKEN_SETTINGS_URL = "https://modal.com/settings/proxy-auth-tokens";

export class VscodeStateStore implements StateStore {
  public constructor(private readonly memento: vscode.Memento) {}

  public get<T>(key: string, defaultValue: T): T {
    return this.memento.get<T>(key, defaultValue) ?? defaultValue;
  }

  public update<T>(key: string, value: T): Thenable<void> {
    return this.memento.update(key, value);
  }
}

export class VscodeSecretStore implements SecretStore {
  public constructor(private readonly storage: vscode.SecretStorage) {}

  public async get(key: string): Promise<string | undefined> {
    if (key !== "modalProxyToken") {
      return this.storage.get(key);
    }
    return resolveModalProxyToken({
      stored: () => this.storage.get(key),
      environment: () => process.env.MODAL_PROXY_TOKEN,
      prompt: () => this.promptForToken(),
      store: (value) => this.storage.store(key, value),
    });
  }

  public async configureProxyToken(): Promise<boolean> {
    const token = await this.promptForToken();
    if (!token) {
      return false;
    }
    await this.storage.store("modalProxyToken", token);
    return true;
  }

  public store(key: string, value: string): Thenable<void> {
    return this.storage.store(key, value);
  }

  public delete(key: string): Thenable<void> {
    return this.storage.delete(key);
  }

  private async promptForToken(): Promise<string | undefined> {
    const openAction = await vscode.window.showInformationMessage(
      "Create a Modal Proxy Token in Settings > Proxy Tokens, then enter its ID and secret here.",
      "Open Modal Token Settings",
    );
    if (openAction) {
      await vscode.env.openExternal(vscode.Uri.parse(PROXY_TOKEN_SETTINGS_URL));
    }

    const tokenIdOrCombined = await vscode.window.showInputBox({
      title: "Modal Proxy Token ID",
      prompt: "Paste wk-... or a combined wk-...ws-... token. Two values separated by whitespace are also accepted.",
      ignoreFocusOut: true,
      validateInput: (value) => {
        if (!value.trim()) {
          return "Enter a Modal Proxy Token ID or combined token.";
        }
        try {
          normalizeModalProxyToken(value);
          return undefined;
        } catch (error) {
          if (value.includes("." ) || value.trim().split(/\s+/).length === 2) {
            return error instanceof Error ? error.message : String(error);
          }
          return value.trim().startsWith("wk-") ? undefined : "Token ID must start with wk-.";
        }
      },
    });
    if (!tokenIdOrCombined?.trim()) {
      return undefined;
    }

    try {
      return normalizeModalProxyToken(tokenIdOrCombined);
    } catch (error) {
      // A plain wk-... value is the two-field form; the second field is masked.
      if (!tokenIdOrCombined.trim().startsWith("wk-")) {
        throw error;
      }
    }

    const tokenSecret = await vscode.window.showInputBox({
      title: "Modal Proxy Token Secret",
      prompt: "Paste the ws-... secret. Modal shows it only once.",
      password: true,
      ignoreFocusOut: true,
      validateInput: (value) => value.trim().startsWith("ws-") ? undefined : "Token secret must start with ws-.",
    });
    if (!tokenSecret?.trim()) {
      return undefined;
    }
    return normalizeModalProxyToken(tokenIdOrCombined, tokenSecret);
  }
}