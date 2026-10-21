/**
 * v0.17 dashboards for any model type (layout 2) against the MSW-mocked API (`vite --mode mock`,
 * production build with the CSP and Trusted Types enforced). The matrix of plan §5 WP14 on the
 * mock's `acme/retail` fixtures (`V2_DASHBOARD_IDS` in src/mocks/dashboards.ts): the media
 * dashboard as the automatic upgrade leaves it, a forecast, a classifier (try a case, score a
 * file, check accuracy), a regression, an effect of a change, a field map at 360 px and an image
 * model, each as the analyst (the builder and its preview), as a client user (the portal) and
 * through a share link; and the two v0.16 dashboards of `acme/growth`, which stay layout 1.
 *
 * Every test fails on a page error, a CSP violation (Trusted Types included: a blocked sink is a
 * `securitypolicyviolation` of `require-trusted-types-for`) or an unexpected console error.
 *
 * The mock keeps its state in memory per page load, so each test starts from the fixtures.
 * Screenshots land in the test output folder (and in MAPI_SHOTS_DIR when set).
 */
import { expect, test, type Locator, type Page, type TestInfo } from "@playwright/test";

const IDS = {
  forecast: "dsh_01jaf3w2fcst2visits3x4y5z6",
  regression: "dsh_01jaf3w2regr2demand3x4y5z6",
  classifier: "dsh_01jaf3w2clsf2highdm3x4y5z6",
  causal: "dsh_01jaf3w2caus2loyalt3x4y5z6",
  field: "dsh_01jaf3w2fild2incmap3x4y5z6",
  image: "dsh_01jaf3w2imag2shapes3x4y5z6",
  mmm: "dsh_01jaf3w2mmm22upgrd3x4y5z6a",
};
/** The v0.16 dashboards of acme/growth (layout 1, the migration fixtures). */
const V1 = { mmm: "dsh_01jaf3w1mmm2weekly3x4y5z6", forecast: "dsh_01jaf3w1fcst2monthly3x4y5", draft: "dsh_01jaf3w1promo2draft3x4y5z" };
const TOKENS = {
  forecast: `mpd_${"f2v3w4x5".repeat(5)}`,
  regression: `mpd_${"e2d3g4j5".repeat(5)}`,
  classifier: `mpd_${"c4s5b6n7".repeat(5)}`,
  image: `mpd_${"m8g9h2k3".repeat(5)}`,
  mmm: `mpd_${"p4q5r6t7".repeat(5)}`,
};
const UPGRADE_NOTE = "This dashboard has a new layout. The numbers come from the same model. The likely ranges now show where one new result will probably be. They are wider than before.";
const SHOTS = process.env.MAPI_SHOTS_DIR;

type Persona = "ana" | "cleo" | "signed-out";

/** Sign in as a persona (once per test: a reload keeps what the page stored) in a project. */
async function as(page: Page, persona: Persona, project: "retail" | "growth" = "retail") {
  await page.addInitScript(
    ({ p, project }) => {
      if (window.top !== window || sessionStorage.getItem("mapi.test.persona")) return;
      localStorage.setItem("mapi.mock.session", p);
      if (p !== "signed-out") localStorage.setItem("mapi.scope", JSON.stringify({ state: { preferred: { tenantId: "acme", projectId: project }, recent: [] }, version: 1 }));
      // Queued readouts (tried cases, scored files, sent results) become ready quickly.
      localStorage.setItem("mapi.mock.readoutMs", "300");
      sessionStorage.setItem("mapi.test.persona", "1");
    },
    { p: persona, project },
  );
}

async function shot(page: Page, info: TestInfo, name: string) {
  const path = info.outputPath(`${name}.png`);
  await page.screenshot({ path, fullPage: true });
  await info.attach(name, { path, contentType: "image/png" });
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
}

async function noHorizontalScroll(page: Page) {
  const { scroll, client, wide } = await page.evaluate(() => {
    const client = document.documentElement.clientWidth;
    const wide = [...document.querySelectorAll<HTMLElement>("main *")]
      .filter((el) => el.getBoundingClientRect().right > client + 1 && !(el.parentElement && el.parentElement.getBoundingClientRect().right > client + 1))
      .slice(0, 5)
      .map((el) => `${el.tagName.toLowerCase()}.${String(el.className).slice(0, 80)} [${el.getAttribute("aria-label") ?? el.dataset.readout ?? ""}] right=${Math.round(el.getBoundingClientRect().right)}`);
    return { scroll: document.documentElement.scrollWidth, client, wide };
  });
  expect(scroll, `wider than ${client} px: ${wide.join(" | ")}`).toBeLessThanOrEqual(client);
}

/** The sections of a layout 2 readout (or the planner's tabs). */
function sections(scope: Page | Locator): Locator {
  return scope.getByRole("tablist", { name: "Dashboard sections" }).getByRole("tab");
}

async function openSection(page: Page, name: string): Promise<Locator> {
  await page.getByRole("tablist", { name: "Dashboard sections" }).getByRole("tab", { name, exact: true }).click();
  const panel = page.getByRole("tabpanel", { name });
  await expect(panel).toBeVisible();
  return panel;
}

const csv = (lines: string[]) => ({ name: "cases.csv", mimeType: "text/csv", buffer: Buffer.from(`${lines.join("\n")}\n`) });

/** Requests the page sends to the API (the mock answers them in the page). */
function track(page: Page): { url: string; method: string; headers: Record<string, string> }[] {
  const seen: { url: string; method: string; headers: Record<string, string> }[] = [];
  page.on("request", (r) => {
    if (/\/v1\//.test(r.url())) seen.push({ url: r.url(), method: r.method(), headers: r.headers() });
  });
  return seen;
}

/**
 * Answers the mock gives on purpose: the v0.16 MMM fixture's model version has no stored response
 * grid file (the builder then reads the grid from the dashboard), in both projects.
 */
const EXPECTED_FAILURES = [/^404 GET \/v1\/models\/[^/]+\/versions\/\d+\/artifacts\/ui%2Fgrids%2Fresponse_curves\.json$/];
const consoleErrors = new WeakMap<Page, string[]>();
const failedCalls = new WeakMap<Page, string[]>();

test.beforeEach(async ({ page }) => {
  page.on("pageerror", (err) => {
    throw err;
  });
  const errors: string[] = [];
  const failed: string[] = [];
  consoleErrors.set(page, errors);
  failedCalls.set(page, failed);
  // A failed call logs "Failed to load resource" without its URL: the response says which one.
  page.on("console", (m) => {
    if (m.type() === "error" && !/^Failed to load resource: the server responded with a status of \d+ /.test(m.text())) errors.push(m.text());
  });
  page.on("response", (r) => {
    if (r.status() < 400) return;
    const call = `${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`;
    if (!EXPECTED_FAILURES.some((re) => re.test(call))) failed.push(call);
  });
  await page.addInitScript(() => {
    const w = window as unknown as { __csp: string[] };
    w.__csp = [];
    document.addEventListener("securitypolicyviolation", (e) => w.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
  });
});

test.afterEach(async ({ page }) => {
  const violations = await page.evaluate(() => (window as unknown as { __csp?: string[] }).__csp ?? []);
  expect(violations, "CSP or Trusted Types violations").toEqual([]);
  expect(consoleErrors.get(page) ?? [], "console errors").toEqual([]);
  expect(failedCalls.get(page) ?? [], "failed API calls").toEqual([]);
});

// ---------------------------------------------------------------------------------------- the analyst

test.describe("the analyst", () => {
  test("lists a layout 2 dashboard of each model family in acme/retail", async ({ page }) => {
    await as(page, "ana");
    await page.goto("/dashboards");
    const list = page.getByRole("table", { name: "Dashboards" });
    for (const title of ["Monthly store visits", "Weekly demand per store", "High demand weeks", "Effect of the loyalty launch", "Incident map", "Shape check", "Weekly media readout (retail)"]) {
      await expect(list.getByText(title, { exact: true })).toBeVisible();
    }
  });

  test("the automatically upgraded media dashboard opens in the builder, with the planner in the preview", async ({ page }, info) => {
    await as(page, "ana");
    await page.goto(`/dashboards/${IDS.mmm}`);
    await expect(page.getByRole("heading", { level: 1, name: "Weekly media readout (retail)" })).toBeVisible();
    await expect(page.getByText(/Revision 2 published/)).toBeVisible();
    // Layout 2 already: no "Use the new layout" banner.
    await expect(page.getByTestId("upgrade-banner")).toHaveCount(0);

    await page.getByRole("tablist", { name: "Dashboard builder" }).getByRole("tab", { name: "Setup" }).click();
    await expect(page.getByRole("combobox", { name: "Change the layout" })).toHaveValue("mmm_planner");
    const panels = page.getByRole("list", { name: "Panels" });
    await expect(panels.getByRole("listitem")).toHaveCount(6);
    await expect(panels.getByRole("button", { name: /^1\. This period , Planner part\. Edit\.$/ })).toBeVisible();
    // The channels of the layout 1 spec came along.
    await expect(page.getByRole("table", { name: "Channels" }).getByRole("textbox", { name: "Label of Paid search" })).toHaveValue("Paid search");
    // It opens cleanly: the planner's KPI and the model's response grid are found.
    await expect(page.getByText(/The response grid has 4 channels/)).toBeVisible();
    await expect(page.getByText(/has no output named/)).toHaveCount(0);
    await expect(page.getByText(/no response grid/)).toHaveCount(0);

    // History: both revisions and the layout 1 readouts from before the upgrade.
    await page.getByRole("tablist", { name: "Dashboard builder" }).getByRole("tab", { name: "History" }).click();
    await expect(page.getByRole("table", { name: "Published revisions" }).getByRole("row")).toHaveCount(3);
    await expect(page.getByRole("table", { name: "All readouts" }).getByRole("button", { name: /^Hide readout \d+$/ })).toHaveCount(9);

    // The preview is the client's planner, without the one-time note.
    await page.getByRole("tablist", { name: "Dashboard builder" }).getByRole("tab", { name: "Preview" }).click();
    const preview = page.getByRole("tabpanel", { name: "Preview" });
    await expect(preview.getByRole("heading", { level: 1, name: "Weekly media readout (retail)" })).toBeVisible();
    await expect(sections(preview)).toHaveText(["This week", "Trends", "Plan scenarios", "Update numbers", "History"]);
    await expect(preview.getByText(UPGRADE_NOTE)).toHaveCount(0);
    await expect(preview.getByRole("list", { name: "Key numbers" }).getByRole("listitem")).toHaveCount(4);
    await shot(page, info, "v2-analyst-mmm-preview");
  });

  test("the classifier's preview shows the inputs but sends nothing", async ({ page }) => {
    const seen = track(page);
    await as(page, "ana");
    await page.goto(`/dashboards/${IDS.classifier}?tab=preview`);
    const preview = page.getByRole("tabpanel", { name: "Preview" });
    await expect(preview.getByText("This is what a client sees. Inputs show, but they send nothing from the preview.")).toBeVisible();
    await expect(sections(preview)).toHaveText(["Overview", "Try a case", "Check accuracy", "Score a file", "History"]);
    await expect(preview.getByRole("list", { name: "Key numbers" }).getByRole("listitem")).toHaveCount(3);
    await expect(preview.locator("[data-layout='2']")).toHaveAttribute("data-preset", "classifier");

    await preview.getByRole("tab", { name: "Try a case" }).click();
    await expect(preview.getByRole("spinbutton", { name: "Price" })).toBeDisabled();
    await expect(preview.getByRole("button", { name: "Calculate" })).toBeDisabled();
    await expect(preview.getByText("Inputs are off in the preview.")).toBeVisible();

    await preview.getByRole("tab", { name: "Score a file" }).click();
    await expect(preview.getByRole("button", { name: "Score the file" })).toBeDisabled();
    expect(seen.filter((r) => r.method === "POST" && /\/(predictions|batch)$/.test(new URL(r.url).pathname))).toEqual([]);
  });

  test("a layout 1 draft moves to the new layout from the banner", async ({ page }) => {
    await as(page, "ana", "growth");
    await page.goto(`/dashboards/${V1.draft}`);
    await expect(page.getByRole("heading", { name: "Spring promotion plan" })).toBeVisible();
    const banner = page.getByTestId("upgrade-banner");
    await expect(banner.getByText("This dashboard uses the old layout.")).toBeVisible();
    await banner.getByRole("button", { name: "Use the new layout" }).click();
    await expect(page.getByTestId("upgrade-result").getByText("The draft uses the new layout now.")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("upgrade-banner")).toHaveCount(0);
    await expect(page.getByRole("combobox", { name: "Change the layout" })).toHaveValue("mmm_planner");
  });
});

// ---------------------------------------------------------------------------------------- the client user

test.describe("a client user", () => {
  test("sees every layout 2 dashboard granted in acme/retail", async ({ page }) => {
    await as(page, "cleo");
    await page.goto("/c");
    await expect(page.getByRole("heading", { level: 1, name: "Your dashboards" })).toBeVisible();
    const list = page.getByRole("list", { name: "Dashboards" });
    await expect(list.getByRole("listitem")).toHaveCount(7);
    await list.getByText("Effect of the loyalty launch").click();
    await expect(page).toHaveURL(new RegExp(`/c/${IDS.causal}$`));
    await expect(page.locator("[data-layout='2']")).toHaveAttribute("data-preset", "causal_impact");
  });

  test("the upgraded media dashboard keeps its planner and shows the upgrade note once", async ({ page }, info) => {
    await as(page, "cleo");
    await page.goto(`/c/${IDS.mmm}`);
    await expect(page.getByRole("heading", { level: 1, name: "Weekly media readout (retail)" })).toBeVisible();
    const note = page.locator("[data-readout=upgrade-note]");
    await expect(note).toHaveText(UPGRADE_NOTE);
    await expect(sections(page)).toHaveText(["This week", "Trends", "Plan scenarios", "Update numbers", "History"]);
    // The expected KPI carries a predictive band.
    await expect(page.getByRole("list", { name: "Key numbers" }).getByRole("listitem").first()).toContainText(/Likely range [\d,]+ to [\d,]+/);
    await shot(page, info, "v2-client-mmm-upgraded");

    // "Close" hides the note for this viewer, also after a reload.
    await page.getByRole("button", { name: "Close", exact: true }).click();
    await expect(note).toHaveCount(0);
    await page.reload();
    await expect(page.getByRole("heading", { level: 1, name: "Weekly media readout (retail)" })).toBeVisible();
    await expect(page.locator("[data-readout=upgrade-note]")).toHaveCount(0);

    // The planner's slider still answers in the browser.
    await openSection(page, "Plan scenarios");
    const slider = page.getByRole("slider", { name: "Paid search" });
    await slider.focus();
    for (let i = 0; i < 4; i++) await page.keyboard.press("ArrowRight");
    await expect(slider).toHaveAttribute("aria-valuetext", /^\+20% spend/);

    // History mixes the layout 1 readouts and the first layout 2 one; each opens by its shape.
    const history = await openSection(page, "History");
    const readouts = history.getByRole("list", { name: "Readouts" });
    await expect(readouts.getByRole("listitem")).toHaveCount(9);
    await readouts.getByRole("button", { name: /^Week ending 18\/10\/2026/ }).click();
    const chosen = history.getByRole("region", { name: "The chosen readout" });
    await expect(chosen.getByRole("list", { name: "Key numbers" }).getByRole("listitem")).not.toHaveCount(0);
    await readouts.getByRole("button", { name: /^First readout of the new layout/ }).click();
    await expect(chosen.getByRole("list", { name: "Key numbers" }).getByRole("listitem")).not.toHaveCount(0);
  });

  test("the forecast shows the next period with its range and sends actual results", async ({ page }) => {
    await as(page, "cleo");
    await page.goto(`/c/${IDS.forecast}`);
    await expect(page.getByRole("heading", { level: 1, name: "Monthly store visits" })).toBeVisible();
    await expect(sections(page)).toHaveText(["Overview", "Try a case", "Check accuracy", "History"]);
    const tiles = page.getByRole("list", { name: "Key numbers" }).getByRole("listitem");
    await expect(tiles.first()).toContainText(/^Visits next month[\d,]+Likely between [\d,]+ and [\d,]+\./);
    await expect(page.getByRole("img", { name: /^Visits and forecast each month, 72 months to 01\/12\/2020\./ })).toBeVisible();
    // No media-mix words outside the planner.
    await expect(page.getByRole("main")).not.toContainText(/media|channel|spend/i);

    // Check accuracy: the measures of the last upload and a time-matched upload form.
    const acc = await openSection(page, "Check accuracy");
    await expect(acc.getByRole("heading", { name: "How accurate the model is" })).toBeVisible();
    const upload = acc.getByRole("button", { name: "Drop a .csv file here, or choose a file" });
    await expect(upload).toBeVisible();
    await acc.locator("input[type=file]").setInputFiles(csv(["t,y,price", "2020-01-01,1050,10", "2020-02-01,1080,10", "2020-03-01,1010,10"]));
    await expect(acc.getByRole("list", { name: "Checks" })).toContainText("Passed: 3 rows to send.");
    await acc.getByRole("button", { name: "Send", exact: true }).click();
    await expect(acc.getByText("We received your numbers. The new readout appears in History when it is ready.")).toBeVisible();
    await expect(acc.getByText(/^We expect [\d,]+ store visits next month, likely between [\d,]+ and [\d,]+\. On the 3 results you sent, the error was [\d.]+% on average\.$/)).toBeVisible({ timeout: 15_000 });
  });

  test("the forecast's accuracy measures read in percent", async ({ page }) => {
    await as(page, "cleo");
    await page.goto(`/c/${IDS.forecast}`);
    const acc = await openSection(page, "Check accuracy");
    // The server's MAPE and band hit rate are percent already (ErrorAccuracy): 3.56 and 58.33.
    await expect(acc.getByText("On average the prediction was 3.6% from the actual number.")).toBeVisible();
    await expect(acc.getByText("The actual number was in the likely range 58.3% of the time.")).toBeVisible();
    const measures = acc.getByLabel("Measures");
    await expect(measures).toContainText("MAPE3.6%");
    await expect(measures).toContainText("Inside the likely range58.3%");
  });

  test("the classifier scores a tried case and refuses a value out of range at its field", async ({ page }) => {
    await as(page, "cleo");
    await page.goto(`/c/${IDS.classifier}`);
    await expect(page.getByRole("heading", { level: 1, name: "High demand weeks" })).toBeVisible();
    const tab = await openSection(page, "Try a case");
    const price = tab.getByRole("spinbutton", { name: "Price" });
    await price.fill("55");
    await expect(tab.getByText("Price must be between 5 and 40.")).toBeVisible();
    await expect(price).toHaveAttribute("aria-invalid", "true");
    await expect(tab.getByRole("button", { name: "Calculate" })).toBeDisabled();

    await price.fill("12");
    await tab.getByRole("button", { name: "Calculate" }).click();
    const result = tab.locator("[data-readout=prediction]");
    await expect(result.getByRole("list", { name: "Predictions" })).toContainText(/^Chance of high demand\s*[\d.]+%/, { timeout: 15_000 });
    await expect(tab.getByRole("button", { name: "Keep this case" })).toBeEnabled();
  });

  test("the classifier scores a file and gives the predictions to download", async ({ page }) => {
    await as(page, "cleo");
    await page.goto(`/c/${IDS.classifier}`);
    const tab = await openSection(page, "Score a file");
    await expect(tab.getByRole("button", { name: "Download template (.csv)" })).toBeVisible();
    await tab.locator("input[type=file]").setInputFiles(csv(["store,time,price,promo", "case1,2024-10-13,18,0", "case2,2024-10-13,12,1", "case3,2024-10-20,30,0"]));
    await expect(tab.getByText("cases.csv: the first 3 of 3 rows.")).toBeVisible();
    await tab.getByRole("button", { name: "Score the file" }).click();
    const batch = tab.locator("[data-readout=batch]");
    await expect(batch).toBeVisible({ timeout: 15_000 });
    const download = page.waitForEvent("download");
    await batch.getByRole("button", { name: "Download predictions (.csv)" }).click();
    const file = await download;
    expect(file.suggestedFilename()).toMatch(/\.csv$/);
  });

  test("the classifier's accuracy has the probability scores, calibration and confusion", async ({ page }) => {
    await as(page, "cleo");
    await page.goto(`/c/${IDS.classifier}`);
    const acc = await openSection(page, "Check accuracy");
    const measures = acc.getByLabel("Measures");
    for (const m of ["Accuracy", "Brier score", "Log score", "AUC"]) await expect(measures.getByText(m, { exact: true })).toBeVisible();
    await expect(acc.getByRole("img", { name: /^Are the probabilities right\?: 10 groups of cases\./ })).toBeVisible();
    await expect(acc.getByRole("table", { name: /^Actual and predicted results: 35 of 45 cases were predicted right\./ })).toBeVisible();
    // A row-matched upload: one row per case with its actual result.
    await expect(acc.getByRole("button", { name: "Download template (.csv)" })).toBeVisible();
  });

  test("the regression predicts a case with a likely range", async ({ page }) => {
    await as(page, "cleo");
    await page.goto(`/c/${IDS.regression}`);
    await expect(page.getByRole("heading", { level: 1, name: "Weekly demand per store" })).toBeVisible();
    await expect(sections(page)).toHaveText(["Overview", "Try a case", "Check accuracy", "Score a file", "History"]);
    await expect(page.getByRole("list", { name: "Key numbers" }).getByRole("listitem").first()).toContainText(/Likely between [\d.]+ and [\d.]+/);
    const tab = await openSection(page, "Try a case");
    await tab.getByRole("button", { name: "Calculate" }).click();
    await expect(tab.locator("[data-readout=prediction]")).toBeVisible({ timeout: 15_000 });
  });

  test("the effect of a change shows the effect with its range and no tried cases", async ({ page }) => {
    await as(page, "cleo");
    await page.goto(`/c/${IDS.causal}`);
    await expect(page.getByRole("heading", { level: 1, name: "Effect of the loyalty launch" })).toBeVisible();
    await expect(sections(page)).toHaveText(["Overview", "Check accuracy", "History"]);
    await expect(page.locator("[data-readout=lead]")).toHaveText(/^The change added about [\d,]+ orders \(likely [\d,]+ to [\d,]+\)/);
    await expect(page.getByRole("list", { name: "Key numbers" }).getByRole("listitem")).toHaveCount(4);
    await expect(page.getByRole("img", { name: /^Effect of the change over time each week/ })).toBeVisible();
  });

  test("the image model shows its picture and offers no file scoring", async ({ page }) => {
    await as(page, "cleo");
    await page.goto(`/c/${IDS.image}`);
    await expect(page.getByRole("heading", { level: 1, name: "Shape check" })).toBeVisible();
    // Clients and share links never score images (plan §10).
    await expect(sections(page)).toHaveText(["Overview", "History"]);
    await expect(page.getByRole("img", { name: /^Chance of each shape: 3 bars\./ })).toBeVisible();
    const picture = page.getByRole("img", { name: "Test images with the shape the model chose" });
    await expect(picture).toBeVisible({ timeout: 15_000 });
    expect(await picture.getAttribute("src")).toMatch(/^blob:/);
  });

  test("the v0.16 dashboards of acme/growth stay layout 1", async ({ page }) => {
    await as(page, "cleo", "growth");
    await page.goto(`/c/${V1.forecast}`);
    await expect(page.getByRole("heading", { level: 1, name: "Monthly sales forecast" })).toBeVisible();
    await expect(page.locator("[data-layout='2']")).toHaveCount(0);
    await expect(page.locator("[data-readout=upgrade-note]")).toHaveCount(0);
    await page.goto(`/c/${V1.mmm}`);
    await expect(page.getByRole("heading", { level: 1, name: "Weekly media readout" })).toBeVisible();
    await expect(page.locator("[data-layout='2']")).toHaveCount(0);
    await expect(page.getByRole("table", { name: "Channel scorecard" })).toBeVisible();
  });

  test.describe("on a phone (360 px)", () => {
    test.use({ viewport: { width: 360, height: 780 } });

    test("the field map fits the width and its frame slider moves through time", async ({ page }, info) => {
      await as(page, "cleo");
      await page.goto(`/c/${IDS.field}`);
      await expect(page.getByRole("heading", { level: 1, name: "Incident map" })).toBeVisible();
      const map = page.getByRole("img", { name: /^Incidents per cell, 2012-01-01: a map of 5 by 6 cells\./ });
      await expect(map).toBeVisible();
      await noHorizontalScroll(page);
      const slider = page.getByRole("slider", { name: /^Frame: / });
      await slider.focus();
      await page.keyboard.press("ArrowRight");
      await expect(page.getByRole("slider", { name: /^Frame: / })).not.toHaveAccessibleName("Frame: 2012-01-01");
      await expect(page.getByRole("img", { name: /^Incidents per cell, 2012-01-01:/ })).toHaveCount(0);
      await noHorizontalScroll(page);
      await shot(page, info, "v2-client-field-360");
      await openSection(page, "History");
      await noHorizontalScroll(page);
    });

    test("the classifier's sections fit the width", async ({ page }) => {
      await as(page, "cleo");
      await page.goto(`/c/${IDS.classifier}`);
      await expect(page.getByRole("heading", { level: 1, name: "High demand weeks" })).toBeVisible();
      for (const s of ["Overview", "Try a case", "Check accuracy", "Score a file", "History"]) {
        await openSection(page, s);
        await noHorizontalScroll(page);
      }
    });
  });
});

// ---------------------------------------------------------------------------------------- share links

test.describe("a share link", () => {
  test.beforeEach(async ({ page }) => {
    await as(page, "signed-out");
  });

  /** Every share-link call sends the share session header and no cookie-session headers. */
  function shareOnly(seen: ReturnType<typeof track>) {
    const calls = seen.filter((r) => /\/v1\/(?:shared|share-sessions)(?:[/:?]|$)/.test(r.url));
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) {
      expect(c.headers["x-mapi-project"]).toBeUndefined();
      expect(c.headers["x-csrf-token"]).toBeUndefined();
    }
    expect(seen.filter((r) => !/\/v1\/(?:shared|share-sessions)(?:[/:?]|$)/.test(r.url)).map((r) => r.url)).toEqual([]);
  }

  test("the classifier link tries a case, scores a file and downloads the predictions", async ({ page }, info) => {
    const seen = track(page);
    await page.goto(`/s#${TOKENS.classifier}`);
    await expect(page.getByRole("heading", { level: 1, name: "High demand weeks" })).toBeVisible();
    expect(page.url()).not.toContain(TOKENS.classifier);
    await expect(sections(page)).toHaveText(["Overview", "Try a case", "Check accuracy", "Score a file", "History"]);
    await shot(page, info, "v2-share-classifier");

    const tryTab = await openSection(page, "Try a case");
    const price = tryTab.getByRole("spinbutton", { name: "Price" });
    await price.fill("4");
    await expect(tryTab.getByText("Price must be between 5 and 40.")).toBeVisible();
    await expect(tryTab.getByRole("button", { name: "Calculate" })).toBeDisabled();
    await price.fill("20");
    await tryTab.getByRole("button", { name: "Calculate" }).click();
    await expect(tryTab.locator("[data-readout=prediction]").getByRole("list", { name: "Predictions" })).toContainText(/^Chance of high demand\s*[\d.]+%/, { timeout: 15_000 });
    // A share link cannot keep a case for the analyst.
    await expect(tryTab.getByRole("button", { name: "Keep this case" })).toHaveCount(0);

    const fileTab = await openSection(page, "Score a file");
    await fileTab.locator("input[type=file]").setInputFiles(csv(["store,time,price,promo", "case1,2024-10-13,18,0", "case2,2024-10-13,12,1", "=1+1,2024-10-20,30,0"]));
    await expect(fileTab.getByText("cases.csv: the first 3 of 3 rows.")).toBeVisible();
    await fileTab.getByRole("button", { name: "Score the file" }).click();
    const batch = fileTab.locator("[data-readout=batch]");
    await expect(batch).toBeVisible({ timeout: 15_000 });
    const download = page.waitForEvent("download");
    await batch.getByRole("button", { name: "Download predictions (.csv)" }).click();
    const file = await download;
    expect(file.suggestedFilename()).toMatch(/\.csv$/);
    const body = await (await file.createReadStream()).toArray().then((chunks) => Buffer.concat(chunks).toString("utf8"));
    expect(body.split("\n")[0]).toMatch(/^store,time,price,promo,y_hat/);
    expect(body.trim().split("\n")).toHaveLength(4);
    // A formula-looking id is escaped in the file.
    expect(body).toMatch(/^'=1\+1,/m);
    expect(body).not.toMatch(/^=1\+1/m);
    shareOnly(seen);
  });

  test("the classifier link sends actual results, one row per case as in the template", async ({ page }) => {
    await page.goto(`/s#${TOKENS.classifier}`);
    const acc = await openSection(page, "Check accuracy");
    // The template's own columns (its example CSV): the id, the drivers and the outcome.
    await acc.locator("input[type=file]").setInputFiles(csv(["store,time,price,promo,y", "s0,2024-10-13,18,0,1", "s1,2024-10-13,12,1,0", "s2,2024-10-13,30,0,1"]));
    await expect(acc.getByRole("list", { name: "Checks" })).toContainText("Passed: 3 cases to send.");
    await acc.getByRole("button", { name: "Send", exact: true }).click();
    await expect(acc.getByRole("status").filter({ hasText: "We received the results." })).toBeVisible({ timeout: 15_000 });
    await expect(acc.locator("[data-readout=sent] [data-readout=accuracy]")).toBeVisible({ timeout: 15_000 });
  });

  test("the regression link has no accuracy uploads", async ({ page }) => {
    await page.goto(`/s#${TOKENS.regression}`);
    await expect(page.getByRole("heading", { level: 1, name: "Weekly demand per store" })).toBeVisible();
    await expect(sections(page)).toHaveText(["Overview", "Try a case", "Score a file", "History"]);
    const tab = await openSection(page, "Try a case");
    await tab.getByRole("button", { name: "Calculate" }).click();
    await expect(tab.locator("[data-readout=prediction]")).toBeVisible({ timeout: 15_000 });
  });

  test("the forecast link tries a horizon", async ({ page }) => {
    await page.goto(`/s#${TOKENS.forecast}`);
    await expect(page.getByRole("heading", { level: 1, name: "Monthly store visits" })).toBeVisible();
    await expect(sections(page)).toHaveText(["Overview", "Try a case", "Check accuracy", "History"]);
    const tab = await openSection(page, "Try a case");
    const ahead = tab.getByRole("spinbutton", { name: "Periods ahead" });
    await ahead.fill("13");
    await expect(tab.getByText(/the number of periods must be 1 to 12/i)).toBeVisible();
    await ahead.fill("3");
    await tab.getByRole("button", { name: "Calculate" }).click();
    await expect(tab.locator("[data-readout=prediction]")).toBeVisible({ timeout: 15_000 });
  });

  test("the image link shows the picture through the share session and scores nothing", async ({ page }) => {
    const seen = track(page);
    await page.goto(`/s#${TOKENS.image}`);
    await expect(page.getByRole("heading", { level: 1, name: "Shape check" })).toBeVisible();
    await expect(sections(page)).toHaveText(["Overview", "History"]);
    const picture = page.getByRole("img", { name: "Test images with the shape the model chose" });
    await expect(picture).toBeVisible({ timeout: 15_000 });
    expect(await picture.getAttribute("src")).toMatch(/^blob:/);
    expect(await picture.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
    const asset = seen.find((r) => /\/assets\//.test(r.url));
    expect(asset?.headers["x-mapi-share"]).toBeTruthy();
    shareOnly(seen);
  });

  test("the upgraded media link is read-only and shows the note while the page is open", async ({ page }) => {
    await page.goto(`/s#${TOKENS.mmm}`);
    await expect(page.getByRole("heading", { level: 1, name: "Weekly media readout (retail)" })).toBeVisible();
    await expect(sections(page)).toHaveText(["This week", "Trends", "History"]);
    await expect(page.getByRole("slider")).toHaveCount(0);
    await expect(page.locator("[data-readout=upgrade-note]")).toHaveText(UPGRADE_NOTE);
    await page.getByRole("button", { name: "Close", exact: true }).click();
    await expect(page.locator("[data-readout=upgrade-note]")).toHaveCount(0);
  });
});
