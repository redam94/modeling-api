/**
 * M3.5 governance smoke tests against the MSW-mocked API (`vite --mode mock`, production build
 * with the CSP and Trusted Types enforced): edit a policy (checks with About cards, limits,
 * promotion gating) and see the promotion refused with NOT_VALIDATED; approve a validation as a
 * second person; the submitter cannot approve; a reviewer is granted on Members; the evidence
 * pack is exported.
 */
import { expect, test, type Page } from "@playwright/test";

const MODEL = "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0";
const AWAITING = "val_01jaf3m1n2p3q4r5s6t7v8w9x0";
const SHOTS = process.env.MAPI_SHOTS_DIR;

async function as(page: Page, persona: "ana" | "rui" | "vic") {
  await page.addInitScript((p) => {
    if (window.top !== window || sessionStorage.getItem("mapi.test.persona")) return;
    localStorage.setItem("mapi.mock.session", p);
    sessionStorage.setItem("mapi.test.persona", "1");
  }, persona);
}

async function shot(page: Page, name: string) {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
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

test("a policy is edited and then gates promotion with NOT_VALIDATED", async ({ page }) => {
  await as(page, "ana");
  await page.goto("/governance");
  await page.getByRole("table", { name: "Policies" }).getByRole("link", { name: "Forecasts we publish" }).click();
  const editor = page.getByTestId("policy-editor");
  await expect(editor.getByText("version 2")).toBeVisible();

  // Add a check from the catalog: its About card shows before it is added.
  await editor.getByLabel("Add a check").selectOption("null.noise_regressors");
  // (Other checks' cards name it among their related items, so pick the card by its title.)
  await expect(editor.getByRole("article", { name: "No effect from false drivers" })).toContainText("Use it when");
  await editor.getByRole("button", { name: "Add check" }).click();
  await expect(editor.locator('li[data-check="null.noise_regressors"]')).toBeVisible();

  // Custom limits are checked as the server checks them.
  const whiteness = editor.locator('li[data-check="residuals.whiteness"]');
  await whiteness.getByRole("switch", { name: "Custom limits for residuals.whiteness" }).click();
  await whiteness.getByLabel("Fail below").fill("0.2");
  await expect(whiteness.getByText(/The fail limit is more than the warn limit/)).toBeVisible();
  await whiteness.getByLabel("Fail below").fill("0.01");
  await expect(whiteness.getByText(/The fail limit is more than the warn limit/)).toHaveCount(0);

  await editor.getByRole("switch", { name: "Only validated models can be promoted" }).click();
  await shot(page, "policy-editor");
  await editor.getByRole("button", { name: "Save version 3" }).click();
  await expect(page.getByTestId("policy-editor").getByText("version 3", { exact: true })).toBeVisible();
  await expect(page.getByTestId("policy-versions")).toContainText("v3");

  // In the same session (no reload), promoting the model is refused: it is not validated.
  await page.getByRole("link", { name: "Integrations" }).first().click();
  await page.getByRole("tab", { name: "Endpoints" }).click();
  await page.getByRole("button", { name: "Promote a model" }).click();
  const dialog = page.getByRole("dialog", { name: "Promote a model to an endpoint" });
  await dialog.getByLabel("Endpoint name").fill("geo-mmm-governed");
  await dialog.getByRole("button", { name: "Promote", exact: true }).click();
  const notice = dialog.getByTestId("not-validated");
  await expect(notice).toContainText("A governance policy needs a validated model");
  await expect(notice).toContainText("Awaiting approval");
  await notice.getByRole("link", { name: "Open the validation" }).click();
  await expect(page).toHaveURL(new RegExp(`/validations/${AWAITING}$`));
});

test("a reviewer approves a validation that a colleague submitted", async ({ page }) => {
  await as(page, "ana");
  await page.goto("/validations?status=awaiting_approval");
  await expect(page.getByRole("heading", { name: "Review queue" })).toBeVisible();
  await page.getByRole("table", { name: "Validations" }).getByRole("link").first().click();
  const view = page.getByTestId("validation-view");
  await expect(view.getByRole("list", { name: "Validation progress" })).toContainText("Awaiting approval");
  // Evidence: the shuffled-result test's distribution is drawn in the plot sandbox.
  const permutation = view.locator('article[data-check="null.permutation_target"]');
  await permutation.getByText(/^Evidence/).click();
  await expect(permutation.getByTestId("plot-frame")).toBeVisible();
  await shot(page, "validation");

  const form = view.getByTestId("decision-form");
  await form.getByRole("button", { name: "Reject" }).click();
  await expect(form.getByText("The comment is empty. Type the reason for the rejection.")).toBeVisible();
  await form.getByLabel("Comment").fill("Null tests are convincing; benchmarks beaten by 22%.");
  await form.getByRole("button", { name: "Approve" }).click();
  await expect(view.locator("[data-validation-status]").first()).toHaveText("Validated");
  await expect(view.getByRole("list", { name: "History" })).toContainText("Approved");
  await expect(view.getByTestId("decision-form")).toHaveCount(0);

  // The evidence pack is saved as a file (never rendered in the app).
  await view.getByLabel("Format").selectOption("json");
  const download = page.waitForEvent("download");
  await view.getByRole("button", { name: "Export evidence pack" }).click();
  expect((await download).suggestedFilename()).toBe(`evidence-pack-${MODEL}-v1.json`);
});

test("the submitter cannot approve their own validation", async ({ page }) => {
  await as(page, "ana");
  await page.goto(`/models/${MODEL}/versions/1/validations`);
  await page.getByLabel("Note (optional)").fill("Holiday refresh");
  await page.getByRole("button", { name: "Request validation" }).click();
  await expect(page).toHaveURL(/\/validations\/val_/);
  const view = page.getByTestId("validation-view");
  await expect(view.locator("[data-validation-status]").first()).toHaveText("Checks running");
  await expect(view.locator("[data-validation-status]").first()).toHaveText("Awaiting approval", { timeout: 10_000 });
  const form = view.getByTestId("decision-form");
  await expect(form.getByRole("note")).toHaveText("You submitted this validation. Another person must review it.");
  await expect(form.getByRole("button", { name: "Approve" })).toBeDisabled();
});

test("decisions are hidden without the reviewer capability, which an admin grants on Members", async ({ page }) => {
  await as(page, "rui");
  await page.goto(`/validations/${AWAITING}`);
  await expect(page.getByTestId("validation-view")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Approvals" })).toBeVisible();
  await expect(page.getByTestId("decision-form")).toHaveCount(0);

  await page.evaluate(() => {
    localStorage.setItem("mapi.mock.session", "ana");
  });
  await page.goto("/members");
  const toggle = page.getByRole("switch", { name: "Rui Runner can approve validations" });
  await expect(toggle).toHaveAttribute("aria-checked", "false");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-checked", "true");
  await expect(page.getByText("Rui Runner can now approve validations.")).toBeVisible();
});

test.describe("phone layout (375 px)", () => {
  test.use({ viewport: { width: 375, height: 812 } });
  const pages: [string, string][] = [
    ["/workflows?tab=templates", "Time-series readiness to ARIMAX"],
    ["/workflows/wf_01jaf3e1f2g3h4j5k6m7n8p9q0?tab=editor", "Workflow settings"],
    ["/workflow-runs/run_01jaf3w1x2y3z4a5b6c7d8e9f0", "21 of 21 steps finished"],
    ["/governance", "Forecasts we publish"],
    ["/governance/policies/pol_01jaf3h1j2k3m4n5p6q7r8s9t1", "Required checks"],
    [`/validations/${AWAITING}`, "Better than shuffled data"],
  ];
  for (const [path, marker] of pages) {
    test(`${path} does not scroll horizontally`, async ({ page }) => {
      await page.goto(path);
      await expect(page.getByRole("main").getByText(marker).first()).toBeVisible();
      await page.waitForTimeout(500); // lazy content (the canvas, plots) lays out
      const { scroll, client } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
      expect(scroll, `${path} is ${scroll}px wide in a ${client}px viewport`).toBeLessThanOrEqual(client);
    });
  }
});
