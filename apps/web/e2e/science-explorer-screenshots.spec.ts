/**
 * Screenshots of the Data Explorer on scientific data (docs/guides/data-explorer.md, part
 * "Scientific data"), taken from mock mode: the science datasets of `src/mocks/explore-sci.ts`
 * (synthetic exports of the pymc-challenges cases, ingested by the real runner). Skipped
 * unless MAPI_EXPLORER_SCREENSHOTS=1; `make docs-screenshots-explorers` runs it after
 * explorer-screenshots.spec.ts and shrinks the PNGs into
 * docs/assets/screenshots/explorer-sci-*.png (profiles, points and the section list; the images,
 * map, network, tree and roses come from the real exports: scripts/docs_screenshots_science.mjs):
 *
 *   MAPI_EXPLORER_SCREENSHOTS=1 MAPI_SMOKE_WEB_PORT=15173 MAPI_SMOKE_PLOTS_PORT=15174 \
 *     pnpm --filter @mapi/web exec playwright test e2e/science-explorer-screenshots.spec.ts
 *
 * Raw captures land in .local/screenshots-explorers/ (the folder of the other explorer
 * screenshots).
 */
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

const OUT = (name: string) => fileURLToPath(new URL(`../../../.local/screenshots-explorers/explorer-sci-${name}.png`, import.meta.url));
const ds = (c: string) => `ds-sci-${c}`;

test.skip(!process.env.MAPI_EXPLORER_SCREENSHOTS, "set MAPI_EXPLORER_SCREENSHOTS=1 to refresh the explorer screenshots");
test.use({ viewport: { width: 1280, height: 800 }, colorScheme: "light" });

async function settle(page: Page) {
  // every plot iframe has finished rendering when its spinner is gone
  await expect(page.locator("figure .animate-spin")).toHaveCount(0, { timeout: 20_000 });
  await page.waitForTimeout(600);
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
  await page.setViewportSize({ width: 1280, height: Math.min(3000, Math.max(800, height)) });
  await settle(page);
  await page.waitForTimeout(800);
  await page.screenshot({ path: OUT(name) });
  await page.setViewportSize({ width: 1280, height: 800 });
}

/** Open a section of a science dataset and wait for its picture (a plot frame or a canvas). */
async function open(page: Page, dataset: string, section: string) {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(`/datasets/${ds(dataset)}/versions/1/explore?section=${section}`);
  await expect(page.getByRole("navigation", { name: "Data Explorer sections" })).toBeVisible({ timeout: 15_000 });
  await expect(page.locator("main").locator("figure, canvas").first()).toBeVisible({ timeout: 20_000 });
  expect(errors, `page errors on ${dataset} ${section}`).toEqual([]);
}

test("science explorer screenshots", async ({ page }) => {
  test.setTimeout(240_000);

  // E79: the angular power spectrum along the multipole (log x)
  await open(page, "e79", "profiles");
  await shot(page, "profiles");
  // E77: the colour chips on the survey grid
  await open(page, "e77", "points");
  await shot(page, "points");

  // E60: the sections of a vector field, with the closed list of the others opened
  await page.goto(`/datasets/${ds("e60")}/versions/1/explore?section=structure`);
  const nav = page.getByRole("navigation", { name: "Data Explorer sections" });
  await expect(nav).toBeVisible({ timeout: 15_000 });
  await page.getByText(/^Not for this data/).first().click();
  await shot(page, "nav", false);
});
