import { defineConfig, devices } from "@playwright/test";

/**
 * Two suites:
 * - default (`pnpm test:smoke`, part of `pnpm test`): the app in `--mode mock` (MSW) plus the
 *   real plot sandbox on its own origin. No backend needed.
 * - `@e2e` (`pnpm e2e`, sets MAPI_E2E=1): the app against a running control plane
 *   (VITE_API_BASE, default http://127.0.0.1:8000). Never run by default.
 *
 * The smoke suite normally serves on 5173/5174. When those ports are taken (a `make dev`
 * stack, another checkout), set MAPI_SMOKE_WEB_PORT / MAPI_SMOKE_PLOTS_PORT: the mock build
 * then points at the sandbox on that port and the sandbox admits the app's origin.
 */
const e2e = Boolean(process.env.MAPI_E2E);
const sandboxDir = new URL("../plot-sandbox/", import.meta.url).pathname;
const smokeWebPort = Number(process.env.MAPI_SMOKE_WEB_PORT || 5173);
const smokePlotsPort = Number(process.env.MAPI_SMOKE_PLOTS_PORT || 5174);
const customPorts = smokeWebPort !== 5173 || smokePlotsPort !== 5174;
// The @e2e suite can reuse a running `make dev` stack on other ports (MAPI_WEB_ORIGIN /
// MAPI_PLOTS_ORIGIN, as scripts/dev.py uses them).
const webOrigin = (e2e && process.env.MAPI_WEB_ORIGIN) || `http://127.0.0.1:${e2e ? 5173 : smokeWebPort}`;
const plotsOrigin = (e2e && process.env.MAPI_PLOTS_ORIGIN) || `http://127.0.0.1:${e2e ? 5174 : smokePlotsPort}`;
// Read by the specs (frame src and CSP assertions).
process.env.MAPI_SMOKE_PLOTS_ORIGIN = plotsOrigin;

const smokeWeb = customPorts
  ? // Both steps need the sandbox origin: the build bakes it into the CSP meta tag and the
    // client, preview sends it in the CSP header.
    `VITE_PLOTS_ORIGIN=${plotsOrigin} pnpm exec vite build --mode mock --outDir dist/mock --emptyOutDir && VITE_PLOTS_ORIGIN=${plotsOrigin} pnpm exec vite preview --mode mock --outDir dist/mock --port ${smokeWebPort}`
  : "pnpm exec vite build --mode mock --outDir dist/mock --emptyOutDir && pnpm exec vite preview --mode mock --outDir dist/mock";

export default defineConfig({
  testDir: "e2e",
  // @e2e signs in to the stack once (dev profile) and reuses the session in every test.
  ...(e2e ? { globalSetup: "./e2e/global-setup.ts" } : {}),
  ...(e2e ? { grep: /@e2e/ } : { grepInvert: /@e2e/ }),
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  timeout: 45_000,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: webOrigin,
    ...(e2e ? { storageState: "e2e/.auth/state.json" } : {}),
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      // The smoke suite runs the production build (CSP + Trusted Types enforced) in mock mode.
      command: e2e ? "pnpm exec vite" : smokeWeb,
      url: webOrigin,
      reuseExistingServer: !process.env.CI && !customPorts,
      timeout: 180_000,
    },
    {
      command: customPorts ? `VITE_APP_ORIGINS=${webOrigin} pnpm exec vite --port ${smokePlotsPort}` : "pnpm exec vite",
      cwd: sandboxDir,
      url: plotsOrigin,
      reuseExistingServer: !process.env.CI && !customPorts,
      timeout: 120_000,
    },
  ],
});
