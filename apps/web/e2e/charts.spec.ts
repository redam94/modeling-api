/**
 * v0.10 chart wiring, against the MSW-mocked API (`src/mocks/score-shapes.ts`) and the real
 * plot sandbox: the Plots card of the run detail page, the classification view of "Results by
 * output", the quick chart of a grid output table (one cell at a time) and the elpd plot of the
 * model comparison. Every test ends without a CSP violation.
 */
import { expect, test, type FrameLocator, type Locator } from "@playwright/test";

const FIT_RUN = "run_01jaf3q2x8m9v7k5h4g3f2e1a1";
const CLASSIFIER = "mdl_01jaf3v10clf0000000001";
const CLASSIFIER_RUN = "run_01jaf3v10clf0000000001";
const AREAL_RUN = "run_01jaf3v10area000000001";
const GRID = "mdl_01jaf3v10grid000000001";

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

/** The sandbox of a plot frame, after it rendered its marks. */
async function rendered(frame: Locator): Promise<FrameLocator> {
  await frame.scrollIntoViewIfNeeded();
  const inner = frame.contentFrame();
  await expect(inner.locator("svg.marks, .main-svg").first()).toBeVisible({ timeout: 20_000 });
  return inner;
}

test("the run detail page shows the plots of a finished run", async ({ page }) => {
  await page.goto(`/runs/${CLASSIFIER_RUN}`);
  const card = page.getByRole("region", { name: "Run plots" });
  await expect(card.getByText("ROC and precision-recall curves")).toBeVisible();
  await expect(card.getByText("The AUC is 0.88.", { exact: false })).toBeVisible();
  await rendered(card.locator('iframe[title="ROC and precision-recall curves"]'));

  // A plot whose data is a file of the run: this page has no signed link to it.
  await page.goto(`/runs/${AREAL_RUN}`);
  await expect(page.getByText("The data of this plot is in a file of the run. Open the model version to see the plot.")).toBeVisible();
  await rendered(page.locator('iframe[title="Area map of rr"]'));

  // A fit shows the plots of the version it made.
  await page.goto(`/runs/${FIT_RUN}`);
  await expect(page.getByRole("navigation", { name: "Plots" }).getByText("Posterior predictive fan")).toBeVisible();
  await rendered(page.locator('iframe[data-testid="plot-frame"]').first());
});

test("Results by output shows a classifier's ROC, confusion and calibration plots", async ({ page }) => {
  await page.goto(`/score?model=${CLASSIFIER}&version=1`);
  await page.getByRole("button", { name: "Forecast", exact: true }).click();
  const section = page.getByRole("region", { name: "Results by output" });
  await expect(section.getByText("y_hat (classification)")).toBeVisible({ timeout: 20_000 });
  for (const title of ["ROC and precision-recall curves", "Confusion matrix"]) await rendered(section.locator(`iframe[title="${title}"]`));
  await expect(section.locator('iframe[data-testid="plot-frame"]')).toHaveCount(3);
  // The plot list below leaves out what the view shows and keeps the others.
  const list = page.getByRole("region", { name: "Run plots" });
  await expect(list.getByText("Predictions against actuals")).toBeVisible();
  await expect(list.getByText("Confusion matrix")).toHaveCount(0);
});

test("the quick chart of a grid output draws one cell over time", async ({ page }) => {
  await page.goto(`/score?model=${GRID}&version=1`);
  await page.getByRole("button", { name: "Forecast", exact: true }).click();
  await expect(page.getByRole("table", { name: "ui/summary.arrow" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("864 rows")).toBeVisible();
  const cell = page.getByLabel("Cell (row · col)");
  await expect(cell).toHaveValue("0 · 0");
  const inner = await rendered(page.locator('iframe[title="ui/summary.arrow chart"]'));
  // A line along time (8 quarters at 8 x positions), not a stroke per quarter.
  const xs = async () => {
    const d = (await inner.locator("g.mark-line path").first().getAttribute("d")) ?? "";
    return new Set([...d.matchAll(/[ML](-?[\d.]+),/g)].map((m) => m[1])).size;
  };
  await expect.poll(xs).toBe(8);
  await expect(inner.locator("g.mark-area path").first()).toBeAttached();
  await cell.selectOption("4 · 5");
  await expect.poll(xs).toBe(8);
});

test("the comparison of versions shows their elpd", async ({ page }) => {
  await page.goto("/models/compare");
  await page.getByLabel("Model", { exact: true }).selectOption({ label: "sales-arimax" });
  await page.getByLabel("Version", { exact: true }).selectOption({ label: "v2" });
  await page.getByRole("button", { name: "Add" }).click();
  await page.getByLabel("Version", { exact: true }).selectOption({ label: "v1" });
  await page.getByRole("button", { name: "Add" }).click();
  await page.getByRole("button", { name: /^Compare 2 versions/ }).click();
  await expect(page.getByText("Which version predicts new data best?")).toBeVisible({ timeout: 15_000 });
  // the example plot compares WAIC estimates: the card must not say they come from LOO
  await expect(page.getByText(/each version, from the widely applicable information criterion \(WAIC\)/)).toBeVisible();
  await rendered(page.locator('iframe[title="Model comparison"]'));
});
