/**
 * v0.15 image collections on the dataset page: the version view (badge, the Collection tab with
 * counts, classes and splits) and the wizard step "Folder of images" against the mock API (hash,
 * missing images, manifest and part uploads, version create with `collection`).
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { COLLECTION_DS_ID } from "@/mocks/collections";
import { createHandlers } from "@/mocks/handlers";
import { renderApp } from "@/test/render-app";

import { CollectionSummary } from "./version-views";

const T = { timeout: 8000 };
const enc = new TextEncoder();

const server = setupServer(...createHandlers());
beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function picked(path: string, content: string): File {
  const file = new File([enc.encode(content)], path.split("/").at(-1)!, { type: "image/png" });
  Object.defineProperty(file, "webkitRelativePath", { value: path });
  return file;
}

describe("the collection version view", () => {
  it("shows the badge and what the collection holds", async () => {
    renderApp(`/datasets/${COLLECTION_DS_ID}`);
    expect(await screen.findByText("Image collection", undefined, T)).toBeInTheDocument();
    const summary = await screen.findByTestId("collection-summary", undefined, T);
    expect(summary).toHaveTextContent("60");
    expect(summary).toHaveTextContent("pixels(image, y, x, channel)");
    expect(summary).toHaveTextContent("16 × 16 pixels");
    const classes = within(screen.getByTestId("collection-classes"));
    for (const c of ["circle", "cross", "square"]) expect(classes.getByText(c)).toBeInTheDocument();
    const splits = within(screen.getByRole("region", { name: "Splits" }));
    expect(splits.getByText("train")).toBeInTheDocument();
    expect(splits.getByText("test")).toBeInTheDocument();
  });

  it("lists the 10 largest classes and counts the others", () => {
    const classes = Array.from({ length: 12 }, (_, i) => ({ label: `c${i}`, count: 30 - i }));
    const version = { dataset_id: "ds", version: 3, layout: "collection", collection: { n_images: 300, label: true, packs: [], classes, other_classes: 4, splits: {}, new_images: 10, reused_images: 290, base_version: 2, min_size: [8, 8], max_size: [32, 40], size: null, mode: "gray", formats: { png: 300 }, total_bytes: 2048 } } as never;
    const { container } = render(<CollectionSummary version={version} />);
    expect(container).toHaveTextContent("10 new, 290 from version v2");
    expect(container).toHaveTextContent("8 × 8 to 32 × 40 pixels");
    expect(container).toHaveTextContent("6 other classes are not in the list.");
    expect(container).toHaveTextContent("gray (1 channel)");
    expect(within(container).getAllByRole("listitem")).toHaveLength(10);
  });
});

describe("the wizard step Folder of images", () => {
  it("uploads the new images in parts and creates the collection version", { timeout: 30_000 }, async () => {
    const bodies: { collection?: { manifest_upload_id: string; part_upload_ids: string[]; base_version: number | null; options: Record<string, unknown> } }[] = [];
    const puts: string[] = [];
    server.events.on("request:start", async ({ request }) => {
      if (request.method === "POST" && request.url.endsWith(`/v1/datasets/${COLLECTION_DS_ID}/versions`)) bodies.push((await request.clone().json()) as (typeof bodies)[number]);
      if (request.method === "PUT") puts.push(request.url);
    });
    const { container } = renderApp(`/datasets/${COLLECTION_DS_ID}?upload=true`);
    await userEvent.click(await screen.findByRole("button", { name: "Choose a folder of images" }, T));
    const input = container.querySelector<HTMLInputElement>('[data-testid="collection-folder-input"]')!;
    expect(input).toHaveAttribute("webkitdirectory");
    await userEvent.upload(input, [picked("shapes/circle/a.png", "circle a"), picked("shapes/circle/b.png", "circle b"), picked("shapes/square/c.png", "square c"), picked("shapes/notes.txt", "x")]);
    expect(screen.getByTestId("collection-folder-summary")).toHaveTextContent("shapes 3 images, 1 other files (not uploaded)");

    // a height without a width blocks the upload
    await userEvent.type(screen.getByLabelText("Height (pixels)"), "32");
    expect(await screen.findByText("Type the height and the width, or clear both.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload images" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Width (pixels)"), "32");
    await userEvent.type(screen.getByLabelText("Version message (optional)"), "May photos");

    await userEvent.click(screen.getByRole("button", { name: "Upload images" }));
    await waitFor(() => expect(bodies).toHaveLength(1), T);
    const c = bodies[0]!.collection!;
    expect(c.part_upload_ids).toHaveLength(1);
    expect(c.base_version).toBe(1); // the version the images were compared with, pinned
    expect(c.options).toMatchObject({ label_column: "label", path_column: "path", sha256_column: "sha256", size: [32, 32], resize: "stretch", mode: "auto" });
    expect(puts).toHaveLength(2); // the manifest and one part
    // the new version is selected and shows its collection (jsdom sends no Blob bytes through
    // XHR, so the mock cannot count the manifest rows here; the Playwright test does)
    const versions = within(await screen.findByRole("navigation", { name: "Versions" }, T));
    await waitFor(() => expect(versions.getByRole("button", { name: /v2/ })).toHaveAttribute("aria-current", "true"), T);
    expect(await screen.findByTestId("collection-summary", undefined, T)).toHaveTextContent("from version v1");
  });
});
