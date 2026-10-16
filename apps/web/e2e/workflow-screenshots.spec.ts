/**
 * Screenshots of the v0.5 workflow and governance screens for the docs (docs/guides/workflows.md,
 * governance.md, web-ui.md and the home page), taken from mock mode like the explorer ones.
 * Skipped unless MAPI_WORKFLOW_SCREENSHOTS=1; `make docs-screenshots-workflows` runs it and
 * shrinks the PNGs into docs/assets/screenshots/workflows-*.png and governance-*.png:
 *
 *   MAPI_WORKFLOW_SCREENSHOTS=1 MAPI_SMOKE_WEB_PORT=15183 MAPI_SMOKE_PLOTS_PORT=15184 \
 *     pnpm --filter @mapi/web exec playwright test e2e/workflow-screenshots.spec.ts
 *
 * Raw captures land in .local/screenshots-workflows/.
 */
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

const WORKFLOW = "wf_01jaf3e1f2g3h4j5k6m7n8p9q0";
const POLICY = "pol_01jaf3h1j2k3m4n5p6q7r8s9t1";
const AWAITING = "val_01jaf3m1n2p3q4r5s6t7v8w9x0";
const OUT = (name: string) => fileURLToPath(new URL(`../../../.local/screenshots-workflows/${name}.png`, import.meta.url));

test.skip(!process.env.MAPI_WORKFLOW_SCREENSHOTS, "set MAPI_WORKFLOW_SCREENSHOTS=1 to refresh the workflow screenshots");
test.use({ viewport: { width: 1280, height: 800 }, colorScheme: "light" });

async function as(page: Page, persona: "ana" | "rui", guided = false) {
  await page.addInitScript(
    ({ p, g }) => {
      if (window.top !== window || sessionStorage.getItem("mapi.test.persona")) return;
      localStorage.setItem("mapi.mock.session", p);
      localStorage.setItem("mapi.mock.wfStepMs", "40");
      if (g) {
        localStorage.setItem(
          "mapi.ui",
          JSON.stringify({ state: { theme: "light", persona: "guided", personas: { usr_01jaf3b9c8d7e6f5g4h3j2k1m0: "guided" }, sidebarCollapsed: false }, version: 3 }),
        );
      }
      sessionStorage.setItem("mapi.test.persona", "1");
    },
    { p: persona, g: guided },
  );
}

async function settle(page: Page) {
  await expect(page.locator("figure .animate-spin")).toHaveCount(0, { timeout: 20_000 });
  await page.waitForTimeout(600);
}

/** The viewport grows to `height` (cross-origin plot frames stay blank below a viewport). */
async function shot(page: Page, name: string, height = 800, top = false) {
  if (height !== 800) await page.setViewportSize({ width: 1280, height });
  if (top) await page.evaluate(() => window.scrollTo(0, 0));
  // (a folded evidence plot keeps its spinner until it is opened)
  if (name === "governance-validation") await page.waitForTimeout(1000);
  else await settle(page);
  await page.screenshot({ path: OUT(name) });
  if (height !== 800) await page.setViewportSize({ width: 1280, height: 800 });
}

test("workflow screenshots", async ({ page }) => {
  test.setTimeout(240_000);
  await as(page, "ana");

  // The gallery
  await page.goto("/workflows?tab=templates");
  await expect(page.getByRole("main").getByText("Time-series readiness to ARIMAX").first()).toBeVisible();
  await shot(page, "workflows-gallery");

  // The editor on the canonical workflow
  await page.goto(`/workflows/${WORKFLOW}?tab=editor`);
  await expect(page.getByTestId("workflow-canvas")).toBeVisible();
  await page.waitForTimeout(800);
  await page.getByRole("button", { name: "Fit View" }).click();
  await page.getByTestId("canvas-node-wanders").click();
  await expect(page.getByLabel("Condition")).toBeVisible();
  await shot(page, "workflows-editor", 1000, true);

  // A run, followed live, then its steps and a node's drawer
  await page.goto(`/workflows/${WORKFLOW}?tab=runs`);
  await page.getByRole("button", { name: "Run workflow" }).click();
  const dialog = page.getByRole("dialog", { name: /Run revision/ });
  await dialog.getByLabel("Your data").selectOption({ label: "eu_media_daily – Daily spend and revenue per market" });
  await dialog.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/workflow-runs\/run_/);
  const monitor = page.getByTestId("run-monitor");
  await expect(monitor).toHaveAttribute("data-status", "succeeded", { timeout: 30_000 });
  await page.getByRole("button", { name: "Fit View" }).click().catch(() => undefined);
  await shot(page, "workflows-run-monitor", 1000, true);

  await page.getByRole("tab", { name: "Steps" }).click();
  await page.getByRole("table", { name: "Steps" }).getByRole("button", { name: /Does it wander\? \(ADF\)/ }).click();
  await expect(page.getByTestId("node-drawer").getByTestId("test-verdict")).toContainText("The series wanders");
  await shot(page, "workflows-node-drawer");
  await page.keyboard.press("Escape");

  await page.getByRole("tab", { name: /Decisions/ }).click();
  await expect(page.getByTestId("branch-decision").first()).toBeVisible();
  await shot(page, "workflows-decisions");

  // Schedules and triggers
  await page.goto(`/workflows/${WORKFLOW}?tab=automation`);
  await expect(page.locator("li[data-schedule]").first()).toBeVisible();
  await page.getByRole("button", { name: "Schedule", exact: true }).click();
  const form = page.getByRole("form", { name: "New schedule" });
  await form.getByRole("button", { name: "Every weekday at 07:00" }).click();
  await form.getByLabel("Time zone").fill("Europe/Berlin");
  await expect(form.getByTestId("cron-preview").locator("li")).toHaveCount(5);
  await shot(page, "workflows-schedule");

  // Governance: policies in force, the policy editor, the review queue and a validation
  await page.goto("/governance");
  await expect(page.getByRole("table", { name: "Policies" })).toBeVisible();
  await shot(page, "governance-policies");

  await page.goto(`/governance/policies/${POLICY}`);
  const editor = page.getByTestId("policy-editor");
  await expect(editor.getByText("Required checks").first()).toBeVisible();
  await editor.getByLabel("Add a check").selectOption("null.noise_regressors");
  const card = editor.getByRole("article", { name: "Ignores fake drivers" });
  await expect(card).toBeVisible();
  await shot(page, "governance-policy", 900, true);
  await card.evaluate((el) => el.scrollIntoView({ block: "center" }));
  await shot(page, "governance-policy-editor", 900);

  await page.goto("/validations?status=awaiting_approval");
  await expect(page.getByRole("heading", { name: "Review queue" })).toBeVisible();
  await shot(page, "governance-review-queue");

  await page.goto(`/validations/${AWAITING}`);
  const view = page.getByTestId("validation-view");
  await expect(view.getByRole("list", { name: "Validation progress" })).toContainText("Awaiting approval");
  await shot(page, "governance-validation", 1000, true);
  const permutation = view.locator('article[data-check="null.permutation_target"]');
  await permutation.getByText(/^Evidence/).click();
  await expect(permutation.getByTestId("plot-frame")).toBeVisible();
  await permutation.evaluate((el) => el.scrollIntoView({ block: "start" }));
  await shot(page, "governance-evidence", 1000);
});

test("guided recipe screenshot", async ({ page }) => {
  test.setTimeout(120_000);
  await as(page, "rui", true);
  await page.goto("/workflows/recipe?template=ts_readiness_arimax");
  const runner = page.getByTestId("recipe-runner");
  await expect(runner.getByTestId("about-card")).toContainText("Time-series readiness to ARIMAX");
  await runner.getByLabel("Dataset").selectOption({ label: "eu_media_daily – Daily spend and revenue per market" });
  await shot(page, "workflows-recipe");
});
