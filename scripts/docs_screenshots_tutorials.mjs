#!/usr/bin/env node
/**
 * Capture the screenshots of the v0.4 tutorials (Google Sheets, webhooks, embeds) from the
 * web app in mock mode and the real plot sandbox - no backend needed:
 *
 *   pnpm --filter @mapi/web exec vite build --mode mock --outDir dist/mock   (VITE_PLOTS_ORIGIN=…)
 *   pnpm --filter @mapi/web exec vite preview --mode mock --outDir dist/mock --port 15173
 *   (cd apps/plot-sandbox && VITE_APP_ORIGINS=http://127.0.0.1:15173 pnpm exec vite --port 15174)
 *   make docs-screenshots-tutorials
 *
 * The embed page asks the API for `GET /v1/embeds/<token>`; Playwright answers with the
 * EmbedView that scripts/docs_embed_view.py writes from a real `mmm.response_curves` recipe
 * (MAPI_EMBED_VIEW). optimise_screenshots.py then keeps each PNG under 150 KB.
 */
import { mkdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(join(ROOT, "apps/web/package.json"));
const { chromium, expect } = require("@playwright/test");

const WEB = (process.env.MAPI_WEB_ORIGIN ?? "http://127.0.0.1:15173").replace(/\/$/, "");
const PLOTS = (process.env.MAPI_PLOTS_ORIGIN ?? "http://127.0.0.1:15174").replace(/\/$/, "");
// The sandbox dev server's default VITE_API_URL; nothing listens there, Playwright answers.
const API = (process.env.MAPI_EMBED_API ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const VIEW = readFileSync(resolve(process.env.MAPI_EMBED_VIEW ?? join(ROOT, ".local/embed-view.json")), "utf-8");
const TOKEN = "mpe_7k2m9q4x0123456789abcdefghjkmnpqrstvwxyz";
const OUT = resolve(process.argv[2] ?? join(ROOT, ".local/screenshots-tutorials"));
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();

async function shot(page, name, target) {
  await page.mouse.move(0, 0);
  await page.waitForTimeout(300);
  await (target ?? page).screenshot({ path: join(OUT, `${name}.png`), animations: "disabled" });
  console.log(`${name}.png`);
}

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "light" });
  await context.addInitScript(() => {
    if (!sessionStorage.getItem("mapi.docs")) {
      localStorage.setItem("mapi.mock.session", "ana");
      sessionStorage.setItem("mapi.docs", "1");
    }
  });
  const page = await context.newPage();
  page.setDefaultTimeout(20_000);

  await page.goto(`${WEB}/integrations`);
  await expect(page.getByLabel("Apps Script", { exact: true })).toBeVisible();
  const sheets = page.locator('section[data-slot="card"]').filter({ hasText: "Score a model from a spreadsheet" });
  await shot(page, "integrations-sheets", sheets);

  await page.getByRole("button", { name: "Set up a webhook" }).click();
  const create = page.getByRole("dialog", { name: "New webhook" });
  await create.getByLabel("URL").fill("https://hooks.acme.example/mapi");
  await create.getByLabel("Description (optional)").fill("Planning pipeline");
  await create.getByRole("button", { name: "Create webhook" }).click();
  const secret = page.getByRole("dialog", { name: "Copy the signing secret now" });
  await expect(secret).toBeVisible();
  await secret.getByText("Verify a delivery (Python)").click();
  await shot(page, "webhook-secret", secret);
  await context.close();

  const embed = await browser.newContext({ viewport: { width: 960, height: 390 }, colorScheme: "light" });
  const view = await embed.newPage();
  await view.route(`${API}/**`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Access-Control-Allow-Origin": PLOTS },
      body: VIEW,
    }),
  );
  await view.goto(`${PLOTS}/embed/${TOKEN}`);
  await expect(view.locator("#plot svg").first()).toBeVisible();
  await shot(view, "embed-page");
  await embed.close();
} finally {
  await browser.close();
}
