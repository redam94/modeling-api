/**
 * v0.21 global search against the MSW-mocked API (`vite --mode mock`, production build with the
 * CSP and Trusted Types enforced): the keyboard opens the overlay, a dataset name gives grouped
 * results with the match marked, the arrows and Enter open the dataset, and the next time the
 * overlay shows it under Recent. "See all" lands on the list page with its search box filled and
 * the list filtered; a visit to a detail page is a recent item too. A client user has no search.
 *
 * Screenshots of the overlay go to MAPI_SHOTS_DIR when it is set.
 */
import { expect, test, type Page } from "@playwright/test";

const DATASET = "ds_01jaf3a1b2c3d4e5f6g7h8j9k0";
const SHOTS = process.env.MAPI_SHOTS_DIR;

async function as(page: Page, persona: "ana" | "cleo", theme: "light" | "dark" = "light") {
  await page.addInitScript(
    ([p, t]) => {
      if (window.top !== window || sessionStorage.getItem("mapi.test.persona")) return;
      localStorage.setItem("mapi.mock.session", p ?? "ana");
      localStorage.setItem("mapi.noTour", "1");
      localStorage.setItem("mapi.ui", JSON.stringify({ state: { theme: t, persona: "expert", personas: {}, sidebarCollapsed: false, navMoreOpen: false }, version: 4 }));
      sessionStorage.setItem("mapi.test.persona", "1");
    },
    [persona, theme],
  );
}

async function shot(page: Page, name: string) {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png` });
}

async function openSearch(page: Page) {
  await page.goto("/settings");
  await expect(page.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();
  await page.keyboard.press("ControlOrMeta+k");
  const box = page.getByRole("combobox", { name: "Search" });
  await expect(box).toBeFocused();
  return box;
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
  const violations = await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp);
  expect(violations).toEqual([]);
});

test("the search finds a dataset by name, opens it and shows it under Recent", async ({ page }) => {
  await as(page, "ana");
  const box = await openSearch(page);
  const dialog = page.getByRole("dialog", { name: "Search" });
  // No text yet: examples, then the pages and the actions.
  await expect(dialog.getByRole("group", { name: "Try" }).getByRole("option")).toHaveCount(3);
  await expect(dialog.getByRole("group", { name: "Go to" }).getByRole("option").first()).toContainText("Home");

  await box.pressSequentially("media");
  const datasets = dialog.getByRole("group", { name: "Datasets" });
  const row = datasets.getByRole("option").filter({ hasText: "eu_media_daily" });
  await expect(row).toBeVisible();
  await expect(row.locator("mark")).toHaveText("media");
  await expect(row).toHaveAttribute("aria-selected", "true");
  // Other kinds are in their own groups, after Datasets.
  await expect(dialog.getByRole("group", { name: "Dashboards" }).getByRole("option").filter({ hasText: "Weekly media readout" })).toBeVisible();
  await expect(dialog.getByRole("group").nth(0)).toHaveAccessibleName("Datasets");
  await expect(dialog.getByRole("status")).toHaveText(/^\d+ results$/);

  await page.keyboard.press("ArrowDown");
  await expect(datasets.getByRole("option", { name: /See all in Datasets/ })).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowUp");
  await expect(row).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`/datasets/${DATASET}$`));
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("heading", { level: 1, name: "eu_media_daily" })).toBeVisible();

  await page.keyboard.press("ControlOrMeta+k");
  const recent = dialog.getByRole("group", { name: "Recent" });
  await expect(recent.getByRole("option").first()).toContainText("eu_media_daily");
  await expect(recent.getByRole("option", { name: /Clear recent items/ })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});

test("\"See all\" lands on the list page with the search box filled and the list filtered", async ({ page }) => {
  await as(page, "ana");
  const box = await openSearch(page);
  const dialog = page.getByRole("dialog", { name: "Search" });
  await box.pressSequentially("media");
  await dialog.getByRole("group", { name: "Datasets" }).getByRole("option", { name: /See all in Datasets/ }).click();
  await expect(page).toHaveURL(/\/datasets\?q=media$/);
  await expect(dialog).toHaveCount(0);
  const search = page.getByRole("searchbox", { name: "Search datasets" });
  await expect(search).toHaveValue("media");
  const table = page.getByRole("table", { name: "Datasets" });
  await expect(table.getByText("eu_media_daily", { exact: true })).toBeVisible();
  await expect(table.getByText("lens_cubes", { exact: true })).toHaveCount(0);
  await expect(page.getByTestId("list-count")).toHaveText(/^1 of \d+ datasets$/);

  // A text that finds nothing: one sentence and a button that brings the list back.
  await search.fill("media zzqq");
  await expect(page.getByRole("heading", { name: "No datasets match" })).toBeVisible();
  await expect(page).toHaveURL(/\/datasets\?q=media(\+|%20)zzqq$/);
  await shot(page, "list-no-matches");
  await page.getByRole("button", { name: "Clear the search", exact: true }).click();
  await expect(page).toHaveURL(/\/datasets$/);
  await expect(search).toHaveValue("");
  await expect(table.getByText("lens_cubes", { exact: true })).toBeVisible();

  // The same from another group, on a page that also has filter pills.
  await page.keyboard.press("ControlOrMeta+k");
  await box.pressSequentially("media");
  await dialog.getByRole("group", { name: "Dashboards" }).getByRole("option", { name: /See all in Dashboards/ }).click();
  await expect(page).toHaveURL(/\/dashboards\?q=media$/);
  await expect(page.getByRole("searchbox", { name: "Search dashboards" })).toHaveValue("media");
  const dashboards = page.getByRole("table", { name: "Dashboards" });
  await expect(dashboards.getByText("Weekly media readout", { exact: true })).toBeVisible();
  await expect(dashboards.getByText("Monthly sales forecast", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("radiogroup", { name: "Dashboard status" })).toBeVisible();
});

test("a visit to a page shows under Recent, without a search for it first", async ({ page }) => {
  await as(page, "ana");
  await page.goto(`/datasets/${DATASET}`);
  await expect(page.getByRole("heading", { level: 1, name: "eu_media_daily" })).toBeVisible();
  await page.keyboard.press("ControlOrMeta+k");
  const recent = page.getByRole("dialog", { name: "Search" }).getByRole("group", { name: "Recent" });
  await expect(recent.getByRole("option").first()).toContainText("eu_media_daily");
});

test("a client user has no search", async ({ page }) => {
  await as(page, "cleo");
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Your dashboards" })).toBeVisible();
  await page.keyboard.press("ControlOrMeta+k");
  await page.keyboard.press("/");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("combobox", { name: "Search" })).toHaveCount(0);
});

for (const theme of ["light", "dark"] as const) {
  for (const [width, height] of [
    [1440, 900],
    [390, 844],
  ] as const) {
    test(`the overlay fits a ${width} px window in the ${theme} theme`, async ({ page }) => {
      await page.setViewportSize({ width, height });
      await as(page, "ana", theme);
      const box = await openSearch(page);
      const dialog = page.getByRole("dialog", { name: "Search" });
      const inside = async () => {
        const b = await dialog.boundingBox();
        expect(b).not.toBeNull();
        expect(b!.x).toBeGreaterThanOrEqual(0);
        expect(b!.x + b!.width).toBeLessThanOrEqual(width);
        expect(b!.width).toBeLessThanOrEqual(680);
        expect(b!.y + b!.height).toBeLessThanOrEqual(height);
      };
      await inside();
      await shot(page, `search-${theme}-${width}-empty`);

      await box.pressSequentially("media");
      await expect(dialog.getByRole("group", { name: "Datasets" }).getByRole("option").first()).toContainText("eu_media_daily");
      await expect(dialog.locator("[aria-busy='true']")).toHaveCount(0);
      await inside();
      await shot(page, `search-${theme}-${width}-query`);

      await box.fill("zzqqxx");
      await expect(dialog.getByText("No page, action or item matches this text.")).toBeVisible();
      await expect(dialog.getByRole("option", { name: /Open Help me choose/ })).toBeVisible();
      await inside();
      await shot(page, `search-${theme}-${width}-none`);
    });
  }
}
