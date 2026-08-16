import * as path from "node:path";
import * as vscode from "vscode";
import type { Endpoint } from "../backend/types";
import { QWEN38_27B } from "../models/qwen";
import { DEPLOYMENT_DEFAULTS } from "../product";
import type { SecretStore } from "../state/types";

export class LocalmodalMcpProvider
  implements vscode.McpServerDefinitionProvider<vscode.McpStdioServerDefinition>
{
  public constructor(
    private readonly extensionPath: string,
    private readonly secrets: SecretStore,
    private readonly profileId: () => string,
    private readonly getEndpoint?: () => Endpoint | undefined,
  ) {}

  public provideMcpServerDefinitions(): vscode.McpStdioServerDefinition[] {
    return [this.definition()];
  }

  public async resolveMcpServerDefinition(
    server: vscode.McpStdioServerDefinition,
    _token: vscode.CancellationToken,
  ): Promise<vscode.McpStdioServerDefinition> {
    const proxyToken = await this.secrets.get("modalProxyToken");
    const currentEndpoint = this.getEndpoint?.();
    const appName = process.env.LOCALMODAL_TEST_APP_NAME ?? DEPLOYMENT_DEFAULTS.appName;

    server.cwd = vscode.Uri.file(this.extensionPath);
    server.env = {
      ...server.env,
      LOCALMODAL_DEPLOYMENT_ROOT: this.extensionPath,
      LOCALMODAL_APP_NAME: appName,
      LOCALMODAL_GPU: DEPLOYMENT_DEFAULTS.gpu,
      LOCALMODAL_DEPLOYMENT_FILE: DEPLOYMENT_DEFAULTS.deploymentFile,
      LOCALMODAL_MODAL_COMMAND: DEPLOYMENT_DEFAULTS.modalCommand,
      LOCALMODAL_CONTEXT_PROFILE: this.profileId(),
      LOCALMODAL_MODEL_ID: QWEN38_27B.id,
      LOCALMODAL_MODEL_REVISION: QWEN38_27B.revision,
      ...(currentEndpoint ? { LOCALMODAL_ENDPOINT: currentEndpoint.baseUrl } : {}),
      ...(proxyToken ? { MODAL_PROXY_TOKEN: proxyToken } : {}),
    };
    return server;
  }

  private definition(): vscode.McpStdioServerDefinition {
    return new vscode.McpStdioServerDefinition(
      "localmodal",
      process.execPath,
      [path.join(this.extensionPath, "dist", "mcp.js")],
      {},
      "0.0.1",
    );
  }
}