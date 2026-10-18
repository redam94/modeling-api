/**
 * M4 smoke tests against the MSW-mocked API (`src/mocks/tree.ts`, fixtures computed by the
 * real runner in `scripts/gen_tree_fixtures.py`): several files in one version (stacked
 * along a dim, plus a node), a raw HDF5 upload inspected and mapped, a refused unsafe file,
 * a tree version with its nodes and the Data Explorer per node, and `data.nodes` hints in the
 * spec editor. Every test ends without a CSP violation.
 */
import { expect, test, type Page, type Request } from "@playwright/test";

const DATASET = "ds_01jaf3a1b2c3d4e5f6g7h8j9k0";
const TREE = "ds_01jaf3a1b2c3d4e5f6g7h8tree";

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

const file = (name: string, bytes = 64) => ({ name, mimeType: "application/octet-stream", buffer: Buffer.alloc(bytes, 7) });

function versionPosts(page: Page): Request[] {
  const posts: Request[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && new RegExp(`/v1/datasets/${DATASET}/versions$`).test(r.url())) posts.push(r);
  });
  return posts;
}

test("two yearly files stack along a dim and a third file becomes a node of the tree", async ({ page }) => {
  const posts = versionPosts(page);
  await page.goto(`/datasets/${DATASET}?upload=true`);
  await page.locator('input[type="file"]').setInputFiles([file("sales_2024.nc"), file("sales_2025.nc")]);
  await expect(page.getByRole("region", { name: "File sales_2024.nc" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Format of sales_2025.nc" })).toHaveValue("netcdf");

  // Two files in one node need a combine rule: the problem shows and the upload waits.
  const problems = page.getByRole("list", { name: "Problems with the files" });
  await expect(problems).toContainText("Node / has 2 files (sales_2024.nc, sales_2025.nc)");
  const upload = page.getByRole("button", { name: "Upload and ingest" });
  await expect(upload).toBeDisabled();

  for (const [name, label] of [
    ["sales_2024.nc", "2024"],
    ["sales_2025.nc", "2024"],
  ] as const) {
    await page.getByRole("combobox", { name: `Combine rule for ${name}` }).selectOption("concat");
    await page.getByRole("textbox", { name: `Stack dim for ${name}` }).fill("year");
    await page.getByRole("textbox", { name: `Label of ${name}` }).fill(label);
  }
  await expect(problems).toContainText("Node /: two files have the label 2024");
  await page.getByRole("textbox", { name: "Label of sales_2025.nc" }).fill("2025");
  await expect(problems).toBeHidden();

  // A third file goes to its own node. Its own problem shows once, under its fields, linked to
  // the input and in a live region; the list of the files' shared problems does not repeat it.
  await page.locator('input[type="file"]').setInputFiles([file("instrument.nc")]);
  const nodeInput = page.getByRole("textbox", { name: "Node for instrument.nc" });
  await nodeInput.fill("instrument");
  const own = page.getByRole("list", { name: "Problems with the place of instrument.nc" });
  await expect(own).toContainText('instrument.nc: The node "instrument" must start with /');
  await expect(page.getByText('instrument.nc: The node "instrument" must start with /')).toHaveCount(1);
  await expect(problems).toBeHidden();
  await expect(nodeInput).toHaveAttribute("aria-invalid", "true");
  await expect(nodeInput).toHaveAccessibleDescription(/The node "instrument" must start with \//);
  await expect(own.locator("xpath=ancestor::*[@aria-live='polite']")).toHaveCount(1);
  await expect(upload).toHaveAccessibleDescription("Correct the problem above to upload.");
  await nodeInput.fill("/instrument");
  await expect(nodeInput).toHaveAttribute("aria-invalid", "false");
  const nodes = page.getByRole("list", { name: "Nodes of the version" });
  await expect(nodes).toContainText("/← stacked along year:");
  await expect(nodes).toContainText("sales_2025.nc = 2025");
  await expect(nodes).toContainText("/instrument");

  await expect(upload).toBeEnabled();
  await upload.click();
  await expect(page.getByText("The ingest of v2 started")).toBeVisible();
  expect(posts).toHaveLength(1);
  const body = posts[0]!.postDataJSON() as { upload_id?: string; assembly: { sources: Record<string, unknown>[] } };
  expect(body.upload_id).toBeUndefined();
  expect(body.assembly.sources).toEqual([
    expect.objectContaining({ mapping: { format: "netcdf" }, node: "/", combine: "concat", concat_dim: "year", concat_label: 2024, join: "exact" }),
    expect.objectContaining({ mapping: { format: "netcdf" }, node: "/", combine: "concat", concat_dim: "year", concat_label: 2025, join: "exact" }),
    expect.objectContaining({ mapping: { format: "netcdf" }, node: "/instrument", combine: "node" }),
  ]);
  expect(new Set(body.assembly.sources.map((s) => s.upload_id)).size).toBe(3);
});

test("a raw HDF5 upload is inspected, its arrays are mapped and it becomes a version", async ({ page }) => {
  const posts = versionPosts(page);
  const uploads: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && /\/uploads$/.test(r.url())) uploads.push(r.url());
  });
  await page.goto(`/datasets/${DATASET}?upload=true`);
  await page.locator('input[type="file"]').setInputFiles([file("lens_raw.h5", 2048)]);
  await expect(page.getByRole("combobox", { name: "Format of lens_raw.h5" })).toHaveValue("hdf5");
  const upload = page.getByRole("button", { name: "Upload and ingest" });
  await expect(upload).toBeDisabled();

  // The inspect run answers "not ready" first; the page keeps polling.
  await page.getByRole("button", { name: "Upload and list the arrays" }).click();
  const arrays = page.getByRole("table", { name: "Arrays of lens_raw.h5" });
  await expect(arrays).toBeVisible();
  await expect(arrays.getByRole("row", { name: /\/observations\/image/ })).toContainText("4 × 5");
  await expect(arrays.getByRole("row", { name: /\/observations\/image/ })).toContainText("deflate");
  await expect(arrays.getByRole("row", { name: /\/cube\/flux/ })).toContainText("(band, pixel)");
  const mappingProblems = page.getByRole("list", { name: "Problems with the mapping of lens_raw.h5" });
  await expect(mappingProblems).toContainText("Select one or more arrays to make variables.");

  await page.getByRole("checkbox", { name: "Map /observations/image" }).click();
  await page.getByRole("checkbox", { name: "Map /psf/kernel" }).click();
  const dimsInput = page.getByRole("textbox", { name: "Dims for /observations/image" });
  await dimsInput.fill("row");
  await expect(mappingProblems).toContainText("/observations/image has 2 axes (shape 4 × 5). Type 2 dim names, not 1.");
  // The problem marks the Dims input (not the variable name) and describes it.
  await expect(dimsInput).toHaveAttribute("aria-invalid", "true");
  await expect(dimsInput).toHaveAccessibleDescription(/has 2 axes/);
  await expect(page.getByRole("textbox", { name: "Variable name for /observations/image" })).toHaveAttribute("aria-invalid", "false");
  await expect(page.getByRole("textbox", { name: "Units for /observations/image" })).toHaveAttribute("maxlength", "64");
  await expect(page.getByRole("combobox", { name: "Missing values of /observations/image" })).toHaveValue("forbid");
  await page.getByRole("textbox", { name: "Dims for /observations/image" }).fill("row, col");
  await page.getByRole("combobox", { name: "Role for /observations/image" }).selectOption("target");
  await expect(page.getByRole("textbox", { name: "Units for /observations/image" })).toHaveValue("counts");
  await page.getByRole("textbox", { name: "Node for /psf/kernel" }).fill("/psf");
  await page.getByRole("textbox", { name: "Dims for /psf/kernel" }).fill("prow, pcol");

  // Coordinates: labels from a 1-D array, a numeric range, and indexes for the PSF.
  await page.getByRole("combobox", { name: "Labels of row" }).selectOption("path");
  await page.getByRole("combobox", { name: "Label array for row" }).selectOption("/axes/rows");
  await page.getByRole("combobox", { name: "Labels of col" }).selectOption("range");
  await page.getByRole("textbox", { name: "Range start for col" }).fill("0");
  await page.getByRole("textbox", { name: "Range step for col" }).fill("w");
  await expect(mappingProblems).toContainText("Coordinate col: a range is two numbers");
  await page.getByRole("textbox", { name: "Range step for col" }).fill("0.5");
  await expect(mappingProblems).toBeHidden();

  await upload.click();
  await expect(page.getByText("The ingest of v2 started")).toBeVisible();
  // The file was uploaded once (for the inspection) and the version reuses that upload.
  expect(uploads).toHaveLength(1);
  const body = posts[0]!.postDataJSON() as { upload_id: string; mapping: unknown; assembly?: unknown };
  expect(body.assembly).toBeUndefined();
  expect(body.mapping).toEqual({
    format: "hdf5",
    hdf5: {
      vars: [
        { path: "/observations/image", node: "/", name: "image", dims: ["row", "col"], role: "target", units: "counts" },
        { path: "/psf/kernel", node: "/psf", name: "kernel", dims: ["prow", "pcol"], role: "covariate" },
      ],
      coords: [
        { dim: "row", node: "/", path: "/axes/rows" },
        { dim: "col", node: "/", start: 0, step: 0.5 },
      ],
    },
  });
});

test("an unsupported file and files of one node with different rules say what is wrong", async ({ page }) => {
  await page.goto(`/datasets/${DATASET}?upload=true`);
  await page.locator('input[type="file"]').setInputFiles([file("sales_2024.nc"), file("sales_2025.nc")]);
  for (const [name, label] of [
    ["sales_2024.nc", "2024"],
    ["sales_2025.nc", "2025"],
  ] as const) {
    await page.getByRole("combobox", { name: `Combine rule for ${name}` }).selectOption("concat");
    await page.getByRole("textbox", { name: `Stack dim for ${name}` }).fill("year");
    await page.getByRole("textbox", { name: `Label of ${name}` }).fill(label);
  }
  await page.locator('input[type="file"]').setInputFiles([file("weird.bin")]);
  // The unknown type is a listed problem, linked to the format select.
  const format = page.getByRole("combobox", { name: "Format of weird.bin" });
  await expect(format).toHaveAttribute("aria-invalid", "true");
  await expect(format).toHaveAccessibleDescription(/weird\.bin: the app cannot tell the format of this file/);
  // weird.bin is in node / with the default rule: the summary does not list it as stacked.
  const nodes = page.getByRole("list", { name: "Nodes of the version" });
  await expect(nodes).toContainText("/← the files combine in different ways:");
  await expect(nodes).toContainText("sales_2024.nc = 2024 (stacked along year)");
  await expect(nodes).toContainText("weird.bin (the only file of the node)");
  await expect(page.getByRole("list", { name: "Problems with the files" })).toContainText("combine in different ways");
  const upload = page.getByRole("button", { name: "Upload and ingest" });
  await expect(upload).toBeDisabled();
  await expect(upload).toHaveAccessibleDescription("Correct the 2 problems above to upload.");

  await format.selectOption("netcdf");
  await page.getByRole("combobox", { name: "Combine rule for weird.bin" }).selectOption("concat");
  await page.getByRole("textbox", { name: "Stack dim for weird.bin" }).fill("year");
  await page.getByRole("textbox", { name: "Label of weird.bin" }).fill("2026");
  await expect(nodes).toContainText("/← stacked along year:");
  await expect(nodes).not.toContainText("(stacked along year)");
  await expect(upload).toBeEnabled();
});

test("several files fit a 375 px wide screen", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  await page.goto(`/datasets/${DATASET}?upload=true`);
  await page.locator('input[type="file"]').setInputFiles([file("sales_2024.nc"), file("sales_2025.nc")]);
  await page.getByRole("combobox", { name: "Combine rule for sales_2024.nc" }).selectOption("concat");
  await expect(page.getByRole("button", { name: "Preview structure" }).first()).toBeVisible();
  const width = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(width).toBeLessThanOrEqual(375);
  for (const name of ["File sales_2024.nc", "File sales_2025.nc"]) {
    const box = await page.getByRole("region", { name }).boundingBox();
    expect(box!.x + box!.width).toBeLessThanOrEqual(375);
  }
});

test("each file's inspect button is busy only while its own request is pending", async ({ page }) => {
  await page.goto(`/datasets/${DATASET}?upload=true`);
  await page.locator('input[type="file"]').setInputFiles([file("slow_a.h5"), file("b.h5")]);
  const a = page.getByRole("region", { name: "File slow_a.h5" }).getByRole("button", { name: /list the arrays/i });
  const b = page.getByRole("region", { name: "File b.h5" }).getByRole("button", { name: /list the arrays/i });
  await a.click();
  await expect(a).toBeDisabled();
  // Read at once (no retry): the request of slow_a.h5 takes 1.5 s, and b.h5 must not wait for it.
  expect(await b.isEnabled()).toBe(true);
  expect(await a.isDisabled()).toBe(true);
  await expect(page.getByRole("table", { name: "Arrays of slow_a.h5" })).toBeVisible();
});

test("the same dim in two sibling nodes gets two coordinates with different names", async ({ page }) => {
  await page.goto(`/datasets/${DATASET}?upload=true`);
  await page.locator('input[type="file"]').setInputFiles([file("lens_raw.h5", 2048)]);
  await page.getByRole("button", { name: "Upload and list the arrays" }).click();
  await page.getByRole("checkbox", { name: "Map /psf/kernel" }).click();
  await page.getByRole("checkbox", { name: "Map /cube/flux" }).click();
  await page.getByRole("textbox", { name: "Node for /psf/kernel" }).fill("/a");
  await page.getByRole("textbox", { name: "Dims for /psf/kernel" }).fill("x, y");
  await page.getByRole("textbox", { name: "Node for /cube/flux" }).fill("/b");
  await page.getByRole("textbox", { name: "Dims for /cube/flux" }).fill("x, z");
  await expect(page.getByRole("combobox", { name: "Labels of x (node /a)", exact: true })).toHaveCount(1);
  await expect(page.getByRole("combobox", { name: "Labels of x (node /b)", exact: true })).toHaveCount(1);
  // A node problem marks the Node input.
  await page.getByRole("textbox", { name: "Node for /cube/flux" }).fill("b");
  await expect(page.getByRole("textbox", { name: "Node for /cube/flux" })).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByRole("textbox", { name: "Variable name for /cube/flux" })).toHaveAttribute("aria-invalid", "false");
});

test("listing the arrays again after a failed inspect run shows the new run's arrays", async ({ page }) => {
  await page.goto(`/datasets/${DATASET}?upload=true`);
  await page.locator('input[type="file"]').setInputFiles([file("flaky.h5")]);
  await page.getByRole("button", { name: "Upload and list the arrays" }).click();
  const alert = page.getByRole("alert").filter({ hasText: "The sandbox could not list the arrays of flaky.h5" });
  await expect(alert).toBeVisible();
  await page.getByRole("button", { name: "List the arrays" }).click();
  await expect(page.getByRole("table", { name: "Arrays of flaky.h5" })).toBeVisible();
  await expect(alert).toBeHidden();
});

test("an unsafe HDF5 file shows the inspect run's problem and is not ingested", async ({ page }) => {
  await page.goto(`/datasets/${DATASET}?upload=true`);
  await page.locator('input[type="file"]').setInputFiles([file("unsafe_links.h5")]);
  await page.getByRole("button", { name: "Upload and list the arrays" }).click();
  const alert = page.getByRole("alert").filter({ hasText: "The sandbox could not list the arrays of unsafe_links.h5" });
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("The upload is not safe to read: the HDF5 file links to another file");
  await expect(page.getByRole("button", { name: "Upload and ingest" })).toBeDisabled();
  // The file is uploaded already: listing again does not upload it again.
  await expect(page.getByRole("button", { name: "List the arrays" })).toBeVisible();
});

test("netCDF files can preview their structure before the ingest", async ({ page }) => {
  await page.goto(`/datasets/${DATASET}?upload=true`);
  await page.locator('input[type="file"]').setInputFiles([file("cube.nc")]);
  await page.getByRole("button", { name: "Preview structure" }).click();
  const arrays = page.getByRole("table", { name: "Arrays of cube.nc" });
  await expect(arrays.getByRole("row", { name: /\/psf\/kernel/ })).toBeVisible();
  // A self-describing file maps nothing: there is no "map" column.
  await expect(arrays.getByRole("checkbox")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Upload and ingest" })).toBeEnabled();
});

test("a tree version shows its nodes and the Data Explorer explores one node at a time", async ({ page }) => {
  await page.goto(`/datasets/${TREE}`);
  await expect(page.getByRole("heading", { name: "lens_cubes" })).toBeVisible();
  const tree = page.getByRole("tree", { name: "Tree of nodes" });
  await expect(tree.getByRole("treeitem")).toHaveText([/^\/\s*2 vars$/, /^instrument\s*1 var$/, /^psf\s*1 var$/]);
  // The About card describes this tree, not another dataset.
  await expect(page.getByLabel("Highlights").getByText("3 nodes", { exact: true })).toBeVisible();
  await expect(page.getByText("156 weeks of history")).toHaveCount(0);

  // The tree keyboard: one tab stop, arrows move (and select), parents are expanded.
  const items = tree.getByRole("treeitem");
  await expect(items.nth(0)).toHaveAttribute("tabindex", "0");
  await expect(items.nth(1)).toHaveAttribute("tabindex", "-1");
  await expect(items.nth(0)).toHaveAttribute("aria-expanded", "true");
  await expect(items.nth(1)).toHaveAttribute("aria-expanded", "true");
  await expect(items.nth(2)).not.toHaveAttribute("aria-expanded");
  await items.nth(0).focus();
  await page.keyboard.press("ArrowDown");
  await expect(items.nth(1)).toBeFocused();
  await expect(page.getByRole("region", { name: "Node /instrument" })).toBeVisible();
  await page.keyboard.press("End");
  await expect(items.nth(2)).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await expect(items.nth(1)).toBeFocused();
  await page.keyboard.press("Home");
  await expect(items.nth(0)).toBeFocused();
  await expect(items.nth(0)).toHaveAttribute("tabindex", "0");
  await expect(items.nth(1)).toHaveAttribute("tabindex", "-1");
  const node = page.getByRole("region", { name: "Node /" });
  await expect(node.getByText("default node")).toBeVisible();
  await expect(node.getByRole("cell", { name: "sales", exact: true })).toBeVisible();
  // The default node's preview and coordinates (summary.json nodes).
  await expect(node.getByText(/sample rows/)).toBeVisible();
  await expect(node.getByText("2024", { exact: true }).first()).toBeVisible();

  await tree.getByRole("treeitem", { name: /psf/ }).click();
  const psf = page.getByRole("region", { name: "Node /instrument/psf" });
  await expect(psf.getByRole("cell", { name: "kernel", exact: true })).toBeVisible();
  await expect(psf.getByText("prow", { exact: true }).first()).toBeVisible();
  await expect(psf.getByText("The ingest previews the default node")).toBeVisible();
  await psf.getByRole("link", { name: "Explore this node" }).click();

  await expect(page).toHaveURL(/\/explore\?node=%2Finstrument%2Fpsf/);
  const picker = page.getByRole("combobox", { name: "Node" });
  await expect(picker).toHaveValue("/instrument/psf");
  await expect(picker.locator("option")).toHaveText(["/ (2 variables)", "/instrument (1 variable)", "/instrument/psf (1 variable)"]);
  const overview = page.locator("#section-overview");
  await expect(overview).toContainText("kernel");
  await expect(overview).not.toContainText("sales");

  await picker.selectOption("/");
  await expect(page).toHaveURL(/node=%2F(&|$)/);
  await expect(overview).toContainText("sales");
  await expect(overview).not.toContainText("kernel");
});

/** Put the cursor at the end of the first line containing `line`, minus `back` characters. */
async function cursorAfter(page: Page, line: string, back = 0) {
  const editor = page.getByTestId("yaml-editor");
  await editor.locator(".view-line", { hasText: line }).first().click();
  await page.keyboard.press("End");
  for (let i = 0; i < back; i++) await page.keyboard.press("Backspace");
}

test("the spec editor completes data.nodes from the tree dataset", async ({ page }) => {
  await page.goto("/build/new");
  const editor = page.getByTestId("yaml-editor");
  await expect(editor.locator(".view-lines")).toContainText("apiVersion: mapi/v1", { timeout: 20_000 });
  await page.getByRole("button", { name: "Insert" }).click();
  const search = page.getByRole("textbox", { name: "Search models, steps, datasets and columns" });
  await search.fill("lens");
  await page.locator('[data-insert="dataset:dataset://default/lens_cubes"]').click();
  await expect(editor.locator(".view-lines")).toContainText("dataset://default/lens_cubes@latest");

  // The Insert panel lists the nodes of the tree and adds one to data.nodes.
  await search.fill("kernel");
  await page.locator('[data-insert="node:/instrument/psf"]').click();
  await expect(editor.locator(".view-lines")).toContainText("nodes:    psf:      path: /instrument/psf");

  // Completions: the node paths of the tree for `path`.
  await cursorAfter(page, "path: /instrument/psf", "/instrument/psf".length);
  await page.keyboard.press("Control+Space");
  const suggest = page.locator(".suggest-widget");
  await expect(suggest).toContainText("/instrument/psf");
  await expect(suggest).toContainText("/instrument");
  await page.keyboard.press("Escape");
  await page.keyboard.type("/nope");

  // A node the dataset does not have is a static issue, like any other.
  await page.getByRole("tab", { name: /Issues/ }).click();
  await expect(page.getByRole("tabpanel").getByText("the dataset has no node '/nope'").first()).toBeVisible({ timeout: 10_000 });
});
