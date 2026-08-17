import * as vscode from "vscode";

export class LocalmodalMcpProvider
  implements vscode.McpServerDefinitionProvider<vscode.McpHttpServerDefinition>
{
  public constructor(
    private readonly uri: vscode.Uri,
    private readonly authorizationHeader: string,
  ) {}

  public provideMcpServerDefinitions(): vscode.McpHttpServerDefinition[] {
    return [new vscode.McpHttpServerDefinition(
      "localmodal",
      this.uri,
      { Authorization: this.authorizationHeader },
      "0.0.1",
    )];
  }
}