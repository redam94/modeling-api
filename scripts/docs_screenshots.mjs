#!/usr/bin/env node
/**
 * Capture the documentation screenshots from a running, seeded local stack.
 *
 *   make dev                                    # shell 1 (any ports: MAPI_* variables)
 *   make seed                                   # shell 2
 *   uv run python scripts/seed.py --export demo/
 *   uv run mapi datasets upload demo/eu_media_future.zarr.zip --name eu_media_future
 *   uv run mapi datasets upload demo/eu_media_future_fr.zarr.zip --name eu_media_future_fr
 *   node scripts/docs_screenshots.mjs [out-dir]     # default: .local/screenshots
 *   uv run python scripts/optimise_screenshots.py .local/screenshots docs/assets/screenshots
 *
 * It opens the seeded `eu-geo-mmm` spec, fits it from the editor, follows the run, inspects
 * the model (scorecard, plots, data tree, preprocessing, provenance), scores an 8-week
 * forecast and a new market, and visits every other page. One PNG per view, at 1440 x 900.
 * Playwright comes from the web app's dev dependencies (chromium only).
 */
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(join(ROOT, "apps/web/package.json"));
const { chromium, expect } = require("@playwright/test");

const WEB = (process.env.MAPI_WEB_ORIGIN ?? "http://127.0.0.1:5173").replace(/\/$/, "");
const OUT = resolve(process.argv[2] ?? join(ROOT, ".local/screenshots"));
const SPEC = process.env.MAPI_DOCS_SPEC ?? "eu-geo-mmm";
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "light" });
const page = await context.newPage();
page.setDefaultTimeout(30_000);

async function shot(name, target) {
  await page.mouse.move(0, 0);
  await (target ?? page).screenshot({ path: join(OUT, `${name}.png`), animations: "disabled" });
  console.log(`${name}.png`);
}

async function go(path) {
  await page.goto(`${WEB}${path}`);
  await page.waitForLoadState("networkidle").catch(() => undefined);
}

/** Wait until a sandboxed plot inside `scope` has drawn a real SVG. */
async function plotDrawn(scope) {
  const frame = scope.locator('iframe[data-testid="plot-frame"]').first();
  const svg = frame.contentFrame().locator("svg.marks").first();
  await expect(svg).toBeVisible({ timeout: 60_000 });
  await expect.poll(async () => Number(await svg.getAttribute("width")), { timeout: 30_000 }).toBeGreaterThan(100);
}

try {
  // ------------------------------------------------------------------ build: the seeded spec
  await go("/build");
  await page.getByRole("row").filter({ hasText: `spec://default/${SPEC}` }).first().click();
  await expect(page).toHaveURL(/\/build\/spc_/);
  await expect(page.getByText("valid", { exact: true })).toBeVisible();
  await expect(page.getByRole("list", { name: "Pipeline" }).getByText("to_eur", { exact: true })).toBeVisible();
  await shot("editor");

  // ------------------------------------------------------------------ fit and follow the run
  await page.getByRole("button", { name: "Fit" }).click();
  await expect(page).toHaveURL(/\/runs\/run_/);
  await expect(page.locator("[data-state=running]").first()).toBeVisible({ timeout: 180_000 });
  await expect(page.getByText(/chain \d+\/\d+: (tuning|sampling)/).first()).toBeVisible({ timeout: 300_000 });
  await shot("run-progress");
  await expect(page.locator("[data-state=succeeded]").first()).toBeVisible({ timeout: 15 * 60_000 });
  const inspect = page.getByRole("link", { name: /Inspect model:\/\// });
  await expect(inspect).toBeVisible({ timeout: 60_000 });
  await shot("run-succeeded");
  await inspect.click();
  await expect(page).toHaveURL(/\/models\/mdl_[^/]+\/versions\/\d+/);
  const modelPath = new URL(page.url()).pathname;

  // ------------------------------------------------------------------ inspect
  await expect(page.getByRole("heading", { name: "Scorecard" })).toBeVisible();
  await shot("scorecard");
  await page.getByRole("tab", { name: /Diagnostics/ }).click();
  await expect(page.getByRole("tabpanel").getByText("RHAT").first()).toBeVisible();
  await shot("diagnostics");
  await page.getByRole("tab", { name: "Plots" }).click();
  const plots = page.getByRole("navigation", { name: "Plots" });
  await plots.getByRole("button", { name: /Contribution by channel/ }).click();
  const card = page.locator("section").filter({ has: page.getByText("Contribution by channel", { exact: true }) }).last();
  await plotDrawn(card);
  await shot("decomposition");
  await plots.getByRole("button", { name: /Fit and forecast/ }).click();
  await plotDrawn(page.locator("section").filter({ has: page.getByText("Fit and forecast", { exact: true }) }).last());
  await shot("ppc");
  for (const [tab, name] of [["Data tree", "data-tree"], ["Preprocessing", "preprocessing"], ["Provenance", "provenance"]]) {
    await page.getByRole("tab", { name: tab }).click();
    await page.waitForTimeout(1_500);
    await shot(name);
  }

  // ------------------------------------------------------------------ score
  for (const [data, name, market] of [["eu_media_future", "score-forecast", "NL"], ["eu_media_future_fr", "score-new-market", "FR"]]) {
    await go(modelPath);
    await page.getByRole("main").getByRole("link", { name: "Score" }).click();
    await expect(page).toHaveURL(/\/score\?model=mdl_/);
    await page.getByLabel("Input data").fill(`dataset://default/${data}@v1`);
    if (name === "score-forecast") await shot("score-form");
    await page.getByRole("button", { name: "Score", exact: true }).click();
    const results = page.getByRole("region", { name: "Results" });
    await expect(results.locator("[data-state=succeeded]")).toBeVisible({ timeout: 5 * 60_000 });
    const runPlots = results.getByRole("region", { name: "Run plots" });
    await plotDrawn(runPlots);
    await expect(runPlots.locator('iframe[data-testid="plot-frame"]').first().contentFrame().locator("svg.marks").first()).toContainText(market);
    await runPlots.scrollIntoViewIfNeeded();
    await shot(name);
  }

  // ------------------------------------------------------------------ every other page
  for (const [path, name] of [
    ["/", "dashboard"],
    ["/datasets", "datasets"],
    ["/runs", "runs"],
    ["/models", "models"],
    ["/catalog", "catalog"],
    ["/settings", "settings"],
    ["/guided", "guided"],
    ["/build/new", "editor-new"],
  ]) {
    await go(path);
    await page.waitForTimeout(1_500);
    await shot(name);
  }
  await go("/datasets");
  await page.getByRole("row").filter({ hasText: "dataset://default/eu_media_daily" }).first().click();
  await expect(page.getByRole("tab", { name: "Schema" })).toBeVisible();
  await page.waitForTimeout(1_000);
  await shot("dataset-detail");
} finally {
  await browser.close();
}
