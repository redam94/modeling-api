/**
 * Screenshots of the Guided tutorial (docs/guides/guided-tutorial.md), taken from mock mode.
 * Skipped unless MAPI_GUIDED_SCREENSHOTS=1; `make docs-screenshots-guided` runs it and shrinks
 * the PNGs into docs/assets/screenshots/guided-*.png (scripts/optimise_screenshots.py):
 *
 *   MAPI_GUIDED_SCREENSHOTS=1 MAPI_SMOKE_WEB_PORT=15173 MAPI_SMOKE_PLOTS_PORT=15174 \
 *     pnpm --filter @mapi/web exec playwright test e2e/guided-screenshots.spec.ts
 *
 * Raw captures land in .local/screenshots-guided/.
 */
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

const SAMPLE = fileURLToPath(new URL("./fixtures/guided-geo-mmm.csv", import.meta.url));
const OUT = (name: string) => fileURLToPath(new URL(`../../../.local/screenshots-guided/guided-${name}.png`, import.meta.url));

test.skip(!process.env.MAPI_GUIDED_SCREENSHOTS, "set MAPI_GUIDED_SCREENSHOTS=1 to refresh the tutorial screenshots");
test.use({ viewport: { width: 1180, height: 760 }, colorScheme: "light" });

async function shot(page: Page, name: string) {
  await page.waitForTimeout(400); // let transitions and the plot iframes settle
  await page.screenshot({ path: OUT(name), fullPage: true });
}

async function frames(page: Page) {
  // every plot iframe has finished rendering when its spinner is gone
  await expect(page.locator("figure .animate-spin")).toHaveCount(0, { timeout: 15_000 });
}

test("guided tutorial screenshots", async ({ page }) => {
  test.setTimeout(180_000);
  await page.addInitScript(() => {
    if (window.top !== window || sessionStorage.getItem("mapi.test.persona")) return;
    localStorage.setItem("mapi.mock.session", "ana");
    sessionStorage.setItem("mapi.test.persona", "1");
  });
  await page.goto("/guided");
  await expect(page.getByRole("list", { name: "Templates" })).toBeVisible();
  await shot(page, "templates");

  await page.getByRole("link", { name: /^Geo marketing mix/ }).click();
  await page.getByLabel("Name this analysis").fill("EU media mix");
  await page.getByLabel("Choose a data file").setInputFiles(SAMPLE);
  await expect(page.getByRole("complementary", { name: "Preview" })).toContainText("You answered all the questions that the model needs.");
  await shot(page, "data");
  await page.getByRole("button", { name: "Upload and continue" }).click();

  const tv = page.locator('[data-channel="tv"]');
  await expect(tv).toBeVisible();
  await tv.getByRole("checkbox").click(); // channels start without a belief
  await tv.getByLabel("From").fill("1.5");
  await tv.getByLabel("To").fill("4");
  await page.locator('[data-channel="search"]').getByRole("checkbox").click();
  await page.getByRole("button", { name: "Add an experiment" }).click();
  const exp = page.getByRole("listitem", { name: "Experiment 1" });
  await exp.getByLabel("Channel tested").selectOption("tv");
  await exp.getByLabel("Market").selectOption("de");
  await exp.getByLabel("First day").fill("2025-06-01");
  await exp.getByLabel("Last day").fill("2025-06-28");
  await exp.getByLabel("Spend change").fill("-40000");
  await exp.getByLabel("Measured change in results").fill("-90000");
  await exp.getByLabel("± uncertainty (standard error)").fill("25000");
  await page.getByRole("button", { name: "Preview what this means" }).click();
  await expect(page.getByRole("heading", { name: "What your beliefs mean before the model sees the data" })).toBeVisible({ timeout: 10_000 });
  // plot frames load as they scroll into view: bring the KPI fan in before the full-page shot
  await page.locator('iframe[title="Results the model expects before fitting"]').scrollIntoViewIfNeeded();
  await frames(page);
  await shot(page, "priors");

  await page.getByRole("button", { name: "View as YAML" }).click();
  await expect(page.getByRole("dialog").getByText("dataset://growth/eu-media-mix-data@latest")).toBeVisible({ timeout: 15_000 });
  await page.waitForTimeout(500);
  await page.screenshot({ path: OUT("yaml") });
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: "Save and continue" }).click();
  await page.getByRole("button", { name: "Fit the model" }).click();
  await expect(page.getByRole("list", { name: "What happens during the fit" })).toBeVisible();
  await expect.poll(async () => Number(await page.getByRole("progressbar", { name: "Fit progress" }).getAttribute("aria-valuenow")), { timeout: 10_000 }).toBeGreaterThan(40);
  await shot(page, "run");
  await expect(page.getByRole("heading", { name: "Your model is ready" })).toBeVisible({ timeout: 20_000 });
  await page.getByRole("button", { name: "See the results" }).click();
  await expect(page.getByRole("heading", { name: "Sanity scorecard" })).toBeVisible();
  await frames(page);
  await shot(page, "report");
  await page.getByRole("heading", { name: "Sanity scorecard" }).scrollIntoViewIfNeeded();
  await page.locator("section", { has: page.getByRole("heading", { name: "Sanity scorecard" }) }).screenshot({ path: OUT("scorecard") });

  await page.getByRole("button", { name: "What if…" }).click();
  const slider = page.getByRole("slider", { name: "Spend on tv" });
  await slider.focus();
  for (let i = 0; i < 4; i++) await page.keyboard.press("ArrowRight");
  const search = page.getByRole("slider", { name: "Spend on search" });
  await search.focus();
  for (let i = 0; i < 2; i++) await page.keyboard.press("ArrowLeft");
  await page.getByRole("button", { name: "Run exact" }).click();
  await expect(page.getByTestId("exact-result")).toBeVisible({ timeout: 10_000 });
  await page.getByRole("button", { name: "Make a forecast" }).click();
  await expect(page.getByTestId("forecast-result")).toBeVisible();
  await frames(page);
  await shot(page, "whatif");

  await page.getByRole("button", { name: /^Share/ }).click();
  await expect(page.getByTestId("gate-sentence")).toBeVisible();
  await page.getByLabel("Why is it fine to go live anyway?").fill("Holdout error is high because of the summer promotion; accepted.");
  await page.getByRole("button", { name: "Approve and go live" }).click();
  await expect(page.getByTestId("endpoint-live")).toBeVisible();
  await page.getByRole("button", { name: "Create link" }).click();
  await expect(page.getByTestId("embed-link")).toBeVisible();
  await shot(page, "share");

  await page.setViewportSize({ width: 375, height: 760 });
  await page.getByRole("link", { name: "Back to all analyses" }).click();
  await page.getByRole("list", { name: "Your analyses" }).getByRole("link").first().click();
  await expect(page.getByText(/Step 4 of 6/)).toBeVisible();
  await page.screenshot({ path: OUT("phone") });
});
