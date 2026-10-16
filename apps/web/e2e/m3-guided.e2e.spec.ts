/**
 * The M3 "unaided tester" script, clicked through the web app against the real local stack
 * (`make dev`, then `make e2e` or `pnpm --filter @mapi/web e2e`): what a non-technical analyst
 * does in Guided mode - upload → report → scenario - followed by the Integrations hub
 * (endpoint, snippets and token, webhook, embed link).
 *
 * Every step is timed (wall clock, written to e2e/screenshots/m3-timings.json) and captured
 * (e2e/screenshots/m3-*.png, git-ignored) for the usability review recorded in
 * docs/operations/m3-verification.md. Automation clicks without hesitation, so the timings are
 * a floor for a human tester: the system's own waits (upload, preview, fit, exact runs).
 *
 * `tests/e2e/m3_browser_seed.py` creates a fresh project in the `default` tenant and writes the
 * analyst's spreadsheet (a weekly CSV for five regions and three channels over two years); the
 * project is deleted afterwards. The signed-in account is the global setup's
 * (admin@dev.local, the default tenant's admin). Tagged @e2e: never part of the mocked run.
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Locator, type Page } from "@playwright/test";

const ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const SHOTS = fileURLToPath(new URL("./screenshots/", import.meta.url));
const RUN = Date.now().toString(36);
const FIT_TIMEOUT = 10 * 60_000;
const SCORE_TIMEOUT = 3 * 60_000;

interface Seeded {
  tenant: string;
  tenant_name: string;
  project: string;
  project_name: string;
  csv: string;
}
let seeded: Seeded;
const timings: Record<string, number> = {};
const state: { analysis?: string; alias?: string; embed?: string } = {};

function seedScript(...args: string[]): string {
  return execFileSync("uv", ["run", "python", "tests/e2e/m3_browser_seed.py", ...args], { cwd: ROOT, encoding: "utf8" });
}

async function shot(page: Page, name: string, target?: Locator): Promise<void> {
  await page.mouse.move(0, 0);
  await (target ?? page).screenshot({ path: join(SHOTS, `${name}.png`), animations: "disabled", ...(target ? {} : { fullPage: true }) });
}

/** Time one step of the tester's journey (wall clock, seconds). */
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

/** Every plot iframe has finished rendering (its spinner is gone). */
async function frames(page: Page): Promise<void> {
  await expect(page.locator("figure .animate-spin")).toHaveCount(0, { timeout: 30_000 });
}

function watch(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(String(err)));
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  return errors;
}

test.describe.configure({ mode: "serial" });

test.describe("@e2e M3: an unaided tester in Guided mode, then the Integrations hub", () => {
  test.use({ actionTimeout: 30_000 });
  test.beforeAll(async ({ request }) => {
    test.setTimeout(5 * 60_000);
    const api = (process.env.VITE_API_BASE ?? process.env.MAPI_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
    const ready = await request.get(`${api}/readyz`);
    expect(ready.ok(), `the API at ${api} must be ready (make dev)`).toBeTruthy();
    mkdirSync(SHOTS, { recursive: true });
    seeded = JSON.parse(seedScript("seed", "--run", RUN).trim().split("\n").at(-1) ?? "{}") as Seeded;
    expect(seeded.project).toMatch(/^m3ui-/);
  });

  test.afterAll(() => {
    const total = Object.entries(timings)
      .filter(([k]) => k.startsWith("guided."))
      .reduce((s, [, v]) => s + v, 0);
    const report = { run: RUN, project: seeded?.project, timings_s: timings, guided_total_s: Math.round(total * 10) / 10 };
    writeFileSync(join(SHOTS, "m3-timings.json"), `${JSON.stringify(report, null, 2)}\n`);
    console.log(`M3 tester timings: ${JSON.stringify(report)}`);
    if (seeded?.project) seedScript("cleanup", "--project", seeded.project);
  });

  test("upload → report → scenario, as a first-time analyst @e2e", async ({ page }) => {
    test.setTimeout(25 * 60_000);
    const errors = watch(page);
    await page.setViewportSize({ width: 1366, height: 900 });

    // 1. "What do you want to learn?"
    await step("guided.1_template", async () => {
      await page.goto("/guided");
      await openProject(page);
      await expect(page.getByRole("heading", { name: "What do you want to learn?" })).toBeVisible();
      await shot(page, "m3-01-templates");
      await page.getByRole("link", { name: /^Geo marketing mix/ }).click();
      await expect(page).toHaveURL(/\/guided\/new\/geo_mmm$/);
    });

    // 2. the spreadsheet, the questions (pre-filled from the browser's profile), the upload
    await step("guided.2_data_and_upload", async () => {
      await page.getByLabel("Name this analysis").fill("Media mix by region");
      await page.getByLabel("Choose a data file").setInputFiles(seeded.csv);
      await expect(page.getByLabel("Which column is the result you want to explain?")).toHaveValue("Revenue");
      await expect(page.getByLabel("Which column holds the date or week?")).toHaveValue("Week");
      await expect(page.getByLabel("Which column names the market or region?")).toHaveValue("Region");
      const spend = page.getByRole("group", { name: /media spend/ });
      for (const ch of ["TV Spend", "Search Spend", "Social Spend"]) await expect(spend.getByRole("checkbox", { name: new RegExp(ch) })).toBeChecked();
      const preview = page.getByRole("complementary", { name: "Preview" });
      await expect(preview).toContainText("Everything the model needs is answered.");
      await shot(page, "m3-02-data-questions");
      await page.getByRole("button", { name: "Upload and continue" }).click();
      await expect(page).toHaveURL(/\/guided\/spc_[0-9a-z]+\/priors$/, { timeout: SCORE_TIMEOUT });
      state.analysis = /\/guided\/(spc_[0-9a-z]+)\//.exec(page.url())?.[1];
    });

    // 3. what the analyst believes: one ROI range, previewed in the sandbox
    await step("guided.3_beliefs_and_preview", async () => {
      await expect(page.getByRole("heading", { name: "What you believe" })).toBeVisible();
      await shot(page, "m3-03-beliefs");
      const tv = page.locator('[data-channel="tv"]');
      await expect(page.locator('[data-channel="search"]')).toContainText("No view on this channel", { timeout: SCORE_TIMEOUT });
      await tv.getByRole("checkbox").click();
      await tv.getByLabel("From").fill("1.5");
      await tv.getByLabel("To").fill("4");
      await expect(tv).toContainText("Every 1 spent brings back between 1.5 and 4.");
      await page.getByRole("button", { name: "Preview what this means" }).click();
      await expect(page.getByRole("heading", { name: "What your beliefs mean, before seeing the data" })).toBeVisible({ timeout: SCORE_TIMEOUT });
      await expect(page.locator('iframe[title="Prior return on spend per channel"]')).toBeVisible();
      await expect(page.getByText(/Results without any media: /)).not.toContainText(/-\d+%/);
      // plot frames load as they scroll into view
      await page.locator('iframe[title="Results the model expects before fitting"]').scrollIntoViewIfNeeded();
      await frames(page);
      await shot(page, "m3-04-beliefs-preview");
      await page.getByRole("button", { name: "Save and continue" }).click();
      await expect(page).toHaveURL(/\/run$/);
    });

    // 4. fit, with plain-language progress
    await step("guided.4_fit", async () => {
      await page.getByRole("button", { name: "Fit the model" }).click();
      await expect(page.getByRole("progressbar", { name: "Fit progress" })).toBeVisible({ timeout: 60_000 });
      await shot(page, "m3-05-fit-progress");
      await expect(page.getByRole("heading", { name: "Your model is ready" })).toBeVisible({ timeout: FIT_TIMEOUT });
      await shot(page, "m3-06-fit-done");
      await page.getByRole("button", { name: "See the results" }).click();
      await expect(page).toHaveURL(/\/report$/);
    });

    // 5. the report and the sanity scorecard
    await step("guided.5_report", async () => {
      await expect(page.getByRole("heading", { name: "Sanity scorecard" })).toBeVisible({ timeout: 60_000 });
      const table = page.getByRole("table", { name: /Return on spend and share of results per channel/ });
      await expect(table.getByRole("rowheader", { name: "tv" })).toBeVisible();
      await expect(page.locator('iframe[title="Fit and forecast"]')).toBeVisible();
      for (const check of ["holdout.mape", "data.history", "convergence.rhat"]) {
        await expect(page.locator(`[data-check="${check}"]`)).toBeVisible();
      }
      // the headline reads MAPE as a fraction (the SDK's metric is in percent)
      await expect(page.getByText("Forecast error on held-back weeks").locator("..")).not.toContainText(/\b[1-9]\d%/);
      await frames(page);
      await shot(page, "m3-07-report");
    });

    // 6. the scenario: a slider answered instantly, then the exact run, a preset, a forecast
    await step("guided.6_scenario", async () => {
      await page.getByRole("button", { name: "What if…" }).click();
      await expect(page).toHaveURL(/\/explore$/);
      const slider = page.getByRole("slider", { name: "Spend on search" });
      await slider.focus();
      for (let i = 0; i < 4; i++) await page.keyboard.press("ArrowRight");
      await expect(slider).toHaveAttribute("aria-valuetext", /^\+20%/);
      await expect(page.getByTestId("whatif-delta")).toHaveText(/^\+/);
      await shot(page, "m3-08-whatif");
      await page.getByRole("button", { name: "Run exact" }).click();
      await expect(page.getByTestId("exact-result")).toContainText(/\+/, { timeout: SCORE_TIMEOUT });
      await page.getByLabel("What if we turned off a channel?").selectOption("tv");
      await page.getByRole("button", { name: "Turn off tv" }).click();
      await expect(page.getByTestId("scenario-result")).toContainText(/−/, { timeout: SCORE_TIMEOUT });
      await page.getByRole("button", { name: "Make a forecast" }).click();
      await expect(page.getByTestId("forecast-result")).toContainText("Over the next", { timeout: SCORE_TIMEOUT });
      await expect(page.locator('iframe[title="Forecast with its likely range"]')).toBeVisible();
      await frames(page);
      await shot(page, "m3-09-scenarios");
    });

    // 7. share: the gate explained in plain words; promote (with an admin's reason on a warning)
    await step("share.promote_and_link", async () => {
      await page.getByRole("button", { name: /^Share/ }).click();
      await expect(page).toHaveURL(/\/share$/);
      const gate = page.getByTestId("gate-sentence");
      await expect(gate).toBeVisible();
      await shot(page, "m3-10-share-gate");
      const reason = page.getByLabel("Why is it fine to go live anyway?");
      await expect(page.getByRole("button", { name: /go live/i })).toBeVisible();
      if (await reason.isVisible()) {
        await reason.fill("Reviewed the scorecard warnings for this test; fine for planning.");
        await page.getByRole("button", { name: "Approve and go live" }).click();
      } else {
        const golive = page.getByRole("button", { name: /go live/i });
        if (await golive.isEnabled()) await golive.click();
      }
      const live = page.getByTestId("endpoint-live");
      const promoted = await live
        .waitFor({ state: "visible", timeout: 30_000 })
        .then(() => true)
        .catch(() => false);
      if (promoted) {
        state.alias = /endpoint:\/\/[^/]+\/([a-z0-9-]+)/.exec((await live.textContent()) ?? "")?.[1];
        await page.getByRole("button", { name: "Create link" }).click();
        await expect(page.getByTestId("embed-link")).toContainText(/\/embed\/mpe_[0-9a-z]+/);
        state.embed = /https?:\/\/\S+?\/embed\/mpe_[0-9a-z]+/.exec((await page.getByTestId("embed-link").textContent()) ?? "")?.[0];
      }
      await shot(page, "m3-11-share-done");
    });
    expect(errors.filter((e) => !/ws:\/\/|WebSocket|favicon/.test(e))).toEqual([]);
  });

  test("the Integrations hub: endpoint, snippets, token, webhook and embed link @e2e", async ({ page, context }) => {
    test.setTimeout(10 * 60_000);
    test.skip(!state.alias, "the analysis was not promoted (see the share step's screenshot)");
    const errors = watch(page);
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.setViewportSize({ width: 1366, height: 900 });
    await step("hub.overview", async () => {
      await page.goto("/integrations");
      await openProject(page);
      await shot(page, "m3-12-hub-overview");
      await page.getByRole("tab", { name: "Endpoints" }).click();
      await page.getByRole("table", { name: "Endpoints" }).getByRole("link", { name: state.alias! }).click();
      await expect(page.getByRole("heading", { name: state.alias! })).toBeVisible();
      await shot(page, "m3-13-hub-endpoint");
    });
    await step("hub.try_it", async () => {
      await page.getByRole("tab", { name: "Schema and Try it" }).click();
      await page.getByRole("button", { name: "Validate" }).click();
      await expect(page.getByText("The call is valid")).toBeVisible({ timeout: 30_000 });
      await page.getByRole("button", { name: "Score", exact: true }).click();
      await expect(page.getByText("Scored")).toBeVisible({ timeout: SCORE_TIMEOUT });
      await shot(page, "m3-14-hub-try-it");
    });
    await step("hub.snippets_and_token", async () => {
      await page.getByRole("tab", { name: "Snippets" }).click();
      await page.getByRole("tab", { name: "curl" }).click();
      await page.getByRole("button", { name: "Copy curl snippet" }).click();
      expect(await page.evaluate(() => navigator.clipboard.readText())).toContain(`/v1/endpoints/${state.alias}/score`);
      await shot(page, "m3-15-hub-snippets");
      await page.getByRole("link", { name: `Create a score:${state.alias} token` }).click();
      const dialog = page.getByRole("dialog", { name: "New API token" });
      await expect(dialog.getByLabel("Score models (optional)")).toHaveValue(state.alias!);
      await dialog.getByLabel("Name", { exact: true }).fill(`sheets-${RUN}`);
      await dialog.getByRole("button", { name: "Create token" }).click();
      const secret = page.getByRole("dialog", { name: "Copy your new token now" });
      await expect(secret.getByLabel("Token", { exact: true })).toHaveValue(/^mapi_/);
      await shot(page, "m3-16-hub-token");
      await secret.getByRole("checkbox").click();
      await secret.getByRole("button", { name: "Done" }).click();
    });
    await step("hub.webhook", async () => {
      await page.goto("/integrations?tab=webhooks");
      await page.getByRole("button", { name: "New webhook" }).click();
      const create = page.getByRole("dialog", { name: "New webhook" });
      await create.getByLabel("URL").fill("https://127.0.0.1:8443/mapi-hook");
      await create.getByRole("checkbox", { name: /Endpoint promoted/ }).click();
      await create.getByRole("button", { name: "Create webhook" }).click();
      const secret = page.getByRole("dialog", { name: "Copy the signing secret now" });
      expect(await secret.getByLabel("Signing secret", { exact: true }).inputValue()).toMatch(/^whsec_/);
      await shot(page, "m3-17-hub-webhook-secret");
      await secret.getByRole("button", { name: "Copy signing secret" }).click();
      await secret.getByRole("button", { name: "Done" }).click();
      await expect(page.getByRole("table", { name: "Webhooks" }).getByText("https://127.0.0.1:8443/mapi-hook")).toBeVisible();
    });
    await step("hub.embed_view_and_revoke", async () => {
      expect(state.embed, "the share step made an embed link").toBeTruthy();
      const viewer = await context.newPage();
      await viewer.goto(state.embed!);
      await expect(viewer.locator("#plot svg").first()).toBeVisible({ timeout: 30_000 });
      await expect(viewer.getByRole("button")).toHaveCount(0); // read-only: nothing to press
      await shot(viewer, "m3-19-embed-view");
      await page.goto("/integrations?tab=sharing");
      const card = page.getByTestId("saved-plot").first();
      await expect(card).toBeVisible();
      await shot(page, "m3-18-hub-sharing");
      await card.getByRole("button", { name: /^Revoke / }).first().click();
      await page.getByRole("dialog").getByRole("button", { name: "Revoke link" }).click();
      await viewer.reload();
      await expect(viewer.getByText(/expired or was revoked/)).toBeVisible({ timeout: 30_000 });
      await shot(viewer, "m3-20-embed-revoked");
      await viewer.close();
    });
    expect(errors.filter((e) => !/ws:\/\/|WebSocket|favicon/.test(e))).toEqual([]);
  });
});
