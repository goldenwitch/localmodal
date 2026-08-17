import { randomBytes, randomUUID, timingSafeEqual } from "node:crypto";
import { createServer, type Server as HttpServer } from "node:http";
import type { AddressInfo } from "node:net";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import type { LocalmodalRuntime } from "../runtime";
import { createLocalmodalMcpServer } from "./server";

interface ActiveSession {
  server: McpServer;
  transport: StreamableHTTPServerTransport;
}

export class LocalmodalMcpHost {
  private readonly accessToken = randomBytes(32).toString("base64url");
  private readonly activeSessions = new Set<ActiveSession>();
  private readonly sessionsById = new Map<string, ActiveSession>();
  private readonly httpServer: HttpServer;
  private address: URL | undefined;
  private closing = false;

  private constructor(
    private readonly runtime: LocalmodalRuntime,
    private readonly modelId: string,
    private readonly profileId: () => string,
    private readonly report: (message: string) => void,
  ) {
    this.httpServer = createServer((request, response) => {
      void this.handleRequest(request, response);
    });
  }

  public static async start(
    runtime: LocalmodalRuntime,
    modelId: string,
    profileId: () => string,
    report: (message: string) => void,
  ): Promise<LocalmodalMcpHost> {
    const host = new LocalmodalMcpHost(runtime, modelId, profileId, report);
    await new Promise<void>((resolve, reject) => {
      host.httpServer.once("error", reject);
      host.httpServer.listen(0, "127.0.0.1", () => {
        host.httpServer.off("error", reject);
        resolve();
      });
    });
    const address = host.httpServer.address() as AddressInfo;
    host.address = new URL(`http://127.0.0.1:${address.port}/mcp`);
    report(`MCP control surface listening at ${host.address.origin}`);
    return host;
  }

  public get url(): URL {
    if (!this.address) {
      throw new Error("The localmodal MCP host has not started.");
    }
    return this.address;
  }

  public get authorizationHeader(): string {
    return `Bearer ${this.accessToken}`;
  }

  public async close(): Promise<void> {
    if (this.closing) {
      return;
    }
    this.closing = true;
    await Promise.allSettled(
      [...this.activeSessions].flatMap(({ server, transport }) => [
        transport.close(),
        server.close(),
      ]),
    );
    this.activeSessions.clear();
    this.sessionsById.clear();
    await new Promise<void>((resolve, reject) => {
      this.httpServer.close((error) => (error ? reject(error) : resolve()));
    });
  }

  private async handleRequest(
    request: import("node:http").IncomingMessage,
    response: import("node:http").ServerResponse,
  ): Promise<void> {
    if (this.closing) {
      response.writeHead(503).end();
      return;
    }
    if (request.url !== "/mcp") {
      response.writeHead(404).end();
      return;
    }
    if (!this.isAuthorized(request.headers.authorization)) {
      response.writeHead(401, { "WWW-Authenticate": "Bearer" }).end();
      return;
    }

    const sessionIdHeader = request.headers["mcp-session-id"];
    const sessionId = Array.isArray(sessionIdHeader) ? sessionIdHeader[0] : sessionIdHeader;
    let activeSession = sessionId ? this.sessionsById.get(sessionId) : undefined;
    let created = false;

    if (sessionId && !activeSession) {
      response.writeHead(404, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        jsonrpc: "2.0",
        error: { code: -32001, message: "MCP session not found" },
        id: null,
      }));
      return;
    }

    if (!activeSession) {
      created = true;
      let session: ActiveSession;
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => randomUUID(),
        onsessioninitialized: (initializedSessionId) => {
          this.sessionsById.set(initializedSessionId, session);
        },
      });
      const server = createLocalmodalMcpServer({
        runtime: this.runtime,
        modelId: this.modelId,
        profileId: this.profileId,
      });
      session = { server, transport };
      activeSession = session;
      this.activeSessions.add(session);
      transport.onclose = () => this.removeSession(session);
    }

    try {
      if (created) {
        await activeSession.server.connect(activeSession.transport);
      }
      await activeSession.transport.handleRequest(request, response);
      if (created && !activeSession.transport.sessionId) {
        this.removeSession(activeSession);
        await Promise.allSettled([
          activeSession.transport.close(),
          activeSession.server.close(),
        ]);
      }
    } catch (error) {
      this.report(`MCP request failed: ${error instanceof Error ? error.message : String(error)}`);
      if (!response.headersSent) {
        response.writeHead(500, { "Content-Type": "application/json" });
        response.end(JSON.stringify({
          jsonrpc: "2.0",
          error: { code: -32603, message: "Internal server error" },
          id: null,
        }));
      }
      if (created) {
        this.removeSession(activeSession);
        await Promise.allSettled([
          activeSession.transport.close(),
          activeSession.server.close(),
        ]);
      }
    }
  }

  private removeSession(session: ActiveSession): void {
    this.activeSessions.delete(session);
    if (session.transport.sessionId) {
      this.sessionsById.delete(session.transport.sessionId);
    }
  }

  private isAuthorized(value: string | undefined): boolean {
    if (!value) {
      return false;
    }
    const actual = Buffer.from(value);
    const expected = Buffer.from(this.authorizationHeader);
    return actual.length === expected.length && timingSafeEqual(actual, expected);
  }
}
