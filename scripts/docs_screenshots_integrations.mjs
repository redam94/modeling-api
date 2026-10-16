#!/usr/bin/env node
/**
 * Capture the documentation screenshots of the Integrations hub and the trust pages (v0.4)
 * from the web app in mock mode (MSW fixtures, no backend needed):
 *
 *   pnpm --filter @mapi/web exec vite build --mode mock --outDir dist/mock
 *   pnpm --filter @mapi/web exec vite preview --mode mock --outDir dist/mock --port 15173
 *   make docs-screenshots-integrations      # MAPI_WEB_ORIGIN=http://127.0.0.1:15173 by default
 *
 * One PNG per view at 1440 x 900, light theme, signed in as the mock persona Ana (organisation
 * admin and operator). optimise_screenshots.py then keeps each under 150 KB.
 */
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(join(ROOT, "apps/web/package.json"));
const { chromium, expect } = require("@playwright/test");

const WEB = (process.env.MAPI_WEB_ORIGIN ?? "http://127.0.0.1:15173").replace(/\/$/, "");
const OUT = resolve(process.argv[2] ?? join(ROOT, ".local/screenshots-integrations"));
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "light" });
await context.addInitScript(() => {
  if (!sessionStorage.getItem("mapi.docs")) {
    localStorage.setItem("mapi.mock.session", "ana");
    sessionStorage.setItem("mapi.docs", "1");
  }
});
const page = await context.newPage();
page.setDefaultTimeout(20_000);

async function shot(name, target) {
  await page.mouse.move(0, 0);
  await page.waitForTimeout(300);
  await (target ?? page).screenshot({ path: join(OUT, `${name}.png`), animations: "disabled" });
  console.log(`${name}.png`);
}

async function go(path) {
  await page.goto(`${WEB}${path}`);
  await page.waitForLoadState("networkidle").catch(() => undefined);
}

try {
  await go("/integrations");
  await expect(page.getByLabel("Apps Script", { exact: true })).toBeVisible();
  await shot("integrations-overview");

  await go("/integrations?tab=endpoints");
  await page.getByRole("button", { name: "Promote a model" }).click();
  const promote = page.getByRole("dialog", { name: "Promote a model to an endpoint" });
  await promote.getByLabel("Endpoint name").fill("geo-mmm-staging");
  await promote.getByRole("button", { name: "Promote", exact: true }).click();
  await expect(promote.getByText("The gate needs an admin override")).toBeVisible();
  await promote.getByLabel("Override reason").fill("Holdout error accepted for the Q4 plan; refit after the holidays.");
  await shot("integrations-promote-gate", promote);
  await page.keyboard.press("Escape");

  await go("/integrations/endpoints/geo-mmm-prod");
  await expect(page.getByRole("table", { name: "Endpoint history" })).toBeVisible();
  await shot("integrations-endpoint");

  await go("/integrations/endpoints/geo-mmm-prod?tab=schema");
  await page.getByRole("button", { name: "Validate" }).click();
  await expect(page.getByText("The call is valid")).toBeVisible();
  await shot("integrations-try-it");

  await go("/integrations/endpoints/geo-mmm-prod?tab=snippets");
  await expect(page.getByLabel("Python (mapi-client) snippet", { exact: true })).toBeVisible();
  await shot("integrations-snippets");

  await go("/integrations/webhooks/whk_01jaf3w1x2y3z4a5b6c7d8e9f0");
  const log = page.getByRole("table", { name: "Deliveries" });
  await log.getByRole("row").filter({ hasText: "Failed" }).getByRole("button", { name: "Details" }).click();
  await shot("integrations-webhook");

  await go("/integrations?tab=sharing");
  await expect(page.getByTestId("saved-plot").first()).toBeVisible();
  await shot("integrations-sharing");
  await page.getByTestId("saved-plot").first().getByRole("button", { name: "Create link" }).click();
  const embed = page.getByRole("dialog", { name: /Share “/ });
  await embed.getByLabel("Label (optional)").fill("Weekly review in Looker Studio");
  await embed.getByLabel(/Sites that may frame it/).fill("https://lookerstudio.google.com");
  await embed.getByRole("button", { name: "Create link" }).click();
  await expect(page.getByRole("dialog", { name: "Copy the link now" })).toBeVisible();
  await shot("integrations-embed-link", page.getByRole("dialog", { name: "Copy the link now" }));

  await go("/settings#data");
  await expect(page.getByRole("heading", { name: "Data", exact: true })).toBeVisible();
  await shot("settings-data", page.locator("#data"));

  await go("/admin?tab=signups");
  await expect(page.getByRole("table", { name: "Sign-ups" })).toBeVisible();
  await shot("admin-signups");

  await context.clearCookies();
  await page.evaluate(() => localStorage.setItem("mapi.mock.session", "signed-out"));
  await go("/signup");
  await page.getByLabel("Work e-mail").fill("nia@yopmail.com");
  await page.getByLabel("Your name").fill("Nia Newcomer");
  await page.getByRole("checkbox", { name: /I accept the terms/ }).click();
  await page.getByRole("button", { name: "Create workspace" }).click();
  await expect(page.getByText(/Disposable e-mail addresses cannot sign up/)).toBeVisible();
  await shot("signup");
} finally {
  await browser.close();
}
