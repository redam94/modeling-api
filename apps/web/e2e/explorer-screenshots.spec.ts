/**
 * Screenshots of the M3.5 guides (docs/guides/data-explorer.md, model-explorer.md and
 * docs/concepts/about-and-choosing.md), taken from mock mode like the Guided tutorial's.
 * Skipped unless MAPI_EXPLORER_SCREENSHOTS=1; `make docs-screenshots-explorers` runs it and
 * shrinks the PNGs into docs/assets/screenshots/explorer-*.png:
 *
 *   MAPI_EXPLORER_SCREENSHOTS=1 MAPI_SMOKE_WEB_PORT=15173 MAPI_SMOKE_PLOTS_PORT=15174 \
 *     pnpm --filter @mapi/web exec playwright test e2e/explorer-screenshots.spec.ts
 *
 * Raw captures land in .local/screenshots-explorers/.
 */
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

const DATASET = "ds_01jaf3a1b2c3d4e5f6g7h8j9k0";
const MMM = "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0";
const VECM = "mdl_01jaf3m35vecm0000000001";
const OUT = (name: string) => fileURLToPath(new URL(`../../../.local/screenshots-explorers/explorer-${name}.png`, import.meta.url));

test.skip(!process.env.MAPI_EXPLORER_SCREENSHOTS, "set MAPI_EXPLORER_SCREENSHOTS=1 to refresh the explorer screenshots");
test.use({ viewport: { width: 1280, height: 800 }, colorScheme: "light" });

async function settle(page: Page) {
  // every plot iframe has finished rendering when its spinner is gone
  await expect(page.locator("figure .animate-spin")).toHaveCount(0, { timeout: 20_000 });
  await page.waitForTimeout(500);
}

/**
 * Plot frames are cross-origin iframes, which a full-page capture leaves blank below the
 * viewport: the viewport grows to the page's height instead, and shrinks back afterwards.
 */
async function shot(page: Page, name: string, fullPage = true) {
  await settle(page);
  if (!fullPage) {
    await page.screenshot({ path: OUT(name) });
    return;
  }
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  await page.setViewportSize({ width: 1280, height: Math.min(4000, Math.max(800, height)) });
  await settle(page);
  await page.waitForTimeout(800);
  await page.screenshot({ path: OUT(name) });
  await page.setViewportSize({ width: 1280, height: 800 });
}

async function mode(page: Page, m: "Guided" | "Expert") {
  await page.getByRole("radiogroup", { name: "Mode" }).getByRole("radio", { name: m }).click();
}

test("explorer screenshots", async ({ page }) => {
  test.setTimeout(240_000);

  // Help me choose
  await page.goto("/choose?goal=explain_marketing&data_shape=panel&uncertainty=essential");
  await expect(page.getByRole("list", { name: "Recommendations" }).getByRole("listitem").first()).toBeVisible();
  await page.getByRole("button", { name: "Next" }).click();
  await shot(page, "chooser", false);

  // the About browser and a card
  await page.goto("/catalog?about=model%3Ats.sarimax");
  await expect(page.getByRole("dialog")).toBeVisible();
  await shot(page, "about-card", false);
  await page.keyboard.press("Escape");
  await page.goto("/catalog");
  await expect(page.getByRole("list", { name: "Catalog items" })).toBeVisible();
  await shot(page, "catalog-browse", false);

  // Data Explorer (Expert), with the deeper sections computed
  await page.goto(`/datasets/${DATASET}/versions/1/explore?section=stationarity`);
  await page.getByTestId("section-missing").getByRole("button", { name: "Compute it now" }).click();
  await expect(page.getByRole("table", { name: "Stationarity by series" })).toBeVisible({ timeout: 15_000 });
  await shot(page, "data-stationarity");
  const nav = page.getByRole("navigation", { name: "Data Explorer sections" });
  await nav.getByRole("button", { name: /Trend and season/ }).click();
  await shot(page, "data-stl");
  await nav.getByRole("button", { name: /Leads and lags/ }).click();
  await shot(page, "data-ccf");
  await nav.getByRole("button", { name: /Correlations/ }).click();
  await shot(page, "data-correlation");
  // Guided: the plain reading first
  await mode(page, "Guided");
  await page.goto(`/datasets/${DATASET}/versions/1/explore?section=overview`);
  await expect(page.getByRole("heading", { name: "About this dataset" })).toBeVisible();
  await shot(page, "data-overview-guided");
  await mode(page, "Expert");

  // Model Explorer
  await page.goto(`/models/${MMM}/versions/1/explore`);
  await expect(page.getByRole("heading", { name: "At a glance" })).toBeVisible();
  await shot(page, "model-summary");
  await page.goto(`/models/${MMM}/versions/1/explore?section=params`);
  await expect(page.getByRole("table", { name: "Parameters" })).toBeVisible();
  await shot(page, "model-params");
  await page.goto(`/models/${MMM}/versions/1/explore?section=residuals`);
  await shot(page, "model-residuals");
  await page.goto(`/models/${MMM}/versions/1/explore?section=response_curves`);
  await shot(page, "model-response-curves");
  await page.goto(`/models/${MMM}/versions/1/explore?section=governance`);
  await expect(page.getByRole("list", { name: "Check results" })).toBeVisible();
  await shot(page, "model-governance");
  await page.goto(`/models/${VECM}/versions/1/explore?section=irf`);
  await shot(page, "model-irf");

  // comparison
  await page.goto("/models/compare?refs=%5B%22model%3A%2F%2Fdefault%2Fsales-arimax%40v2%22%2C%22model%3A%2F%2Fdefault%2Fsales-arimax%40v1%22%2C%22model%3A%2F%2Fdefault%2Fsales-ets%40v1%22%5D");
  await page.getByRole("button", { name: /^Compare 3 versions/ }).click();
  await expect(page.getByRole("table", { name: "Diebold-Mariano tests" })).toBeVisible({ timeout: 15_000 });
  await shot(page, "model-compare");

  // budget optimisation
  await page.goto(`/models/${MMM}/versions/1/optimize`);
  await page.getByRole("button", { name: "Optimise the budget" }).click();
  await expect(page.getByTestId("plan-result")).toBeVisible({ timeout: 15_000 });
  await shot(page, "optimize");

  // dark theme
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto(`/models/${MMM}/versions/1/explore?section=fit`);
  await shot(page, "model-fit-dark", false);
});
