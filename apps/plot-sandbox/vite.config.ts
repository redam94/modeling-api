/// <reference types="vitest/config" />
import type { IncomingMessage, ServerResponse } from "node:http";

import { defineConfig, loadEnv, type Plugin } from "vite";

import {
  buildCsp,
  buildEmbedCsp,
  embedHeaders,
  parseApiUrl,
  parseOrigins,
  sandboxHeaders,
  type CspOptions,
  type EmbedCspOptions,
} from "./src/csp";

const HOST = "127.0.0.1";
const PORT = 5174;
const DEFAULT_APP_ORIGINS = ["http://127.0.0.1:5173", "http://localhost:5173"];
const DEFAULT_API_URL = "http://127.0.0.1:8000";
/** `/embed/<token>` is served by embed.html (the token is read from the path). */
const EMBED_ROUTE = /^\/embed\/[^/?#]+\/?$/;

type Next = (err?: unknown) => void;

/** Fill the CSP (and API origin) placeholders of each page. */
function pageMeta(csp: string, embedCsp: string, apiUrl: string): Plugin {
  return {
    name: "mapi-csp-meta",
    transformIndexHtml(html, ctx) {
      if (ctx.filename.endsWith("embed.html") || ctx.path.startsWith("/embed")) {
        if (!html.includes("%EMBED_CSP%")) throw new Error("embed.html lost its CSP placeholder");
        return html.replace("%EMBED_CSP%", embedCsp).replace("%MAPI_API_URL%", apiUrl);
      }
      if (!html.includes("%MAPI_CSP%")) throw new Error("index.html lost its CSP placeholder");
      return html.replace("%MAPI_CSP%", csp);
    },
  };
}

/**
 * Per-path response headers (the sandbox policy, or the embed page's), and the
 * `/embed/<token>` route. Registered before Vite's own middlewares, for dev and preview.
 */
function pageRoutes(sandbox: Record<string, string>, embed: Record<string, string>): Plugin {
  const route = (req: IncomingMessage, res: ServerResponse, next: Next): void => {
    const path = (req.url ?? "/").split("?")[0] ?? "/";
    const isEmbed = EMBED_ROUTE.test(path) || path === "/embed.html";
    if (EMBED_ROUTE.test(path)) req.url = "/embed.html";
    for (const [k, v] of Object.entries(isEmbed ? embed : sandbox)) res.setHeader(k, v);
    next();
  };
  return {
    name: "mapi-sandbox-routes",
    configureServer(server) {
      server.middlewares.use(route);
    },
    configurePreviewServer(server) {
      server.middlewares.use(route);
    },
  };
}

export default defineConfig(({ mode, command, isPreview }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  const appOrigins = parseOrigins(env.VITE_APP_ORIGINS, DEFAULT_APP_ORIGINS);
  const apiUrl = parseApiUrl(env.VITE_API_URL, DEFAULT_API_URL);
  const selfOrigin = `http://${HOST}:${PORT}`;
  const dev = command === "serve" && !isPreview;
  const cspOpts: CspOptions = { appOrigins, dev, selfOrigin };
  const embedOpts: EmbedCspOptions = { apiUrl, dev, selfOrigin, frameOrigins: appOrigins };
  return {
    plugins: [
      pageMeta(buildCsp(cspOpts), buildEmbedCsp(embedOpts), apiUrl),
      pageRoutes(sandboxHeaders(cspOpts), embedHeaders(embedOpts)),
    ],
    server: { host: HOST, port: PORT, strictPort: true, cors: false },
    preview: { host: HOST, port: PORT, strictPort: true, cors: false },
    build: {
      target: "es2022",
      sourcemap: false,
      modulePreload: { polyfill: false },
      assetsInlineLimit: 0,
      chunkSizeWarningLimit: 6144,
      rollupOptions: { input: { index: "index.html", embed: "embed.html" } },
    },
    test: {
      environment: "jsdom",
      include: ["src/**/*.test.ts"],
    },
  };
});
