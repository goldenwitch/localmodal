const esbuild = require("esbuild");
const fs = require("node:fs");

fs.rmSync("dist/mcp.js", { force: true });
fs.rmSync("dist/mcp.js.map", { force: true });

esbuild.build({
  entryPoints: ["src/extension.ts"],
  bundle: true,
  external: ["vscode"],
  format: "cjs",
  platform: "node",
  sourcemap: true,
  outfile: "dist/extension.js",
}).catch(() => process.exit(1));