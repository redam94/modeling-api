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
  await items.getByRole("button", { name: /^Better than simple benchmarks/ }).click();
  await expect(page).toHaveURL(/about=check%3Abaseline\.beats_naive|about=check:baseline\.beats_naive/);
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Avoid it when")).toBeVisible();
  await expect(dialog.getByText("baseline.beats_naive")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("tab", { name: /Models/ }).click();
  await expect(page.getByRole("table", { name: "Model plugins" })).toBeVisible();
});

test("the catalog shows every plot as a card with an example", async ({ page }) => {
  await page.goto("/catalog?tab=plots");
  const cards = page.getByRole("list", { name: "Plot recipes" }).getByRole("listitem");
  // v0.10: core.roc_pr, core.confusion, core.loss_curve, core.loo_pit; v0.12: the MMM pack (8 mmm.*,
  // core.energy, core.prior_posterior); v0.13: causal.impact, causal.pre_fit, causal.donor_weights,
  // causal.event_study
  await expect(cards).toHaveCount(54);
  // the first card's thumbnail renders in the sandbox (Vega-Lite)
  const first = page.getByTestId("plot-card").first();
  await expect(first.locator('iframe[data-testid="plot-frame"]').contentFrame().locator("svg.marks")).toBeVisible({ timeout: 20_000 });
  await page.getByRole("button", { name: /^Science/ }).click();
  await expect(cards).toHaveCount(12);
  await page.getByRole("button", { name: /^All plots/ }).click();
  await page.getByRole("searchbox", { name: "Search the plots" }).or(page.getByLabel("Search the plots")).fill("residuals");
  await page.getByTestId("plot-card").filter({ hasText: "core.residuals_over_time" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Use it when")).toBeVisible();
  await expect(dialog.getByText("An example made from synthetic data")).toBeVisible();
  await expect(dialog.locator('iframe[data-testid="plot-frame"]').contentFrame().locator("svg.marks")).toBeVisible({ timeout: 20_000 });
  await dialog.getByRole("button", { name: "Close" }).click();
  // a Plotly example (science pack) renders too
  await page.getByLabel("Search the plots").fill("");
  await page.getByRole("button", { name: /^Science/ }).click();
  await page.getByTestId("plot-card").filter({ hasText: "sci.field_map" }).click();
  await expect(page.getByRole("dialog").locator('iframe[data-testid="plot-frame"]').contentFrame().locator(".main-svg").first()).toBeVisible({ timeout: 20_000 });
});

test("a plot's About card in Browse shows its example", async ({ page }) => {
  await page.goto("/catalog");
  await page.getByRole("button", { name: /^Plots/ }).click();
  await page.getByRole("list", { name: "Catalog items" }).getByRole("button").first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByTestId("plot-example")).toBeVisible();
  await expect(dialog.locator('iframe[data-testid="plot-frame"]').contentFrame().locator("svg.marks, .main-svg").first()).toBeVisible({ timeout: 20_000 });
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

// v0.10: the Data Explorer on scientific data (src/mocks/explore-sci-demo.ts: the spec's example views)
const SCI_DEMO = "ds-sci-demo";
const SCI_DEMO_TREE = "ds-sci-demo-tree";

/** The plot sandbox drew the section's first plot (Vega-Lite or Plotly), or the field viewer its canvas. */
async function pictureRendered(page: Page, section: string) {
  const card = page.locator(`#section-${section}`);
  if (section === "images") {
    await expect(card.getByTestId("field-viewer").locator("canvas").first()).toBeVisible({ timeout: 20_000 });
    return;
  }
  const frame = card.locator('iframe[data-testid="plot-frame"]').first();
  await expect(frame).toBeVisible({ timeout: 20_000 });
  await expect(frame.contentFrame().locator("svg.marks, svg.main-svg").first()).toBeVisible({ timeout: 20_000 });
}

test("the Data Explorer on scientific data: the list follows the shapes and every picture renders", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text());
  });
  await page.goto(`/datasets/${SCI_DEMO}/versions/1/explore`);
  const nav = page.getByRole("navigation", { name: "Data Explorer sections" });
  await expect(nav).toBeVisible();
  // the relevance order: the picture sections come first, the time sections are not listed
  const listed = nav.locator("[data-section]:not([data-reason])");
  await expect(listed.first()).toHaveAttribute("data-section", "overview");
  await expect(listed.nth(1)).toHaveAttribute("data-section", "structure");
  await expect(nav.getByRole("heading", { name: "Pictures of the data" })).toBeVisible();
  await expect(nav.locator('[data-section="stl"]:not([data-reason])')).toHaveCount(0);
  const hidden = nav.getByTestId("nav-not-applicable");
  await expect(hidden).toContainText("Not for this data (7)");
  // the overview links each shape to its section
  await page.getByTestId("shape-chips").getByRole("button", { name: /^Image/ }).click();
  await expect(page).toHaveURL(/section=images/);
  await pictureRendered(page, "images");
  for (const [section, title] of [
    ["structure", "Structure"],
    ["profiles", "Profiles and spectra"],
    ["map", "Map"],
    ["network", "Network"],
    ["tree", "Tree"],
    ["circular", "Directions and times of day"],
    ["points", "Points"],
  ] as const) {
    await nav.getByRole("button", { name: new RegExp(`^${title}`) }).first().click();
    await expect(page).toHaveURL(new RegExp(`section=${section}`));
    await pictureRendered(page, section);
  }
  // a hidden section still opens from its link, with the reason
  await page.goto(`/datasets/${SCI_DEMO}/versions/1/explore?section=stl`);
  await expect(page.getByTestId("section-skipped")).toContainText("the data has no time dimension");
  await expect(page.getByTestId("nav-not-applicable").getByRole("button", { name: /Trend and season/ })).toHaveAttribute("aria-current", "page");
  expect(consoleErrors).toEqual([]);
});

test("the Data Explorer on a tree dataset: each node has its own pictures", async ({ page }) => {
  await page.goto(`/datasets/${SCI_DEMO_TREE}/versions/1/explore?node=%2Flinks&section=network`);
  await pictureRendered(page, "network");
  const nav = page.getByRole("navigation", { name: "Data Explorer sections" });
  await expect(nav.locator('[data-section="images"]')).toHaveAttribute("data-reason", "not_applicable");
  await page.getByRole("combobox", { name: "Node", exact: true }).selectOption("/image");
  await expect(page).toHaveURL(/node=%2Fimage/);
  await nav.getByRole("button", { name: /^Images and fields/ }).click();
  await pictureRendered(page, "images");
  await expect(nav.locator('[data-section="network"]')).toHaveAttribute("data-reason", "not_applicable");
});

test("the Model Explorer: parameters, diagnostics, forecasts, governance and lineage", async ({ page }) => {
  await page.goto(`/models/${MMM}/versions/1`);
  await expect(page.getByRole("link", { name: /^Validation: Awaiting approval/ })).toBeVisible();
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
  await expect(checks.locator('[data-check="null.permutation_target"]')).toHaveAttribute("data-status", "pass");
  await expect(page.getByRole("link", { name: /Open this validation/ })).toHaveAttribute("href", /\/validations\/val_/);
  await nav.getByRole("button", { name: /Lineage/ }).click();
  await expect(page.getByRole("list", { name: "Lineage" })).toContainText("eu-geo-mmm@v1");
  await expect(page.getByText("mmm.bayesian@1.0.0")).toBeVisible();
});

test("impulse responses of a VECM and a side-by-side comparison of forecasters", async ({ page }) => {
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
  // forecasters score the held-out periods from one origin: no significance test on that path
  await expect(page.getByText(/^No significance test: .* one forecast origin/).first()).toBeVisible();
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

test("Help me choose fits a 375 px phone: the suggestion tabs scroll inside their strip", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 900 });
  for (const url of ["/choose", "/choose?goal=forecast", "/choose?goal=forecast&kind=node"]) {
    await page.goto(url);
    await expect(page.getByRole("heading", { name: "Help me choose" })).toBeVisible();
    await page.waitForTimeout(300);
    const { scroll, client } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
    expect(scroll, `${url} is ${scroll}px wide in a ${client}px viewport`).toBeLessThanOrEqual(client);
    const tabs = page.getByRole("radiogroup", { name: "What to suggest" });
    const box = await tabs.boundingBox();
    expect(box!.x + box!.width, url).toBeLessThanOrEqual(client);
    // every tab label stays on one line (no three-line "Where to start")
    for (const name of ["Where to start", "Workflow nodes"]) {
      const tab = tabs.getByRole("radio", { name });
      const h = (await tab.boundingBox())!.height;
      expect(h, `${name} in ${url}`).toBeLessThanOrEqual(32);
    }
  }
  // the last tab is reachable by scrolling the strip
  await page.getByRole("radio", { name: "Workflow nodes" }).click();
  await expect(page).toHaveURL(/kind=node/);
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
  await expect(plan).toContainText("The chance that it is better than today's plan");
  await expect(page).toHaveURL(/run=run_/);
  await plotsRendered(page);
});
