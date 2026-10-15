/// <reference types="vitest/config" />
import { defineConfig, loadEnv, type Plugin } from "vite";

import { buildCsp, parseOrigins, sandboxHeaders } from "./src/csp";

const HOST = "127.0.0.1";
const PORT = 5174;
const DEFAULT_APP_ORIGINS = ["http://127.0.0.1:5173", "http://localhost:5173"];

function cspMeta(csp: string): Plugin {
  return {
    name: "mapi-csp-meta",
    transformIndexHtml(html) {
      if (!html.includes("%MAPI_CSP%")) throw new Error("index.html lost its CSP placeholder");
      return html.replace("%MAPI_CSP%", csp);
    },
  };
}

export default defineConfig(({ mode, command, isPreview }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  const appOrigins = parseOrigins(env.VITE_APP_ORIGINS, DEFAULT_APP_ORIGINS);
  const selfOrigin = `http://${HOST}:${PORT}`;
  const dev = command === "serve" && !isPreview;
  const cspOpts = { appOrigins, dev, selfOrigin };
  const headers = sandboxHeaders(cspOpts);
  return {
    plugins: [cspMeta(buildCsp(cspOpts))],
    server: { host: HOST, port: PORT, strictPort: true, cors: false, headers },
    preview: { host: HOST, port: PORT, strictPort: true, cors: false, headers },
    build: {
      target: "es2022",
      sourcemap: false,
      modulePreload: { polyfill: false },
      assetsInlineLimit: 0,
      chunkSizeWarningLimit: 6144,
    },
    test: {
      environment: "jsdom",
      include: ["src/**/*.test.ts"],
    },
  };
});
