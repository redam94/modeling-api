/**
 * v0.14 run comparison against the MSW-mocked API (`src/mocks/experiments.ts`, `media.ts`) and
 * the real plot sandbox: the runs table (tracked columns, filters, sort, saved views,
 * selection), the compare page (curves, metric bars, params diff, media slider), the run page's
 * Media tab and the experiments pages. Every test ends without a CSP violation.
 */
import { expect, test, type FrameLocator, type Locator, type Page } from "@playwright/test";

const FIT_A = "run_01jaf3q2x8m9v7k5h4g3f2e1h6";
const FIT_B = "run_01jaf3q2x8m9v7k5h4g3f2e1j7";
const EXP_GEO = "exp_01jaf3x1geo2adstock3m4n5p6";

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
  await expect(inner.locator("svg.marks").first()).toBeVisible({ timeout: 20_000 });
  return inner;
}

/** The `src` of the image a run shows in a media block of the compare page. */
function runImage(page: Page, runId: string): Locator {
  return page.locator(`[data-compare-media="samples"] [data-run="${runId}"] img`);
}

test("select two runs, compare their curves, params and media", async ({ page }) => {
  await page.goto("/runs");
  const table = page.getByRole("table", { name: "Runs" });
  await table.getByRole("checkbox", { name: "Select the run Adstock 8 weeks" }).click();
  await table.getByRole("checkbox", { name: "Select the run Adstock 4 weeks" }).click();
  await page.getByRole("button", { name: "Compare (2)" }).click();
  await expect(page).toHaveURL(/\/runs\/compare\?/);
  await expect(page.getByRole("heading", { name: "Compare runs" })).toBeVisible();
  const chips = page.getByRole("list", { name: "Compared runs" });
  await expect(chips.getByRole("link", { name: "Adstock 8 weeks" })).toBeVisible();

  // Curves: the loss of both runs, train and val, as lines.
  const inner = await rendered(page.locator('[data-compare-metric="loss"] iframe[title="loss"]'));
  await expect.poll(async () => inner.locator("g.mark-line path").count()).toBeGreaterThanOrEqual(4);

  // Metric bars: the smaller RMSE wins.
  await page.getByRole("tab", { name: "Metric bars" }).click();
  const bars = page.getByRole("table", { name: "Metric values by run" });
  const rmse = bars.getByRole("row").filter({ hasText: /^rmse/ });
  await expect(rmse.getByRole("cell").nth(1)).toContainText("398.2");
  await expect(rmse.getByLabel("Best run")).toHaveCount(1);
  await expect(rmse.getByRole("cell").nth(1).getByLabel("Best run")).toBeVisible();

  // Params: only the keys that differ, values as text.
  await page.getByRole("tab", { name: "Params" }).click();
  const params = page.getByRole("table", { name: "Params by run" });
  await expect(params.getByText("settings.adstock.l_max", { exact: true })).toBeVisible();
  await expect(params.getByText("fit.draws", { exact: true })).toHaveCount(0);
  await expect(params.getByText("<b>short</b> adstock <img src=x onerror=alert(1)>")).toBeVisible();
  await expect(params.locator("img, b")).toHaveCount(0);
  await page.getByText("Show only differences").click();
  await expect(params.getByText("fit.draws", { exact: true })).toBeVisible();

  // Media: the step slider changes the image of each run.
  await page.getByRole("tab", { name: "Media" }).click();
  await expect(page.getByRole("button", { name: /samples/ })).toHaveAttribute("aria-pressed", "true");
  const imgA = runImage(page, FIT_A);
  await expect(imgA).toHaveAttribute("src", /^blob:/);
  const last = await imgA.getAttribute("src");
  const slider = page.getByRole("slider", { name: "Step of samples" });
  await slider.fill("1");
  await expect.poll(async () => imgA.getAttribute("src")).not.toBe(last);
  // run B logged no image at step 1: it shows its step 0 image and says so
  await expect(page.locator(`[data-compare-media="samples"] [data-run="${FIT_B}"]`)).toContainText("(step 0)");
  await expect(runImage(page, FIT_B)).toHaveAttribute("src", /^blob:/);
  // the caption is untrusted text
  await slider.fill("4");
  await expect(page.getByText("Posterior sample <b>not bold</b>").first()).toBeVisible();
});

test("the compare page asks for two runs and reports a missing run", async ({ page }) => {
  await page.goto(`/runs/compare?runs=${encodeURIComponent(JSON.stringify([FIT_A]))}`);
  await expect(page.getByText("Select two or more runs")).toBeVisible();
  await page.goto(`/runs/compare?runs=${encodeURIComponent(JSON.stringify([FIT_A, "run_missing"]))}`);
  await expect(page.getByText("A run of the comparison does not exist, or you cannot read it")).toBeVisible();
});

test("the runs table adds tracked columns, sorts on them, filters and applies a saved view", async ({ page }) => {
  await page.goto("/runs");
  const table = page.getByRole("table", { name: "Runs" });
  await expect(table.getByRole("link", { name: "Adstock 4 weeks" })).toBeVisible();

  // a column from the run keys
  await page.getByRole("button", { name: "Columns" }).click();
  const dialog = page.getByRole("dialog", { name: "Columns" });
  await dialog.getByRole("textbox", { name: "Find a key" }).fill("rmse");
  await dialog.getByRole("region", { name: "Metrics" }).getByRole("checkbox").first().click();
  await dialog.getByRole("button", { name: "Apply" }).click();
  await expect(page).toHaveURL(/c=/);
  await expect(table.getByRole("columnheader", { name: /rmse/ })).toBeVisible();
  await expect(table.getByRole("cell", { name: "398.2" })).toBeVisible();

  // sort on it: ascending puts the smallest RMSE first
  await table.getByRole("button", { name: /rmse/ }).click();
  await expect(page).toHaveURL(/o=metric\.rmse/);
  await expect(table.locator("tbody tr").first()).toContainText("Adstock 8 weeks");

  // a filter the grammar refuses never reaches the URL
  const input = page.getByRole("textbox", { name: "New filter" });
  await input.fill("metric.rmse ~ 3");
  await page.getByRole("button", { name: "Add filter" }).click();
  await expect(page.getByText(/The filter is not correct: filter 'metric.rmse ~ 3'/)).toBeVisible();
  await input.fill("metric.rmse < 420");
  await page.getByRole("button", { name: "Add filter" }).click();
  await expect(page.getByRole("button", { name: "Remove the filter metric.rmse < 420" })).toBeVisible();
  await expect(table.getByRole("link", { name: "Adstock 4 weeks" })).toHaveCount(0);
  await expect(table.getByRole("link", { name: "Adstock 8 weeks" })).toBeVisible();

  // a saved view replaces the table's query
  await page.getByRole("button", { name: /^Views/ }).click();
  await page.getByRole("menuitem", { name: "Best RMSE first" }).click();
  await expect(page).toHaveURL(/view=rv_/);
  await expect(page.getByRole("button", { name: /View: Best RMSE first/ })).toBeVisible();
  await expect(table.getByRole("columnheader", { name: /l_max/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Remove the filter metric.rmse < 1000" })).toBeVisible();
});

test("the Media tab of a run shows images by step and a table as text", async ({ page }) => {
  await page.goto(`/runs/${FIT_A}?tab=media`);
  const card = page.locator('[data-media-key="samples"]');
  const img = card.locator("img");
  await expect(img).toHaveAttribute("src", /^blob:/);
  const last = await img.getAttribute("src");
  await card.getByRole("slider", { name: "Step of samples" }).fill("0");
  await expect.poll(async () => img.getAttribute("src")).not.toBe(last);
  await expect(card.getByText("Posterior sample at step 0")).toBeVisible();

  const table = page.locator('[data-media-key="coefficients"]').getByRole("table");
  await expect(table.getByText("<script>alert(1)</script>")).toBeVisible();
  await expect(table.locator("script")).toHaveCount(0);

  // a run without media says so
  await page.goto(`/runs/run_01jaf3q2x8m9v7k5h4g3f2e1a1?tab=media`);
  await expect(page.getByText("The run did not log images or tables.", { exact: false })).toBeVisible();
});

test("experiments: list, open, create and add runs", async ({ page }) => {
  await page.goto("/experiments");
  const list = page.getByRole("table", { name: "Experiments" });
  await expect(list.getByRole("link", { name: "Geo adstock study" })).toBeVisible();
  // archived experiments show on request; names are text
  await expect(list.getByText("Baseline 2025 <b>old</b>")).toHaveCount(0);
  await page.getByText("Show archived experiments").click();
  await expect(list.getByText("Baseline 2025 <b>old</b>")).toBeVisible();

  await list.getByRole("link", { name: "Geo adstock study" }).click();
  await expect(page).toHaveURL(new RegExp(`/experiments/${EXP_GEO}`));
  await expect(page.getByRole("heading", { name: "Geo adstock study" })).toBeVisible();
  const runs = page.getByRole("table", { name: "Runs" });
  await expect(runs.getByRole("link", { name: "Adstock 8 weeks" })).toBeVisible();
  await expect(runs.getByRole("columnheader", { name: "Experiment" })).toHaveCount(0);

  // a new experiment, then add a run to it from the runs table
  await page.getByRole("link", { name: "Experiments", exact: true }).first().click();
  await page.getByRole("button", { name: "New experiment" }).click();
  const create = page.getByRole("dialog", { name: "New experiment" });
  await create.getByLabel("Name").fill("geo adstock study");
  await create.getByRole("button", { name: "Create experiment" }).click();
  await expect(create.getByText("An experiment with this name exists. Type a different name.")).toBeVisible();
  await create.getByLabel("Name").fill("Holiday effects");
  await create.getByRole("button", { name: "Create experiment" }).click();
  await expect(page.getByRole("heading", { name: "Holiday effects" })).toBeVisible();
  await expect(page.getByText("The experiment has no runs")).toBeVisible();

  // (in the app: a new page load starts the mock API again)
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Runs", exact: true }).click();
  await page.getByRole("table", { name: "Runs" }).getByRole("checkbox", { name: "Select the run Adstock 4 weeks" }).click();
  await page.getByRole("button", { name: "Add to experiment" }).click();
  const add = page.getByRole("dialog", { name: "Add to experiment" });
  await add.getByLabel("Experiment").selectOption({ label: "Holiday effects" });
  await add.getByRole("button", { name: "Add runs" }).click();
  await expect(page.getByText("The app added 1 run to the experiment")).toBeVisible();
  await page.getByRole("combobox", { name: "Filter by experiment" }).selectOption({ label: "Holiday effects" });
  await expect(page.getByRole("table", { name: "Runs" }).locator("tbody tr")).toHaveCount(1);
});

test("the Notes tab moves a run to another experiment", async ({ page }) => {
  await page.goto(`/runs/${FIT_B}?tab=notes`);
  const select = page.getByLabel("Experiment of the run");
  await expect(select).toHaveValue(EXP_GEO);
  const patch = page.waitForRequest((r) => r.method() === "PATCH" && r.url().endsWith(`/v1/runs/${FIT_B}`));
  await select.selectOption({ label: "Prior sensitivity" });
  expect((await patch).postDataJSON()).toEqual({ experiment_id: "exp_01jaf3x1priors2q3r4s5t6v7w" });
  await expect(page.getByText("The app moved the run to the experiment")).toBeVisible();
});

test.describe("phone layout (375 px)", () => {
  test.use({ viewport: { width: 375, height: 800 } });
  const pages: [string, string][] = [
    ["/runs", "Runs"],
    [`/runs/compare?runs=${encodeURIComponent(JSON.stringify([FIT_A, FIT_B]))}`, "Compare runs"],
    ["/experiments", "Experiments"],
    [`/experiments/${EXP_GEO}`, "Geo adstock study"],
    [`/runs/${FIT_A}?tab=media`, "Media"],
  ];
  for (const [path, marker] of pages) {
    test(`${path} does not scroll horizontally`, async ({ page }) => {
      await page.goto(path);
      await expect(page.getByRole("main").getByText(marker, { exact: true }).first()).toBeVisible();
      await page.waitForTimeout(800); // let lazy content lay out
      const { scroll, client } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
      expect(scroll, `${path} is ${scroll}px wide in a ${client}px viewport`).toBeLessThanOrEqual(client);
    });
  }
});
