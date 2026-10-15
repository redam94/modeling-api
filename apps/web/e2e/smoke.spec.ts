/**
 * Smoke test against the MSW-mocked API (`vite --mode mock`) and the real plot sandbox on its
 * own origin. Runs by default in `pnpm test`.
 */
import { expect, test } from "@playwright/test";

const RUNNING = "run_01jaf3q2x8m9v7k5h4g3f2e1d0";
const FAILED = "run_01jaf3q2x8m9v7k5h4g3f2e1b2";
const MODEL = "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0";
const SPEC = "spc_01jaf3b1c2d3e4f5g6h7j8k9m0";

test.beforeEach(async ({ page }) => {
  page.on("pageerror", (err) => {
    throw err;
  });
});

test("home shows runs in flight and navigates by keyboard", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "1 run in flight" })).toBeVisible();
  await expect(page.getByText("API connected")).toBeVisible();
  await page.keyboard.press("g");
  await page.keyboard.press("r");
  await expect(page).toHaveURL(/\/runs$/);
  await expect(page.getByRole("heading", { name: "Runs" })).toBeVisible();
});

test("run detail streams chain progress and renders logs as text", async ({ page }) => {
  await page.goto(`/runs/${RUNNING}`);
  await expect(page.getByText("Live")).toBeVisible();
  const chain0 = page.getByRole("progressbar", { name: "Chain 0" });
  await expect(chain0).toBeVisible();
  const before = Number(await chain0.getAttribute("aria-valuenow"));
  await expect.poll(async () => Number(await chain0.getAttribute("aria-valuenow")), { timeout: 8000 }).toBeGreaterThan(before);
  await expect(page.getByText("2 div.").first()).toBeVisible();

  const log = page.getByRole("log", { name: "Run logs" });
  await expect(log.getByText("<img src=x onerror=alert(1)> plain text only")).toBeVisible();
  await expect(log.locator("img")).toHaveCount(0);
  await expect(log.getByText("Sampling 4 chains", { exact: false })).toBeVisible();
  await expect(log).not.toContainText("\u001b");
});

test("failed run shows the problem and strips untrusted markup", async ({ page }) => {
  await page.goto(`/runs/${FAILED}`);
  await expect(page.getByRole("alert").filter({ hasText: "Run exhausted its resources" })).toBeVisible();
  await page.getByText("Output from the run (untrusted)").click();
  const pre = page.locator("pre").filter({ hasText: "MemoryError" });
  await expect(pre).toContainText("<script>alert(1)</script>");
  await expect(pre.locator("script")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
});

test("model plots render inside the isolated sandbox origin", async ({ page }) => {
  await page.goto(`/models/${MODEL}/versions/1?tab=plots`);
  const frame = page.locator('iframe[data-testid="plot-frame"]');
  await expect(frame).toHaveAttribute("sandbox", "allow-scripts");
  await expect(frame).toHaveAttribute("src", "http://127.0.0.1:5174/");
  await expect(frame.contentFrame().locator("svg.marks")).toBeVisible({ timeout: 20_000 });
  await expect(frame.contentFrame().locator("path").first()).toBeAttached();
});

test("spec editor validates live and shows the pipeline with its split", async ({ page }) => {
  await page.goto(`/build/${SPEC}`);
  await expect(page.getByTestId("yaml-editor").locator(".monaco-editor")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("yaml-editor").locator(".view-lines")).toContainText("apiVersion: mapi/v1");
  await expect(page.getByText("valid", { exact: true })).toBeVisible();
  const pipeline = page.getByRole("list", { name: "Pipeline" });
  await expect(pipeline.getByText("weekly", { exact: true })).toBeVisible();
  await expect(pipeline.getByRole("separator", { name: "Train/holdout split" })).toBeVisible();
  await expect(pipeline.getByText("Keeps a 6-step tail along time", { exact: false })).toBeVisible();
});

test("theme can be switched to dark", async ({ page }) => {
  await page.goto("/settings");
  await page.getByRole("radio", { name: "Dark" }).click();
  await expect(page.locator("html")).toHaveClass(/dark/);
});
