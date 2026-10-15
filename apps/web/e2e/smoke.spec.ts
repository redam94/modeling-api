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
  // Record CSP / Trusted Types violations on the app origin; every test must end with none.
  await page.addInitScript(() => {
    const w = window as unknown as { __csp: string[] };
    w.__csp = [];
    document.addEventListener("securitypolicyviolation", (e) => w.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
  });
});

test.afterEach(async ({ page }) => {
  const violations = await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp);
  expect(violations).toEqual([]);
});

test("the app is served with a CSP that enforces Trusted Types", async ({ page }) => {
  const res = await page.goto("/");
  expect(res?.headers()["content-security-policy"]).toContain("require-trusted-types-for 'script'");
  const meta = await page.locator('meta[http-equiv="Content-Security-Policy"]').getAttribute("content");
  expect(meta).toContain("frame-src http://127.0.0.1:5174");
  expect(await page.evaluate(() => typeof (window as unknown as { trustedTypes?: unknown }).trustedTypes)).toBe("object");
  const blocked = await page.evaluate(() => {
    try {
      // eslint-disable-next-line no-restricted-properties -- deliberate Trusted Types probe
      document.createElement("div").innerHTML = "<img src=x>";
      return false;
    } catch {
      return true;
    }
  });
  expect(blocked).toBe(true);
  // The probe above is the only expected violation; clear it for the afterEach check.
  const probe = await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp.splice(0));
  expect(probe.every((v) => v.startsWith("require-trusted-types-for"))).toBe(true);
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

async function typeInEditor(page: import("@playwright/test").Page, text: string) {
  const editor = page.getByTestId("yaml-editor");
  await expect(editor.locator(".view-lines")).toContainText("apiVersion: mapi/v1", { timeout: 20_000 });
  await editor.locator(".view-line").first().click();
  await page.keyboard.press("End");
  await page.keyboard.press("Enter");
  await page.keyboard.type(text);
  await expect(editor.locator(".view-lines")).toContainText(text);
}

test("spec editor keeps unsaved edits when a newer revision appears", async ({ page }) => {
  await page.goto(`/build/${SPEC}`);
  // The revision being edited is pinned in the URL once the latest one is known.
  await expect(page).toHaveURL(/[?&]rev=2\b/);
  await typeInEditor(page, "# MY UNSAVED EDIT");
  await expect(page.getByText("unsaved changes", { exact: true })).toBeVisible();
  // Another tab (or the CLI) saves r3.
  const status = await page.evaluate(async (spec) => {
    const res = await fetch(`http://127.0.0.1:8000/v1/specs/${spec}/revisions`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ yaml: "apiVersion: mapi/v1\nkind: ModelSpec\n", parent_revision: 2 }),
    });
    return res.status;
  }, SPEC);
  expect(status).toBe(201);
  // Let the spec query go stale (staleTime 10 s), then refocus the window to refetch it.
  await page.waitForTimeout(10_500);
  await page.evaluate(() => {
    // TanStack Query's focusManager listens for `visibilitychange` on window.
    window.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(page.getByRole("status").filter({ hasText: "r3" })).toBeVisible();
  await expect(page.getByTestId("yaml-editor").locator(".view-lines")).toContainText("# MY UNSAVED EDIT");
  await expect(page.getByText("unsaved changes", { exact: true })).toBeVisible();
});

test("a CSV is previewed locally, mapped, uploaded to the signed URL and ingested", async ({ page }) => {
  await page.goto("/datasets/ds_01jaf3a1b2c3d4e5f6g7h8j9k0?upload=true");
  const csv = "date,market,revenue,spend\n2026-01-01,de,10.5,3\n2026-01-02,de,11,4\n";
  await page.locator('input[type="file"]').setInputFiles({ name: "eu.csv", mimeType: "text/csv", buffer: Buffer.from(csv) });
  await expect(page.getByText("Local preview: first 2 rows")).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Use date as" })).toHaveValue("dim");
  await expect(page.getByRole("combobox", { name: "Use revenue as" })).toHaveValue("var");
  await page.getByRole("button", { name: "Upload and ingest" }).click();
  await expect(page.getByText("Ingesting v2")).toBeVisible();
  await expect(page).toHaveURL(/version=2/);
  await expect(page.getByText("ingesting").first()).toBeVisible();
});

test("scoring returns outputs that open as a table and a sandboxed chart", async ({ page }) => {
  await page.goto(`/score?model=${MODEL}&version=1`);
  await page.getByRole("button", { name: "Add" }).click();
  await page.getByRole("textbox", { name: "Selection" }).fill("channel=tv");
  await page.getByRole("button", { name: "Score", exact: true }).click();
  await expect(page.getByText("ui/plotdata/ppc_fan.arrow")).toBeVisible();
  await expect(page.getByRole("table", { name: "ui/plotdata/ppc_fan.arrow" })).toContainText("week");
  const frame = page.locator('iframe[data-testid="plot-frame"]');
  await expect(frame.contentFrame().locator("svg.marks")).toBeVisible({ timeout: 20_000 });
});

test("theme can be switched to dark", async ({ page }) => {
  await page.goto("/settings");
  await page.getByRole("radio", { name: "Dark" }).click();
  await expect(page.locator("html")).toHaveClass(/dark/);
});
