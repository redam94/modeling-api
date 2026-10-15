/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv, type Plugin } from "vite";

import { buildAppCsp, type AppCspOptions } from "./csp";

const HOST = "127.0.0.1";
const PORT = 5173;

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
    plotsOrigin: env.VITE_PLOTS_ORIGIN || "http://127.0.0.1:5174",
    storageOrigins: (env.VITE_STORAGE_ORIGINS || "http://127.0.0.1:4443").split(",").map((s) => s.trim()).filter(Boolean),
  };
  const headers = {
    "Content-Security-Policy": buildAppCsp({ ...csp, header: true }),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
  };
  return {
    plugins: [react(), tailwindcss(), cspMeta(csp)],
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
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (id.includes("node_modules/monaco-editor") || id.includes("node_modules/monaco-yaml")) {
              return "monaco";
            }
            if (id.includes("node_modules/apache-arrow")) return "arrow";
            return undefined;
          },
        },
      },
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
