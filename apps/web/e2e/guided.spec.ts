/**
 * Guided mode smoke tests against the MSW-mocked API (`vite --mode mock`, `src/mocks/guided.ts`)
 * and the real plot sandbox: the whole task flow of a geo marketing mix (template → data →
 * business priors with a preview → fit → report and scorecard → what-if slider → "Run exact" →
 * endpoint and embed link), "View as YAML", resuming, the persona lock and the phone layout.
 */
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

const SAMPLE = fileURLToPath(new URL("./fixtures/guided-geo-mmm.csv", import.meta.url));

/** Start the page as a mock persona; optional mock-only settings (e.g. a Guided-locked project). */
async function as(page: Page, persona: "ana" | "rui", extra: Record<string, string> = {}) {
  await page.addInitScript(
    ([p, more]) => {
      if (window.top !== window || sessionStorage.getItem("mapi.test.persona")) return;
      localStorage.setItem("mapi.mock.session", p);
      for (const [k, v] of Object.entries(more)) localStorage.setItem(k, v);
      sessionStorage.setItem("mapi.test.persona", "1");
    },
    [persona, extra] as const,
  );
}

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

async function noSideScroll(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

/** Template → data: pick the geo MMM, choose the sample file, accept the suggestions, upload. */
async function startGeoMmm(page: Page) {
  await page.goto("/guided");
  await expect(page.getByRole("heading", { name: "What do you want to learn?" })).toBeVisible();
  await page.getByRole("link", { name: /^Geo marketing mix/ }).click();
  await expect(page).toHaveURL(/\/guided\/new\/geo_mmm$/);
  await page.getByLabel("Name this analysis").fill("EU media mix");
  await page.getByLabel("Choose a data file").setInputFiles(SAMPLE);
  // role detection pre-fills every question from the browser's profile of the file
  await expect(page.getByLabel("Which column is the result you want to explain?")).toHaveValue("Revenue (EUR)");
  await expect(page.getByLabel("Which column holds the date or week?")).toHaveValue("week");
  await expect(page.getByLabel("Which column names the market or region?")).toHaveValue("market");
  const spend = page.getByRole("group", { name: /media spend/ });
  for (const ch of ["tv_spend", "search_spend", "social_spend"]) await expect(spend.getByRole("checkbox", { name: new RegExp(ch) })).toBeChecked();
  await expect(page.getByRole("group", { name: /move the result/ }).getByRole("checkbox", { name: /avg_price/ })).toBeChecked();
  await expect(page.getByText("Very likely match").first()).toBeVisible();
  // live preview: the file read locally, with a sparkline per chosen series
  const preview = page.getByRole("complementary", { name: "Preview" });
  await expect(preview.getByRole("table")).toContainText("2025-01-05");
  await expect(preview.getByRole("img", { name: /Spend: tv_spend/ })).toBeVisible();
  await expect(preview).toContainText("Everything the model needs is answered.");
  await page.getByRole("button", { name: "Upload and continue" }).click();
  await expect(page).toHaveURL(/\/guided\/spc_[0-9a-z]+\/priors$/);
}

test("a geo marketing mix from template to shared endpoint", async ({ page }) => {
  test.setTimeout(90_000);
  await as(page, "ana");
  await startGeoMmm(page);

  // 3. business priors: ROI ranges, an experiment, and the sandboxed preview
  await expect(page.getByRole("heading", { name: "What you believe" })).toBeVisible();
  const tv = page.locator('[data-channel="tv"]');
  await expect(tv).toBeVisible();
  await tv.getByRole("checkbox").click(); // channels start without a belief
  await tv.getByLabel("From").fill("1.5");
  await tv.getByLabel("To").fill("4");
  await expect(tv).toContainText("Every 1 spent brings back between 1.5 and 4.");
  await page.locator('[data-channel="search"]').getByRole("checkbox").click(); // the default range for search; no view on social
  await page.getByRole("button", { name: "Add an experiment" }).click();
  const exp = page.getByRole("listitem", { name: "Experiment 1" });
  await exp.getByLabel("Channel tested").selectOption("tv");
  await exp.getByLabel("Market").selectOption("de");
  await exp.getByLabel("First day").fill("2025-06-01");
  await exp.getByLabel("Last day").fill("2025-06-28");
  await exp.getByLabel("Spend change").fill("-40000");
  await exp.getByLabel("Measured change in results").fill("-90000");
  await exp.getByLabel("± uncertainty (standard error)").fill("25000");
  await page.getByRole("button", { name: "Preview what this means" }).click();
  await expect(page.getByRole("heading", { name: "What your beliefs mean, before seeing the data" })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(/of what the model believes falls inside your range/).first()).toBeVisible();
  await expect(page.locator('iframe[title="Prior return on spend per channel"]')).toBeVisible();
  await expect(page.locator('iframe[title="Results the model expects before fitting"]')).toBeVisible();

  // View as YAML shows the real spec with the calibration block, read-only
  await page.getByRole("button", { name: "View as YAML" }).click();
  const drawer = page.getByRole("dialog", { name: "Your model, as YAML" });
  await expect(drawer.getByRole("button", { name: "Edit in Expert" })).toBeEnabled();
  await expect(drawer.getByText("dataset://growth/eu-media-mix-data@latest")).toBeVisible({ timeout: 15_000 });
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: "Save and continue" }).click();
  await expect(page).toHaveURL(/\/run$/);

  // 4. run: plain-language phases, progress and ETA over SSE
  await page.getByRole("button", { name: "Fit the model" }).click();
  await expect(page.getByRole("progressbar", { name: "Fit progress" })).toBeVisible();
  await expect(page.getByRole("list", { name: "What happens during the fit" })).toContainText("Learning from your data");
  await expect(page.getByRole("heading", { name: "Your model is ready" })).toBeVisible({ timeout: 20_000 });
  await page.getByRole("button", { name: "See the results" }).click();

  // 5. report and sanity scorecard
  await expect(page).toHaveURL(/\/report$/);
  await expect(page.getByText("Best return on spend")).toBeVisible();
  const table = page.getByRole("table", { name: /Return on spend and share of results per channel/ });
  await expect(table.getByRole("rowheader", { name: "tv" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Sanity scorecard" })).toBeVisible();
  await expect(page.getByText("Mostly fine, with something to look at")).toBeVisible();
  const mape = page.locator('[data-check="holdout.mape"]');
  await expect(mape.locator('[data-status="warn"]')).toBeVisible();
  await expect(mape).toContainText("What to do:");
  await expect(page.getByRole("link", { name: "Inspect in Expert mode" })).toHaveAttribute("href", /\/models\/mdl_[0-9a-z]+\/versions\/1\?tab=diagnostics$/);
  await expect(page.locator('iframe[title="Model fit against your data"]')).toBeVisible();
  await page.getByRole("button", { name: "What if…" }).click();

  // 6. what-if: the slider answers instantly from the per-draw grid; Run exact sits next to it
  await expect(page).toHaveURL(/\/explore$/);
  const delta = page.getByTestId("whatif-delta");
  await expect(delta).toHaveText("EUR 0");
  const slider = page.getByRole("slider", { name: "Spend on tv" });
  await slider.focus();
  for (let i = 0; i < 4; i++) await page.keyboard.press("ArrowRight");
  await expect(slider).toHaveAttribute("aria-valuetext", /^\+20%/);
  await expect(delta).toHaveText(/^\+EUR /);
  await page.getByRole("button", { name: "Run exact" }).click();
  await expect(page.getByTestId("exact-result")).toContainText(/\+EUR /, { timeout: 10_000 });
  // presets and the forecast
  await page.getByLabel("What if we turned off a channel?").selectOption("tv");
  await page.getByRole("button", { name: "Turn off tv" }).click();
  await expect(page.getByTestId("scenario-result")).toContainText(/−EUR /);
  await page.getByRole("button", { name: "Make a forecast" }).click();
  await expect(page.getByTestId("forecast-result")).toContainText("Over the next 13 weeks");
  await expect(page.locator('iframe[title="Forecast with its likely range"]')).toBeVisible();
  await page.getByRole("button", { name: /^Share/ }).click();

  // 7. share: the gate (a warning needs an admin's reason), then an embed link
  await expect(page).toHaveURL(/\/share$/);
  await expect(page.getByTestId("gate-sentence")).toContainText("As a project admin you can approve it anyway");
  await page.getByLabel("Why is it fine to go live anyway?").fill("Holdout error is high because of the summer promotion; accepted.");
  await page.getByRole("button", { name: "Approve and go live" }).click();
  await expect(page.getByTestId("endpoint-live")).toContainText("endpoint://growth/eu-media-mix");
  await page.getByRole("button", { name: "Create link" }).click();
  await expect(page.getByTestId("embed-link")).toContainText(/\/embed\/mpe_[0-9a-z]+/);
  await expect(page.getByRole("link", { name: /Open the Integrations hub/ })).toHaveAttribute("href", "/integrations?tab=overview");
  await expect(page.getByRole("link", { name: "Plots and links" })).toHaveAttribute("href", "/integrations?tab=sharing");
  // the hub serves the endpoint this session promoted, with the server's snippets for it
  await page.getByRole("link", { name: "Code for this endpoint" }).click();
  await expect(page).toHaveURL(/\/integrations\/endpoints\/eu-media-mix\?tab=snippets$/);
  await expect(page.getByLabel("Python (mapi-client) snippet", { exact: true })).toContainText("'eu-media-mix'");
  await page.getByRole("tab", { name: "Notebook" }).click();
  await expect(page.getByLabel("Notebook (mapi-client, xarray) snippet", { exact: true })).toContainText("endpoint = api.endpoint('eu-media-mix')");
  await page.goBack();
  await expect(page).toHaveURL(/\/share$/);
  await expect(page.getByTestId("endpoint-live")).toContainText("endpoint://growth/eu-media-mix");

  // resuming: the analysis is listed and opens where it stands
  await page.getByRole("link", { name: "Back to all analyses" }).click();
  await page.getByRole("list", { name: "Your analyses" }).getByRole("link", { name: /eu-media-mix/ }).click();
  await expect(page).toHaveURL(/\/report$/);
});

test("Edit in Expert switches mode and the edit shows up as advanced settings", async ({ page }) => {
  test.setTimeout(60_000);
  await as(page, "ana");
  await startGeoMmm(page);
  await page.getByRole("button", { name: "View as YAML" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Edit in Expert" }).click();
  await expect(page).toHaveURL(/\/build\/spc_[0-9a-z]+/);
  await expect(page.getByRole("radio", { name: "Expert" })).toHaveAttribute("data-state", "on");
  await expect(page.getByRole("link", { name: "Back to Guided mode" })).toBeVisible();
  // an Expert edit of an advanced setting (more draws), saved as a new revision
  const specId = /\/build\/(spc_[0-9a-z]+)/.exec(page.url())![1]!;
  const saved = await page.evaluate(async (id) => {
    const api = "http://127.0.0.1:8000";
    const headers = { "content-type": "application/json", "X-Mapi-Project": "acme/growth", "X-CSRF-Token": "csrf-usr_01jaf3a9b8c7d6e5f4g3h2j1k0" };
    const rev = (await (await fetch(`${api}/v1/specs/${id}/revisions/1`, { headers, credentials: "include" })).json()) as { yaml: string };
    const res = await fetch(`${api}/v1/specs/${id}/revisions`, {
      method: "POST",
      headers,
      credentials: "include",
      body: JSON.stringify({ yaml: rev.yaml.replace(/(?<!ui_)draws: \d+/, "draws: 2000"), parent_revision: 1, message: "more draws" }),
    });
    return res.status;
  }, specId);
  expect(saved).toBe(201);
  await page.getByRole("link", { name: "Back to Guided mode" }).click();
  await expect(page).toHaveURL(new RegExp(`/guided/${specId}/(priors|run)$`));
  await expect(page.getByTestId("advanced-settings")).toHaveText("Advanced settings applied");
  await expect(page.getByRole("radiogroup", { name: "Mode" }).getByRole("radio", { name: "Guided" })).toHaveAttribute("data-state", "on");
});

test("a Guided-locked project shows members below admin Guided mode only", async ({ page }) => {
  await as(page, "rui", { "mapi.mock.guidedLocked": "acme/growth" });
  await page.goto("/");
  await expect(page).toHaveURL(/\/guided$/);
  await expect(page.getByRole("radiogroup", { name: "Mode" })).toHaveCount(0);
  const nav = page.getByRole("navigation", { name: "Primary", exact: true });
  await expect(nav.getByRole("link", { name: "Guided" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Build" })).toHaveCount(0);
  // the Share step's destination stays reachable
  await expect(nav.getByRole("link", { name: "Integrations" })).toBeVisible();
});

test("the Guided/Expert choice is remembered per user", async ({ page }) => {
  await as(page, "ana");
  await page.goto("/");
  await page.getByRole("radiogroup", { name: "Mode" }).getByRole("radio", { name: "Guided" }).click();
  await expect(page).toHaveURL(/\/guided$/);
  const stored = await page.evaluate(() => JSON.parse(localStorage.getItem("mapi.ui") ?? "{}") as { state?: { personas?: Record<string, string> } });
  expect(stored.state?.personas?.usr_01jaf3a9b8c7d6e5f4g3h2j1k0).toBe("guided");
});

test("Guided mode fits a 375 px phone without sideways scrolling", async ({ page }) => {
  test.setTimeout(60_000);
  await page.setViewportSize({ width: 375, height: 800 });
  await as(page, "ana");
  await page.goto("/guided");
  await expect(page.getByRole("list", { name: "Templates" })).toBeVisible();
  await noSideScroll(page);
  await page.getByRole("link", { name: /^Geo marketing mix/ }).click();
  await page.getByLabel("Choose a data file").setInputFiles(SAMPLE);
  await expect(page.getByLabel("Which column is the result you want to explain?")).toHaveValue("Revenue (EUR)");
  await expect(page.getByText(/Step 1 of 6/)).toBeVisible();
  await noSideScroll(page);
  await page.getByRole("button", { name: "Upload and continue" }).click();
  await expect(page).toHaveURL(/\/priors$/);
  await expect(page.locator('[data-channel="tv"]')).toBeVisible();
  await noSideScroll(page);
  await page.getByRole("button", { name: "Continue without beliefs" }).click();
  await page.getByRole("button", { name: "Fit the model" }).click();
  await expect(page.getByRole("progressbar", { name: "Fit progress" })).toBeVisible();
  await noSideScroll(page);
  await page.getByRole("button", { name: "See the results" }).click({ timeout: 20_000 });
  await expect(page.getByRole("heading", { name: "Sanity scorecard" })).toBeVisible();
  await noSideScroll(page);
  await page.getByRole("button", { name: "What if…" }).click();
  await expect(page.getByTestId("whatif-delta")).toBeVisible();
  await noSideScroll(page);
  await page.getByRole("button", { name: /^Share/ }).click();
  await expect(page.getByTestId("gate-sentence")).toBeVisible();
  await noSideScroll(page);
});
