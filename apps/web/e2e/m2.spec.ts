/**
 * v0.3 smoke tests against the MSW-mocked multi-tenant API (`vite --mode mock`): sign-in via
 * the BFF and the mock identity provider, the project switcher, API tokens, quota and
 * permission problems, usage, and the operator console.
 */
import { expect, test, type Page } from "@playwright/test";

const FAILED = "run_01jaf3q2x8m9v7k5h4g3f2e1b2";
const RUNNING = "run_01jaf3q2x8m9v7k5h4g3f2e1d0";

/** Start the page as a mock persona (`signed-out` for none); later sign-ins are kept. */
async function as(page: Page, persona: "ana" | "rui" | "vic" | "signed-out") {
  await page.addInitScript((p) => {
    if (window.top !== window || sessionStorage.getItem("mapi.test.persona")) return;
    localStorage.setItem("mapi.mock.session", p);
    sessionStorage.setItem("mapi.test.persona", "1");
  }, persona);
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
  const violations = await page.evaluate(() => (window as unknown as { __csp?: string[] }).__csp ?? []);
  expect(violations).toEqual([]);
});

test("signing in goes through the identity provider and back to the page", async ({ page }) => {
  await as(page, "signed-out");
  await page.goto("/runs?state=failed");
  await expect(page.getByRole("heading", { name: "Sign in to your workspace" })).toBeVisible();
  await page.getByRole("button", { name: "Continue to sign in" }).click();
  await expect(page).toHaveURL(/\/auth\/login\?return_to=%2Fruns%3Fstate%3Dfailed/);
  await expect(page.getByRole("heading", { name: "Choose who to sign in as" })).toBeVisible();
  await page.getByRole("button", { name: /Sign in as Rui Runner/ }).click();
  await expect(page).toHaveURL(/\/runs\?state=failed$/);
  await expect(page.getByRole("heading", { name: "Runs" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Account: Rui Runner" })).toBeVisible();
  // Rui is not an operator: no admin console in the navigation.
  await expect(page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Admin" })).toHaveCount(0);

  await page.getByRole("button", { name: "Account: Rui Runner" }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Sign in to your workspace" })).toBeVisible();
});

test("an ended session asks to sign in again without leaving the page", async ({ page }) => {
  await as(page, "ana");
  await page.goto("/runs");
  await expect(page.getByRole("heading", { name: "Runs" })).toBeVisible();
  await page.evaluate(() => localStorage.setItem("mapi.mock.session", "signed-out"));
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Models" }).click();
  const dialog = page.getByRole("dialog", { name: "Your session has ended" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Sign in again" })).toBeVisible();
  await dialog.getByRole("button", { name: "Stay on this page" }).click();
  await expect(dialog).toBeHidden();
  const banner = page.getByRole("status").filter({ hasText: "You are signed out" });
  await expect(banner).toBeVisible();
  await banner.getByRole("button", { name: "Sign in again" }).click();
  await expect(page).toHaveURL(/\/auth\/login\?return_to=%2Fmodels$/);
});

test("the project switcher scopes every request and persists the choice", async ({ page }) => {
  await as(page, "ana");
  const projects: string[] = [];
  page.on("request", (req) => {
    if (/\/v1\/runs(\?|$)/.test(req.url())) projects.push(req.headers()["x-mapi-project"] ?? "(none)");
  });
  await page.goto("/runs");
  await expect(page.getByRole("table", { name: "Runs" })).toBeVisible();
  await page.getByRole("button", { name: /^Project: Acme Retail \/ Growth/ }).click();
  await page.getByRole("menuitem", { name: /Retail demand/ }).click();
  await expect(page.getByText("No runs yet")).toBeVisible();
  await expect(page.getByRole("button", { name: /^Project: Acme Retail \/ Retail demand/ })).toBeVisible();
  expect(projects).toContain("acme/growth");
  expect(projects.at(-1)).toBe("acme/retail");

  await page.reload();
  await expect(page.getByRole("button", { name: /^Project: Acme Retail \/ Retail demand/ })).toBeVisible();
  await expect(page.getByText("No runs yet")).toBeVisible();
});

test("a token is minted with scopes, its secret shown once, then revoked", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await as(page, "ana");
  await page.goto("/tokens");
  const table = page.getByRole("table", { name: "API tokens" });
  await expect(table.getByText("atlas-optimiser")).toBeVisible();
  await expect(table.getByLabel("Expires within a week")).toBeVisible();

  await page.getByRole("button", { name: "New token" }).click();
  const create = page.getByRole("dialog", { name: "New API token" });
  await create.getByRole("button", { name: "Create token" }).click();
  await expect(create.getByRole("alert").filter({ hasText: "Give the token a name" })).toBeVisible();
  await create.getByLabel("Name").fill("ci-bot");
  await create.getByRole("checkbox", { name: /Run runs:create/ }).click();
  await create.getByLabel("Rate limit (optional)").fill("60");
  await create.getByRole("button", { name: "Create token" }).click();

  const secret = page.getByRole("dialog", { name: "Copy your new token now" });
  const value = await secret.getByLabel("Token", { exact: true }).inputValue();
  expect(value).toMatch(/^mapi_[0-9a-z]{8}_[0-9a-z]{39}$/);
  const done = secret.getByRole("button", { name: "Done" });
  await expect(done).toBeDisabled();
  await secret.getByRole("button", { name: "Copy token" }).click();
  await expect(done).toBeEnabled();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(value);
  await done.click();
  await expect(secret).toBeHidden();
  // The secret is gone from the page; only the redacted form is listed.
  await expect(page.getByText(value)).toHaveCount(0);
  const row = table.getByRole("row").filter({ hasText: "ci-bot" });
  await expect(row.getByText("runs:create")).toBeVisible();
  await expect(row.getByText("60/min")).toBeVisible();

  await row.getByRole("button", { name: "Revoke ci-bot" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Revoke token" }).click();
  await expect(row.getByText("revoked", { exact: true })).toBeVisible();
  await expect(row.getByRole("button", { name: "Revoke ci-bot" })).toHaveCount(0);
});

test("a quota refusal explains the limit and how to proceed", async ({ page }) => {
  await as(page, "ana");
  await page.goto(`/runs/${FAILED}`);
  await page.getByRole("button", { name: "Retry" }).click();
  const dialog = page.getByRole("dialog", { name: "Retry as a new run" });
  await expect(dialog.getByLabel("Resource class")).toHaveValue("L");
  await dialog.getByRole("button", { name: "Start retry" }).click();
  const alert = dialog.getByRole("alert").filter({ hasText: "Resource class L is not available" });
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("Your organisation (acme) can use resource classes up to M.");
  await expect(alert).toContainText("Choose class M or smaller");
  await dialog.getByLabel("Resource class").selectOption("M");
  await expect(alert).toBeHidden();
  await dialog.getByRole("button", { name: "Start retry" }).click();
  await expect(page).toHaveURL(/\/runs\/run_01jaf3q2x8m9v7k5h4g3f2e1d4$/);
});

test("a viewer sees why run actions are unavailable", async ({ page }) => {
  await as(page, "vic");
  await page.goto(`/runs/${RUNNING}`);
  const cancel = page.getByRole("button", { name: "Cancel" });
  await expect(cancel).toBeDisabled();
  await expect(cancel).toHaveAttribute("title", "Needs the runner role (yours is viewer)");
  await page.goto("/admin");
  await expect(page.getByRole("heading", { name: "The admin console is for platform operators" })).toBeVisible();
});

test("usage shows cost charts and quota warnings", async ({ page }) => {
  await as(page, "ana");
  await page.goto("/usage");
  await expect(page.getByRole("heading", { name: "Usage and cost" })).toBeVisible();
  await expect(page.getByRole("img", { name: /^Daily cost in / })).toBeVisible();
  const today = page.getByRole("meter", { name: "Compute today (organisation)" });
  await expect(today).toHaveAttribute("data-tone", "critical");
  await expect(page.getByText(/Almost used up/)).toBeVisible();
  await expect(page.getByRole("list", { name: "Cost by token" }).getByText("atlas-optimiser")).toBeVisible();
  await page.getByRole("radio", { name: "Organisation" }).click();
  await expect(page.getByRole("list", { name: "Cost by project" }).getByText("Growth")).toBeVisible();
});

test("operators manage tenants, kill switches, executors and the audit log", async ({ page }) => {
  await as(page, "ana");
  await page.goto("/admin");
  await expect(page.getByRole("table", { name: "Tenants" }).getByText("Acme Retail")).toBeVisible();

  await page.getByRole("tab", { name: "Kill switches" }).click();
  await expect(page).toHaveURL(/tab=kill-switches/);
  await page.getByRole("switch", { name: "Allow uploaded code to run" }).click();
  const confirm = page.getByRole("dialog", { name: "Pause all uploaded code?" });
  const go = confirm.getByRole("button", { name: "Confirm" });
  await confirm.getByLabel("Reason (recorded in the audit log)").fill("incident 42: sandbox probe");
  await expect(go).toBeDisabled();
  await confirm.getByLabel("Type PAUSE to confirm").fill("PAUSE");
  await go.click();
  await expect(page.getByText("paused", { exact: true })).toBeVisible();
  await expect(page.getByText("incident 42: sandbox probe")).toBeVisible();

  await page.getByRole("tab", { name: "Executors" }).click();
  const cloudrun = page.getByRole("region", { name: "Executor cloudrun-jobs" });
  await expect(cloudrun.getByText("conformance passed")).toBeVisible();
  await expect(page.getByRole("region", { name: "Executor local-process" }).getByText("1 conformance case(s) failing")).toBeVisible();
  // incident containment: stop placing runs on an executor (audited, reason required)
  await cloudrun.getByRole("button", { name: "Disable" }).click();
  const off = page.getByRole("dialog", { name: "Disable cloudrun-jobs?" });
  await off.getByLabel("Reason (recorded in the audit log)").fill("incident 42: contain");
  await expect(off.getByRole("button", { name: "Confirm" })).toBeDisabled();
  await off.getByLabel("Type DISABLE to confirm").fill("DISABLE");
  await off.getByRole("button", { name: "Confirm" }).click();
  await expect(cloudrun.getByText(/disabled by an operator: incident 42: contain/)).toBeVisible();
  await expect(cloudrun.getByRole("button", { name: "Enable" })).toBeVisible();

  await page.getByRole("tab", { name: "Audit log" }).click();
  const log = page.getByRole("table", { name: "Audit log" });
  await expect(log.getByText("token.create").first()).toBeVisible();
  await page.getByLabel("Outcome").selectOption("denied");
  await page.getByRole("button", { name: "Apply" }).click();
  await expect(log.getByText("quota.denied")).toBeVisible();
  await expect(log.getByText("token.create")).toHaveCount(0);

  await page.getByRole("tab", { name: "Promotions" }).click();
  await expect(page.getByText("No promotions waiting")).toBeVisible();
});
