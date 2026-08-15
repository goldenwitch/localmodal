const esbuild = require("esbuild");

Promise.all([
  esbuild.build({
    entryPoints: ["src/extension.ts"],
    bundle: true,
    external: ["vscode"],
    format: "cjs",
    platform: "node",
    sourcemap: true,
    outfile: "dist/extension.js",
  }),
  esbuild.build({
    entryPoints: ["src/mcp/server.ts"],
    bundle: true,
    format: "cjs",
    platform: "node",
    sourcemap: true,
    outfile: "dist/mcp.js",
  }),
]).catch(() => process.exit(1));