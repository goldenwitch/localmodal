export interface ModalTokenSources {
  stored(): PromiseLike<string | undefined>;
  environment(): string | undefined;
  prompt(): PromiseLike<string | undefined>;
  store(value: string): PromiseLike<void>;
}

export async function resolveModalProxyToken(
  sources: ModalTokenSources,
): Promise<string | undefined> {
  const stored = await sources.stored();
  if (stored) {
    return stored;
  }

  const environment = sources.environment()?.trim();
  if (environment) {
    return environment;
  }

  const entered = (await sources.prompt())?.trim();
  if (!entered) {
    return undefined;
  }

  await sources.store(entered);
  return entered;
}

export function normalizeModalProxyToken(tokenIdOrCombined: string, tokenSecret?: string): string {
  const values = tokenIdOrCombined.trim().split(/\s+/).filter(Boolean);
  let tokenId = values[0] ?? "";
  let secret = tokenSecret?.trim() ?? "";

  if (!secret && values.length === 2) {
    secret = values[1];
  }

  if (!secret) {
    const separator = tokenId.indexOf(".");
    if (separator > 0) {
      secret = tokenId.slice(separator + 1);
      tokenId = tokenId.slice(0, separator);
    }
  }

  if (!tokenId.startsWith("wk-")) {
    throw new Error("Token ID must start with wk-.");
  }
  if (!secret.startsWith("ws-")) {
    throw new Error("Token secret must start with ws-.");
  }
  return `${tokenId}.${secret}`;
}