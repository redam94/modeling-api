/**
 * End-to-end against the real local stack (API on VITE_API_BASE, GCS shim, runner, plot
 * sandbox). Tagged @e2e: run with `pnpm e2e` (MAPI_E2E=1) once `mapi serve` is up.
 * Never part of the default test run.
 */
import { expect, test } from "@playwright/test";

const API = process.env.VITE_API_BASE ?? "http://127.0.0.1:8000";

test.describe("@e2e local stack", () => {
  test.beforeAll(async ({ request }) => {
    const res = await request.get(`${API}/readyz`);
    expect(res.ok(), `the API at ${API} must be ready`).toBeTruthy();
  });

  test("catalog lists built-in steps @e2e", async ({ page }) => {
    await page.goto("/catalog?tab=steps");
    await expect(page.getByRole("table", { name: "Steps" })).toBeVisible();
    await expect(page.getByText("core.resample")).toBeVisible();
  });

  test("a new spec validates live and saves as revision 1 @e2e", async ({ page }) => {
    await page.goto("/build/new");
    const editor = page.getByTestId("yaml-editor").locator(".monaco-editor");
    await expect(editor).toBeVisible({ timeout: 30_000 });
    // The starter is named eu-geo-mmm; a seeded stack already has that spec (409), so save
    // under a unique name.
    await editor.locator(".view-line", { hasText: "name: eu-geo-mmm" }).click();
    await page.keyboard.press("End");
    await page.keyboard.press("Shift+Home");
    await page.keyboard.type(`name: e2e-${Date.now().toString(36)}`);
    await page.keyboard.press("Escape");
    await expect(page.getByText(/valid|error/).first()).toBeVisible({ timeout: 15_000 });
    await page.getByRole("button", { name: "Save spec" }).click();
    await expect(page).toHaveURL(/\/build\/spc_/);
    await expect(page.getByText("r1")).toBeVisible();
  });

  test("a fit started from the editor reaches a terminal state @e2e", async ({ page }) => {
    test.slow();
    await page.goto("/build");
    await page.getByRole("table", { name: "Specs" }).getByRole("row").nth(1).click();
    await page.getByRole("button", { name: "Fit" }).click();
    await expect(page).toHaveURL(/\/runs\/run_/);
    await expect(page.locator("[data-state=succeeded], [data-state=failed], [data-state=partial]").first()).toBeVisible({
      timeout: 15 * 60_000,
    });
  });
});
