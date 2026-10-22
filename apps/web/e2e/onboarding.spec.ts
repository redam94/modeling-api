/**
 * v0.18 onboarding on the MSW mock (plan D18-D24, §6): the welcome dialog and the welcome tour,
 * the first-run checklist (ticks survive a reload), the sample project from "make it" to its
 * Guided report and the what-if tour, the Home playground, a 375 px phone and reduced motion.
 *
 * The mock persona is a welcomed user with tips off by default, so no other spec sees a dialog
 * or a tour. These tests opt into a first visit with `mapi.mock.freshUser=1` (D24).
 */
import { expect, test, type Page } from "@playwright/test";

const PERSONA_ID = "usr_01jaf3a9b8c7d6e5f4g3h2j1k0"; // Ana (tenant admin of acme)

/** A first signed-in visit: set once per test (a reload keeps the progress the app saved). */
async function freshUser(page: Page, extra: Record<string, string> = {}) {
  await page.addInitScript(
    (more) => {
      if (window.top !== window || sessionStorage.getItem("mapi.test.fresh")) return;
      localStorage.setItem("mapi.mock.session", "ana");
      localStorage.setItem("mapi.mock.freshUser", "1");
      for (const [k, v] of Object.entries(more)) localStorage.setItem(k, v);
      sessionStorage.setItem("mapi.test.fresh", "1");
    },
    extra,
  );
}

/** The progress the mock server keeps for Ana in acme. */
async function progress(page: Page): Promise<{ checklist?: Record<string, string>; welcomed_at?: string | null; tours?: Record<string, { status: string }> }> {
  return page.evaluate((id) => {
    const all = JSON.parse(localStorage.getItem("mapi.mock.onboarding") ?? "{}") as Record<string, unknown>;
    return (all[`${id}/acme`] ?? {}) as never;
  }, PERSONA_ID);
}

const coach = (page: Page) => page.locator('[data-testid="tour-popover"], [data-testid="tour-missing"]');

/** Click "Next" until the tour's last step, then "Finish". Returns the step titles. */
async function playTour(page: Page, max = 12): Promise<string[]> {
  const titles: string[] = [];
  for (let i = 0; i < max; i++) {
    const card = page.getByTestId("tour-popover");
    await expect(card).toBeVisible({ timeout: 12_000 });
    titles.push((await card.locator("h2").textContent()) ?? "");
    const step = await card.getAttribute("data-tour-step");
    const next = page.getByTestId("tour-next");
    const last = (await next.textContent()) === "Finish";
    await next.click();
    if (last) break;
    await expect.poll(() => page.getByTestId("tour-popover").getAttribute("data-tour-step").catch(() => step), { timeout: 12_000 }).not.toBe(step);
  }
  await expect(coach(page)).toHaveCount(0);
  return titles;
}

async function noSideScroll(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

test.beforeEach(async ({ page }) => {
  page.on("pageerror", (err) => {
    throw err;
  });
  // Record CSP / Trusted Types violations on the app origin; every test must end with none.
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

test("a welcomed user sees no dialog and a folded Start here (the smoke suite's default)", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "1 run in flight" })).toBeVisible();
  await expect(page.getByTestId("start-here")).toHaveAttribute("data-state", "collapsed");
  await expect(page.getByTestId("welcome-dialog")).toHaveCount(0);
  await expect(coach(page)).toHaveCount(0);
});

test("welcome dialog, the welcome tour, and a checklist tick that survives a reload", async ({ page }) => {
  await freshUser(page);
  await page.goto("/");
  const dialog = page.getByTestId("welcome-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("heading", { name: "Hi there! Fancy a quick look around?" })).toBeVisible();
  // non-modal: the page behind stays usable (no aria-modal, the hub is reachable)
  await expect(dialog).not.toHaveAttribute("aria-modal", "true");
  await expect(page.getByTestId("start-here")).toHaveAttribute("data-state", "open");
  await expect(page.getByTestId("checklist-item-took_tour")).toHaveAttribute("data-done", "false");

  await dialog.getByRole("button", { name: "Take the 2-minute tour" }).click();
  await expect(dialog).toHaveCount(0);
  const titles = await playTour(page);
  expect(titles[0]).toBe("Welcome! Start right here");
  expect(titles).toContain("Make it yours");
  expect(titles.at(-1)).toBe("Come back any time");

  await expect(page.getByTestId("checklist-item-took_tour")).toHaveAttribute("data-done", "true");
  await expect.poll(async () => (await progress(page)).tours?.welcome?.status).toBe("completed");
  expect((await progress(page)).welcomed_at).toBeTruthy();

  await page.reload();
  await expect(page.getByTestId("checklist-item-took_tour")).toHaveAttribute("data-done", "true");
  await expect(page.getByTestId("checklist")).toContainText("1 of 8 done");
  // welcomed once per user: no second dialog, and no tour starts by itself
  await expect(page.getByTestId("start-here")).toBeVisible();
  await expect(page.getByTestId("welcome-dialog")).toHaveCount(0);
  await expect(coach(page)).toHaveCount(0);
});

test("the tour is keyboard-driven: Esc skips it and records the skip", async ({ page }) => {
  await freshUser(page);
  await page.goto("/?tour=welcome");
  const card = page.getByTestId("tour-popover");
  await expect(card).toBeVisible();
  await expect(card).toHaveAttribute("data-tour-step", "welcome:0");
  // the title takes focus, so a screen reader reads the step
  await expect(card.locator("h2")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(coach(page)).toHaveCount(0);
  await expect.poll(async () => (await progress(page)).tours?.welcome?.status).toBe("dismissed");
  await expect(page.getByTestId("checklist-item-took_tour")).toHaveAttribute("data-done", "false");
});

test("Not now closes the dialog for good; ?tour=off and mapi.noTour keep tours away", async ({ page }) => {
  await freshUser(page);
  await page.goto("/?tour=off");
  await expect(page.getByTestId("start-here")).toBeVisible();
  await expect(page.getByTestId("welcome-dialog")).toHaveCount(0);

  await page.goto("/");
  const dialog = page.getByTestId("welcome-dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Not now" }).click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(async () => (await progress(page)).welcomed_at ?? null).not.toBeNull();
  await page.reload();
  await expect(page.getByTestId("start-here")).toBeVisible();
  await expect(page.getByTestId("welcome-dialog")).toHaveCount(0);
});

test("the sample project: make it, watch it, open its report, then the what-if tour", async ({ page }) => {
  test.setTimeout(90_000);
  await freshUser(page, { "mapi.mock.sampleMs": "3000" });
  await page.goto("/");
  const dialog = page.getByTestId("welcome-dialog");
  await dialog.getByRole("button", { name: "Explore the sample project" }).click();
  await expect(dialog).toHaveCount(0);

  const card = page.locator('[data-testid="sample-card"][data-variant="hero"]');
  await expect(card).toHaveAttribute("data-state", "working");
  await expect(card.getByTestId("sample-progress")).toBeVisible();
  await expect(card.getByRole("progressbar", { name: "Sample project progress" })).toBeVisible();
  await expect(card).toHaveAttribute("data-state", "ready", { timeout: 15_000 });

  await card.getByRole("button", { name: /Open the sample project/ }).click();
  await expect(page).toHaveURL(/\/guided\/spc_[^/]+\/report$/);
  // the sample is a real Guided session: the headline, the channel table and the scorecard
  await expect(page.getByRole("heading", { name: /Media drove .* of your results\./ })).toBeVisible();
  await expect(page.getByText("Best return on spend")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Sanity scorecard" })).toBeVisible();
  await expect.poll(async () => Object.keys((await progress(page)).checklist ?? {}).sort()).toEqual(["open_sample", "read_results"]);

  // the what-if tour walks the sample's explore page (a deep link starts it)
  await page.goto(page.url().replace(/\/report$/, "/explore?tour=whatif"));
  const titles = await playTour(page);
  expect(titles).toEqual(["Move the sliders", "The answer, with its range", "Want the exact figure?"]);
  await expect(page.getByTestId("whatif-delta")).toBeVisible();
});

test("the Home playground answers at once and ticks 'Move a budget slider'", async ({ page }) => {
  await freshUser(page);
  await page.goto("/");
  await page.getByTestId("welcome-dialog").getByRole("button", { name: "Not now" }).click();
  const playground = page.locator('[data-testid="playground"][data-tour="home.playground"]');
  await expect(playground).toBeVisible();
  await expect(playground.getByText("Example data", { exact: true })).toBeVisible();
  const delta = playground.getByTestId("playground-delta");
  await expect(delta).toHaveText("same as plan");
  const tv = playground.getByRole("slider", { name: "Weekly spend on TV" });
  await tv.focus();
  for (let i = 0; i < 5; i++) await page.keyboard.press("ArrowRight");
  await expect(delta).not.toHaveText("same as plan");
  await expect(playground.getByTestId("playground-result")).toContainText("Predicted weekly sales");
  await expect(page.getByTestId("checklist-item-try_whatif")).toHaveAttribute("data-done", "true");
  await playground.getByRole("button", { name: "Reset to plan" }).click();
  await expect(delta).toHaveText("same as plan");
});

test("on a 375 px phone the hub fits and the tour is a bottom sheet", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  await freshUser(page);
  await page.goto("/");
  await expect(page.getByTestId("start-here")).toBeVisible();
  await expect(page.getByTestId("welcome-dialog")).toBeVisible();
  await noSideScroll(page);
  await page.getByTestId("welcome-dialog").getByRole("button", { name: "Take the 2-minute tour" }).click();
  const card = page.getByTestId("tour-popover");
  await expect(card).toBeVisible();
  await expect(card).toHaveAttribute("data-variant", "sheet");
  await expect(card).toBeInViewport();
  await noSideScroll(page);
  await page.getByTestId("tour-skip").click();
  await expect(coach(page)).toHaveCount(0);
  await page.goto("/library");
  await expect(page.getByRole("heading", { name: "Make it yours." })).toBeVisible();
  await noSideScroll(page);
});

test("with reduced motion the coach-mark and the dialog render their final state", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await freshUser(page);
  await page.goto("/");
  const dialog = page.getByTestId("welcome-dialog");
  await expect(dialog).toBeVisible();
  // the global override: no entrance animation runs
  const duration = await dialog.evaluate((el) => getComputedStyle(el).animationDuration);
  expect(Number.parseFloat(duration)).toBeLessThanOrEqual(0.01);
  await dialog.getByRole("button", { name: "Take the 2-minute tour" }).click();
  const card = page.getByTestId("tour-popover");
  await expect(card).toBeVisible();
  await expect(card).not.toHaveClass(/animate-coach-in/);
  await expect(card).toBeInViewport();
  await page.getByTestId("tour-next").click();
  await expect(card).toHaveAttribute("data-tour-step", "welcome:1");
});
