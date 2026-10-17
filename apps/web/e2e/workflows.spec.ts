/**
 * M3.5 smoke tests against the MSW-mocked API (`vite --mode mock`, production build with the CSP
 * and Trusted Types enforced): build a small workflow on the canvas (typed ports refuse a wrong
 * edge, the branch condition is checked by the server), follow a run live over SSE and open a
 * node's details, cancel a run, use the Guided recipe flow, add a schedule with its preview.
 */
import { expect, test, type Page } from "@playwright/test";

const WORKFLOW = "wf_01jaf3e1f2g3h4j5k6m7n8p9q0";
const SHOTS = process.env.MAPI_SHOTS_DIR;

async function as(page: Page, persona: "ana" | "rui" | "vic") {
  await page.addInitScript((p) => {
    if (window.top !== window || sessionStorage.getItem("mapi.test.persona")) return;
    localStorage.setItem("mapi.mock.session", p);
    // Mock runs advance one event every 60 ms (default 150).
    localStorage.setItem("mapi.mock.wfStepMs", "60");
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

/** Where a locator is once it stopped moving (the canvas animates when it fits new nodes). */
async function settledBox(page: Page, selector: string) {
  let last = await page.locator(selector).boundingBox();
  for (let i = 0; i < 20; i++) {
    await page.waitForTimeout(100);
    const next = await page.locator(selector).boundingBox();
    if (last && next && Math.abs(last.x - next.x) < 0.5 && Math.abs(last.y - next.y) < 0.5) return next;
    last = next;
  }
  return last;
}

/** Drag from one React Flow handle to another with the mouse. */
async function dragHandle(page: Page, from: string, to: string) {
  const a = await settledBox(page, from);
  const b = await settledBox(page, to);
  if (!a || !b) throw new Error(`handle not found: ${!a ? from : to}`);
  await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2);
  await page.mouse.down();
  await page.mouse.move((a.x + b.x) / 2, (a.y + b.y) / 2, { steps: 5 });
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 5 });
  await page.mouse.up();
}

test("a small workflow is built on the canvas, checked live and saved", async ({ page }) => {
  await as(page, "ana");
  await page.goto("/workflows/new");
  await page.getByLabel("Workflow name").fill("my-readiness");
  await page.getByRole("button", { name: "Open the editor" }).click();
  await expect(page.getByTestId("workflow-canvas")).toBeVisible();

  // Add an ADF test from the palette (keyboard path: pick, read its About, Add).
  const palette = page.getByRole("region", { name: "Node palette" });
  await palette.getByLabel("Search nodes").fill("adf");
  await palette.locator('[data-node="test.adf"]').click();
  await expect(palette.getByTestId("about-card")).toContainText("Use it when");
  await palette.getByRole("button", { name: "Add" }).click();
  await expect(page.getByTestId("canvas-node-adf")).toBeVisible();

  // Connect the workflow input from the node editor.
  const inspector = page.getByTestId("node-inspector");
  await inspector.getByLabel("Connect data").selectOption("inputs.data");
  await expect(page.getByText("Ready to run")).toBeVisible();

  // A differencing node, then a wrong edge on the canvas: a test result is not data.
  await palette.getByLabel("Search nodes").fill("differen");
  await palette.locator('[data-node="transform.difference"]').click();
  await palette.getByRole("button", { name: "Add" }).click();
  await expect(page.getByTestId("canvas-node-difference")).toBeVisible();
  await page.getByRole("button", { name: "Fit View" }).click();
  await page.getByTestId("workflow-canvas").scrollIntoViewIfNeeded();
  // Under load a mouse drag can miss a handle while the canvas settles, so each drag is
  // retried until it has its effect; the right edge is only dragged while it is missing.
  await expect(async () => {
    await dragHandle(page, '[data-testid="canvas-node-adf"] [data-handleid="out:result"]', '[data-testid="canvas-node-difference"] [data-handleid="in:data"]');
    await expect(page.getByRole("alert").filter({ hasText: "That connection is not possible" })).toContainText("difference.data takes dataset, but adf.result gives test result.", { timeout: 1500 });
  }).toPass({ timeout: 20_000 });
  // The right edge: the workflow input feeds the differencing node.
  const edges = page.locator(".react-flow__edge");
  await expect(async () => {
    if ((await edges.count()) < 2) await dragHandle(page, '[data-testid="canvas-node-inputs"] [data-handleid="out:data"]', '[data-testid="canvas-node-difference"] [data-handleid="in:data"]');
    await expect(edges).toHaveCount(2, { timeout: 1500 });
  }).toPass({ timeout: 20_000 });

  // A branch decides on the ADF result: the server checks the condition as it is typed.
  await palette.getByLabel("Search nodes").fill("branch");
  await palette.locator('[data-node="branch.if"]').click();
  await palette.getByRole("button", { name: "Add" }).click();
  const condition = page.getByLabel("Condition");
  await condition.fill("nodes.adf.p_valu < 0.05");
  await expect(page.getByRole("status").filter({ hasText: "results have no field 'p_valu'" })).toBeVisible();
  await expect(page.getByTestId("canvas-node-if")).toContainText("1");
  await condition.fill("not nodes.adf.reject");
  await expect(page.getByText("The condition is correct.")).toBeVisible();
  await shot(page, "workflow-editor");

  // Gate the differencing node on the then arm (inspector), then save.
  await page.getByTestId("canvas-node-difference").click();
  await page.getByTestId("node-inspector").getByLabel("When it runs").selectOption("if.then");
  await expect(page.getByTestId("canvas-node-difference")).toContainText("only if if → then");
  await expect(page.getByText("Ready to run")).toBeVisible();
  await page.getByRole("button", { name: "Create workflow" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(/\/workflows\/wf_[a-z0-9]+\?tab=editor/);
  await expect(page.getByRole("heading", { name: "my-readiness" })).toBeVisible();

  // The YAML view shows the same document.
  await page.getByRole("radio", { name: "YAML" }).click();
  await expect(page.getByTestId("yaml-editor")).toContainText("use: transform.difference");
  await expect(page.getByTestId("yaml-editor")).toContainText("when: if.then");
});

test("a run is followed live, a node's details open and another run is cancelled", async ({ page }) => {
  await as(page, "ana");
  await page.goto(`/workflows/${WORKFLOW}?tab=runs`);
  await page.getByRole("button", { name: "Run workflow" }).click();
  const dialog = page.getByRole("dialog", { name: "Run revision 2" });
  await dialog.getByLabel("Your data").selectOption({ label: "eu_media_daily – Daily spend and revenue per market" });
  await dialog.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/workflow-runs\/run_/);
  const monitor = page.getByTestId("run-monitor");
  await expect(monitor.getByTestId("run-status")).toHaveText(/Queued|Running/);
  await expect(page.getByTestId("canvas-node-adf")).toHaveAttribute("data-status", /queued|running|succeeded/);
  await expect(monitor).toHaveAttribute("data-status", "succeeded", { timeout: 20_000 });
  await expect(page.getByTestId("run-summary")).toContainText(/the tests found that the series wanders rather than returning to a steady level, so it was differenced/i);
  await shot(page, "run-monitor");

  await page.getByRole("tab", { name: "Steps" }).click();
  await page.getByRole("table", { name: "Steps" }).getByRole("button", { name: /Does it wander\? \(ADF\)/ }).click();
  const drawer = page.getByTestId("node-drawer");
  await expect(drawer.getByTestId("test-verdict")).toContainText("The series wanders");
  await expect(drawer.getByText("Sandbox run")).toBeVisible();
  await shot(page, "node-drawer");
  await page.keyboard.press("Escape");

  await page.getByRole("tab", { name: /Decisions/ }).click();
  await expect(page.getByTestId("branch-decision").filter({ hasText: "nodes.kpss.reject" })).toContainText("The condition was met. Ran: Change from the previous period.");

  // Start another run and cancel it while it runs.
  await page.goto(`/workflows/${WORKFLOW}?tab=runs`);
  await page.getByRole("button", { name: "Run workflow" }).click();
  const again = page.getByRole("dialog", { name: "Run revision 2" });
  await again.getByLabel("Your data").selectOption({ label: "eu_media_daily – Daily spend and revenue per market" });
  await again.getByRole("button", { name: "Start run" }).click();
  await page.getByRole("button", { name: "Cancel run" }).click();
  await expect(page.getByTestId("run-monitor")).toHaveAttribute("data-status", "cancelled", { timeout: 10_000 });
  await expect(page.getByRole("table", { name: "Steps" }).or(page.getByTestId("canvas-node-report"))).toBeVisible();
});

test("a Guided user runs a recipe on their data", async ({ page }) => {
  await as(page, "rui");
  await page.addInitScript(() => localStorage.setItem("mapi.ui", JSON.stringify({ state: { theme: "system", persona: "guided", personas: { usr_01jaf3b9c8d7e6f5g4h3j2k1m0: "guided" }, sidebarCollapsed: false }, version: 3 })));
  await page.goto("/workflows/recipe?template=ts_readiness_arimax");
  const runner = page.getByTestId("recipe-runner");
  await expect(runner.getByTestId("about-card")).toContainText("Time-series readiness to ARIMAX");
  await runner.getByRole("button", { name: "Run this recipe" }).click();
  await expect(runner.getByRole("alert")).toContainText("Still needed: your data");
  await runner.getByLabel("Dataset").selectOption({ label: "eu_media_daily – Daily spend and revenue per market" });
  await runner.getByRole("button", { name: "Run this recipe" }).click();
  await expect(page).toHaveURL(/\/workflow-runs\/run_/);
  await expect(page.getByTestId("run-monitor")).toHaveAttribute("data-status", "succeeded", { timeout: 20_000 });
  await expect(page.getByTestId("run-summary")).toBeVisible();
});

test("a schedule previews its next runs and is added", async ({ page }) => {
  await as(page, "ana");
  await page.goto(`/workflows/${WORKFLOW}?tab=automation`);
  await expect(page.locator("li[data-schedule]").filter({ hasText: "Every Monday at 06:00" })).toBeVisible();
  await page.getByRole("button", { name: "Schedule", exact: true }).click();
  const form = page.getByRole("form", { name: "New schedule" });
  await form.getByLabel("Cron expression").fill("*/5 * * * *");
  await expect(form.getByText("schedules may fire at most every 15 minutes")).toBeVisible();
  await form.getByRole("button", { name: "Every weekday at 07:00" }).click();
  await form.getByLabel("Time zone").fill("Europe/Berlin");
  await expect(form.getByTestId("cron-preview")).toContainText("Every weekday at 07:00 (Europe/Berlin)");
  await expect(form.getByTestId("cron-preview").locator("li")).toHaveCount(5);
  await form.getByRole("button", { name: "Add schedule" }).click();
  await expect(page.locator("li[data-schedule]").filter({ hasText: "Every weekday at 07:00" })).toBeVisible();
});
