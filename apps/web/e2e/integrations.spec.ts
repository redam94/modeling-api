/**
 * v0.4 smoke tests against the MSW-mocked API (`vite --mode mock`, production build with the
 * CSP and Trusted Types enforced): the Integrations hub (endpoint promotion through the gate,
 * snippets, webhooks with their delivery log, embed links and renders) and the trust pages
 * (sign-up to e-mail verification, GDPR export, tenant deletion), on desktop and phone.
 */
import { expect, test, type Page } from "@playwright/test";

const ALIAS = "geo-mmm-prod";
const WEBHOOK = "whk_01jaf3w1x2y3z4a5b6c7d8e9f0";
const DISABLED_WEBHOOK = "whk_01jaf3w1x2y3z4a5b6c7d8e9g1";

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

test("an endpoint is promoted through the scorecard gate with an admin override", async ({ page }) => {
  await as(page, "ana");
  await page.goto("/integrations?tab=endpoints");
  await expect(page.getByRole("table", { name: "Endpoints" }).getByRole("link", { name: ALIAS })).toBeVisible();
  await page.getByRole("button", { name: "Promote a model" }).click();
  const dialog = page.getByRole("dialog", { name: "Promote a model to an endpoint" });
  await dialog.getByLabel("Endpoint name").fill("geo-mmm-staging");
  await expect(dialog.getByRole("list", { name: "Scorecard" })).toContainText("Holdout error");
  await expect(dialog.getByText("Expected gate")).toBeVisible();
  await dialog.getByRole("button", { name: "Promote", exact: true }).click();
  await expect(dialog.getByText("The gate needs an admin override")).toBeVisible();
  await expect(dialog.getByRole("list", { name: "Gate reasons" })).toContainText("holdout.mape");
  await dialog.getByLabel("Override reason").fill("Accepted for the Q4 plan; refit after the holidays");
  await dialog.getByRole("button", { name: "Override and promote" }).click();
  await expect(page).toHaveURL(/\/integrations\/endpoints\/geo-mmm-staging$/);
  await expect(page.getByRole("heading", { name: "geo-mmm-staging" })).toBeVisible();
  const history = page.getByRole("table", { name: "Endpoint history" });
  await expect(history).toContainText("Accepted for the Q4 plan");
  await expect(history.getByText("overridden")).toBeVisible();
});

test("a runner cannot promote and is told why", async ({ page }) => {
  await as(page, "rui");
  await page.goto("/integrations?tab=endpoints");
  const promote = page.getByRole("button", { name: "Promote a model" });
  await expect(promote).toBeDisabled();
  await expect(promote).toHaveAttribute("title", "Project admins promote endpoints");
});

test("snippets are copied and the example call is tried", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await as(page, "ana");
  await page.goto(`/integrations/endpoints/${ALIAS}?tab=schema`);
  await page.getByRole("button", { name: "Validate" }).click();
  await expect(page.getByText("The call is valid")).toBeVisible();
  await page.getByRole("button", { name: "Score", exact: true }).click();
  await expect(page.getByText("Scored")).toBeVisible();

  await page.getByRole("tab", { name: "Snippets" }).click();
  await page.getByRole("tab", { name: "curl" }).click();
  await page.getByRole("button", { name: "Copy curl snippet" }).click();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toContain(`/v1/endpoints/${ALIAS}/score`);
  expect(copied).toContain("$MAPI_TOKEN");
  await page.getByRole("tab", { name: "Notebook" }).click();
  await expect(page.getByLabel("Notebook (mapi-client, xarray) snippet", { exact: true })).toContainText(`endpoint = api.endpoint('${ALIAS}')`);
  // The token shortcut opens a score:<alias> token.
  await page.getByRole("link", { name: `Create a score:${ALIAS} token` }).click();
  const tokenDialog = page.getByRole("dialog", { name: "New API token" });
  await expect(tokenDialog.getByLabel("Score models (optional)")).toHaveValue(ALIAS);
});

test("a webhook is created, its deliveries logged and replayed", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await as(page, "ana");
  await page.goto("/integrations?tab=webhooks");
  await page.getByRole("button", { name: "New webhook" }).click();
  const create = page.getByRole("dialog", { name: "New webhook" });
  await create.getByLabel("URL").fill("https://hooks.example.com/mapi");
  await create.getByRole("checkbox", { name: /Endpoint promoted/ }).click();
  await create.getByRole("button", { name: "Create webhook" }).click();
  const secret = page.getByRole("dialog", { name: "Copy the signing secret now" });
  const value = await secret.getByLabel("Signing secret", { exact: true }).inputValue();
  expect(value).toMatch(/^whsec_[A-Za-z0-9_-]{43}$/);
  await secret.getByRole("button", { name: "Copy signing secret" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(value);
  await secret.getByRole("button", { name: "Done" }).click();
  await expect(page.getByText(value)).toHaveCount(0);
  await expect(page.getByRole("table", { name: "Webhooks" }).getByText("https://hooks.example.com/mapi")).toBeVisible();

  await page.goto(`/integrations/webhooks/${WEBHOOK}`);
  const log = page.getByRole("table", { name: "Deliveries" });
  const failed = log.getByRole("row").filter({ hasText: "Failed" });
  await expect(failed).toContainText("500");
  await expect(failed).toContainText("8");
  await failed.getByRole("button", { name: /^Replay delivery/ }).click();
  await expect(log.getByText("replay", { exact: true })).toBeVisible();
  await page.getByLabel("Filter deliveries by status").selectOption("retrying");
  await expect(log.getByRole("row")).toHaveCount(2);

  await page.goto(`/integrations/webhooks/${DISABLED_WEBHOOK}`);
  await expect(page.getByText("This webhook was disabled automatically")).toBeVisible();
  await expect(page.getByRole("button", { name: "Send test" })).toBeDisabled();
});

test("an embed link is created for a saved plot and renders download", async ({ page }) => {
  await as(page, "ana");
  await page.goto("/integrations?tab=sharing");
  const card = page.getByTestId("saved-plot").filter({ hasText: "EU posterior predictive fan (v1)" });
  await card.getByRole("button", { name: "Create link" }).click();
  const dialog = page.getByRole("dialog", { name: /Share “EU posterior predictive fan/ });
  await dialog.getByLabel("Label (optional)").fill("Board deck");
  await dialog.getByLabel("Expires after").selectOption("7");
  await dialog.getByRole("button", { name: "Create link" }).click();
  const created = page.getByRole("dialog", { name: "Copy the link now" });
  const url = await created.getByLabel("Embed link", { exact: true }).inputValue();
  expect(url).toMatch(/\/embed\/mpe_[0-9a-z]{40}$/);
  await expect(created.getByLabel("Embed code", { exact: true })).toContainText("<iframe");
  await created.getByRole("checkbox", { name: "I have copied the link" }).click();
  await created.getByRole("button", { name: "Done" }).click();
  await expect(card.getByRole("table").getByText("Board deck", { exact: true })).toBeVisible();

  const download = page.waitForEvent("download");
  await card.getByRole("button", { name: "Download SVG" }).click();
  expect((await download).suggestedFilename()).toBe("EU_posterior_predictive_fan_v1.svg");

  const plotly = page.getByTestId("saved-plot").filter({ hasText: "Spend by market (3D)" });
  await plotly.getByRole("button", { name: "Download PNG" }).click();
  await expect(plotly.getByTestId("plotly-refused")).toBeVisible();
  await expect(plotly.getByRole("button", { name: "Download PDF" })).toBeDisabled();
});

test("signing up goes to the inbox and the e-mail link creates the workspace", async ({ page }) => {
  await as(page, "signed-out");
  await page.goto("/");
  await page.getByRole("link", { name: "Create a workspace" }).click();
  await expect(page).toHaveURL(/\/signup$/);
  await page.getByLabel("Work e-mail").fill("nia@yopmail.com");
  await page.getByLabel("Your name").fill("Nia Newcomer");
  await page.getByLabel("Organisation (optional)").fill("Northwind");
  await page.getByRole("checkbox", { name: /I accept the terms/ }).click();
  await page.getByRole("button", { name: "Create workspace" }).click();
  await expect(page.getByLabel("Work e-mail")).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByRole("alert").filter({ hasText: "Disposable e-mail addresses cannot sign up" })).toBeVisible();
  await page.getByLabel("Work e-mail").fill("nia@northwind.example");
  await page.getByRole("button", { name: "Create workspace" }).click();
  await expect(page.getByRole("heading", { name: "Check your inbox" })).toBeVisible();

  await page.getByRole("link", { name: "Open the verification link" }).click();
  await expect(page.getByRole("heading", { name: "Confirm your e-mail address" })).toBeVisible();
  await expect(page).toHaveURL(/\/verify-email$/);
  await page.getByRole("button", { name: "Confirm and create my workspace" }).click();
  await expect(page.getByRole("heading", { name: "Your workspace is ready" })).toBeVisible();
  await expect(page.getByText("northwind", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Choose who to sign in as" })).toBeVisible();
  // the new workspace opens in Guided mode
  await expect(page).toHaveURL(/return_to=%2Fguided$/);
});

test("an organisation admin exports the data and schedules, then cancels, the deletion", async ({ page }) => {
  await as(page, "ana");
  await page.goto("/settings");
  await page.getByRole("button", { name: "Request export" }).click();
  const status = page.getByTestId("export-status");
  await expect(status.getByRole("link", { name: /Download archive/ })).toHaveAttribute("href", /mock-artifacts\/exports%2Fexp_/, { timeout: 10_000 });

  await page.getByRole("button", { name: "Delete Acme Retail…" }).click();
  const dialog = page.getByRole("dialog", { name: "Delete Acme Retail?" });
  await dialog.getByLabel("Type acme to confirm").fill("acme");
  await dialog.getByRole("checkbox", { name: /cannot be undone/ }).click();
  await dialog.getByRole("button", { name: "Schedule deletion" }).click();
  await expect(page.getByRole("list", { name: "Deletion progress" })).toBeVisible();
  const banner = page.getByRole("status").filter({ hasText: "is scheduled for deletion on" });
  await expect(banner).toBeVisible();
  await page.getByRole("button", { name: "Cancel the deletion" }).click();
  await expect(banner).toBeHidden();
});

test("operators review flagged sign-ups", async ({ page }) => {
  await as(page, "ana");
  await page.goto("/admin?tab=signups");
  const table = page.getByRole("table", { name: "Sign-ups" });
  await expect(table.getByText("domain_burst")).toBeVisible();
  await table.getByRole("button", { name: "Approve ops+1@globex.example" }).click();
  const dialog = page.getByRole("dialog", { name: "Approve ops+1@globex.example?" });
  await dialog.getByLabel("Reason").fill("Known customer, second team");
  await dialog.getByRole("button", { name: "Approve" }).click();
  await expect(table.getByText("ops+1@globex.example")).toHaveCount(0);
});

test.describe("phone layout (375 px)", () => {
  test.use({ viewport: { width: 375, height: 800 } });

  const pages: [string, string][] = [
    ["/integrations", "Google Sheets"],
    ["/integrations?tab=endpoints", ALIAS],
    [`/integrations/endpoints/${ALIAS}`, "Serving now"],
    [`/integrations/endpoints/${ALIAS}?tab=schema`, "What to send"],
    [`/integrations/endpoints/${ALIAS}?tab=snippets`, "Snippets"],
    ["/integrations?tab=webhooks", "Planning pipeline"],
    [`/integrations/webhooks/${WEBHOOK}`, "Delivery log"],
    ["/integrations?tab=sharing", "Spend by market (3D)"],
    ["/settings", "Export your data"],
    ["/admin?tab=signups", "domain_burst"],
  ];
  for (const [path, marker] of pages) {
    test(`${path} does not scroll horizontally`, async ({ page }) => {
      await as(page, "ana");
      await page.goto(path);
      await expect(page.getByRole("main").getByText(marker).first()).toBeVisible();
      await page.waitForTimeout(400);
      const { scroll, client } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
      expect(scroll, `${path} is ${scroll}px wide in a ${client}px viewport`).toBeLessThanOrEqual(client);
    });
  }

  test("the sign-up page fits a phone", async ({ page }) => {
    await as(page, "signed-out");
    await page.goto("/signup");
    await expect(page.getByRole("heading", { name: "Create your workspace" })).toBeVisible();
    const { scroll, client } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
    expect(scroll).toBeLessThanOrEqual(client);
  });

  test("the compact nav reaches Integrations", async ({ page }) => {
    await as(page, "ana");
    await page.goto("/");
    await page.getByRole("navigation", { name: "Primary (compact)" }).getByRole("link", { name: "Integrations", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Integrations" })).toBeVisible();
  });
});
