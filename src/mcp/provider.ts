import * as path from "node:path";
import * as vscode from "vscode";
import { ModelController } from "../controller";
import { QWEN38_27B } from "../models/qwen";
import type { SecretStore } from "../state/types";

export class LocalmodalMcpProvider
  implements vscode.McpServerDefinitionProvider<vscode.McpStdioServerDefinition>
{
  public constructor(
    private readonly extensionPath: string,
    private readonly controller: ModelController,
    private readonly secrets: SecretStore,
    private readonly profileId: () => string,
  ) {}

  public provideMcpServerDefinitions(): vscode.McpStdioServerDefinition[] {
    return [this.definition()];
  }

  public async resolveMcpServerDefinition(
    server: vscode.McpStdioServerDefinition,
    _token: vscode.CancellationToken,
  ): Promise<vscode.McpStdioServerDefinition> {
    const endpoint = await this.controller.ensureReady(QWEN38_27B.id, this.profileId());
    const proxyToken = await this.secrets.get("modalProxyToken");
    if (!proxyToken) {
      throw new Error("A Modal Proxy Token is required to start the localmodal MCP server.");
    }

    server.cwd = vscode.Uri.file(this.extensionPath);
    server.env = {
      ...server.env,
      LOCALMODAL_ENDPOINT: endpoint.baseUrl,
      LOCALMODAL_MODEL_ID: endpoint.modelId,
      MODAL_PROXY_TOKEN: proxyToken,
    };
    return server;
  }

  private definition(): vscode.McpStdioServerDefinition {
    return new vscode.McpStdioServerDefinition(
      "localmodal inference validation",
      process.execPath,
      [path.join(this.extensionPath, "dist", "mcp.js")],
      {},
      "0.0.1",
    );
  }
}