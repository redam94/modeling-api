/**
 * v0.14 smoke tests of image data against the MSW-mocked API (`src/mocks/explore-sci-demo.ts`:
 * `ds-sci-demo-images`, an image folder of 60 colour pictures of three shapes): the gallery
 * (thumbnails, pages, filters by label and split, the larger view) and the images section
 * drawing an RGB canvas. Every test ends without a CSP violation (thumbnails are `data:` PNGs).
 */
import { expect, test, type Page } from "@playwright/test";

const IMAGES = "ds-sci-demo-images";

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

/** The thumbnails that loaded as images (a decoded PNG has a natural width). */
async function loadedThumbs(page: Page): Promise<number> {
  return page.getByTestId("gallery-thumb").locator("img").evaluateAll((imgs) => imgs.filter((i) => (i as HTMLImageElement).complete && (i as HTMLImageElement).naturalWidth === 16).length);
}

test("the gallery shows the thumbnails, pages through them and filters by label and split", async ({ page }) => {
  await page.goto(`/datasets/${IMAGES}/versions/1/explore?section=gallery`);
  const card = page.locator("#section-gallery");
  await expect(card.getByTestId("gallery-thumb").first()).toBeVisible({ timeout: 20_000 });
  await expect(card.getByTestId("gallery-note")).toHaveText("Shows all 60 images.");
  await expect(card.getByTestId("gallery-count")).toHaveText("60 thumbnails.");
  // 48 on the first page, the rest on the second
  await expect(card.getByTestId("gallery-thumb")).toHaveCount(48);
  await expect.poll(() => loadedThumbs(page)).toBe(48);
  await card.getByRole("button", { name: "Next" }).click();
  await expect(card.getByText("Page 2 of 2")).toBeVisible();
  await expect(card.getByTestId("gallery-thumb")).toHaveCount(12);

  // a label filter changes the count (and goes back to the first page)
  await card.getByRole("group", { name: "label" }).getByRole("button", { name: /^square/ }).click();
  await expect(card.getByTestId("gallery-count")).toHaveText("20 of 60 thumbnails match the filters.");
  await expect(card.getByTestId("gallery-thumb")).toHaveCount(20);
  await card.getByRole("group", { name: "split" }).getByRole("button", { name: /^test/ }).click();
  await expect(card.getByTestId("gallery-count")).toHaveText("5 of 60 thumbnails match the filters.");

  // a click shows the image larger with its values (text only)
  await card.getByTestId("gallery-thumb").first().click();
  const detail = card.getByTestId("gallery-detail");
  await expect(detail).toContainText("square");
  await expect(detail).toContainText("test");
  await expect(detail).toContainText("16 x 16 pixels");

  await card.getByRole("group", { name: "label" }).getByRole("button", { name: "All" }).click();
  await card.getByRole("group", { name: "split" }).getByRole("button", { name: "All" }).click();
  await expect(card.getByTestId("gallery-count")).toHaveText("60 thumbnails.");
});

test("the images section draws the colour pictures on a canvas", async ({ page }) => {
  await page.goto(`/datasets/${IMAGES}/versions/1/explore?section=images`);
  const card = page.locator("#section-images");
  const canvas = card.getByTestId("field-viewer").locator('canvas[data-color="rgb"]');
  await expect(canvas).toBeVisible({ timeout: 20_000 });
  await expect(card.getByTestId("rgb-note")).toHaveText("Red, green and blue from channel (0 to 255).");
  // the pixels have colour: many distinct (r, g, b), and red, green or blue dominate somewhere
  const stats = async () =>
    canvas.evaluate((el) => {
      const c = el as HTMLCanvasElement;
      const data = c.getContext("2d")!.getImageData(0, 0, c.width, c.height).data;
      const colours = new Set<string>();
      let saturated = 0;
      for (let i = 0; i < data.length; i += 4) {
        colours.add(`${data[i]},${data[i + 1]},${data[i + 2]}`);
        if (Math.max(data[i]!, data[i + 1]!, data[i + 2]!) - Math.min(data[i]!, data[i + 1]!, data[i + 2]!) > 80) saturated += 1;
      }
      return { distinct: colours.size, saturated, opaque: data[3] };
    });
  await expect.poll(async () => (await stats()).distinct).toBeGreaterThan(20);
  const s = await stats();
  expect(s.saturated).toBeGreaterThan(10);
  expect(s.opaque).toBe(255);
  // the class means: one colour picture per label
  await card.getByRole("button", { name: "Mean of each label" }).click();
  const slider = card.getByRole("slider", { name: /label/ });
  await expect(slider).toHaveAttribute("aria-valuetext", "label circle");
  await expect.poll(async () => (await stats()).distinct).toBeGreaterThan(5);
});

test("the upload wizard offers an image folder for a zip", async ({ page }) => {
  await page.goto("/datasets/ds_01jaf3a1b2c3d4e5f6g7h8j9k0?upload=true");
  const input = page.locator('input[type="file"]');
  await input.setInputFiles({ name: "shapes.zip", mimeType: "application/zip", buffer: Buffer.from("PK\u0003\u0004") });
  await expect(page.getByLabel("Format of shapes.zip")).toHaveValue("image_folder");
  const form = page.getByTestId("image-folder-form");
  await expect(form).toContainText("Images and manifest");
  await page.getByLabel("Height (pixels) for shapes.zip").fill("32");
  await expect(page.getByText(/Type the height and the width/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Upload and ingest" })).toBeDisabled();
  await page.getByLabel("Width (pixels) for shapes.zip").fill("32");
  await expect(page.getByRole("button", { name: "Upload and ingest" })).toBeEnabled();
});
