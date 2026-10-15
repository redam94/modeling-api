import { defineConfig, devices } from "@playwright/test";

/**
 * Two suites:
 * - default (`pnpm test:smoke`, part of `pnpm test`): the app in `--mode mock` (MSW) plus the
 *   real plot sandbox on its own origin. No backend needed.
 * - `@e2e` (`pnpm e2e`, sets MAPI_E2E=1): the app against a running control plane
 *   (VITE_API_BASE, default http://127.0.0.1:8000). Never run by default.
 */
const e2e = Boolean(process.env.MAPI_E2E);
const sandboxDir = new URL("../plot-sandbox/", import.meta.url).pathname;
// The @e2e suite can reuse a running `make dev` stack on other ports (MAPI_WEB_ORIGIN /
// MAPI_PLOTS_ORIGIN, as scripts/dev.py uses them); the smoke suite always uses the defaults.
const webOrigin = (e2e && process.env.MAPI_WEB_ORIGIN) || "http://127.0.0.1:5173";
const plotsOrigin = (e2e && process.env.MAPI_PLOTS_ORIGIN) || "http://127.0.0.1:5174";

export default defineConfig({
  testDir: "e2e",
  ...(e2e ? { grep: /@e2e/ } : { grepInvert: /@e2e/ }),
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  timeout: 45_000,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: webOrigin,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      // The smoke suite runs the production build (CSP + Trusted Types enforced) in mock mode.
      command: e2e
        ? "pnpm exec vite"
        : "pnpm exec vite build --mode mock --outDir dist/mock --emptyOutDir && pnpm exec vite preview --mode mock --outDir dist/mock",
      url: webOrigin,
      reuseExistingServer: !process.env.CI,
      timeout: 180_000,
    },
    {
      command: "pnpm exec vite",
      cwd: sandboxDir,
      url: plotsOrigin,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
});
