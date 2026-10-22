/**
 * v0.18 "make it yours" on the MSW mock (plan §3.4, §6): the recipe playground ("Try it"), a
 * no-code model recipe used in Build, a chart template with its live preview, a workflow saved
 * as a team template and started from the gallery, the Catalog's Yours tab and the "Add your
 * own" panel, and Make your own on a 375 px phone.
 *
 * Mock switches (mocks/library.ts): `mapi.mock.previewMs` is how long a recipe preview takes
 * (P11: the real one takes about ten seconds, so the page shows a pending state);
 * `mapi.mock.extensions=1` lists a plugin step "Installed by your team".
 */
import { expect, test, type Page } from "@playwright/test";

const WORKFLOW = "wf_01jaf3e1f2g3h4j5k6m7n8p9q0";
const MODEL_RECIPE = "lib_01jaf3l1b2r3y4m5n600000001"; // "Revenue by market" (declarative)

async function mock(page: Page, values: Record<string, string>) {
  await page.addInitScript((v) => {
    if (window.top !== window) return;
    for (const [k, x] of Object.entries(v)) localStorage.setItem(k, x);
  }, values);
}

async function noSideScroll(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
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

test("Try it: the title changes at once, a chart setting redraws on the server, the YAML follows", async ({ page }) => {
  await mock(page, { "mapi.mock.previewMs": "1200" });
  const previews: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && /\/v1\/catalog\/plots\/[^/]+\/[^/]+:preview$/.test(r.url())) previews.push(r.postData() ?? "");
  });
  await page.goto("/catalog?tab=plots");
  await page.getByRole("textbox", { name: "Search the plots" }).fill("ppc_fan");
  await page.getByRole("button", { name: "Try Fit and forecast" }).click();
  const playground = page.getByTestId("recipe-playground");
  await expect(playground).toHaveAttribute("data-plot", "core.ppc_fan");
  const status = page.getByTestId("recipe-preview-status");
  await expect(status).toContainText("This is the example chart");
  await expect(playground.locator("iframe")).toBeVisible();

  // instant: the title is drawn in the browser, nothing goes to the server
  await playground.getByRole("textbox", { name: "Title" }).fill("Weekly sales, fitted");
  await page.waitForTimeout(1000);
  expect(previews).toEqual([]);

  // a chart setting: one debounced preview; the page says it takes a while (P11)
  await playground.getByRole("spinbutton", { name: "Columns" }).fill("2");
  await expect(status).toHaveAttribute("data-tone", "busy");
  await expect(status).toContainText("about 10 seconds");
  await expect(status).toHaveText("Drawn with your settings.", { timeout: 15_000 });
  expect(previews).toHaveLength(1);
  expect(JSON.parse(previews[0]!)).toEqual({ settings: { columns: 2 } });

  await playground.getByText("Use it in a spec or a workflow").click();
  const yaml = page.getByTestId("recipe-yaml");
  await expect(yaml).toContainText("core.ppc_fan@");
  await expect(yaml).toContainText("columns: 2");
  await expect(yaml).toContainText("Weekly sales, fitted");
});

test("Try it with previews switched off offers Apply", async ({ page }) => {
  await mock(page, { "mapi.mock.previewOff": "1" });
  await page.goto("/catalog?tab=plots");
  await page.getByRole("textbox", { name: "Search the plots" }).fill("ppc_fan");
  await page.getByRole("button", { name: "Try Fit and forecast" }).click();
  const playground = page.getByTestId("recipe-playground");
  await playground.getByRole("spinbutton", { name: "Columns" }).fill("2");
  const status = page.getByTestId("recipe-preview-status");
  await expect(status).toHaveAttribute("data-tone", "warn", { timeout: 10_000 });
  await expect(playground.getByRole("button", { name: "Apply" })).toBeVisible();
});

test("a no-code model recipe goes to Build and validates", async ({ page }) => {
  await page.goto(`/library?tab=models&item=${MODEL_RECIPE}`);
  await expect(page.getByRole("heading", { level: 1, name: "Revenue by market" })).toBeVisible();
  await expect(page.getByTestId("model-recipe-summary")).toContainText("Explains revenue_local");
  await page.getByRole("combobox", { name: "Which data will you use it on?" }).selectOption("eu_media_daily");
  const aside = page.getByRole("complementary", { name: "Your model in words" });
  await expect(aside.getByText("Looks good")).toBeVisible();
  const uses: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().endsWith(`/v1/library/${MODEL_RECIPE}:use`)) uses.push(r.postData() ?? "");
  });
  await aside.getByRole("button", { name: "Use in Build" }).click();
  await expect(page).toHaveURL(/\/build\/spc_[^/?]+/);
  expect(uses).toHaveLength(1);
  expect(JSON.parse(uses[0]!)).toMatchObject({ dataset: expect.stringContaining("eu_media_daily") });
  await page.getByRole("button", { name: "Validate" }).click();
  await expect(page.getByText("valid", { exact: true })).toBeVisible();
});

test("a model recipe's prior is chosen in plain words, with its range drawn", async ({ page }) => {
  await page.goto(`/library?tab=models&item=${MODEL_RECIPE}`);
  const driver = page.getByRole("listitem", { name: "Driver 1: Media spend" });
  const range = driver.getByRole("img", { name: /Before seeing the data, the effect is most likely between/ });
  const before = await range.getAttribute("aria-label");
  await driver.getByRole("group", { name: "How sure are you about Media spend?" }).getByRole("button", { name: "Very sure" }).click();
  await expect(range).not.toHaveAttribute("aria-label", before ?? "");
  // the change is unsaved now: the summary's save button wakes up
  await expect(page.getByRole("complementary", { name: "Your model in words" }).getByRole("button", { name: /^Save/ })).toBeEnabled();
});

test("a chart template draws a live preview from the chart type's example", async ({ page }) => {
  const previews: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().endsWith("/v1/library/charts:preview")) previews.push(r.postData() ?? "");
  });
  await page.goto("/catalog?tab=plots");
  await page.getByRole("textbox", { name: "Search the plots" }).fill("fit_vs_actual");
  await page.getByRole("link", { name: "Make a chart template from Predicted vs actual" }).click();
  await expect(page).toHaveURL(/\/library\?.*source=core\.fit_vs_actual/);
  await expect(page.getByRole("heading", { level: 1, name: "Make a chart template" })).toBeVisible();
  const preview = page.getByTestId("chart-preview");
  await expect(preview.locator("iframe")).toBeVisible({ timeout: 10_000 });
  await expect.poll(() => previews.length).toBeGreaterThan(0);
  const before = previews.length;
  await page.getByRole("group", { name: "Mark" }).getByRole("button", { name: "Dots" }).click();
  await expect.poll(() => previews.length).toBeGreaterThan(before);
  expect(JSON.parse(previews.at(-1)!)).toMatchObject({ document: { kind: "ChartTemplate", source_recipe: expect.stringContaining("core.fit_vs_actual") } });
  await expect(preview.locator("iframe")).toBeVisible();
});

test("a workflow saved as a team template is started from the gallery", async ({ page }) => {
  await page.goto(`/workflows/${WORKFLOW}`);
  const save = page.getByRole("button", { name: "Save as team template" });
  await expect(save).toBeEnabled({ timeout: 10_000 });
  await save.click();
  const dialog = page.getByRole("dialog", { name: "Save as a team template" });
  // an admin may share it with the whole tenant
  await expect(dialog.getByRole("group", { name: "Who can use it?" }).getByRole("button", { name: "Everyone in the tenant" })).toBeEnabled();
  await dialog.getByLabel("Name the template").fill("Team readiness check");
  await dialog.getByRole("button", { name: "Save template" }).click();
  await expect(dialog.getByText('Saved. Your team finds it in the gallery under "From your team".')).toBeVisible();
  await dialog.getByRole("button", { name: "Done" }).click();

  // client-side navigation keeps the mock's state (a reload would start it again)
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Workflows" }).click();
  await page.getByRole("tab", { name: "Templates" }).click();
  const team = page.getByTestId("team-templates");
  await expect(team.getByRole("heading", { name: "Team readiness check" })).toBeVisible();
  await team.getByRole("button", { name: "Start from Team readiness check" }).click();
  await expect(page).toHaveURL(/\/workflows\/wf_[^/?]+/);
  await expect(page.getByTestId("workflow-editor")).toBeVisible();
});

test("Catalog › Yours lists the team's items, installed plugins and, for admins, problems as text", async ({ page }) => {
  await mock(page, { "mapi.mock.extensions": "1" });
  await page.goto("/catalog");
  await page.getByRole("tab", { name: /^Yours/ }).click();
  const yours = page.getByTestId("catalog-yours");
  await expect(yours.getByRole("heading", { name: /^From your team/ })).toBeVisible();
  await expect(yours.getByRole("link", { name: "Edit Revenue by market" })).toBeVisible();
  // untrusted text stays text
  await expect(yours.getByText("<b>Markup here is text.</b>", { exact: false })).toBeVisible();
  await expect(yours.locator("b")).toHaveCount(0);
  await expect(yours.getByRole("heading", { name: /^Installed by your team/ })).toBeVisible();
  await expect(yours.getByText("acme.winsorize@0.3.0")).toBeVisible();
  const problems = page.getByTestId("diagnostics-list");
  await expect(problems.getByTestId("diagnostic")).toHaveCount(3);
  await expect(problems.getByText(/<script>alert\(1\)<\/script>/)).toBeVisible();
  await expect(problems.locator("script")).toHaveCount(0);
});

test("Add your own: the plugin command copies, and the no-code side opens Make your own", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/catalog");
  const panel = page.getByTestId("add-your-own");
  await panel.scrollIntoViewIfNeeded();
  await panel.getByRole("button", { name: "Model", exact: true }).click();
  const command = panel.getByTestId("plugin-command");
  await expect(command).toContainText("mapi plugin init");
  await expect(command).toContainText("--kind model");
  await panel.getByRole("button", { name: "Copy the command" }).click();
  await expect(panel.getByText("The command is copied.")).toBeAttached();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toBe(((await command.textContent()) ?? "").replace(/^\$\s*/, ""));

  await panel.getByRole("link").first().click();
  await expect(page).toHaveURL(/\/library/);
});

test("Make your own on a 375 px phone", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  await page.goto("/library");
  await expect(page.getByRole("heading", { name: "Make it yours." })).toBeVisible();
  await expect(page.getByRole("tablist", { name: "Kinds" })).toBeVisible();
  await noSideScroll(page);
  await page.goto(`/library?tab=models&item=${MODEL_RECIPE}`);
  await expect(page.getByTestId("model-recipe-summary")).toBeVisible();
  await noSideScroll(page);
  await page.goto("/catalog");
  await expect(page.getByTestId("add-your-own")).toBeVisible();
  await noSideScroll(page);
});
