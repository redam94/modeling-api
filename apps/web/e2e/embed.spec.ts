/**
 * The plot sandbox's embed page (`/embed/<token>`) in a real browser: it fetches the public
 * embed view from the API (mocked here with `page.route`), renders it under the embed CSP,
 * offers the PNG fallback link, explains expired links, and cannot reach any other API path.
 */
import { expect, test, type Page } from "@playwright/test";
import { tableFromArrays, tableToIPC } from "apache-arrow";

const PLOTS_ORIGIN = process.env.MAPI_SMOKE_PLOTS_ORIGIN ?? "http://127.0.0.1:5174";
const API = "http://127.0.0.1:8000"; // the sandbox dev server's default VITE_API_URL
const TOKEN = "mpe_0123456789abcdefghjkmnpqrstvwxyzabcdefgh";

function arrowBase64(): string {
  const bytes = tableToIPC(tableFromArrays({ t: Int32Array.from([1, 2, 3, 4]), y: Float64Array.from([1, 3, 2, 4]) }));
  return Buffer.from(bytes).toString("base64");
}

const VIEW = {
  title: "EU response curves (v3)",
  caption_plain: "Expected revenue for each level of spend.",
  plot: {
    id: "mmm.response_curves",
    title: "Response curves",
    kind: "vega-lite",
    schema_version: "v5",
    spec: {
      mark: "line",
      data: { name: "fan" },
      encoding: { x: { field: "t", type: "quantitative" }, y: { field: "y", type: "quantitative" } },
    },
    datasets: { fan: { kind: "inline_arrow", data: arrowBase64(), rows: 4 } },
    provenance: { recipe: "mmm.response_curves@1.0.0", generated_at: "2026-10-15T09:40:00Z" },
  },
  generated_at: "2026-10-15T09:40:00Z",
  expires_at: "2027-01-13T09:30:00Z",
  allowed_origins: [],
  image_url: "https://storage.example/t/acme/embeds/emb_1/plot.png?sig=1",
};

async function serveEmbed(page: Page, status: number, body: unknown): Promise<string[]> {
  const asked: string[] = [];
  await page.route(`${API}/**`, async (route) => {
    asked.push(route.request().url());
    await route.fulfill({
      status,
      contentType: "application/json",
      headers: { "Access-Control-Allow-Origin": PLOTS_ORIGIN },
      body: JSON.stringify(body),
    });
  });
  return asked;
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

test("an embed link renders the plot with its PNG fallback under the embed CSP", async ({ page }) => {
  const asked = await serveEmbed(page, 200, VIEW);
  const res = await page.goto(`${PLOTS_ORIGIN}/embed/${TOKEN}`);
  const csp = res?.headers()["content-security-policy"] ?? "";
  expect(csp).toContain(`connect-src 'self'`);
  expect(csp).toContain(`${API}/v1/embeds/`);
  expect(csp).toContain("frame-ancestors 'self' https:");
  await expect(page.getByRole("heading", { name: VIEW.title })).toBeVisible();
  await expect(page.locator("#plot svg").first()).toBeVisible();
  await expect(page.getByText(VIEW.caption_plain)).toBeVisible();
  const png = page.getByRole("link", { name: "Open as PNG" });
  await expect(png).toHaveAttribute("href", VIEW.image_url);
  await expect(page.getByText(/link expires 2027-01-13/)).toBeVisible();
  expect(asked).toEqual([`${API}/v1/embeds/${TOKEN}`]);
  expect(await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp)).toEqual([]);
});

test("an expired link says so", async ({ page }) => {
  await serveEmbed(page, 410, { code: "LINK_EXPIRED", status: 410, title: "Gone" });
  await page.goto(`${PLOTS_ORIGIN}/embed/${TOKEN}`);
  await expect(page.getByRole("status")).toHaveText(/expired or was revoked/);
  await expect(page.locator("#plot svg")).toHaveCount(0);
});

test("the embed page can reach no API path but the embed view", async ({ page }) => {
  await serveEmbed(page, 200, VIEW);
  await page.goto(`${PLOTS_ORIGIN}/embed/${TOKEN}`);
  await expect(page.locator("#plot svg").first()).toBeVisible();
  const blocked = await page.evaluate(async (api) => {
    try {
      await fetch(`${api}/v1/me`, { credentials: "include" });
      return false;
    } catch {
      return true;
    }
  }, API);
  expect(blocked).toBe(true);
  const violations = await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp);
  expect(violations.some((v) => v.startsWith("connect-src"))).toBe(true);
});
