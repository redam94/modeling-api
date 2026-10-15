#!/usr/bin/env node
/**
 * Capture the documentation screenshots of the multi-tenant (v0.3) screens: sign-in, the project
 * switcher, members, tokens, usage and the operators' admin console.
 *
 *   make dev                                          # shell 1: a dev-profile stack (any ports:
 *                                                     #   MAPI_* variables, as for make dev)
 *   make seed                                         # shell 2: data and a spec in default/default
 *   ...                                               # some tenants, members, tokens, a fit
 *   make docs-screenshots-admin                       # this script into .local/screenshots-admin,
 *                                                     #   then optimise_screenshots.py --only those
 *
 * The published captures were taken after `make seed`, a fit of the demo spec, and the steps of
 * the Administration guide (docs/snippets/admin_tour.sh: tenants acme, northwind and a suspended
 * one, project acme/growth with members, a tenant quota, a monthly spend cap, three tokens in
 * default/default, and admin@dev.local added to acme/growth so the project switcher has two
 * tenants).
 *
 * It signs in through the development IdP as MAPI_DEV_ACCOUNT (default admin@dev.local, the
 * platform operator), mints a token named bi-dashboard from the Tokens page (so the "shown once"
 * dialog is real; its secret is replaced by the documentation's example before the capture) and
 * visits every multi-tenant page. One PNG per view at 1440 x 900, light theme. Playwright comes
 * from the web app's dev dependencies (chromium only). Revoke bi-dashboard afterwards.
 */
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(join(ROOT, "apps/web/package.json"));
const { chromium, expect } = require("@playwright/test");

const WEB = (process.env.MAPI_WEB_ORIGIN ?? "http://127.0.0.1:5173").replace(/\/$/, "");
const API = (process.env.MAPI_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const ACCOUNT = process.env.MAPI_DEV_ACCOUNT || "admin@dev.local";
const OUT = resolve(process.argv[2] ?? join(ROOT, ".local/screenshots-admin"));
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "light" });
const page = await context.newPage();
page.setDefaultTimeout(30_000);

async function shot(name, target) {
  await page.mouse.move(0, 0);
  await (target ?? page).screenshot({ path: join(OUT, `${name}.png`), animations: "disabled" });
  console.log(`${name}.png`);
}

async function go(path) {
  await page.goto(`${WEB}${path}`);
  await page.waitForLoadState("networkidle").catch(() => undefined);
}

try {
  // ------------------------------------------------------------------ sign-in
  await go("/");
  await expect(page.getByRole("heading", { name: "Sign in to your workspace" })).toBeVisible();
  await shot("signin");
  await page.getByRole("button", { name: /Continue to sign in/ }).click();
  await page.waitForURL((url) => url.toString().startsWith(`${API}/dev/idp`));
  await expect(page.getByRole("link", { name: ACCOUNT, exact: true })).toBeVisible();
  await shot("dev-idp");
  await page.getByRole("link", { name: ACCOUNT, exact: true }).click();
  await page.waitForURL((url) => url.toString().startsWith(WEB), { waitUntil: "commit" });
  await page.waitForLoadState("networkidle").catch(() => undefined);

  // ------------------------------------------------------------------ project switcher
  await go("/");
  await page.getByRole("button", { name: /Switch project/ }).click();
  await page.waitForTimeout(500);
  await shot("project-switcher");
  // the rest is captured in the project with data (MAPI_DOCS_PROJECT, default "Default project")
  await page.getByText(process.env.MAPI_DOCS_PROJECT || "Default project", { exact: true }).last().click();
  await page.waitForLoadState("networkidle").catch(() => undefined);

  // ------------------------------------------------------------------ members, tokens, usage
  await go("/members");
  await page.waitForTimeout(1_000);
  await shot("members");

  await go("/tokens");
  await page.waitForTimeout(1_000);
  await shot("tokens");
  await page.getByRole("button", { name: "New token" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Name").fill("bi-dashboard");
  await dialog.getByRole("checkbox", { name: /^Read/ }).check();
  await page.waitForTimeout(300);
  await shot("token-new");
  await dialog.getByRole("button", { name: "Create token" }).click();
  await expect(page.getByRole("heading", { name: "Copy your new token now" })).toBeVisible();
  // never publish a working secret, even a development one: show the documentation's example
  await page.locator("#tok-secret").evaluate((el) => {
    el.value = "mapi_7k2m9q4x_0123456789abcdefghjkmnpqrstvwxyz072gj7d";
  });
  await page.getByRole("heading", { name: "Copy your new token now" }).click(); // drop the selection
  await page.waitForTimeout(500);
  await shot("token-created");
  page.once("dialog", (d) => d.accept()); // "close without copying?"
  await page.keyboard.press("Escape");

  await go("/usage");
  await page.waitForTimeout(1_500);
  await shot("usage");

  // ------------------------------------------------------------------ admin console (operators)
  for (const [tab, name] of [
    ["", "admin-tenants"],
    ["quotas", "admin-quotas"],
    ["kill-switches", "admin-kill-switches"],
    ["executors", "admin-executors"],
    ["audit", "admin-audit"],
  ]) {
    await go(tab ? `/admin?tab=${tab}` : "/admin");
    await page.waitForTimeout(1_500);
    await shot(name);
  }
} finally {
  await browser.close();
}
