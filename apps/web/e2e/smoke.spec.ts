/**
 * Smoke test against the MSW-mocked API (`vite --mode mock`) and the real plot sandbox on its
 * own origin. Runs by default in `pnpm test`.
 */
import { expect, test } from "@playwright/test";

const RUNNING = "run_01jaf3q2x8m9v7k5h4g3f2e1d0";
const FAILED = "run_01jaf3q2x8m9v7k5h4g3f2e1b2";
const MODEL = "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0";
const SPEC = "spc_01jaf3b1c2d3e4f5g6h7j8k9m0";
const PLOTS_ORIGIN = process.env.MAPI_SMOKE_PLOTS_ORIGIN ?? "http://127.0.0.1:5174";

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
  expect(meta).toContain(`frame-src ${PLOTS_ORIGIN}`);
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
  await expect(frame).toHaveAttribute("src", `${PLOTS_ORIGIN}/`);
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
  await expect(pipeline.getByText("cal", { exact: true })).toBeVisible();
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
    // What any v0.3 browser client sends: the project and the session's CSRF token.
    const session = await fetch("http://127.0.0.1:8000/auth/session", { credentials: "include" });
    const csrf = session.headers.get("X-CSRF-Token") ?? "";
    const res = await fetch(`http://127.0.0.1:8000/v1/specs/${spec}/revisions`, {
      method: "POST",
      headers: { "content-type": "application/json", "X-Mapi-Project": "acme/growth", "X-CSRF-Token": csrf },
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

test("saving edits of an older revision asks before replacing the latest", async ({ page }) => {
  const posted: unknown[] = [];
  page.on("request", (req) => {
    if (req.method() === "POST" && /\/v1\/specs\/[^/]+\/revisions$/.test(req.url())) posted.push(req.postDataJSON());
  });
  await page.goto(`/build/${SPEC}?rev=1`);
  await typeInEditor(page, "# restore r1");
  const dialogs: string[] = [];
  page.once("dialog", (d) => {
    dialogs.push(d.message());
    void d.dismiss();
  });
  await page.getByRole("button", { name: "Save revision" }).click();
  await expect.poll(() => dialogs.length).toBe(1);
  expect(dialogs[0]).toContain("r1");
  expect(posted).toEqual([]);
  page.once("dialog", (d) => void d.accept());
  await page.getByRole("button", { name: "Save revision" }).click();
  await expect(page).toHaveURL(/[?&]rev=3\b/);
  expect(posted).toEqual([expect.objectContaining({ parent_revision: 2 })]);
});

/** Put the cursor at the end of the first line containing `line`, minus `back` characters. */
async function cursorAfter(page: import("@playwright/test").Page, line: string, back = 0, left = 0) {
  const editor = page.getByTestId("yaml-editor");
  await editor.locator(".view-line", { hasText: line }).first().click();
  await page.keyboard.press("End");
  for (let i = 0; i < left; i++) await page.keyboard.press("ArrowLeft");
  for (let i = 0; i < back; i++) await page.keyboard.press("Backspace");
}

test("spec editor suggests catalog models, dataset columns and step settings", async ({ page }) => {
  await page.goto("/build/new");
  const editor = page.getByTestId("yaml-editor");
  await expect(editor.locator(".view-lines")).toContainText("apiVersion: mapi/v1", { timeout: 20_000 });
  const suggest = page.locator(".suggest-widget");

  // model.use: every model in the catalog.
  await cursorAfter(page, "use: mmm.bayesian@^1", "mmm.bayesian@^1".length);
  await page.keyboard.press("Control+Space");
  await expect(suggest).toContainText("ts.arima@^1");
  await expect(suggest).toContainText("tabular.hierarchical_glm@^1");
  await page.keyboard.press("Escape");
  await page.keyboard.type("mmm.bayesian@^1");

  // data.bind values: the columns of the dataset the spec reads.
  await cursorAfter(page, "vars: {y: revenue_local", "revenue_local, spend: spend_local}".length);
  await page.keyboard.press("Control+Space");
  await expect(suggest).toContainText("spend_local");
  await page.keyboard.press("Escape");
  await page.keyboard.type("revenue_local, spend: spend_local}");

  // The model's own settings: a new key under `model.settings`.
  await cursorAfter(page, "seasonality: {yearly_order: 3}");
  await page.keyboard.press("Enter");
  await page.keyboard.press("Control+Space");
  await expect(suggest).toContainText("controls");
  await page.keyboard.press("Escape");

  // A step's `with`: core.impute's own settings (a new key inside its flow mapping).
  await cursorAfter(page, "id: fill", 0, "}}".length);
  await page.keyboard.type(", unse");
  await page.keyboard.press("Control+Space");
  await expect(suggest).toContainText("unseen_levels");
});

test("the Insert panel adds a pipeline step and a plot from a search", async ({ page }) => {
  await page.goto("/build/new");
  const editor = page.getByTestId("yaml-editor");
  await expect(editor.locator(".view-lines")).toContainText("apiVersion: mapi/v1", { timeout: 20_000 });
  await page.getByRole("button", { name: "Insert" }).click();
  const search = page.getByRole("textbox", { name: "Search models, steps, datasets and columns" });
  await expect(search).toBeFocused();
  await search.fill("mean_scale");
  await page.locator('[data-insert="step:core.mean_scale"]').click();
  await expect(editor.locator(".view-lines")).toContainText("id: mean_scale, use: core.mean_scale@^1");
  // Columns of the dataset, by the name the spec binds them to.
  await search.fill("revenue");
  await expect(page.locator('[data-insert="col:var:revenue_local"]')).toContainText("y ← revenue_local");
  // A plot of the model that the starter does not list goes into outputs.plots (below the
  // fold, so read the document that is saved).
  await search.fill("marginal");
  await page.locator('[data-insert="plot:mmm.marginal_cost"]').click();
  const saved = page.waitForRequest((r) => r.method() === "POST" && /\/v1\/specs$/.test(r.url()));
  await page.getByRole("button", { name: "Save spec" }).click();
  const yaml = ((await saved).postDataJSON() as { yaml: string }).yaml;
  expect(yaml).toContain("plots: [core.ppc_fan, core.convergence, mmm.decomposition, mmm.response_curves, mmm.marginal_cost]");
  // Edits elsewhere keep the starter's formatting.
  expect(yaml).toContain("dims: {geo: market, channel: channel, time: date}");
});

test("the visual builder adds, reorders and edits steps by drag and drop", async ({ page }) => {
  await page.goto("/build/new");
  await expect(page.getByTestId("yaml-editor").locator(".view-lines")).toContainText("apiVersion: mapi/v1", { timeout: 20_000 });
  await page.getByRole("radio", { name: "Visual" }).click();
  const builder = page.getByTestId("visual-builder");
  const stepIds = () => builder.locator('[data-block^="step:"]').evaluateAll((els) => els.map((e) => e.getAttribute("data-block")));
  await expect.poll(stepIds).toEqual(["step:weekly", "step:fill", "step:cal"]);

  // Drag a step from the palette between weekly and fill.
  await builder.getByRole("textbox", { name: "Search the palette" }).fill("mean_scale");
  await builder.locator('[data-palette="step:core.mean_scale@^1"]').dragTo(builder.locator('[data-drop-index="1"]'));
  await expect.poll(stepIds).toEqual(["step:weekly", "step:mean_scale", "step:fill", "step:cal"]);

  // Drag cal by its handle to the start.
  await builder.locator('[data-block="step:cal"] [data-drag-step]').dragTo(builder.locator('[data-drop-index="0"]'));
  await expect.poll(stepIds).toEqual(["step:cal", "step:weekly", "step:mean_scale", "step:fill"]);

  // Edit a setting in the step's form, with a suggested name.
  await builder.locator('[data-block="step:fill"] header button[aria-expanded]').click();
  const fill = builder.locator('[data-block="step:fill"]');
  await fill.getByRole("combobox", { name: "Method" }).selectOption("mean");
  await fill.getByRole("group", { name: "Add to Vars" }).getByRole("button", { name: "spend_local" }).click();

  // The YAML view shows the same document.
  await page.getByRole("radio", { name: "YAML" }).click();
  const saved = page.waitForRequest((r) => r.method() === "POST" && /\/v1\/specs$/.test(r.url()));
  await page.getByRole("button", { name: "Save spec" }).click();
  const yaml = ((await saved).postDataJSON() as { yaml: string }).yaml;
  expect(yaml).toMatch(/- \{id: cal, use: core\.calendar@\^1[^\n]*\n\s+- \{id: weekly,[^\n]*\n\s+- \{id: mean_scale, use: core\.mean_scale@\^1/);
  expect(yaml).toContain("- {id: fill, use: core.impute@^1, with: {vars: [spend, spend_local], method: mean, over: [time]}}");
});

test("the Score page explains the model's inputs and scores typed-in values", async ({ page }) => {
  const sent: unknown[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && /\/versions\/\d+:score$/.test(r.url())) sent.push(r.postDataJSON());
  });
  await page.goto(`/score?model=${MODEL}&version=1`);
  const needs = page.getByRole("region", { name: "What this model needs" });
  await expect(needs.getByText("needed on future periods")).toBeVisible({ timeout: 20_000 });
  const grid = page.getByRole("table", { name: "Values to score" });
  await expect(grid.getByLabel("time, row 1", { exact: true })).toHaveValue("2026-10-01");
  await grid.getByLabel("spend, row 1", { exact: true }).fill("1500");
  await page.getByRole("textbox", { name: "Value to fill with" }).fill("1000");
  await page.getByRole("button", { name: /Fill blanks/ }).click();
  await expect(grid.getByLabel("spend, row 1", { exact: true })).toHaveValue("1500");
  await expect(grid.getByLabel("spend, row 48")).toHaveValue("1000");
  await page.getByRole("button", { name: /^Forecast$/ }).click();
  await expect(page.getByRole("region", { name: "Results" }).getByText("ui/plotdata/ppc_fan.arrow")).toBeVisible();
  const body = sent[0] as { request: { data?: string }; inline: { data: { data_vars: { spend: { data: number[][][] } } } } };
  expect(body.request.data).toBeUndefined();
  expect(body.inline.data.data_vars.spend.data[0]![0]![0]).toBe(1500);
});

test("a CSV is previewed locally, mapped, uploaded to the signed URL and ingested", async ({ page }) => {
  await page.goto("/datasets/ds_01jaf3a1b2c3d4e5f6g7h8j9k0?upload=true");
  const csv = "date,market,revenue,spend\n2026-01-01,de,10.5,3\n2026-01-02,de,11,4\n";
  await page.locator('input[type="file"]').setInputFiles({ name: "eu.csv", mimeType: "text/csv", buffer: Buffer.from(csv) });
  await expect(page.getByText("Local preview: first 2 rows")).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Use date as" })).toHaveValue("dim");
  await expect(page.getByRole("combobox", { name: "Use revenue as" })).toHaveValue("var");
  await page.getByRole("button", { name: "Upload and ingest" }).click();
  await expect(page.getByText("The ingest of v2 started")).toBeVisible();
  await expect(page).toHaveURL(/version=2/);
  await expect(page.getByText("ingesting").first()).toBeVisible();
});

test("scoring returns outputs that open as a table and a sandboxed chart", async ({ page }) => {
  await page.goto(`/score?model=${MODEL}&version=1`);
  await page.getByRole("button", { name: "Add a change" }).click();
  await page.getByRole("textbox", { name: "Selection" }).fill("channel=tv");
  await page.getByRole("button", { name: "Forecast", exact: true }).click();
  await expect(page.getByText("ui/plotdata/ppc_fan.arrow")).toBeVisible();
  await expect(page.getByRole("table", { name: "ui/plotdata/ppc_fan.arrow" })).toContainText("week");
  const frame = page.locator('iframe[data-testid="plot-frame"]');
  await expect(frame.contentFrame().locator("svg.marks")).toBeVisible({ timeout: 20_000 });
  // Downloading an output saves a file and keeps the results on screen.
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download summary.json" }).click();
  expect((await download).suggestedFilename()).toBe("summary.json");
  await expect(page).toHaveURL(/\/score\?/);
  await expect(page.getByText("ui/plotdata/ppc_fan.arrow")).toBeVisible();
});

test("score form errors are marked on their fields and focus the first one", async ({ page }) => {
  await page.goto(`/score?model=${MODEL}&version=1`);
  await page.getByText("Advanced", { exact: true }).click();
  await page.getByLabel("Draws").fill("abc");
  await page.getByLabel("Variables (optional)").fill("bad name!");
  await page.getByRole("button", { name: "Forecast", exact: true }).click();
  const draws = page.getByLabel("Draws");
  await expect(draws).toBeFocused();
  await expect(draws).toHaveAttribute("aria-invalid", "true");
  await expect(draws).toHaveAccessibleDescription(/Draws must be between 0 and 10000\..*0 returns summaries only\./);
  await expect(page.getByLabel("Variables (optional)")).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByRole("alert").filter({ hasText: "Draws must be between" })).toBeVisible();
  await expect(page.getByLabel("Seed (optional)")).not.toHaveAttribute("aria-invalid");
});

test("theme can be switched to dark", async ({ page }) => {
  await page.goto("/settings");
  await page.getByRole("radio", { name: "Dark" }).click();
  await expect(page.locator("html")).toHaveClass(/dark/);
});

test.describe("phone layout (375 px)", () => {
  test.use({ viewport: { width: 375, height: 800 } });
  const DATASET = "ds_01jaf3a1b2c3d4e5f6g7h8j9k0";

  const pages: [string, string][] = [
    ["/", "1 run in flight"],
    [`/models/${MODEL}/versions/1`, "Diagnostics"],
    [`/datasets/${DATASET}`, "Versions"],
    [`/runs/${RUNNING}`, "attempt 1"],
    [`/score?model=${MODEL}&version=1`, "What this model needs"],
  ];
  for (const [path, marker] of pages) {
    test(`${path} does not scroll horizontally`, async ({ page }) => {
      await page.goto(path);
      await expect(page.getByRole("main").getByText(marker).first()).toBeVisible();
      await page.waitForTimeout(500); // let lazy content lay out (SSE keeps the network busy)
      const { scroll, client } = await page.evaluate(() => ({
        scroll: document.documentElement.scrollWidth,
        client: document.documentElement.clientWidth,
      }));
      expect(scroll, `${path} is ${scroll}px wide in a ${client}px viewport`).toBeLessThanOrEqual(client);
    });
  }

  test("the compact nav shows every destination, Settings included", async ({ page }) => {
    await page.goto("/");
    const nav = page.getByRole("navigation", { name: "Primary (compact)" });
    for (const label of ["Home", "Datasets", "Build", "Runs", "Models", "Score", "Catalog", "Settings"]) {
      const link = nav.getByRole("link", { name: label, exact: true });
      await expect(link).toBeVisible();
      const box = await link.boundingBox();
      expect(box, label).not.toBeNull();
      expect(box!.x, label).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width, label).toBeLessThanOrEqual(375);
    }
  });
});
