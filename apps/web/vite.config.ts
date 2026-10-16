/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv, type Plugin } from "vite";

import { bundleBudget } from "./bundle-budget";
import { canonicalHost } from "./canonical-host";
import { buildAppCsp, type AppCspOptions } from "./csp";

const HOST = "127.0.0.1";
const PORT = 5173;

/**
 * What every page may download before first paint: the entry chunk and its static imports.
 * Monaco, monaco-yaml and Arrow load only on the pages that use them (lazy routes + import()).
 */
const ENTRY_BUDGET = {
  maxEntryBytes: 640 * 1024,
  // M3.5: React Flow (the workflow canvas) loads only with the workflow editor and run graph.
  forbidden: [/node_modules\/(?:\.pnpm\/[^/]+\/node_modules\/)?(?:monaco-editor|monaco-yaml|apache-arrow|@xyflow)\//],
};

function cspMeta(opts: AppCspOptions): Plugin {
  return {
      name: "mapi-app-csp",
      apply: "build",
      transformIndexHtml() {
        return [{ tag: "meta", attrs: { "http-equiv": "Content-Security-Policy", content: buildAppCsp(opts) }, injectTo: "head-prepend" }];
      },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  const csp: AppCspOptions = {
    apiBase: env.VITE_API_BASE || "http://127.0.0.1:8000",
    ...(env.VITE_AUTH_BASE ? { authBase: env.VITE_AUTH_BASE } : {}),
    plotsOrigin: env.VITE_PLOTS_ORIGIN || "http://127.0.0.1:5174",
    storageOrigins: (env.VITE_STORAGE_ORIGINS || "http://127.0.0.1:4443").split(",").map((s) => s.trim()).filter(Boolean),
  };
  const headers = {
    "Content-Security-Policy": buildAppCsp({ ...csp, header: true }),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
  };
  return {
    plugins: [react(), tailwindcss(), canonicalHost(HOST), cspMeta(csp), bundleBudget(ENTRY_BUDGET)],
    resolve: { alias: { "@": new URL("./src", import.meta.url).pathname } },
    // `--mode mock` serves the MSW service worker (mock-public/) and enables the mocks;
    // production builds never contain it.
    publicDir: mode === "mock" ? "mock-public" : "public",
    server: { host: HOST, port: PORT, strictPort: true },
    preview: { host: HOST, port: PORT, strictPort: true, headers },
    build: {
      target: "es2022",
      sourcemap: false,
      chunkSizeWarningLimit: 4096,
    },
    worker: { format: "es" },
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
      css: false,
      restoreMocks: true,
    },
  };
});
