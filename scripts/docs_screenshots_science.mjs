#!/usr/bin/env node
/**
 * Capture the screenshots of the science tutorials (docs/tutorials/pymc-challenges/e18, e51
 * and e21, and docs/tutorials/complex-workflows.md) and of the Data Explorer on scientific data
 * from a real dev stack. It follows the
 * tutorials: it uploads the exported datasets, builds each spec from the YAML of its page,
 * fits it, opens the plots and the Fields section, and runs the recipe sci_spatial_ladder.
 *
 *   make dev                                          # shell 1: a dev-profile stack (any ports:
 *                                                     #   MAPI_* variables, as for make dev)
 *   mkdir -p .local/science && cd .local/science      # shell 2: the three exports
 *   uv run python -m mapi_plugin_science.export e18 --challenges <pymc-challenges>/data --out scotland_lip.nc
 *   uv run python -m mapi_plugin_science.export e51 --challenges <pymc-challenges>/data --out heat_plate_e51.nc
 *   uv run python -m mapi_plugin_science.export e21 --challenges <pymc-challenges>/data --out lazega_coworkers.nc
 *   cd ../.. && make docs-screenshots-science         # this script into .local/screenshots-science,
 *                                                     #   then optimise_screenshots.py --only those
 *
 * The explorer part (run first) also needs three more exports in the same folder:
 *
 *   uv run python -m mapi_plugin_science.export e58 --challenges <pymc-challenges>/data --out unc13_dstorm.nc
 *   uv run python -m mapi_plugin_science.export e23 --challenges <pymc-challenges>/data --out primates301.nc
 *   uv run python -m mapi_plugin_science.export e81b --challenges <pymc-challenges>/data --out bci_camera_traps.nc
 *
 * It opens the Data Explorer of the raw datasets (E18 map, E21 network, E58 images, E23 tree,
 * E81b times of day; docs/guides/data-explorer.md) and uploads a dataset when its project does
 * not have it. A second argument selects the parts: `explorer`, `tutorials` or both (default):
 *
 *   node scripts/docs_screenshots_science.mjs .local/screenshots-science explorer
 *
 * MAPI_SCIENCE_DATA names the folder of the .nc files (default .local/science). The
 * script signs in through the development IdP as MAPI_DEV_ACCOUNT (default admin@dev.local).
 * It makes the projects epidemiology, engineering, sociology, microscopy, biology and ecology
 * when they are not there. When
 * a dataset or a spec is already there, it uses it again (the upload preview then comes from
 * "Upload new version", without an ingest). Each run fits the three specs again, so each run
 * adds a model version. The sandbox time is about three minutes. One PNG per view at
 * 1440 x 900, light theme. Playwright comes from the web app's dev dependencies (chromium only).
 */
import { mkdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(join(ROOT, "apps/web/package.json"));
const { chromium, expect } = require("@playwright/test");

const WEB = (process.env.MAPI_WEB_ORIGIN ?? "http://127.0.0.1:5173").replace(/\/$/, "");
const API = (process.env.MAPI_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const ACCOUNT = process.env.MAPI_DEV_ACCOUNT || "admin@dev.local";
const DATA = resolve(process.env.MAPI_SCIENCE_DATA ?? join(ROOT, ".local/science"));
const OUT = resolve(process.argv[2] ?? join(ROOT, ".local/screenshots-science"));
mkdirSync(OUT, { recursive: true });

/** The three cases: the project, the dataset, the file, the Insert search and the model title. */
const CASES = {
  e18: {
    page: "e18-scottish-lip-cancer.md",
    project: ["epidemiology", "Epidemiology"],
    dataset: "scotland_lip_cancer",
    description: "Male lip cancer in 56 districts of Scotland, 1975 to 1980, with district outlines",
    file: "scotland_lip.nc",
    search: "areal",
    model: "Disease risk on a map of areas",
  },
  e51: {
    page: "e51-heat-plate-design.md",
    project: ["engineering", "Engineering"],
    dataset: "heat_plate_e51",
    description: "Calibration of the gradient plate: three 10 W test runs, twelve thermocouples",
    file: "heat_plate_e51.nc",
    search: "heat",
    model: "Temperature of a heated plate",
  },
  e21: {
    page: "e21-law-firm-network.md",
    project: ["sociology", "Sociology"],
    dataset: "lazega_coworkers",
    description: "Coworker ties of the 71 lawyers of the Lazega law firm",
    file: "lazega_coworkers.nc",
    search: "network",
    model: "Who works with whom in a network",
  },
};

/**
 * The Data Explorer on the raw exports (docs/guides/data-explorer.md, part "Scientific data"):
 * the project, the dataset, the file, the section and the screenshot. E18 and E21 are the
 * datasets of CASES; the others are uploaded when they are not there.
 */
const EXPLORER = [
  { case: "e18", project: CASES.e18.project, dataset: CASES.e18.dataset, file: CASES.e18.file, section: "map", shot: "science-explorer-map" },
  { case: "e21", project: CASES.e21.project, dataset: CASES.e21.dataset, file: CASES.e21.file, section: "network", shot: "science-explorer-network" },
  { case: "e58", project: ["microscopy", "Microscopy"], dataset: "dannhauser2022_unc13_dstorm", file: "unc13_dstorm.nc", section: "images", shot: "science-explorer-images" },
  { case: "e23", project: ["biology", "Biology"], dataset: "primates301", file: "primates301.nc", section: "tree", shot: "science-explorer-tree" },
  { case: "e81b", project: ["ecology", "Ecology"], dataset: "bci_camera_traps", file: "bci_camera_traps.nc", section: "circular", shot: "science-explorer-circular" },
];

/** The parts of this script to run: `tutorials`, `explorer` or both (the second argument). */
const PARTS = new Set((process.argv[3] || "tutorials,explorer").split(",").map((p) => p.trim()));

/** The full spec of a tutorial page: its first YAML block with `kind: ModelSpec`. */
function specOf(page) {
  const text = readFileSync(join(ROOT, "docs/tutorials/pymc-challenges", page), "utf8");
  const block = [...text.matchAll(/```yaml\n([\s\S]*?)```/g)].map((m) => m[1]).find((b) => b.includes("kind: ModelSpec"));
  if (!block) throw new Error(`${page} has no ModelSpec block`);
  return block;
}

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "light" });
await context.grantPermissions(["clipboard-read", "clipboard-write"]);
const page = await context.newPage();
page.setDefaultTimeout(30_000);

const session = await (await page.request.get(`${API}/auth/session`)).json();
if (!session.authenticated) {
  await page.goto(`${API}/auth/login?return_to=${encodeURIComponent("/")}`);
  await page.getByRole("link", { name: ACCOUNT, exact: true }).click();
  await page.waitForURL((url) => url.toString().startsWith(WEB), { waitUntil: "commit" });
}

async function shot(name, { keepPointer = false } = {}) {
  if (!keepPointer) {
    for (const b of await page.getByRole("button", { name: "Dismiss" }).all()) await b.click().catch(() => undefined);
    await page.mouse.move(0, 0);
  }
  await page.waitForTimeout(400);
  await page.screenshot({ path: join(OUT, `${name}.png`), animations: "disabled" });
  console.log(`${name}.png`);
}

async function go(path) {
  await page.goto(`${WEB}${path}`);
  await page.waitForLoadState("networkidle").catch(() => undefined);
}

/** Select the project in the header switcher. Make it first if it is not there. */
async function useProject([id, name]) {
  await go("/");
  const switcher = page.getByRole("button", { name: /Switch project/ });
  await switcher.click();
  const item = page.getByRole("menuitem", { name: new RegExp(`^${name}`) });
  if (await item.count()) {
    await item.first().click();
  } else {
    await page.getByText(/New project in/).first().click();
    await page.getByRole("textbox", { name: "Name", exact: true }).fill(name);
    await page.getByRole("textbox", { name: "Id", exact: true }).fill(id);
    await page.getByRole("button", { name: "Create project" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await switcher.click();
    await item.first().click();
  }
  await expect(switcher).toContainText(name);
}

/** Wait until the sandbox has listed the arrays of the chosen file. */
async function previewStructure() {
  await page.getByRole("button", { name: "Preview structure" }).click();
  await expect(page.getByText(/The sandbox lists the arrays/)).toBeVisible();
  await expect(page.getByText(/The sandbox lists the arrays/)).toHaveCount(0, { timeout: 180_000 });
  await expect(page.getByText(/arrays in \d+ groups below the top level/)).toBeVisible();
}

/** Upload the export as a new dataset (tutorial step 3), or preview it again on an old one. */
async function upload(c, shotName) {
  await go("/datasets");
  const row = page.getByRole("row").filter({ hasText: `dataset://${c.project[0]}/${c.dataset}` });
  if (await row.count()) {
    await row.first().click();
    await page.getByRole("button", { name: /Upload new version/ }).click();
    await page.locator("input[type=file]").first().setInputFiles(join(DATA, c.file));
    await previewStructure();
    if (shotName) {
      await page.getByText("Upload a new version").scrollIntoViewIfNeeded();
      await shot(shotName);
    }
    await page.getByRole("button", { name: "Close upload" }).click();
    return;
  }
  await page.getByRole("button", { name: "New dataset" }).first().click();
  await page.getByRole("textbox", { name: "Name", exact: true }).fill(c.dataset);
  await page.getByRole("textbox", { name: "Description" }).fill(c.description);
  await page.getByRole("button", { name: "Create dataset" }).click();
  await page.locator("input[type=file]").first().setInputFiles(join(DATA, c.file));
  await previewStructure();
  if (shotName) await shot(shotName);
  await page.getByRole("button", { name: "Upload and ingest" }).click();
  await expect(page.getByRole("tab", { name: "Schema" })).toBeVisible({ timeout: 300_000 });
}

/** Build the spec of the page in the YAML editor (tutorial step 4), or open the saved one. */
async function buildSpec(c) {
  const yaml = specOf(c.page);
  const name = yaml.match(/name: ([\w-]+)/)[1];
  await go("/build");
  const row = page.getByRole("row").filter({ hasText: `spec://${c.project[0]}/${name}` });
  if (await row.count()) {
    await row.first().click();
    await expect(page).toHaveURL(/\/build\/spc_/);
    await page.getByRole("radio", { name: "YAML" }).click();
  } else {
    await page.getByRole("link", { name: /New spec/ }).first().click();
    await page.getByRole("radio", { name: "YAML" }).click();
    const lines = page.getByTestId("yaml-editor").locator(".monaco-editor .view-lines");
    await expect(lines).toContainText("apiVersion: mapi/v1", { timeout: 30_000 });
    const panel = page.getByRole("tabpanel");
    await page.getByRole("tab", { name: "Insert" }).click();
    await panel.locator("summary").filter({ hasText: /^Datasets/ }).click();
    await panel.getByRole("button", { name: new RegExp(c.dataset) }).first().click();
    await panel.getByPlaceholder(/Search models/).fill(c.search);
    await panel.getByRole("button", { name: new RegExp(c.model) }).first().click();
    // replace the rest of the starter template with the spec of the page (a paste keeps the
    // indentation; typing would be re-indented by the editor)
    await page.evaluate((text) => navigator.clipboard.writeText(text), yaml);
    await expect(async () => {
      await lines.click();
      await page.keyboard.press("ControlOrMeta+A");
      await page.keyboard.press("Delete");
      await expect(lines).not.toContainText("apiVersion", { timeout: 2_000 });
      await page.keyboard.press("ControlOrMeta+V");
      await expect(lines).toContainText("outputs", { timeout: 3_000 });
      await page.keyboard.press("ControlOrMeta+ArrowUp");
      await expect(lines).toContainText(name, { timeout: 3_000 });
    }).toPass({ timeout: 30_000 });
    await page.getByRole("button", { name: "Validate" }).click();
    await page.getByRole("button", { name: "Save spec" }).click();
    await expect(page).toHaveURL(/\/build\/spc_/);
  }
  await expect(page.getByText("valid", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Pipeline" }).click();
}

/** Fit from the editor and open the new model version (tutorial step 5). */
async function fit(runShot) {
  await page.getByRole("button", { name: "Fit" }).click();
  await expect(page).toHaveURL(/\/runs\/run_/);
  await expect(page.locator("[data-state=running]").first()).toBeVisible({ timeout: 180_000 });
  if (runShot) {
    await expect(page.getByText(/chain \d+\/\d+: (tuning|sampling)/).first()).toBeVisible({ timeout: 300_000 });
    await page.waitForTimeout(4_000); // a little further into the sampler
    await shot(runShot);
  }
  await expect(page.locator("[data-state=succeeded]").first()).toBeVisible({ timeout: 30 * 60_000 });
  await page.getByRole("link", { name: /Inspect model:\/\// }).click();
  await expect(page).toHaveURL(/\/models\/mdl_[^/]+\/versions\/\d+/);
  return new URL(page.url()).pathname;
}

/** Open one plot of the Plots tab and wait until the sandbox has drawn it. */
async function plot(title, name) {
  await page.getByRole("tab", { name: "Plots" }).click();
  await page.getByRole("navigation", { name: "Plots" }).getByRole("button", { name: new RegExp(title) }).click();
  const frame = page.locator('iframe[data-testid="plot-frame"]').first().contentFrame();
  await expect(frame.locator(".main-svg, svg.marks").first()).toBeVisible({ timeout: 90_000 });
  await page.waitForTimeout(2_500);
  await shot(name);
}

/**
 * Open the Data Explorer of the latest version of a raw export (upload it first when the
 * project does not have it) at one section, and wait until its pictures are drawn.
 */
async function explore(c) {
  await useProject(c.project);
  await go("/datasets");
  await page.getByText("dataset://").first().waitFor({ timeout: 20_000 }).catch(() => undefined);
  const ref = page.getByText(`dataset://${c.project[0]}/${c.dataset}`, { exact: true });
  if (await ref.count()) {
    await ref.first().click();
  } else {
    await upload({ ...c, description: `${c.case.toUpperCase()} export (pymc-challenges)` });
  }
  await page.getByRole("link", { name: /Explore this version/ }).first().click();
  await expect(page).toHaveURL(/\/versions\/\d+\/explore/);
  const url = new URL(page.url());
  url.searchParams.set("section", c.section);
  await go(`${url.pathname}${url.search}`);
  await expect(page.getByRole("navigation", { name: "Data Explorer sections" })).toBeVisible({ timeout: 30_000 });
  const main = page.locator("main");
  await expect(main.locator('iframe[data-testid="plot-frame"], canvas').first()).toBeVisible({ timeout: 60_000 });
  for (const f of await main.locator('iframe[data-testid="plot-frame"]').all()) {
    await expect(f.contentFrame().locator(".main-svg, svg.marks, canvas").first()).toBeVisible({ timeout: 90_000 });
  }
  // the section title at the top of the screenshot, below the header; for an image, the
  // viewer from its controls to the colour bar
  if (c.section === "images") {
    // the frame with the brightest spot (a molecule that blinks) of the frames that the slider shows
    const slider = page.getByTestId("field-viewer").first().locator("input[type=range]").first();
    if (await slider.count()) {
      const max = Number(await slider.getAttribute("max"));
      let best = [0, -1];
      for (let f = 0; f <= max; f++) {
        await slider.fill(String(f));
        await page.waitForTimeout(150);
        const peak = await page.getByTestId("field-viewer").first().locator("canvas").first().evaluate((el) => {
          const d = el.getContext("2d").getImageData(0, 0, el.width, el.height).data;
          let m = 0;
          for (let i = 0; i < d.length; i += 4) m = Math.max(m, d[i] + d[i + 1] + d[i + 2]);
          return m;
        });
        if (peak > best[1]) best = [f, peak];
      }
      await slider.fill(String(best[0]));
      await slider.evaluate((el) => el.blur());
    }
    await page.getByTestId("field-viewer").first().evaluate((el) => el.scrollIntoView({ block: "start" }));
    await page.mouse.wheel(0, -90);
  } else {
    // the network in the colours of the office of each lawyer (the homophily of the summary)
    if (c.section === "network") await page.getByLabel("Colour by").selectOption({ label: "office" });
    await page.getByTestId("plain-summary").first().evaluate((el) => el.scrollIntoView({ block: "start" }));
    await page.mouse.wheel(0, -240);
  }
  await page.waitForTimeout(2_500);
  await shot(c.shot);
}

/** The screenshots of the tutorials: E51, E18 and E21 fitted, and the E18 recipe. */
async function tutorials() {
  // ------------------------------------------------------------------ E51: upload, Fields
  const e51 = CASES.e51;
  await useProject(e51.project);
  await upload(e51, "science-upload-preview");
  await buildSpec(e51);
  const heat = await fit();
  await go(`${heat}/explore?section=fields`);
  const fields = page.getByRole("heading", { name: "Fields", exact: true });
  await expect(fields).toBeVisible();
  await expect(page.getByText("Mean of field")).toBeVisible({ timeout: 60_000 });
  await page.getByText("Temperature of each cell").evaluate((el) => el.scrollIntoView({ block: "start" }));
  await page.mouse.wheel(0, -24);
  await page.waitForTimeout(1_000);
  // the pointer on the hot spot of the test heater shows the readout of one cell
  const canvas = page.locator("canvas").first();
  const box = await canvas.boundingBox();
  if (box) await page.mouse.move(box.x + box.width * 0.145, box.y + box.height * 0.6);
  await page.waitForTimeout(800);
  await shot("science-fields-heat-plate", { keepPointer: true });

  // ------------------------------------------------------------------ E18: spec, run, area map
  const e18 = CASES.e18;
  await useProject(e18.project);
  await upload(e18);
  await buildSpec(e18);
  await shot("science-spec-editor");
  const lip = await fit("science-run-progress");
  await go(lip);
  await plot("Area map of rr", "science-area-map");

  // ------------------------------------------------------------------ E21: network graph
  const e21 = CASES.e21;
  await useProject(e21.project);
  await upload(e21);
  await buildSpec(e21);
  const net = await fit();
  await go(net);
  await plot("Network of tie_prob", "science-network-graph");

  // ------------------------------------------------------------------ E18: the ladder recipe
  await useProject(e18.project);
  await page.getByRole("radio", { name: "Guided" }).click();
  await page.getByRole("link", { name: "Recipes", exact: true }).click();
  const card = page
    .getByTestId("template-gallery")
    .locator(":scope > *")
    .filter({ has: page.getByRole("heading", { name: /^Disease map: which spatial model\?/ }) });
  await card.getByRole("link", { name: "Run on my data" }).click();
  const data = page.getByLabel("Dataset", { exact: true });
  await expect(data.locator("option", { hasText: e18.dataset })).toHaveCount(1);
  const options = await data.locator("option").allInnerTexts();
  await data.selectOption({ label: options.find((o) => o.startsWith(e18.dataset)) });
  await page.getByText("Fine-tune (optional", { exact: false }).click();
  await page.getByLabel("Engine").selectOption("nutpie");
  await page.getByRole("button", { name: "Run this recipe" }).click();
  await expect(page).toHaveURL(/\/workflow-runs\//, { timeout: 60_000 });
  const monitor = page.getByTestId("run-monitor");
  await expect(page.getByText(/[1-9]\d* of \d+ steps finished/)).toBeVisible({ timeout: 300_000 });
  await shot("science-recipe-monitor");
  await expect(monitor).toHaveAttribute("data-status", "succeeded", { timeout: 30 * 60_000 });
  await page.getByRole("tab", { name: "Outputs" }).click();
  // the report of the run: the metrics of the three rungs above the map of the best rung
  const report = page.getByRole("heading", { name: "Disease map: which spatial model?", level: 4 });
  await expect(report).toBeVisible({ timeout: 30_000 });
  const mapTitle = page.getByText("Relative risk map: Relative risk of each area (best model)");
  await mapTitle.evaluate((el) => el.scrollIntoView({ block: "start" }));
  await page.mouse.wheel(0, -300);
  const reportFrame = page.locator('iframe[data-testid="plot-frame"]').last().contentFrame();
  await expect(reportFrame.locator(".main-svg, svg.marks").first()).toBeVisible({ timeout: 90_000 });
  await page.waitForTimeout(2_500);
  await shot("science-recipe-report");
}

try {
  if (PARTS.has("explorer")) {
    // ---------------------------------------------------------------- the Data Explorer
    for (const c of EXPLORER) await explore(c);
  }
  if (PARTS.has("tutorials")) await tutorials();
} finally {
  await browser.close();
}
