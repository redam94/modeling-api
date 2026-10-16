/**
 * M3.5 smoke tests against the MSW-mocked API (`src/mocks/explore.ts`, views computed by the
 * real runner) and the real plot sandbox: Help me choose, the catalog's About cards, the Data
 * Explorer, the Model Explorer with governance and lineage, the comparison of model versions
 * and budget optimisation (also from Guided mode). Every test ends without a CSP violation.
 */
import { expect, test, type Page } from "@playwright/test";

const DATASET = "ds_01jaf3a1b2c3d4e5f6g7h8j9k0";
const MMM = "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0";
const VECM = "mdl_01jaf3m35vecm0000000001";

test.beforeEach(async ({ page }) => {
  page.on("pageerror", (err) => {
    throw err;
  });
  await page.addInitScript(() => {
    const w = window as unknown as { __csp: string[] };
    w.__csp = [];
    document.addEventListener("securitypolicyviolation", (e) => w.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
  });
});

test.afterEach(async ({ page }) => {
  const violations = await page.evaluate(() => (window as unknown as { __csp?: string[] }).__csp ?? []);
  expect(violations).toEqual([]);
});

async function plotsRendered(page: Page) {
  const frames = page.locator('iframe[data-testid="plot-frame"]');
  await expect(frames.first()).toBeVisible({ timeout: 20_000 });
  await expect(frames.first().contentFrame().locator("svg.marks")).toBeVisible({ timeout: 20_000 });
}

async function setMode(page: Page, mode: "Guided" | "Expert") {
  await page.getByRole("radiogroup", { name: "Mode" }).getByRole("radio", { name: mode }).click();
}

test("Help me choose: questions, ranked matches with reasons, and a one-click start", async ({ page }) => {
  await page.goto("/");
  const teaser = page.getByTestId("chooser-teaser");
  await expect(teaser).toBeVisible();
  await teaser.getByRole("link", { name: "Forecast the future" }).click();
  await expect(page).toHaveURL(/\/choose\?goal=forecast/);
  await expect(page.getByRole("heading", { name: "Help me choose" })).toBeVisible();
  const matches = page.getByRole("list", { name: "Recommendations" });
  await expect(matches.getByRole("listitem").first()).toBeVisible();
  await expect(matches.getByText(/^Suits: forecast the future/).first()).toBeVisible();
  // answer the next questions with the keyboard-accessible radio cards
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByRole("checkbox", { name: "One number over time" }).check();
  await page.getByRole("button", { name: "Next" }).click();
  await expect(page).toHaveURL(/data_shape=one_series/);
  await page.getByRole("button", { name: "Skip" }).click();
  await page.getByRole("button", { name: "Skip" }).click();
  await page.getByRole("button", { name: "Skip" }).click();
  await page.getByRole("button", { name: "Skip" }).click();
  await page.getByRole("radio", { name: "New to it" }).check();
  await expect(page).toHaveURL(/experience=beginner/);
  const first = matches.getByRole("listitem").first();
  await expect(first).toContainText("Best match");
  // the About card of a recommendation, then start with it
  await first.getByRole("button", { name: /^About / }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Use it when")).toBeVisible();
  await page.keyboard.press("Escape");
  // a model leads to a new spec that uses it
  await page.getByRole("radio", { name: "Models" }).click();
  await expect(page).toHaveURL(/kind=model/);
  const model = matches.getByRole("listitem").first();
  const id = await model.getAttribute("data-recommendation");
  await model.getByRole("link", { name: "Start with this" }).click();
  await expect(page).toHaveURL(new RegExp(`/build/new\\?model=${id!.replace(".", "\\.")}`));
  await expect(page.getByTestId("yaml-editor").locator(".view-lines")).toContainText(`use: ${id}@^1`, { timeout: 20_000 });
});

test("the catalog browses every item by its About card", async ({ page }) => {
  await page.goto("/catalog");
  await page.getByRole("button", { name: /^Governance checks/ }).click();
  const items = page.getByRole("list", { name: "Catalog items" });
  await expect(items.getByRole("listitem")).toHaveCount(12);
  await items.getByRole("button", { name: /^Beats simple benchmarks/ }).click();
  await expect(page).toHaveURL(/about=check%3Abaseline\.beats_naive|about=check:baseline\.beats_naive/);
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Avoid it when")).toBeVisible();
  await expect(dialog.getByText("baseline.beats_naive")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("tab", { name: /Models/ }).click();
  await expect(page.getByRole("table", { name: "Model plugins" })).toBeVisible();
});

test("the Data Explorer shows the ingest views and computes the rest on demand", async ({ page }) => {
  await page.goto(`/datasets/${DATASET}`);
  await expect(page.getByRole("heading", { name: "About this dataset" })).toBeVisible();
  await expect(page.getByTestId("dataset-summary")).toContainText("156 weeks");
  // the owner's description is editable
  await page.getByRole("button", { name: "Edit" }).click();
  await page.getByLabel("Describe the data for your colleagues").fill("Weekly media spend per region from finance.");
  await page.getByRole("button", { name: "Save description" }).click();
  await expect(page.getByTestId("dataset-description")).toHaveText("Weekly media spend per region from finance.");

  await page.getByRole("link", { name: "Explore this version" }).click();
  await expect(page).toHaveURL(new RegExp(`/datasets/${DATASET}/versions/1/explore`));
  await expect(page.getByTestId("plain-summary").first()).toContainText("156 week");
  const nav = page.getByRole("navigation", { name: "Data Explorer sections" });
  await expect(nav.locator('[data-section="stationarity"]')).toHaveAttribute("data-status", "missing");

  // a section the ingest did not compute: one click computes the rest in an explore run
  await nav.getByRole("button", { name: /Stable or wandering/ }).click();
  await page.getByTestId("section-missing").getByRole("button", { name: "Compute it now" }).click();
  await expect(page).toHaveURL(/run=run_/);
  await expect(page.getByRole("table", { name: "Stationarity by series" })).toBeVisible({ timeout: 15_000 });
  await expect(nav.locator('[data-section="stl"]')).toHaveAttribute("data-status", "ok");

  // pickers and plots
  await nav.getByRole("button", { name: /Trend and season/ }).click();
  await page.getByLabel("Series").selectOption({ index: 1 });
  await plotsRendered(page);
  await nav.getByRole("button", { name: /Leads and lags/ }).click();
  await expect(page.getByTestId("plain-summary").first()).toContainText("revenue");
  await nav.getByRole("button", { name: /Data quality/ }).click();
  await expect(page.getByRole("list", { name: "Data-quality warnings" })).toBeVisible();

  // Guided mode: the plain reading first, the statistics one click away
  await setMode(page, "Guided");
  await page.goto(`/datasets/${DATASET}/versions/1/explore?section=distributions`);
  await expect(page.getByText("Show the statistics", { exact: true })).toBeVisible();
  await expect(page.getByRole("table", { name: "Summary statistics" })).toBeHidden();
  await page.getByText("Show the statistics", { exact: true }).click();
  await expect(page.getByRole("table", { name: "Summary statistics" })).toBeVisible();
});

test("the Model Explorer: parameters, diagnostics, forecasts, governance and lineage", async ({ page }) => {
  await page.goto(`/models/${MMM}/versions/1`);
  await expect(page.getByText("Checks failed")).toBeVisible();
  await page.getByRole("link", { name: "Explore" }).click();
  await expect(page.getByRole("heading", { name: "At a glance" })).toBeVisible();
  await expect(page.getByTestId("plain-summary").first()).toContainText("R-hat");
  const nav = page.getByRole("navigation", { name: "Model Explorer sections" });
  await expect(nav.locator('[data-section="irf"]')).toHaveAttribute("data-status", "skipped");

  await nav.getByRole("button", { name: /Parameters/ }).click();
  await expect(page.getByRole("table", { name: "Parameters" })).toBeVisible();
  await page.getByRole("table", { name: "Parameters" }).getByRole("button", { name: "R-hat" }).click();
  await expect(page.getByRole("table", { name: "Parameters" }).locator("th", { hasText: "R-hat" })).toHaveAttribute("aria-sort", /ascending|descending/);
  await plotsRendered(page);

  await nav.getByRole("button", { name: /Residuals/ }).click();
  await expect(page.getByRole("table", { name: "Residual tests" })).toBeVisible();

  await nav.getByRole("button", { name: /Forecast/ }).click();
  await page.getByTestId("section-missing").getByRole("button", { name: "Compute it now" }).click();
  await expect(page.getByTestId("plain-summary").first()).toContainText("likely between", { timeout: 15_000 });

  await nav.getByRole("button", { name: /Governance/ }).click();
  const checks = page.getByRole("list", { name: "Check results" });
  await expect(checks.locator('[data-check="convergence"]')).toHaveAttribute("data-status", "fail");
  await nav.getByRole("button", { name: /Lineage/ }).click();
  await expect(page.getByRole("list", { name: "Lineage" })).toContainText("eu-geo-mmm@v1");
  await expect(page.getByText("mmm.bayesian@1.0.0")).toBeVisible();
});

test("impulse responses of a VECM and a side-by-side comparison with Diebold-Mariano tests", async ({ page }) => {
  await page.goto(`/models/${VECM}/versions/1/explore?section=irf`);
  await expect(page.getByLabel("A shock to")).toBeVisible();
  await plotsRendered(page);
  await page.goto(`/models/${VECM}/versions/1/explore?section=fevd`);
  await expect(page.getByTestId("plain-summary").first()).toContainText("own surprises");

  await page.goto("/models/compare");
  await page.getByLabel("Model", { exact: true }).selectOption({ label: "sales-arimax" });
  await page.getByLabel("Version", { exact: true }).selectOption({ label: "v2" });
  await page.getByRole("button", { name: "Add" }).click();
  await page.getByLabel("Version", { exact: true }).selectOption({ label: "v1" });
  await page.getByRole("button", { name: "Add" }).click();
  await page.getByLabel("Model", { exact: true }).selectOption({ label: "sales-ets" });
  await page.getByRole("button", { name: "Add" }).click();
  await page.getByRole("button", { name: /^Compare 3 versions/ }).click();
  await expect(page.getByRole("table", { name: "Holdout metrics" })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("table", { name: "Diebold-Mariano tests" })).toContainText("about equally accurate");
  await expect(page.getByTestId("plain-summary").first()).toContainText("sales-arimax@v2 has the lowest");
});

test("the explorers and the chooser fit a phone without sideways scrolling", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const url of [`/datasets/${DATASET}/versions/1/explore`, `/models/${MMM}/versions/1/explore?section=params`, `/models/${MMM}/versions/1/optimize`, "/choose?goal=forecast"]) {
    await page.goto(url);
    await expect(page.locator("main h1").first()).toBeVisible();
    await page.waitForTimeout(300);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, url).toBeLessThanOrEqual(0);
  }
});

test("budget optimisation: bounds explained before the run, then the plan and its frontier", async ({ page }) => {
  await page.goto(`/models/${MMM}/versions/1/optimize`);
  await expect(page.getByRole("heading", { name: "Optimise a budget" })).toBeVisible();
  const limits = page.getByRole("table", { name: "Limits per channel" });
  await limits.getByLabel("Limits for tv in").selectOption({ label: "money" });
  await limits.getByLabel("Minimum for tv").fill("200000");
  await expect(page.getByRole("list", { name: "Problems with the plan" })).toContainText("more than the budget");
  await expect(page.getByRole("button", { name: "Optimise the budget" })).toBeDisabled();
  await limits.getByLabel("Minimum for tv").fill("40000");
  await page.getByRole("radio", { name: /Cautious/ }).first().check();
  await page.getByRole("button", { name: "Optimise the budget" }).click();
  const plan = page.getByTestId("plan-result");
  await expect(plan).toBeVisible({ timeout: 15_000 });
  await expect(plan.getByRole("table", { name: "Allocation" })).toContainText("search");
  await expect(plan).toContainText("chance it beats today's plan");
  await expect(page).toHaveURL(/run=run_/);
  await plotsRendered(page);
});
