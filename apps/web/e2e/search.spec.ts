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
  // With the keyboard: the button goes away, and the focus goes to the search box.
  await page.getByRole("button", { name: "Clear the search", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/datasets$/);
  await expect(search).toHaveValue("");
  await expect(search).toBeFocused();
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

test("a mouse click in the overlay leaves the keyboard focus in the search box", async ({ page }) => {
  await as(page, "ana");
  const box = await openSearch(page);
  const dialog = page.getByRole("dialog", { name: "Search" });
  // An example fills the box; the next keys go to the box.
  await dialog.getByRole("group", { name: "Try" }).getByRole("option").filter({ hasText: "fit" }).click();
  await expect(box).toHaveValue("fit");
  await expect(box).toBeFocused();
  await page.keyboard.type("ness");
  await expect(box).toHaveValue("fitness");
  await dialog.getByRole("button", { name: "Clear the text" }).click();
  await expect(box).toBeFocused();
  await page.keyboard.type("media");
  await expect(box).toHaveValue("media");
  // A group heading, the empty area of the list and the footer.
  await dialog.getByText("Datasets", { exact: true }).first().click();
  await expect(box).toBeFocused();
  await dialog.getByTestId("search-hints").click();
  await expect(box).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(dialog.getByRole("option", { selected: true })).toHaveCount(1);
  await page.keyboard.type("x");
  await expect(box).toHaveValue("mediax");
  // The last use of the pointer: the Close button of a phone is not in the way here.
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});

test("a list that answers late does not move the row the user is on", async ({ page }) => {
  await as(page, "ana");
  // The list of datasets takes 1.5 s longer than the other lists.
  await page.addInitScript(() => {
    const orig = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (/\/v1\/datasets(\?|$)/.test(url)) await new Promise((r) => setTimeout(r, 1500));
      return orig(input, init);
    };
  });
  const box = await openSearch(page);
  const dialog = page.getByRole("dialog", { name: "Search" });
  await box.pressSequentially("media");
  const dashboard = dialog.getByRole("group", { name: "Dashboards" }).getByRole("option").filter({ hasText: "Weekly media readout" });
  await expect(dashboard).toBeVisible();
  const datasets = dialog.getByRole("group", { name: "Datasets" });
  await expect(datasets).toHaveAttribute("aria-busy", "true");
  await expect(datasets.getByRole("option")).toHaveCount(0);
  // The user moves to a row, by the keyboard, while the datasets load.
  const chosen = dialog.getByRole("option", { selected: true });
  await page.keyboard.press("ArrowDown");
  const title = await chosen.locator("[data-title]").textContent();
  const kind = await chosen.evaluate((el) => el.closest("[role=group]")?.getAttribute("aria-labelledby"));
  // The datasets come in above that row. The same row is still the active row.
  await expect(datasets.getByRole("option").filter({ hasText: "eu_media_daily" })).toBeVisible();
  await expect(dialog.getByRole("option", { selected: true })).toHaveCount(1);
  expect(await chosen.locator("[data-title]").textContent()).toBe(title);
  expect(await chosen.evaluate((el) => el.closest("[role=group]")?.getAttribute("aria-labelledby"))).toBe(kind);
  expect(await datasets.getByRole("option").first().getAttribute("aria-selected")).toBe("false");
  expect(await box.getAttribute("aria-activedescendant")).toBe(await chosen.getAttribute("id"));
  await page.keyboard.press("Enter");
  await expect(dialog).toHaveCount(0);
  await expect(page).not.toHaveURL(/\/datasets\//);
});

test("\"See all\" shows the rows the search showed, for a text of two words and for a text that looks like a value", async ({ page }) => {
  await as(page, "ana");
  const box = await openSearch(page);
  const dialog = page.getByRole("dialog", { name: "Search" });
  // One word of two is no match: the list page needs each word.
  await box.pressSequentially("media zzqq");
  await expect(dialog.getByText("No page, action or item matches this text.")).toBeVisible();
  await expect(dialog.getByRole("group", { name: "Datasets" })).toHaveCount(0);

  await box.fill("eu daily");
  const datasets = dialog.getByRole("group", { name: "Datasets" });
  await expect(datasets.getByRole("option").filter({ hasText: "eu_media_daily" })).toBeVisible();
  await expect(dialog.locator("[aria-busy='true']")).toHaveCount(0);
  const shown = await datasets.locator("[role=option] [data-title]").allTextContents();
  const names = shown.filter((t) => !t.startsWith("See all in "));
  expect(names.length).toBeGreaterThan(0);
  await datasets.getByRole("option", { name: /See all in Datasets/ }).click();
  await expect(page).toHaveURL(/\/datasets\?q=eu(\+|%20)daily$/);
  await expect(page.getByRole("searchbox", { name: "Search datasets" })).toHaveValue("eu daily");
  const table = page.getByRole("table", { name: "Datasets" });
  for (const name of names) await expect(table.getByText(name, { exact: true })).toBeVisible();
  await expect(page.getByTestId("list-count")).toHaveText(new RegExp(`^${names.length} of \\d+ datasets$`));

  // "null" is a word in a modeling app; a plain ?q=null would be read as no query.
  await page.keyboard.press("ControlOrMeta+k");
  await box.pressSequentially("null");
  const catalog = dialog.getByRole("group", { name: "Catalog" });
  await expect(catalog.getByRole("option").first()).toBeVisible();
  await catalog.getByRole("option", { name: /See all in Catalog/ }).click();
  await expect(page).toHaveURL(/\/catalog\?q=%22null%22$/);
  await expect(page.getByRole("searchbox", { name: "Search the catalog" })).toHaveValue("null");
  await expect(page.getByTestId("list-count")).toHaveText(/^\d+ of \d+ catalog items$/);
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
      // A phone has no Esc key: it gets a Close button and no row of key hints.
      const close = dialog.getByRole("button", { name: "Close", exact: true });
      const hints = dialog.getByTestId("search-hints");
      if (width < 640) {
        await expect(close).toBeVisible();
        await expect(hints).toBeHidden();
      } else {
        await expect(close).toBeHidden();
        await expect(hints).toBeVisible();
      }

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
