/**
 * M3.5 journeys across screens (MSW-mocked API, production build with the CSP and Trusted
 * Types enforced): a Guided user goes from the Data Explorer to the readiness recipe on that
 * data, follows the run and opens the fitted model in the Model Explorer and its validations;
 * Guided what-if leads to budget optimisation and back. Every test ends without a CSP violation.
 */
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

const DATASET = "ds_01jaf3a1b2c3d4e5f6g7h8j9k0";
const SAMPLE = fileURLToPath(new URL("./fixtures/guided-geo-mmm.csv", import.meta.url));
const RUI = "usr_01jaf3b9c8d7e6f5g4h3j2k1m0";

/** Start as a mock persona; `guided` stores Guided mode as the user's choice. */
async function as(page: Page, persona: "ana" | "rui", guided = false) {
  await page.addInitScript(
    ([p, g, rui]) => {
      if (window.top !== window || sessionStorage.getItem("mapi.test.persona")) return;
      localStorage.setItem("mapi.mock.session", p);
      localStorage.setItem("mapi.mock.wfStepMs", "60");
      if (g) localStorage.setItem("mapi.ui", JSON.stringify({ state: { theme: "system", persona: "guided", personas: { [rui]: "guided" }, sidebarCollapsed: false }, version: 3 }));
      sessionStorage.setItem("mapi.test.persona", "1");
    },
    [persona, guided, RUI] as const,
  );
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

test("Guided: from the Data Explorer to a recipe run, the fitted model and its validation", async ({ page }) => {
  test.setTimeout(60_000);
  await as(page, "rui", true);
  await page.goto(`/datasets/${DATASET}/versions/1/explore`);
  const nav = page.getByRole("navigation").filter({ has: page.getByRole("link", { name: /Recipes/ }) });
  await expect(nav.getByRole("link", { name: /Help me choose/ })).toBeVisible();

  await page.getByRole("link", { name: "Test if it is ready to forecast" }).click();
  await expect(page).toHaveURL(/\/workflows\/recipe\?template=ts_readiness_arimax&data=/);
  const runner = page.getByTestId("recipe-runner");
  await expect(runner.getByLabel("Dataset")).toHaveValue("dataset://default/eu_media_daily");
  await runner.getByRole("button", { name: "Run this recipe" }).click();
  await expect(page).toHaveURL(/\/workflow-runs\/run_/);
  await expect(page.getByTestId("run-monitor")).toHaveAttribute("data-status", "succeeded", { timeout: 20_000 });

  await page.getByTestId("run-models").getByRole("link", { name: /in the Model Explorer/ }).click();
  await expect(page).toHaveURL(/\/models\/mdl_[a-z0-9]+\/versions\/1\/explore/);
  await expect(page.getByRole("heading", { name: "At a glance" })).toBeVisible();
  await page.getByRole("link", { name: /^Validation: / }).click();
  await expect(page).toHaveURL(/\/versions\/1\/validations$/);
});

test("Guided what-if leads to budget optimisation and back", async ({ page }) => {
  test.setTimeout(90_000);
  await as(page, "ana");
  await page.goto("/guided");
  await page.getByRole("link", { name: /^Geo marketing mix/ }).click();
  await page.getByLabel("Choose a data file").setInputFiles(SAMPLE);
  await expect(page.getByLabel("Which column is the result you want to explain?")).toHaveValue("Revenue (EUR)");
  await page.getByRole("button", { name: "Upload and continue" }).click();
  await expect(page).toHaveURL(/\/priors$/);
  await page.getByRole("button", { name: "Continue without beliefs" }).click();
  await page.getByRole("button", { name: "Fit the model" }).click();
  await page.getByRole("button", { name: "See the results" }).click({ timeout: 20_000 });
  await page.getByRole("button", { name: "What if…" }).click();
  await expect(page.getByTestId("whatif-delta")).toBeVisible();

  const entry = page.getByTestId("optimise-entry");
  await expect(entry).toContainText("Optimise my budget");
  await entry.getByRole("link", { name: "Find the best split" }).click();
  await expect(page).toHaveURL(/\/optimize\?back=/);
  await expect(page.getByRole("heading", { name: "Optimise a budget" })).toBeVisible();
  await page.getByRole("button", { name: "Optimise the budget" }).click();
  await expect(page.getByTestId("plan-result")).toBeVisible({ timeout: 15_000 });
  await page.getByRole("link", { name: "Back to your analysis" }).click();
  await expect(page).toHaveURL(/\/guided\/[^/]+\/explore$/);
  await expect(page.getByTestId("optimise-entry")).toBeVisible();
});
