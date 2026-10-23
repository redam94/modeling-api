/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv, type Plugin } from "vite";

import { bundleBudget, MOTION_PATTERNS, SIGNED_IN_ONLY } from "./bundle-budget";
import { canonicalHost } from "./canonical-host";
import { buildAppCsp, type AppCspOptions } from "./csp";
import { DEV_PREBUNDLE } from "./dev-prebundle";

const HOST = "127.0.0.1";
const PORT = 5173;

const PNPM = String.raw`node_modules\/(?:\.pnpm\/[^/]+\/node_modules\/)?`;
const pkg = (names: string) => new RegExp(`${PNPM}(?:${names})\\/`);

// v0.18 (plan D27, I-8): `MOTION_PATTERNS` (the animation library and lib/motion.tsx) stay out
// of the entry; `SIGNED_IN_ONLY` (those plus the onboarding, library and extension features,
// the interactive controls and the illustrations) stay out of share links and the portal.

/** What no lazy signed-in chunk may pull in statically (they have their own lazy chunks). */
const HEAVY = [pkg("monaco-editor|monaco-yaml"), pkg("apache-arrow"), pkg("@xyflow")];

/**
 * v0.16: what a share link (`/s#…`) and the client portal download. Each budget covers the lazy
 * chunk and everything it imports statically (the entry included), and lists what must never be
 * in that closure: the expert app's router, tables, editors, Arrow and the workflow canvas, and
 * the signed-in session middleware (share links send no cookie, CSRF or project header).
 */
const SHARE_FORBIDDEN = [
  pkg("@tanstack\\/react-router"),
  pkg("@tanstack\\/react-table"),
  /\/src\/api\/queries\.ts$/,
  /\/src\/api\/client\.ts$/,
  /\/src\/components\/shell\//,
  pkg("monaco-editor|monaco-yaml"),
  pkg("apache-arrow"),
  pkg("@xyflow"),
  ...SIGNED_IN_ONLY,
];

/**
 * What every page may download before first paint: the entry chunk and its static imports.
 * Monaco, monaco-yaml and Arrow load only on the pages that use them (lazy routes + import()).
 * The entry only picks the surface (share link, public pages, signed-in app) and loads it lazily.
 */
const ENTRY_BUDGET = {
  maxEntryBytes: 640 * 1024,
  // M3.5: React Flow (the workflow canvas) loads only with the workflow editor and run graph.
  // v0.18 (critic, D27): the animation library is in lazy signed-in chunks only.
  forbidden: [/node_modules\/(?:\.pnpm\/[^/]+\/node_modules\/)?(?:monaco-editor|monaco-yaml|apache-arrow|@xyflow)\//, ...MOTION_PATTERNS],
  chunkBudgets: [
    // The readout page, the dashboards client and the shared radix/tailwind-merge chunk; 263 KiB at v0.16.
    { match: /(?:^|\/)share-app-[^/]*\.js$/, maxBytes: 288 * 1024, forbidden: SHARE_FORBIDDEN },
    // The portal brings its own small router; it loads from the workspace gate, which already
    // holds the session client and `api/queries.ts` (so neither is forbidden here).
    {
      match: /(?:^|\/)client-app-[^/]*\.js$/,
      maxBytes: 260 * 1024,
      forbidden: [pkg("@tanstack\\/react-table"), /\/src\/components\/shell\//, pkg("monaco-editor|monaco-yaml"), pkg("apache-arrow"), pkg("@xyflow"), ...SIGNED_IN_ONLY],
    },
    // v0.17: the lazy parts of the layout 2 readout (plan D18): the heavy panels and the
    // results of the inputs, then each input section with what it adds (the shared charts
    // included). Share links load them too, so they have the share link's forbidden list.
    { match: /(?:^|\/)readout-extra-[^/]*\.js$/, maxBytes: 48 * 1024, forbidden: SHARE_FORBIDDEN },
    { match: /(?:^|\/)try-a-case-[^/]*\.js$/, maxBytes: 48 * 1024, forbidden: SHARE_FORBIDDEN },
    { match: /(?:^|\/)check-accuracy-[^/]*\.js$/, maxBytes: 48 * 1024, forbidden: SHARE_FORBIDDEN },
    { match: /(?:^|\/)score-file-[^/]*\.js$/, maxBytes: 48 * 1024, forbidden: SHARE_FORBIDDEN },
    // v0.18 (I-8): the lazy signed-in chunks. Each budget applies once its module is in the
    // build (a feature mounted later in the wave); from then on the chunk must exist under
    // its name, so the module cannot slip into the entry. Motion lives in the tour host.
    { match: /(?:^|\/)tour-host-[^/]*\.js$/, maxBytes: 48 * 1024, forbidden: HEAVY, requiredWhen: /\/src\/features\/onboarding\/tour-host\.tsx$/ },
    {
      match: /(?:^|\/)(?:start-here|checklist)-[^/]*\.js$/,
      maxBytes: 48 * 1024,
      forbidden: HEAVY,
      requiredWhen: /\/src\/features\/onboarding\/(?:start-here|checklist)\.tsx$/,
    },
    // 72 KiB in the plan; 84 KiB after the Catalog mount (demo grid 40 KiB + sliders 14 KiB), E1.
    { match: /(?:^|\/)playground-[^/]*\.js$/, maxBytes: 84 * 1024, forbidden: HEAVY, requiredWhen: /\/src\/features\/onboarding\/playground\.tsx$/ },
    {
      match: /(?:^|\/)recipe-playground-[^/]*\.js$/,
      maxBytes: 32 * 1024,
      // plot-data (Arrow) comes with the Catalog page that opens the playground (usePlotExample).
      forbidden: [pkg("monaco-editor|monaco-yaml"), pkg("@xyflow")],
      requiredWhen: /\/src\/features\/plots\/recipe-playground\.tsx$/,
    },
    // Make your own: Monaco stays its own lazy chunk (the YAML tab loads it with import()).
    { match: /(?:^|\/)library-[^/]*\.js$/, maxBytes: 160 * 1024, forbidden: HEAVY, requiredWhen: /\/src\/routes\/library\.tsx$/ },
    // v0.19 (D20, I-8): uploaded code (upload, plugin, version and review pages). Recipe examples
    // draw through the plot sandbox from their own lazy chunk (plot data brings Arrow).
    { match: /(?:^|\/)plugins-[^/]*\.js$/, maxBytes: 96 * 1024, forbidden: HEAVY, requiredWhen: /\/src\/routes\/plugins\.tsx$/ },
  ],
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
    optimizeDeps: { include: [...DEV_PREBUNDLE] },
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
