/**
 * The M3.5 screens clicked through against the real local stack (`make dev`, then `make e2e`
 * or `pnpm --filter @mapi/web e2e`): Help me choose, the canonical readiness workflow run and
 * followed live in the run monitor, the Data Explorer of a series with a unit root, the Model
 * Explorer of a statsmodels VAR (impulse responses, variance decomposition) and of an
 * mmm-framework model (contributions, response curves) with a budget optimisation, and the
 * governance record of a model fitted on noise.
 *
 * `tests/e2e/m35_browser_seed.py` creates a fresh project in the `default` tenant, uploads the
 * data (known truth, see tests/e2e/m35_stack.py), fits the models, creates the workflow and
 * the null-test policy; the project is deleted afterwards. The signed-in account is the
 * global setup's (admin@dev.local). Every step is timed (e2e/screenshots/m35-timings.json)
 * and the screens are captured (e2e/screenshots/m35-*.png, git-ignored) for
 * docs/operations/m35-verification.md. Tagged @e2e: never part of the mocked run.
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

const ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const SHOTS = fileURLToPath(new URL("./screenshots/", import.meta.url));
const RUN = Date.now().toString(36);
const WORKFLOW_TIMEOUT = 5 * 60_000;
const COMPUTE_TIMEOUT = 3 * 60_000;

interface Seeded {
  tenant: string;
  tenant_name: string;
  project: string;
  project_name: string;
  datasets: Record<"weekly_sales" | "noise" | "macro" | "geo_panel", string>;
  models: Record<"macro-var" | "geo-mmm" | "arimax-noise", string>;
  workflow: string;
  validation: string;
}
let seeded: Seeded;
const timings: Record<string, number> = {};

function seedScript(...args: string[]): string {
  return execFileSync("uv", ["run", "python", "tests/e2e/m35_browser_seed.py", ...args], { cwd: ROOT, encoding: "utf8" });
}

async function shot(page: Page, name: string): Promise<void> {
  await page.mouse.move(0, 0);
  await page.screenshot({ path: join(SHOTS, `m35-${name}.png`), animations: "disabled", fullPage: true });
}

async function step<T>(name: string, fn: () => Promise<T>): Promise<T> {
  const t0 = Date.now();
  try {
    return await fn();
  } finally {
    timings[name] = Math.round((Date.now() - t0) / 100) / 10;
  }
}

async function openProject(page: Page): Promise<void> {
  const switcher = page.getByRole("button", { name: /^Project: / });
  await expect(switcher).toBeVisible();
  if ((await switcher.getAttribute("aria-label"))?.includes(seeded.project_name)) return;
  await switcher.click();
  await page.getByRole("group", { name: seeded.tenant_name }).getByRole("menuitem", { name: new RegExp(seeded.project_name) }).click();
  await expect(page.getByRole("button", { name: new RegExp(`^Project: .*${seeded.project_name}`) })).toBeVisible();
}

/** Every plot iframe on the page rendered its marks in the sandbox. */
async function plotsRendered(page: Page): Promise<void> {
  const frames = page.locator("iframe[title]");
  await expect(frames.first()).toBeVisible({ timeout: 30_000 });
  await expect(frames.first().contentFrame().locator("svg.marks, canvas.marks").first()).toBeVisible({ timeout: 30_000 });
}

function watch(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(String(err)));
  page.on("console", (msg) => {
    if (msg.type() === "error" && !/Failed to load resource/.test(msg.text())) errors.push(msg.text());
  });
  return errors;
}

test.describe.configure({ mode: "serial" });

test.describe("@e2e M3.5: workflows, explorers, mmm-framework, governance and Help me choose", () => {
  test.use({ actionTimeout: 30_000 });

  test.beforeAll(async ({ request }) => {
    test.setTimeout(10 * 60_000);
    const api = (process.env.VITE_API_BASE ?? process.env.MAPI_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
    const ready = await request.get(`${api}/readyz`);
    expect(ready.ok(), `the API at ${api} must be ready (make dev)`).toBeTruthy();
    mkdirSync(SHOTS, { recursive: true });
    const t0 = Date.now();
    seeded = JSON.parse(seedScript("seed", "--run", RUN).trim().split("\n").at(-1) ?? "{}") as Seeded;
    timings["seed"] = Math.round((Date.now() - t0) / 100) / 10;
    expect(seeded.project).toMatch(/^m35ui-/);
  });

  test.afterAll(() => {
    const report = { run: RUN, project: seeded?.project, timings_s: timings };
    writeFileSync(join(SHOTS, "m35-timings.json"), `${JSON.stringify(report, null, 2)}\n`);
    console.log(`M3.5 browser timings: ${JSON.stringify(report)}`);
    if (seeded?.project && process.env.MAPI_E2E_KEEP !== "1") seedScript("cleanup", "--project", seeded.project);
  });

  test("Help me choose recommends ARIMAX for weekly sales with drivers, with its About card @e2e", async ({ page }) => {
    const errors = watch(page);
    await page.goto("/");
    await openProject(page);
    await step("choose", async () => {
      await page.goto("/choose?goal=forecast&data_shape=one_series&frequency=weekly&drivers=yes&kind=model");
      await expect(page.getByRole("heading", { name: "Help me choose" })).toBeVisible();
      const matches = page.getByRole("list", { name: "Recommendations" });
      const first = matches.getByRole("listitem").first();
      await expect(first).toContainText("Best match");
      await expect(first).toContainText("Suits: yes (prices, promotions, weather, spend)");
      await expect(first).toContainText("Suits: weekly");
      await first.getByRole("button", { name: /^About / }).click();
      const dialog = page.getByRole("dialog");
      await expect(dialog.getByText("Use it when")).toBeVisible();
      await expect(dialog).toContainText("ts.sarimax");
      await shot(page, "chooser");
      await page.keyboard.press("Escape");
      await first.getByRole("link", { name: "Start with this" }).click();
      await expect(page).toHaveURL(/\/build\/new\?model=ts\.sarimax/);
    });
    expect(errors).toEqual([]);
  });

  test("the canonical workflow runs live; the monitor shows the tests, decisions, search and selection @e2e", async ({ page }) => {
    test.setTimeout(WORKFLOW_TIMEOUT + 2 * 60_000);
    const errors = watch(page);
    await page.goto("/");
    await openProject(page);
    await step("workflow.start", async () => {
      await page.goto(`/workflows/${seeded.workflow}?tab=runs`);
      await page.getByRole("button", { name: "Run workflow" }).click();
      const dialog = page.getByRole("dialog", { name: "Run revision 1" });
      const data = dialog.getByLabel("Your data");
      const value = await data.locator("option", { hasText: "weekly_sales" }).first().getAttribute("value");
      expect(value).toBeTruthy();
      await data.selectOption(value ?? "");
      await dialog.getByRole("button", { name: "Start run" }).click();
      await expect(page).toHaveURL(/\/workflow-runs\/run_/);
    });
    const monitor = page.getByTestId("run-monitor");
    await step("workflow.run", async () => {
      await expect(monitor.getByTestId("run-status")).toHaveText(/Queued|Running|Succeeded/);
      await expect(page.getByTestId("canvas-node-adf")).toHaveAttribute("data-status", /queued|running|succeeded/);
      await expect(monitor).toHaveAttribute("data-status", "succeeded", { timeout: WORKFLOW_TIMEOUT });
    });
    const summary = page.getByTestId("run-summary");
    // the branches explain themselves: the series wanders (differenced), no strong season
    await expect(summary).toContainText("so it was differenced");
    await expect(summary).toContainText("so it was not seasonally differenced");
    await expect(summary).not.toContainText("->");
    await expect(summary).toContainText("beats every benchmark");
    await shot(page, "run-monitor");

    await page.getByRole("tab", { name: "Steps" }).click();
    const steps = page.getByRole("table", { name: "Steps" });
    await steps.getByRole("button", { name: /Does it wander\? \(ADF\)/ }).click();
    const drawer = page.getByTestId("node-drawer");
    await expect(drawer.getByTestId("test-verdict")).toContainText("The series wanders");
    await expect(drawer.getByText("Sandbox run")).toBeVisible();
    await shot(page, "adf-drawer");
    await page.keyboard.press("Escape");
    await steps.getByRole("button", { name: /Is it stable\? \(KPSS\)/ }).click();
    await expect(page.getByTestId("node-drawer").getByTestId("test-verdict")).toContainText("The series wanders");
    await page.keyboard.press("Escape");
    await steps.getByRole("button", { name: /AR and MA orders/ }).click();
    await expect(page.getByTestId("node-drawer")).toContainText(/best_p|AR order|p =/);
    await page.keyboard.press("Escape");
    await expect(steps).toContainText(/skipped/i); // the seasonal difference was not needed

    await page.getByRole("tab", { name: /Decisions/ }).click();
    const decisions = page.getByTestId("branch-decision");
    await expect(decisions.first()).toContainText("The condition was met.");
    await expect(decisions.filter({ hasText: "nodes.adf.reject" }).first()).toBeVisible();

    await step("workflow.model_explorer", async () => {
      await page.getByTestId("run-models").getByRole("link", { name: /in the Model Explorer/ }).first().click();
      await expect(page).toHaveURL(/\/models\/mdl_[a-z0-9]+\/versions\/1\/explore/);
      await expect(page.getByRole("heading", { name: "At a glance" })).toBeVisible({ timeout: 60_000 });
      const nav = page.getByRole("navigation", { name: "Model Explorer sections" });
      await nav.getByRole("button", { name: /Parameters/ }).click();
      await expect(page.getByRole("table", { name: "Parameters" })).toContainText("price");
    });
    expect(errors).toEqual([]);
  });

  test("Guided: from the Data Explorer to the readiness recipe on that data, run live @e2e", async ({ page }) => {
    test.setTimeout(WORKFLOW_TIMEOUT + 2 * 60_000);
    const errors = watch(page);
    await page.goto("/");
    await openProject(page);
    await page.getByRole("radiogroup", { name: "Mode" }).getByRole("radio", { name: "Guided" }).click();
    await step("recipe", async () => {
      await page.goto(`/datasets/${seeded.datasets.weekly_sales}/versions/1/explore`);
      const nav = page.getByRole("navigation").filter({ has: page.getByRole("link", { name: /Recipes/ }) });
      await expect(nav.getByRole("link", { name: /Help me choose/ })).toBeVisible();
      await page.getByRole("link", { name: "Check it is ready to forecast" }).click();
      await expect(page).toHaveURL(/\/workflows\/recipe\?template=ts_readiness_arimax&data=/);
      const runner = page.getByTestId("recipe-runner");
      await expect(runner.getByTestId("about-card")).toContainText("Time-series readiness to ARIMAX");
      await expect(runner.getByLabel("Dataset")).toHaveValue(`dataset://${seeded.project}/weekly_sales`);
      // the price plan is known ahead: the one setting worth changing, under Fine-tune
      await runner.getByText("Fine-tune (optional: the defaults suit most data)").click();
      await runner.getByLabel("Drivers").fill("price");
      await runner.getByRole("button", { name: "Run this recipe" }).click();
      await expect(page).toHaveURL(/\/workflow-runs\/run_/);
      await expect(page.getByTestId("run-monitor")).toHaveAttribute("data-status", "succeeded", { timeout: WORKFLOW_TIMEOUT });
      await expect(page.getByTestId("run-summary")).toContainText("Difference? -> then");
      await expect(page.getByTestId("run-summary")).toContainText("beats every benchmark");
      await shot(page, "guided-recipe-run");
      await page.getByTestId("run-models").getByRole("link", { name: /in the Model Explorer/ }).first().click();
      await expect(page.getByRole("heading", { name: "At a glance" })).toBeVisible({ timeout: 60_000 });
    });
    expect(errors).toEqual([]);
  });

  test("the Data Explorer finds the unit root @e2e", async ({ page }) => {
    test.setTimeout(COMPUTE_TIMEOUT + 60_000);
    const errors = watch(page);
    await page.goto("/");
    await openProject(page);
    await step("data_explorer", async () => {
      await page.goto(`/datasets/${seeded.datasets.weekly_sales}/versions/1/explore`);
      await expect(page.getByTestId("plain-summary").first()).toContainText("over 156 weeks, from 2022-01-02");
      const nav = page.getByRole("navigation", { name: "Data Explorer sections" });
      await nav.getByRole("button", { name: /Over time/ }).click();
      await plotsRendered(page);
      await nav.getByRole("button", { name: /Stable or wandering/ }).click();
      const missing = page.getByTestId("section-missing");
      if (await missing.isVisible()) await missing.getByRole("button", { name: "Compute it now" }).click();
      const table = page.getByRole("table", { name: "Stationarity by series" });
      await expect(table).toBeVisible({ timeout: COMPUTE_TIMEOUT });
      await expect(table).toContainText("sales");
      await shot(page, "data-explorer-stationarity");
    });
    expect(errors).toEqual([]);
  });

  test("the Model Explorer of a statsmodels VAR: impulse responses and variance decomposition @e2e", async ({ page }) => {
    const errors = watch(page);
    await page.goto("/");
    await openProject(page);
    await step("model_explorer.var", async () => {
      await page.goto(`/models/${seeded.models["macro-var"]}/versions/1/explore?section=irf`);
      await expect(page.getByLabel("A shock to")).toBeVisible({ timeout: 60_000 });
      await plotsRendered(page);
      await shot(page, "var-irf");
      await page.goto(`/models/${seeded.models["macro-var"]}/versions/1/explore?section=fevd`);
      await expect(page.getByTestId("plain-summary").first()).toContainText(/surprises|shocks/);
      await plotsRendered(page);
    });
    expect(errors).toEqual([]);
  });

  test("mmm-framework: contributions, response curves and an optimised budget @e2e", async ({ page }) => {
    test.setTimeout(COMPUTE_TIMEOUT + 2 * 60_000);
    const errors = watch(page);
    await page.goto("/");
    await openProject(page);
    const mmm = seeded.models["geo-mmm"];
    await step("model_explorer.mmm", async () => {
      await page.goto(`/models/${mmm}/versions/1/explore?section=decomposition`);
      await expect(page.getByTestId("plain-summary").first()).toBeVisible({ timeout: 60_000 });
      await plotsRendered(page);
      await page.goto(`/models/${mmm}/versions/1/explore?section=response_curves`);
      await expect(page.getByTestId("plain-summary").first()).toContainText("the dot is what you spend today");
      await expect(page.getByRole("checkbox", { name: "tv", exact: true })).toBeChecked();
      await expect(page.getByRole("checkbox", { name: "search", exact: true })).toBeChecked();
      await plotsRendered(page);
      await shot(page, "mmm-response-curves");
    });
    await step("optimize", async () => {
      await page.goto(`/models/${mmm}/versions/1/optimize`);
      await expect(page.getByRole("heading", { name: "Optimise a budget" })).toBeVisible();
      await page.getByRole("button", { name: "Optimise the budget" }).click();
      const plan = page.getByTestId("plan-result");
      await expect(plan).toBeVisible({ timeout: COMPUTE_TIMEOUT });
      await expect(plan.getByRole("table", { name: "Allocation" })).toContainText("tv");
      await expect(plan.getByRole("table", { name: "Allocation" })).toContainText("search");
      await expect(plan).toContainText("chance it beats today's plan");
      await shot(page, "optimize-plan");
    });
    expect(errors).toEqual([]);
  });

  test("governance: the model fitted on noise failed its shuffled-target test @e2e", async ({ page }) => {
    const errors = watch(page);
    await page.goto("/");
    await openProject(page);
    await step("governance", async () => {
      await page.goto(`/validations/${seeded.validation}`);
      await expect(page.getByText(/Checks failed/i).first()).toBeVisible({ timeout: 60_000 });
      const check = page.locator('[data-check="null.permutation_target"]').first();
      await expect(check).toHaveAttribute("data-status", "fail");
      await expect(page.locator('[data-check="null.noise_regressors"]').first()).toBeVisible();
      await shot(page, "validation-noise");
      await page.goto(`/models/${seeded.models["arimax-noise"]}/versions/1`);
      await expect(page.getByRole("link", { name: /^Validation: Checks failed/ })).toBeVisible();
    });
    expect(errors).toEqual([]);
  });
});
