/**
 * `@e2e` only: sign the browser in to the running stack once and share the session with every
 * test (Playwright `storageState`).
 *
 * `make dev` runs the servers in the `dev` profile, so the app shows a sign-in screen. This
 * goes through the real flow: the API's `/auth/login`, the built-in development identity
 * provider's account page (`MAPI_DEV_ACCOUNT`, default `admin@dev.local`, the seeded tenant
 * admin and operator), `/auth/callback`, and back to the web app with the session cookie. A
 * single-tenant stack (no sign-in) is detected through `/auth/session` and left alone.
 */
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium, type FullConfig } from "@playwright/test";

export const AUTH_STATE = fileURLToPath(new URL("./.auth/state.json", import.meta.url));

export default async function globalSetup(config: FullConfig): Promise<void> {
  const api = (process.env.VITE_API_BASE ?? process.env.MAPI_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
  const web = String(config.projects[0]?.use.baseURL ?? "http://127.0.0.1:5173").replace(/\/$/, "");
  const account = process.env.MAPI_DEV_ACCOUNT || "admin@dev.local";
  mkdirSync(dirname(AUTH_STATE), { recursive: true });
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    const session = await page.request.get(`${api}/auth/session`);
    const info = session.ok() ? ((await session.json()) as { authenticated?: boolean }) : {};
    if (!info.authenticated) {
      await page.goto(`${api}/auth/login?return_to=${encodeURIComponent("/")}`);
      await page.getByRole("link", { name: account, exact: true }).click();
      // The callback sets the cookie and sends the browser back to the web app.
      await page.waitForURL((url) => url.toString().startsWith(web), { waitUntil: "commit" });
      const after = (await (await page.request.get(`${api}/auth/session`)).json()) as { authenticated?: boolean };
      if (!after.authenticated) throw new Error(`signing in as ${account} at ${api} did not create a session`);
    }
    await context.storageState({ path: AUTH_STATE });
  } finally {
    await browser.close();
  }
}
