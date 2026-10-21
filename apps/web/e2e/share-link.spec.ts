/**
 * v0.16 share links against the MSW-mocked API (`vite --mode mock`, production build with the
 * CSP and Trusted Types enforced), opened anonymously (no session, no persona): a read-only
 * link (the default) shows the readout and no inputs; a passcode link asks for the passcode,
 * refuses a short or wrong one and opens with the right one; a revoked link has ended. The
 * tokens are the mock's (`MOCK_SHARE_TOKENS` in src/mocks/dashboards.ts).
 */
import { expect, test, type Page } from "@playwright/test";

const TOKENS = {
  readOnly: `mpd_${"k7m2q9x4".repeat(5)}`,
  inputs: `mpd_${"r3t8v1w6".repeat(5)}`,
  revoked: `mpd_${"h5j0n2p7".repeat(5)}`,
};
const PASSCODE = "harbour-lights";
const LINK_ENDED = "This link has ended or was removed. Ask the sender for a new link.";

async function anonymous(page: Page) {
  await page.addInitScript(() => {
    if (window.top !== window || sessionStorage.getItem("mapi.test.persona")) return;
    // No one is signed in: the share page must not need a session.
    localStorage.setItem("mapi.mock.session", "signed-out");
    sessionStorage.setItem("mapi.test.persona", "1");
  });
}

/** Requests the page sends to the API (the mock answers them in the page). */
function track(page: Page): { url: string; headers: Record<string, string> }[] {
  const seen: { url: string; headers: Record<string, string> }[] = [];
  page.on("request", (r) => {
    if (/\/v1\/|\/auth\//.test(r.url())) seen.push({ url: r.url(), headers: r.headers() });
  });
  return seen;
}

/**
 * Console errors the share page may cause on purpose: the browser logs every 401 the passcode
 * exchange answers (a missing or wrong passcode) and the 410 of an ended link as "Failed to
 * load resource". Anything else in the console fails the test.
 */
const EXPECTED_CONSOLE = /^Failed to load resource: the server responded with a status of (401|410) /;
const consoleErrors = new WeakMap<Page, string[]>();

test.beforeEach(async ({ page }) => {
  page.on("pageerror", (err) => {
    throw err;
  });
  const errors: string[] = [];
  consoleErrors.set(page, errors);
  page.on("console", (m) => {
    if (m.type() === "error" && !EXPECTED_CONSOLE.test(m.text())) errors.push(m.text());
  });
  await page.addInitScript(() => {
    const w = window as unknown as { __csp: string[] };
    w.__csp = [];
    document.addEventListener("securitypolicyviolation", (e) => w.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
  });
  await anonymous(page);
});

test.afterEach(async ({ page }) => {
  const violations = await page.evaluate(() => (window as unknown as { __csp?: string[] }).__csp ?? []);
  expect(violations).toEqual([]);
  expect(consoleErrors.get(page) ?? []).toEqual([]);
});

test("a read-only link (the default) shows the readout and no inputs", async ({ page }) => {
  const seen = track(page);
  await page.goto(`/s#${TOKENS.readOnly}`);
  await expect(page.getByRole("heading", { level: 1, name: "Weekly media readout" })).toBeVisible();
  // The token leaves the address bar at once and never reaches a URL.
  expect(new URL(page.url()).hash).toBe("");
  expect(page.url()).not.toContain(TOKENS.readOnly);
  await expect(page.getByText(/^Shared view\. The link ends on \d{2}\/\d{2}\/\d{4}\.$/)).toBeVisible();
  await expect(page.getByRole("list", { name: "Key numbers" }).getByRole("listitem")).toHaveCount(4);

  // No inputs: no plan scenarios, no uploads.
  const tabs = page.getByRole("tablist", { name: "Dashboard sections" });
  await expect(tabs.getByRole("tab")).toHaveText(["This week", "Trends", "History"]);
  await expect(page.getByRole("tab", { name: "Plan scenarios" })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "Update numbers" })).toHaveCount(0);
  for (const tab of ["This week", "Trends", "History"]) {
    await tabs.getByRole("tab", { name: tab }).click();
    await expect(page.getByRole("slider")).toHaveCount(0);
    await expect(page.locator("input[type=file]")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Send", exact: true })).toHaveCount(0);
  }
  const readouts = page.getByRole("list", { name: "Readouts" });
  await expect(readouts.getByRole("button").first()).toBeVisible();
  // A client's upload waiting for review is not on a shared link.
  await expect(readouts.getByText("Numbers sent on 20/10/2026")).toHaveCount(0);

  // The full app is never loaded: no session calls, no project header, no navigation.
  await expect(page.getByRole("navigation", { name: "Primary" })).toHaveCount(0);
  expect(seen.some((r) => r.url.includes("/auth/session") || r.url.endsWith("/v1/me"))).toBe(false);
  for (const r of seen) {
    expect(r.url).not.toContain(TOKENS.readOnly);
    expect(r.headers["x-mapi-project"]).toBeUndefined();
  }
  expect(seen.filter((r) => r.url.includes("/v1/shared/")).every((r) => /^mps_/.test(r.headers["x-mapi-share"] ?? ""))).toBe(true);

  // Nothing of the link or its session is kept in the browser's stores.
  const stored = await page.evaluate(() => [localStorage, sessionStorage].flatMap((s) => Object.keys(s).map((k) => `${k}=${s.getItem(k) ?? ""}`)).join("\n") + document.cookie);
  expect(stored).not.toMatch(/mps_|mpd_/);
});

test("a passcode link refuses a short or wrong passcode and opens with the right one", async ({ page }) => {
  await page.goto(`/s#${TOKENS.inputs}`);
  const field = page.getByLabel("Passcode");
  await expect(field).toBeVisible();
  expect(new URL(page.url()).hash).toBe("");

  await field.fill("short");
  await page.getByRole("button", { name: "Open the dashboard" }).click();
  await expect(page.getByText(/at least 8 characters/)).toBeVisible();

  await field.fill("not-the-passcode");
  await page.getByRole("button", { name: "Open the dashboard" }).click();
  await expect(page.getByText("The passcode is not correct. You can try 4 more times before the link locks.")).toBeVisible();

  await page.getByLabel("Passcode").fill(PASSCODE);
  await page.getByRole("button", { name: "Open the dashboard" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Weekly media readout" })).toBeVisible();
  // This link allows plan scenarios and uploads.
  await page.getByRole("tab", { name: "Plan scenarios" }).click();
  await expect(page.getByRole("slider", { name: "Paid search" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Update numbers" })).toBeVisible();
});

test("a revoked link has ended, and a reload asks for the full link", async ({ page }) => {
  await page.goto(`/s#${TOKENS.revoked}`);
  await expect(page.getByText(LINK_ENDED)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Weekly media readout" })).toHaveCount(0);
  await expect(page.getByText(/^Shared view/)).toHaveCount(0);
  // The token was taken out of the address: a reload has nothing to open.
  await page.reload();
  await expect(page.getByText(/This page needs the full link/)).toBeVisible();
});

test.describe("at 390 px in the dark theme", () => {
  test.use({ viewport: { width: 390, height: 844 }, colorScheme: "dark" });

  test("a shared readout fits the phone width", async ({ page }, info) => {
    await page.goto(`/s#${TOKENS.readOnly}`);
    await expect(page.getByRole("heading", { level: 1, name: "Weekly media readout" })).toBeVisible();
    await expect(page.locator("html")).toHaveClass(/\bdark\b/);
    for (const tab of ["This week", "Trends", "History"]) {
      await page.getByRole("tab", { name: tab }).click();
      const { scroll, client } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
      expect(scroll).toBeLessThanOrEqual(client);
    }
    const path = info.outputPath("share-readout-dark-390.png");
    await page.screenshot({ path, fullPage: true });
    await info.attach("share-readout-dark-390", { path, contentType: "image/png" });
  });

  test("a link with inputs fits too: the scenarios and the actuals editor (a wide table)", async ({ page }) => {
    await page.goto(`/s#${TOKENS.inputs}`);
    await page.getByLabel("Passcode").fill(PASSCODE);
    await page.getByRole("button", { name: "Open the dashboard" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Weekly media readout" })).toBeVisible();
    for (const tab of ["Plan scenarios", "Update numbers"]) {
      await page.getByRole("tab", { name: tab }).click();
      await expect(page.getByRole("tab", { name: tab })).toHaveAttribute("aria-selected", "true");
      const { scroll, client } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
      expect(scroll, tab).toBeLessThanOrEqual(client);
    }
  });
});
