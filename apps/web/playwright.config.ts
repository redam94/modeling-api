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
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: e2e ? "pnpm exec vite" : "pnpm exec vite --mode mock",
      url: "http://127.0.0.1:5173",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: "pnpm exec vite",
      cwd: sandboxDir,
      url: "http://127.0.0.1:5174",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
});
