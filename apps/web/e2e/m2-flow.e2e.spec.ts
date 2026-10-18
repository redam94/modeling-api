/**
 * The M2 multi-tenant flow, clicked through the web app against the real local stack
 * (`make dev`, then `make e2e` or `pnpm --filter @mapi/web e2e`):
 *
 *   sign in as a tenant admin through the identity provider → switch to the new tenant →
 *   fit a saved spec → see the run's metered cost (run page, runs list, usage) → mint and
 *   revoke a scoped API token → as the operator, set the global spend cap and see the
 *   tenant's next fit refused → remove the cap and find the changes in the audit log.
 *
 * `tests/e2e/m2_browser_seed.py` creates the tenant (`ui-<run>`, internal tier), its project,
 * the uploaded data and the spec through the API first, and puts the stack back afterwards.
 * The tenant admin is MAPI_E2E_UI_ACCOUNT (default analyst@dev.local, who also belongs to the
 * default tenant, so there is a tenant to switch from). The operator is the global setup's
 * signed-in account (admin@dev.local).
 *
 * Screenshots land in e2e/screenshots/ (git-ignored). Tagged @e2e: never part of the default
 * (mocked) Playwright run.
 */
import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Browser, type BrowserContext, type Locator, type Page } from "@playwright/test";

const API = (process.env.VITE_API_BASE ?? process.env.MAPI_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const SHOTS = fileURLToPath(new URL("./screenshots/", import.meta.url));
const ANALYST_STATE = fileURLToPath(new URL("./.auth/m2-tenant-admin.json", import.meta.url));
const RUN = Date.now().toString(36);

interface Seeded {
  tenant: string;
  tenant_name: string;
  project: string;
  project_name: string;
  spec_id: string;
  spec_name: string;
  account: string;
}
let seeded: Seeded;

function seedScript(...args: string[]): string {
  return execFileSync("uv", ["run", "python", "tests/e2e/m2_browser_seed.py", ...args], { cwd: ROOT, encoding: "utf8" });
}

async function shot(page: Page, name: string, target?: Locator): Promise<void> {
  await page.mouse.move(0, 0);
  await (target ?? page).screenshot({ path: join(SHOTS, `${name}.png`), animations: "disabled" });
}

/** A context signed in as the tenant admin (after the first test has signed in). */
async function tenantAdmin(browser: Browser): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({ storageState: ANALYST_STATE });
  const page = await context.newPage();
  page.on("pageerror", (err) => {
    throw err;
  });
  return { context, page };
}

function projectButton(page: Page, name: RegExp | string): Locator {
  return page.getByRole("button", { name: typeof name === "string" ? new RegExp(`^Project: ${name}`) : name });
}

test.describe.configure({ mode: "serial" });

test.describe("@e2e M2: tenants, run cost, tokens and the kill switch in the UI", () => {
  test.beforeAll(async ({ request }) => {
    test.setTimeout(10 * 60_000);
    const res = await request.get(`${API}/readyz`);
    expect(res.ok(), `the API at ${API} must be ready (make dev)`).toBeTruthy();
    const idp = await request.get(`${API}/dev/idp/.well-known/openid-configuration`);
    test.skip(!idp.ok(), "the M2 flow needs a stack in the dev profile");
    mkdirSync(SHOTS, { recursive: true });
    seeded = JSON.parse(seedScript("seed", "--run", RUN).trim().split("\n").at(-1) ?? "{}") as Seeded;
    expect(seeded.spec_id).toMatch(/^spc_/);
  });

  test.afterAll(() => {
    if (seeded?.tenant) seedScript("cleanup", "--tenant", seeded.tenant);
  });

  test("sign in as the tenant admin and switch to the new tenant @e2e", async ({ browser }) => {
    const context = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    const page = await context.newPage();
    page.on("pageerror", (err) => {
      throw err;
    });
    await page.goto("/runs");
    await expect(page.getByRole("heading", { name: "Sign in to your workspace" })).toBeVisible();
    await shot(page, "m2-01-sign-in");
    await page.getByRole("button", { name: "Continue to sign in" }).click();
    // the development identity provider's account chooser (a real OIDC round trip)
    await expect(page.getByRole("heading", { name: "Development sign-in" })).toBeVisible();
    await page.getByRole("link", { name: seeded.account, exact: true }).click();
    await expect(page).toHaveURL(/\/runs$/);
    await expect(page.getByRole("heading", { name: "Runs" })).toBeVisible();

    const switcher = page.getByRole("button", { name: /^Project: / });
    await expect(switcher).toBeVisible();
    await switcher.click();
    const group = page.getByRole("group", { name: seeded.tenant_name });
    await expect(group).toBeVisible();
    await expect(group.getByText("Internal")).toBeVisible();
    await shot(page, "m2-02-tenant-switcher");
    await group.getByRole("menuitem", { name: new RegExp(seeded.project_name) }).click();
    await expect(projectButton(page, `${seeded.tenant_name} / ${seeded.project_name}`)).toBeVisible();
    // only this tenant's runs: the seed's one ingest run
    const runs = page.getByRole("table", { name: "Runs" });
    await expect(runs.getByRole("row")).toHaveCount(2);
    await expect(runs.getByRole("cell", { name: "ingest", exact: true })).toBeVisible();
    // the choice survives a reload (and scopes every request)
    await page.reload();
    await expect(projectButton(page, `${seeded.tenant_name} / ${seeded.project_name}`)).toBeVisible();
    await shot(page, "m2-03-tenant-selected");
    await context.storageState({ path: ANALYST_STATE });
    await context.close();
  });

  test("fit a spec and see the run's metered cost @e2e", async ({ browser }) => {
    test.setTimeout(15 * 60_000);
    const { context, page } = await tenantAdmin(browser);
    await page.goto(`/build/${seeded.spec_id}`);
    await expect(page.getByText("r1", { exact: true })).toBeVisible({ timeout: 30_000 });
    await page.getByRole("button", { name: "Fit" }).click();
    await expect(page).toHaveURL(/\/runs\/run_/);
    await expect(page.locator("[data-state=succeeded]").first()).toBeVisible({ timeout: 10 * 60_000 });

    // the run page: metered cost in the header and in the details
    // (the ledger entry is written when the attempt ends; the page refetches on completion)
    await expect(page.getByText(/^Metered · price table .* · 1 attempt billed$/)).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText(/^cost \$0\.\d+$/)).toBeVisible();
    await shot(page, "m2-04-run-cost");

    // the runs list shows the same amount in its cost column
    await page.goto("/runs");
    const table = page.getByRole("table", { name: "Runs" });
    await expect(table.getByRole("columnheader", { name: "Cost" })).toBeVisible();
    await expect(table.getByText(/^\$0\.\d+$/).first()).toBeVisible();
    await shot(page, "m2-05-runs-cost");

    // and the usage page adds it up for the project
    await page.goto("/usage");
    await expect(page.getByRole("heading", { name: "Usage and cost" })).toBeVisible();
    await expect(page.getByText(/^\$0\.\d+/).first()).toBeVisible({ timeout: 30_000 });
    await shot(page, "m2-06-usage");
    await context.close();
  });

  test("mint a scoped API token, then revoke it @e2e", async ({ browser }) => {
    const { context, page } = await tenantAdmin(browser);
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.goto("/tokens");
    await expect(projectButton(page, `${seeded.tenant_name} / ${seeded.project_name}`)).toBeVisible();
    await page.getByRole("button", { name: "New token" }).first().click();
    const create = page.getByRole("dialog", { name: "New API token" });
    await create.getByLabel("Name").fill(`ci-reader-${RUN}`);
    await create.getByRole("checkbox", { name: /models:read/ }).check();
    await shot(page, "m2-07-token-new", create);
    await create.getByRole("button", { name: "Create token" }).click();

    const secretDialog = page.getByRole("dialog", { name: "Copy your new token now" });
    const value = await secretDialog.getByLabel("Token", { exact: true }).inputValue();
    expect(value).toMatch(/^mapi_[0-9a-z]{8}_[0-9a-z]{39}$/);
    await secretDialog.getByRole("button", { name: "Copy token" }).click();
    await secretDialog.getByRole("button", { name: "Done" }).click();
    await expect(page.getByText(value)).toHaveCount(0);

    // the token really works against the API, read-only
    const me = await page.request.get(`${API}/v1/me`, { headers: { Authorization: `Bearer ${value}` } });
    expect(me.ok()).toBeTruthy();
    const refused = await page.request.post(`${API}/v1/runs`, {
      headers: { Authorization: `Bearer ${value}` },
      data: { kind: "fit", spec_id: seeded.spec_id, revision: 1 },
    });
    expect(refused.status()).toBe(403);

    const table = page.getByRole("table", { name: "API tokens" });
    const row = table.getByRole("row").filter({ hasText: `ci-reader-${RUN}` });
    await expect(row.getByText("models:read")).toBeVisible();
    await shot(page, "m2-08-tokens");
    await row.getByRole("button", { name: `Revoke ci-reader-${RUN}` }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Revoke token" }).click();
    await expect(row.getByText("revoked", { exact: true })).toBeVisible();
    const after = await page.request.get(`${API}/v1/me`, { headers: { Authorization: `Bearer ${value}` } });
    expect(after.status()).toBe(401);
    await context.close();
  });

  test("the operator's spend-cap kill switch refuses the tenant's next fit @e2e", async ({ page, browser }) => {
    test.setTimeout(5 * 60_000);
    page.on("pageerror", (err) => {
      throw err;
    });
    // operator (the global setup's account): make the tenant a non-internal one that the
    // local executor can still place (verified would be refused for placement first) ...
    await page.goto("/admin");
    const tenants = page.getByRole("table", { name: "Tenants" });
    await expect(tenants.getByText(seeded.tenant_name)).toBeVisible();
    await tenants.getByRole("button", { name: `Edit ${seeded.tenant_name}` }).click();
    const edit = page.getByRole("dialog", { name: `Edit ${seeded.tenant_name}` });
    await edit.getByLabel("Trust tier").selectOption("self-hosted-trusted");
    await edit.getByRole("button", { name: "Save changes" }).click();
    await expect(edit).toBeHidden();
    await expect(tenants.getByRole("row").filter({ hasText: seeded.tenant_name }).getByText("Self-hosted")).toBeVisible();

    // ... and cap today's platform spend below what has been spent
    await page.getByRole("tab", { name: "Kill switches" }).click();
    await page.getByLabel(/^New cap/).fill("0.000001");
    await page.getByRole("button", { name: "Set cap" }).click();
    const confirm = page.getByRole("dialog", { name: /^Set the spend cap to/ });
    await confirm.getByLabel("Reason (recorded in the audit log)").fill(`m2 e2e drill ${RUN}`);
    await confirm.getByRole("button", { name: "Confirm" }).click();
    await expect(confirm).toBeHidden();
    await expect(page.getByText(/per day$/).first()).toBeVisible();
    await expect(page.getByText(`m2 e2e drill ${RUN}`)).toBeVisible();
    await shot(page, "m2-09-kill-switch-set");

    // the tenant admin's next fit is refused with the reason
    const admin = await tenantAdmin(browser);
    await admin.page.goto(`/build/${seeded.spec_id}`);
    await expect(admin.page.getByText("r1", { exact: true })).toBeVisible({ timeout: 30_000 });
    await admin.page.getByRole("button", { name: "Fit" }).click();
    const toast = admin.page.getByText("The fit did not start");
    await expect(toast).toBeVisible({ timeout: 30_000 });
    await expect(admin.page.getByText(/spend cap/).first()).toBeVisible();
    await expect(admin.page).toHaveURL(/\/build\/spc_/);
    await shot(admin.page, "m2-10-fit-refused");
    await admin.context.close();

    // operator: remove the cap, restore the tier, and find it all in the audit log
    await page.getByRole("button", { name: "Remove cap" }).click();
    const remove = page.getByRole("dialog", { name: "Remove the spend cap?" });
    await remove.getByLabel("Reason (recorded in the audit log)").fill(`m2 e2e drill ${RUN} over`);
    await remove.getByLabel("Type REMOVE to confirm").fill("REMOVE");
    await remove.getByRole("button", { name: "Confirm" }).click();
    await expect(remove).toBeHidden();
    await expect(page.getByRole("button", { name: "Remove cap" })).toHaveCount(0);
    await page.getByRole("tab", { name: "Tenants" }).click();
    await tenants.getByRole("button", { name: `Edit ${seeded.tenant_name}` }).click();
    await edit.getByLabel("Trust tier").selectOption("internal");
    await edit.getByRole("button", { name: "Save changes" }).click();
    await expect(edit).toBeHidden();

    await page.getByRole("tab", { name: "Audit log" }).click();
    const log = page.getByRole("table", { name: "Audit log" });
    await expect(log.getByText("kill_switch.update").first()).toBeVisible();
    await expect(log.getByText("tenant.update").first()).toBeVisible();
    await shot(page, "m2-11-audit-log");
  });
});
