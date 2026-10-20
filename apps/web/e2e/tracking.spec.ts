/**
 * v0.11 run tracking against the MSW-mocked API (`src/mocks/tracking.ts`) and the real plot
 * sandbox: the Metrics, Params, Logs and Notes tabs of the run page, live chain step sizes and
 * the name and tags in the run list. Every test ends without a CSP violation.
 */
import { expect, test, type FrameLocator, type Locator, type Page } from "@playwright/test";

const RUNNING = "run_01jaf3q2x8m9v7k5h4g3f2e1d0";
const DONE = "run_01jaf3q2x8m9v7k5h4g3f2e1a1";
const GRID = "run_01jaf3q2x8m9v7k5h4g3f2e1g5";

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

/** Sign in to the mock as a persona (see e2e/m2.spec.ts). */
async function as(page: Page, persona: "ana" | "vic") {
  await page.addInitScript((p) => {
    if (window.top !== window || sessionStorage.getItem("mapi.test.persona")) return;
    localStorage.setItem("mapi.mock.session", p);
    sessionStorage.setItem("mapi.test.persona", "1");
  }, persona);
}

/** The sandbox of a plot frame, after it rendered its marks. */
async function rendered(frame: Locator): Promise<FrameLocator> {
  await frame.scrollIntoViewIfNeeded();
  const inner = frame.contentFrame();
  await expect(inner.locator("svg.marks").first()).toBeVisible({ timeout: 20_000 });
  return inner;
}

test("the Metrics tab draws the step size of each chain and updates live", async ({ page }) => {
  await page.goto(`/runs/${RUNNING}`);
  // Overview: the chain lanes show the sampler's step size.
  await expect(page.getByRole("list", { name: "Chains" }).getByText("step size 0.12", { exact: true })).toBeVisible();

  await page.getByRole("tab", { name: "Metrics" }).click();
  await expect(page).toHaveURL(/\?tab=metrics$/);
  const card = page.locator('[data-metric="step_size"]');
  await expect(card.getByRole("combobox", { name: "Colour by" })).toHaveValue("chain");
  const inner = await rendered(card.locator('iframe[title="step_size"]'));
  // One line per chain.
  await expect.poll(async () => inner.locator("g.mark-line path").count()).toBe(4);
  await expect(page.locator('[data-metric="divergences"]')).toBeVisible();

  // Tracking frames over SSE add points: the lines get longer on refetch.
  const countPoints = async () => {
    const d = (await inner.locator("g.mark-line path").first().getAttribute("d")) ?? "";
    return [...d.matchAll(/[ML]/g)].length;
  };
  const before = await countPoints();
  await expect.poll(countPoints, { timeout: 10_000 }).toBeGreaterThan(before);
});

test("a finished run lists scalar metrics in the Values table", async ({ page }) => {
  await page.goto(`/runs/${DONE}?tab=metrics`);
  const table = page.getByRole("table", { name: "Metric values" });
  await expect(table.getByText("elpd_loo")).toBeVisible();
  await expect(table.getByText("412.5")).toBeVisible();
});

test("the Params tab lists the grid trials as text", async ({ page }) => {
  await page.goto(`/runs/${GRID}?tab=params`);
  const table = page.getByRole("table", { name: "Run parameters" });
  await expect(table.getByText("trial.0.adstock/l_max", { exact: true })).toBeVisible();
  await expect(table.getByText("trial.0.saturation/kind", { exact: true })).toBeVisible();
  await expect(table.getByText("grid.metric", { exact: true })).toBeVisible();
  await page.getByRole("textbox", { name: "Filter parameters" }).fill("trial.0.");
  await expect(table.locator("tbody tr")).toHaveCount(2);
  // Tags show above the table; the grid's rmse is on the Metrics tab.
  await expect(page.getByText("grid.best_point=4").first()).toBeVisible();

  // An untrusted param value stays text.
  await page.goto(`/runs/${RUNNING}?tab=params`);
  await expect(page.getByText("<b>not bold</b> <img src=x onerror=alert(1)>")).toBeVisible();
  await expect(page.getByRole("table", { name: "Run parameters" }).locator("img, b")).toHaveCount(0);
});

test("the Notes tab saves only the changed fields and the header shows the new name", async ({ page }) => {
  await page.goto(`/runs/${DONE}?tab=notes`);
  await page.getByLabel("Name", { exact: true }).fill("Tuned priors");
  await page.getByRole("button", { name: "Add a tag" }).click();
  await page.getByRole("textbox", { name: "Tag 1 key" }).fill("team");
  await page.getByRole("textbox", { name: "Tag 1 value" }).fill("growth");
  const patch = page.waitForRequest((r) => r.method() === "PATCH" && r.url().endsWith(`/v1/runs/${DONE}`));
  await page.getByRole("button", { name: "Save" }).click();
  expect((await patch).postDataJSON()).toEqual({ name: "Tuned priors", tags: { team: "growth" } });
  await expect(page.getByTestId("run-name")).toHaveText("Tuned priors");
  await expect(page.getByRole("button", { name: "Save" })).toBeDisabled();

  // The run list shows the name and the tag (the grid run's tags are capped at three).
  await page.goto("/runs");
  const table = page.getByRole("table", { name: "Runs" });
  await expect(table.getByRole("link", { name: "Grid search: adstock and saturation" })).toBeVisible();
  await expect(table.getByText("grid.best_point=4")).toBeVisible();
});

test("a viewer reads the notes but cannot change them", async ({ page }) => {
  await as(page, "vic");
  await page.goto(`/runs/${GRID}?tab=notes`);
  await expect(page.getByText("You need the runner role to change the name, tags and note. Your role is viewer.")).toBeVisible();
  await expect(page.getByText("12 grid points. Point 4 has the smallest RMSE.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Save" })).toHaveCount(0);
});

test("the Logs tab filters user lines by level", async ({ page }) => {
  await page.goto(`/runs/${RUNNING}?tab=logs`);
  const log = page.getByRole("log", { name: "Run logs" });
  await expect(log.getByText("sampler started")).toBeVisible();
  await expect(log.getByText("step_size=0.012")).toBeVisible();
  await page.getByRole("combobox", { name: "Level" }).selectOption("warning");
  await expect(log.getByText("sampler started")).toHaveCount(0);
  await expect(log.getByText("bootstrap ok")).toHaveCount(0);
  await expect(log.getByText("step size of chain 1 is small")).toBeVisible();
  await page.getByRole("radio", { name: "user" }).click();
  await expect(log.getByText("grid point 7 failed: singular matrix")).toBeVisible();
});

test.describe("phone layout (375 px)", () => {
  test.use({ viewport: { width: 375, height: 800 } });
  for (const tab of ["metrics", "params", "logs", "notes"]) {
    test(`the ${tab} tab does not scroll horizontally`, async ({ page }) => {
      await page.goto(`/runs/${tab === "params" ? GRID : RUNNING}?tab=${tab}`);
      await expect(page.getByRole("tab", { name: tab[0]!.toUpperCase() + tab.slice(1), selected: true })).toBeVisible();
      await page.waitForTimeout(500); // let lazy content lay out
      const { scroll, client } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
      expect(scroll, `${tab} is ${scroll}px wide in a ${client}px viewport`).toBeLessThanOrEqual(client);
    });
  }
});
