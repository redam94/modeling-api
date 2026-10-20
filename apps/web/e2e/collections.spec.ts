/**
 * v0.15 smoke tests of image collections and deep learning against the MSW-mocked API
 * (`src/mocks/collections.ts`, `src/mocks/dl.ts`) and the real plot sandbox:
 *
 * - the upload wizard step "Folder of images": a folder of 6 tiny PNGs (built by the test in
 *   a temporary directory) is hashed, packed into a zip part and uploaded; the new version
 *   shows its collection summary (images, new and reused, classes);
 * - the version page of a collection;
 * - the run page of a `dl.cnn_classifier` fit on the GPU class: the plots of the training
 *   (training curves, confusion over epochs, wrong images) and the media of the validation.
 *
 * Every test ends without a CSP violation.
 */
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { deflateSync } from "node:zlib";

import { expect, test, type FrameLocator, type Locator } from "@playwright/test";

const DATASET = "ds_01jaf3a1b2c3d4e5f6g7h8jc01";
const DL_RUN = "run_01jaf3q2x8m9v7k5h4g3f2e1k8";

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

// ------------------------------------------------------------------------- a tiny PNG writer

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});

function crc32(buf: Buffer): number {
  let c = 0xffffffff;
  for (const b of buf) c = CRC_TABLE[(c ^ b) & 0xff]! ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type: string, data: Buffer): Buffer {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

/** An 8 x 8 RGB PNG: a circle or a square in a colour that depends on `seed`. */
function shapePng(shape: "circle" | "square", seed: number): Buffer {
  const size = 8;
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 2; // colour type RGB
  const rows: number[] = [];
  for (let y = 0; y < size; y++) {
    rows.push(0); // filter: none
    for (let x = 0; x < size; x++) {
      const on = shape === "circle" ? Math.hypot(x - 3.5, y - 3.5) < 3 : x > 0 && x < 7 && y > 0 && y < 7;
      rows.push(on ? 40 + seed * 30 : 240, on ? 80 : 240, on ? 200 - seed * 20 : 240);
    }
  }
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk("IHDR", ihdr), chunk("IDAT", deflateSync(Buffer.from(rows))), chunk("IEND", Buffer.alloc(0))]);
}

/** A temporary folder `shapes/` with 3 circles and 3 squares in class folders, and a note. */
function shapesFolder(): { root: string; folder: string } {
  const root = mkdtempSync(join(tmpdir(), "mapi-collection-"));
  const folder = join(root, "shapes");
  for (const shape of ["circle", "square"] as const) {
    mkdirSync(join(folder, shape), { recursive: true });
    for (let i = 0; i < 3; i++) writeFileSync(join(folder, shape, `${shape}_${i}.png`), shapePng(shape, i));
  }
  writeFileSync(join(folder, "README.txt"), "not an image");
  return { root, folder };
}

/** The sandbox of a plot frame, after it rendered its marks. */
async function rendered(frame: Locator): Promise<FrameLocator> {
  await frame.scrollIntoViewIfNeeded();
  const inner = frame.contentFrame();
  await expect(inner.locator("svg.marks, .main-svg").first()).toBeVisible({ timeout: 20_000 });
  return inner;
}

// ---------------------------------------------------------------------------------- tests

test("the version page of an image collection shows what it holds", async ({ page }) => {
  await page.goto(`/datasets/${DATASET}`);
  await expect(page.getByText("Image collection", { exact: true })).toBeVisible();
  const summary = page.getByTestId("collection-summary");
  await expect(summary).toContainText("60");
  await expect(summary).toContainText("pixels(image, y, x, channel)");
  for (const c of ["circle", "cross", "square"]) await expect(page.getByTestId("collection-classes").getByText(c, { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Splits" })).toContainText("train");
});

test("a folder of images uploads as a new collection version", async ({ page }) => {
  const { root, folder } = shapesFolder();
  try {
    await page.goto(`/datasets/${DATASET}?upload=true`);
    await page.getByRole("button", { name: "Choose a folder of images" }).click();
    const step = page.getByRole("region", { name: "Folder of images" });
    await step.getByTestId("collection-folder-input").setInputFiles(folder);
    await expect(step.getByTestId("collection-folder-summary")).toContainText("6 images, 1 other files (not uploaded)");
    await step.getByLabel("Version message (optional)").fill("Six shapes");
    await step.getByRole("button", { name: "Upload images" }).click();

    // the new version is selected and shows its collection
    await expect(page.getByRole("navigation", { name: "Versions" }).getByRole("button", { name: /v2/ })).toHaveAttribute("aria-current", "true", { timeout: 20_000 });
    const summary = page.getByTestId("collection-summary");
    await expect(summary).toContainText("6 new, 0 from version v1");
    const classes = page.getByTestId("collection-classes");
    await expect(classes.getByText("circle", { exact: true })).toBeVisible();
    await expect(classes.getByText("square", { exact: true })).toBeVisible();

    // the same folder again (no reload: the mock keeps v2): every image is in v2, so no part
    // is uploaded and nothing is new
    await page.getByRole("button", { name: "Upload new version" }).click();
    await page.getByRole("button", { name: "Choose a folder of images" }).click();
    await page.getByTestId("collection-folder-input").setInputFiles(folder);
    await page.getByRole("button", { name: "Upload images" }).click();
    await expect(page.getByRole("navigation", { name: "Versions" }).getByRole("button", { name: /v3/ })).toHaveAttribute("aria-current", "true", { timeout: 20_000 });
    await expect(page.getByTestId("collection-summary")).toContainText("0 new, 6 from version v2");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("the run page of a deep-learning fit shows the training plots and the validation media", async ({ page }) => {
  await page.goto(`/runs/${DL_RUN}`);
  const card = page.getByRole("region", { name: "Run plots" });
  await expect(card.getByText("Training curves", { exact: true })).toBeVisible({ timeout: 20_000 });
  await expect(card.getByText("the model starts to overfit", { exact: false })).toBeVisible();
  await rendered(card.locator('iframe[title="Training curves"]'));
  await rendered(card.locator('iframe[title="Confusion matrix over epochs"]'));
  // the wrong images are Plotly image traces with inline pixels
  await rendered(card.locator('iframe[title="Wrong validation images"]'));

  await page.getByRole("tab", { name: "Media" }).click();
  await expect(page.getByText("val_predictions").first()).toBeVisible();
  await expect(page.getByText("confusion").first()).toBeVisible();
});
