/**
 * The M1 exit criterion, clicked through the web app against the real local stack
 * (`make dev`, then `make e2e` or `pnpm --filter @mapi/web e2e`):
 *
 *   upload the E33 data, side tables and score payloads → paste the spec into the editor,
 *   validate, save → fit → follow the run → inspect the scorecard, the PPC fan and the
 *   decomposition rendered inside the sandboxed plot iframe → score an 8-week forecast, a
 *   new market and a weekly EUR plan, and a counterfactual (tv spend +20%).
 *
 * Screenshots land in e2e/screenshots/ (git-ignored); the docs reuse a few of them.
 * Tagged @e2e: never part of the default (mocked) Playwright run.
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Locator, type Page } from "@playwright/test";

const API = (process.env.VITE_API_BASE ?? process.env.MAPI_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const PLOTS = (process.env.MAPI_PLOTS_ORIGIN ?? "http://127.0.0.1:5174").replace(/\/$/, "");
const ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const SHOTS = fileURLToPath(new URL("./screenshots/", import.meta.url));

/** Per-run names so a stack can be reused between runs without collisions. */
const RUN = Date.now().toString(36);
const DATASETS = {
  eu_media_daily: `ui_eu_media_daily_${RUN}`,
  fx_daily: `ui_fx_daily_${RUN}`,
  holidays_eu: `ui_holidays_eu_${RUN}`,
  eu_media_future: `ui_eu_media_future_${RUN}`,
  eu_media_future_fr: `ui_eu_media_future_fr_${RUN}`,
  eu_media_weekly_plan: `ui_eu_media_weekly_plan_${RUN}`,
  eu_media_window: `ui_eu_media_window_${RUN}`,
} as const;
type DemoFile = keyof typeof DATASETS;
const SPEC_NAME = `ui-geo-mmm-${RUN}`;

const state: { files: Record<string, string>; spec: string; modelPath: string } = {
  files: {},
  spec: "",
  modelPath: "",
};

async function shot(page: Page, name: string, target?: Locator): Promise<void> {
  await (target ?? page).screenshot({ path: join(SHOTS, `${name}.png`), animations: "disabled" });
}

/** The plot iframe's content once the sandbox reports a rendered SVG. */
async function renderedPlot(scope: Page | Locator): Promise<Locator> {
  const frame = scope.locator('iframe[data-testid="plot-frame"]').first();
  await expect(frame).toHaveAttribute("sandbox", "allow-scripts");
  await expect(frame).toHaveAttribute("src", `${PLOTS}/`);
  const svg = frame.contentFrame().locator("svg.marks").first();
  await expect(svg).toBeVisible({ timeout: 30_000 });
  // composite recipes once rendered at zero width: insist on a real drawing
  await expect.poll(async () => Number(await svg.getAttribute("width"))).toBeGreaterThan(100);
  return frame;
}

test.describe.configure({ mode: "serial" });

test.describe("@e2e M1: fit, inspect and score an E33 geo MMM from the UI", () => {
  test.beforeAll(async ({ request }) => {
    const res = await request.get(`${API}/readyz`);
    expect(res.ok(), `the API at ${API} must be ready (make dev)`).toBeTruthy();
    mkdirSync(SHOTS, { recursive: true });
    const dir = mkdtempSync(join(tmpdir(), "mapi-e2e-"));
    const out = execFileSync("uv", ["run", "python", "scripts/seed.py", "--export", dir], { cwd: ROOT, encoding: "utf8" });
    for (const line of out.trim().split("\n")) {
      const [name, path] = line.split(": ");
      if (name && path) state.files[name] = path;
    }
    let spec = readFileSync(state.files["eu-geo-mmm"] ?? "", "utf8");
    for (const [file, name] of Object.entries(DATASETS)) spec = spec.replaceAll(`dataset://default/${file}@latest`, `dataset://default/${name}@latest`);
    state.spec = spec.replace("name: eu-geo-mmm,", `name: ${SPEC_NAME},`);
    expect(state.spec).toContain(`name: ${SPEC_NAME},`);
  });

  test.beforeEach(({ page }) => {
    page.on("pageerror", (err) => {
      throw err;
    });
  });

  test("upload the data, side tables and score payloads @e2e", async ({ page }) => {
    test.setTimeout(10 * 60_000);
    for (const file of Object.keys(DATASETS) as DemoFile[]) {
      await page.goto("/datasets");
      await page.getByRole("button", { name: "New dataset" }).first().click();
      await page.getByLabel("Name").fill(DATASETS[file]);
      await page.getByRole("button", { name: "Create dataset" }).click();
      await expect(page).toHaveURL(/\/datasets\/ds_.*upload=true/);
      await page.locator('input[type="file"]').setInputFiles(state.files[file] ?? "");
      await expect(page.getByText("zarr_zip", { exact: true })).toBeVisible();
      await page.getByRole("button", { name: "Upload and ingest" }).click();
      await expect(page).toHaveURL(/version=1/);
      await expect(page.locator("main").getByText("ready", { exact: true })).toBeVisible({ timeout: 120_000 });
      if (file === "eu_media_daily") {
        await expect(page.getByRole("tab", { name: "Schema" })).toBeVisible();
        await shot(page, "01-dataset-ready");
      }
    }
  });

  test("validate the spec in the editor, save it and fit it @e2e", async ({ page, context }) => {
    test.setTimeout(15 * 60_000);
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.goto("/build/new");
    const editor = page.getByTestId("yaml-editor").locator(".monaco-editor");
    await expect(editor).toBeVisible({ timeout: 30_000 });
    const lines = editor.locator(".view-lines");
    await expect(lines).toContainText("apiVersion: mapi/v1");
    // the starter template has been validated once: the editor is settled
    await expect(page.getByText(/^\s*(valid|\d+ errors?)\s*$/)).toBeVisible({ timeout: 30_000 });
    // Monaco picks its keybindings from the user agent (the device preset may not be the host OS)
    const mod = (await page.evaluate(() => navigator.userAgent.includes("Macintosh"))) ? "Meta" : "Control";
    await page.evaluate((text) => navigator.clipboard.writeText(text), state.spec);
    // a paste keeps the YAML as is (typing would be re-indented by the editor); the browser,
    // not Monaco, maps the paste shortcut, so it follows the host OS. A paste right after the
    // editor settles is sometimes dropped, so the whole replacement is retried.
    await expect(async () => {
      await lines.click();
      await page.keyboard.press(`${mod}+A`);
      await page.keyboard.press("Delete");
      await expect(lines).not.toContainText("apiVersion", { timeout: 2_000 });
      await page.keyboard.press("ControlOrMeta+V");
      await expect(lines).toContainText("target_accept: 0.9", { timeout: 3_000 });
      await page.keyboard.press(mod === "Meta" ? "Meta+ArrowUp" : "Control+Home"); // back to the top
      await expect(lines).toContainText(`name: ${SPEC_NAME},`, { timeout: 3_000 }); // only the pasted spec has it
    }).toPass({ timeout: 30_000 });
    await page.getByRole("button", { name: "Validate" }).click();
    await expect(page.getByText("valid", { exact: true })).toBeVisible({ timeout: 30_000 });
    const pipeline = page.getByRole("list", { name: "Pipeline" });
    await expect(pipeline.getByText("to_eur", { exact: true })).toBeVisible();
    await expect(pipeline.getByRole("separator", { name: "Train/holdout split" })).toBeVisible();
    await shot(page, "02-editor-valid");

    await page.getByRole("button", { name: "Save spec" }).click();
    await expect(page).toHaveURL(/\/build\/spc_/);
    await expect(page.getByText("r1", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Fit" }).click();
    await expect(page).toHaveURL(/\/runs\/run_/);

    // live progress while the sampler runs
    await expect(page.getByText("Live")).toBeVisible({ timeout: 60_000 });
    await expect(page.locator("[data-state=running]").first()).toBeVisible({ timeout: 120_000 });
    await expect(page.getByRole("progressbar").first()).toBeVisible();
    // the sampler's own progress (live SSE frames, no event id) reaches the page
    await expect(page.getByText(/tuning|sampling|sampled/).first()).toBeVisible({ timeout: 120_000 });
    await shot(page, "03-run-progress");

    await expect(page.locator("[data-state=succeeded]").first()).toBeVisible({ timeout: 10 * 60_000 });
    const inspect = page.getByRole("link", { name: /Inspect model:\/\// });
    await expect(inspect).toBeVisible({ timeout: 30_000 });
    await shot(page, "04-run-succeeded");
    await inspect.click();
    await expect(page).toHaveURL(/\/models\/mdl_[^/]+\/versions\/\d+/);
    state.modelPath = new URL(page.url()).pathname;
  });

  test("inspect the scorecard and the plots rendered in the sandbox @e2e", async ({ page }) => {
    test.setTimeout(5 * 60_000);
    await page.goto(state.modelPath);
    const scorecard = page.getByRole("heading", { name: "Scorecard" }).locator("xpath=ancestor::section[1]");
    await expect(scorecard).toBeVisible();
    // the runner's scorecard (v0.4): one item per catalog check, titled in plain words
    await expect(scorecard.getByText("Sampler stayed on track", { exact: true })).toBeVisible();
    await expect(page.getByText(/^scorecard (pass|warn|fail)$/)).toBeVisible();
    await expect(page.getByText("coverage_90", { exact: true }).first()).toBeVisible();
    await shot(page, "05-scorecard");

    await page.getByRole("tab", { name: "Diagnostics" }).click();
    await expect(page.getByRole("tabpanel").getByText("RHAT").first()).toBeVisible();

    await page.getByRole("tab", { name: "Plots" }).click();
    const plots = page.getByRole("navigation", { name: "Plots" });
    for (const [title, file] of [
      ["Fit and forecast", "06-plot-ppc"],
      ["Contribution by component", "07-plot-decomposition"],
      ["Sampler convergence", "08-plot-convergence"],
    ] as const) {
      await plots.getByRole("button", { name: new RegExp(title) }).click();
      const card = page.locator("section").filter({ has: page.getByText(title, { exact: true }) }).last();
      const frame = await renderedPlot(card);
      if (title === "Fit and forecast") {
        // the sandbox is a separate, opaque origin: no access to the app, its storage or cookies
        const probe = await frame.contentFrame().locator("body").evaluate(() => {
          const blocked = (f: () => unknown): string => {
            try {
              f();
              return "allowed";
            } catch (e) {
              return (e as Error).name;
            }
          };
          return {
            origin: window.origin,
            parent: blocked(() => window.parent.document.title),
            storage: blocked(() => window.localStorage.length),
            cookie: blocked(() => document.cookie),
          };
        });
        expect(probe).toEqual({ origin: "null", parent: "SecurityError", storage: "SecurityError", cookie: "SecurityError" });
      }
      await shot(page, file, card);
    }
  });

  const scores = [
    { name: "forecast", data: DATASETS.eu_media_future, stage: "raw", markets: 3, shot: "09-score-forecast" },
    { name: "new market", data: DATASETS.eu_media_future_fr, stage: "raw", markets: 4, shot: "10-score-new-market" },
    { name: "weekly EUR plan", data: DATASETS.eu_media_weekly_plan, stage: "weekly", markets: 3, shot: "11-score-weekly-plan" },
  ];
  for (const s of scores) {
    test(`score: ${s.name} over 8 weeks @e2e`, async ({ page }) => {
      test.setTimeout(5 * 60_000);
      await page.goto(state.modelPath);
      await page.getByRole("main").getByRole("link", { name: "Score" }).click();
      await expect(page).toHaveURL(/\/score\?model=mdl_/);
      await expect(page.getByRole("radio", { name: "forecast" })).toHaveAttribute("aria-checked", "true");
      await expect(page.getByLabel("Periods")).toHaveValue("8");
      await page.getByLabel("Inputs enter at stage").selectOption(s.stage);
      await page.getByLabel("Input data").fill(`dataset://default/${s.data}@v1`);
      // the supplied fx table defaults to the version the fit used
      await expect(page.getByLabel("fx", { exact: true })).toHaveValue(`dataset://default/${DATASETS.fx_daily}@latest`);
      await page.getByRole("button", { name: "Score", exact: true }).click();
      const results = page.getByRole("region", { name: "Results" });
      await expect(results.locator("[data-state=succeeded]")).toBeVisible({ timeout: 4 * 60_000 });
      const runPlots = results.getByRole("region", { name: "Run plots" });
      await expect(runPlots.getByText("Fit and forecast")).toBeVisible();
      const fan = await renderedPlot(runPlots);
      // the forecast fan (v0.4: the KPI summed over markets, with the fitted history)
      await expect(fan.contentFrame().locator("svg.marks").first()).toContainText("date");
      // 8 weeks x markets x 5 components, one row per coordinate (stat pivoted into columns):
      // the new market is scored too (the table is virtualised: count, do not read, its rows)
      await expect(results.getByText(`${8 * s.markets * 5} rows`)).toBeVisible();
      await expect(results.getByRole("table", { name: "ui/summary.arrow" })).toContainText("y_hat_mean");
      // the quick chart over the pivoted summary (its frame loads lazily, once in view)
      const quick = results.locator('iframe[data-testid="plot-frame"]').nth(1);
      await quick.scrollIntoViewIfNeeded();
      await expect(quick.contentFrame().locator("svg.marks")).toBeVisible({ timeout: 30_000 });
      await runPlots.scrollIntoViewIfNeeded();
      await page.mouse.move(0, 0); // no hover toolbar over the plot
      await shot(page, s.shot);
    });
  }

  test("score: counterfactual with tv spend +20% @e2e", async ({ page }) => {
    test.setTimeout(5 * 60_000);
    await page.goto(state.modelPath);
    await page.getByRole("main").getByRole("link", { name: "Score" }).click();
    await page.getByRole("radio", { name: "counterfactual" }).click();
    await page.getByLabel("Input data").fill(`dataset://default/${DATASETS.eu_media_window}@v1`);
    await page.getByRole("button", { name: "Add" }).click();
    await page.getByRole("textbox", { name: "Variable", exact: true }).fill("spend_local");
    await page.getByRole("textbox", { name: "Selection", exact: true }).fill("channel=tv");
    await page.getByRole("textbox", { name: "Value", exact: true }).fill("1.2");
    await page.getByRole("radio", { name: "Predictive" }).click();
    await page.getByRole("button", { name: "Score", exact: true }).click();
    const results = page.getByRole("region", { name: "Results" });
    await expect(results.locator("[data-state=succeeded]")).toBeVisible({ timeout: 4 * 60_000 });
    const runPlots = results.getByRole("region", { name: "Run plots" });
    await expect(runPlots.getByText("Scenario vs baseline")).toBeVisible();
    await renderedPlot(runPlots);
    await runPlots.scrollIntoViewIfNeeded();
    await page.mouse.move(0, 0);
    await shot(page, "12-score-counterfactual");
  });
});
