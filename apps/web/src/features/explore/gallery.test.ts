/**
 * The gallery's pure helpers (v0.14): facet filters (with the `(other)` bucket), paging, the
 * shown counts, the sample note and the thumbnail URL.
 */
import { describe, expect, it } from "vitest";

import { facetLevels, filterItems, galleryNote, GALLERY_PAGE_SIZE, itemLevel, metaText, OTHER_LEVEL, pageCount, pageItems, shownCounts, thumbSrc } from "./gallery";
import type { GalleryFacet, GalleryItem, GalleryView } from "./views";

const PNG = "iVBORw0KGgo=";

function view(n: number, over: Partial<GalleryView> = {}): GalleryView {
  const labels = ["cat", "dog", "bird"];
  const items: GalleryItem[] = Array.from({ length: n }, (_, i) => ({
    index: i,
    label: `img_${i}`,
    meta: { label: labels[i % 3]!, split: i % 4 === 0 ? "test" : "train", site: `s${i % 70}` },
    png: PNG,
  }));
  const facets: GalleryFacet[] = [
    { name: "label", source: "var", counts: { cat: 40, dog: 40, bird: 40 } },
    { name: "split", source: "coord", counts: { train: 90, test: 30 } },
    { name: "site", source: "coord", counts: { s0: 2, s1: 2, [OTHER_LEVEL]: 116 } },
  ];
  return {
    format: "mapi.data-explorer.gallery/v1",
    var: "pixels",
    frame_dim: "image",
    y_dim: "y",
    x_dim: "x",
    channel_dim: "channel",
    color: "gray",
    source_height: 16,
    source_width: 16,
    thumb_height: 16,
    thumb_width: 16,
    n_images: 120,
    items,
    facets,
    selection: "stratified",
    ...over,
  };
}

describe("gallery filters", () => {
  it("filters by one or more facet levels and ignores unset facets", () => {
    const v = view(120);
    expect(filterItems(v, {})).toHaveLength(120);
    expect(filterItems(v, { label: null })).toHaveLength(120);
    const cats = filterItems(v, { label: "cat" });
    expect(cats).toHaveLength(40);
    expect(cats.every((i) => i.meta?.label === "cat")).toBe(true);
    expect(filterItems(v, { label: "cat", split: "test" }).every((i) => i.index % 12 === 0)).toBe(true);
    expect(filterItems(v, { label: "fish" })).toHaveLength(0);
  });

  it("puts values past the cap in the (other) level, last in the chips", () => {
    const v = view(120);
    const site = v.facets[2]!;
    expect(itemLevel(v.items[1]!, site)).toBe("s1");
    expect(itemLevel(v.items[5]!, site)).toBe(OTHER_LEVEL);
    expect(filterItems(v, { site: OTHER_LEVEL })).toHaveLength(120 - 4);
    expect(facetLevels(site).map((l) => l.level)).toEqual(["s0", "s1", OTHER_LEVEL]);
    // a facet without an (other) bucket keeps the value as it is
    expect(itemLevel({ index: 0, png: PNG, meta: {} }, v.facets[0]!)).toBe("");
  });

  it("counts the shown items of each level under the other facets' filters", () => {
    const v = view(120);
    const counts = shownCounts(v, { split: "test", label: "cat" }, v.facets[0]!);
    expect(counts).toEqual({ cat: 10, dog: 10, bird: 10 });
  });
});

describe("gallery paging", () => {
  it("cuts the items into pages of 48 and clamps the page", () => {
    const items = Array.from({ length: 100 }, (_, i) => i);
    expect(GALLERY_PAGE_SIZE).toBe(48);
    expect(pageCount(100)).toBe(3);
    expect(pageCount(0)).toBe(1);
    expect(pageItems(items, 0)).toHaveLength(48);
    expect(pageItems(items, 2)).toEqual([96, 97, 98, 99]);
    expect(pageItems(items, 9)).toEqual([96, 97, 98, 99]);
    expect(pageItems(items, -1)[0]).toBe(0);
  });
});

describe("gallery texts", () => {
  it("says which images it shows", () => {
    expect(galleryNote(view(120, { selection: "all" }))).toBe("Shows all 120 images.");
    expect(galleryNote(view(100, { n_images: 5000 }))).toBe("Shows 100 of 5,000 images: an even sample of each label and split.");
    expect(galleryNote(view(100, { n_images: 5000, selection: "even", facets: [] }))).toBe("Shows 100 of 5,000 images: an even sample.");
  });

  it("makes a data URL of base64 text only, and shows absent values as a dash", () => {
    expect(thumbSrc({ png: PNG })).toBe(`data:image/png;base64,${PNG}`);
    expect(thumbSrc({ png: "javascript:alert(1)" })).toBeNull();
    expect(thumbSrc({ png: "" })).toBeNull();
    expect(metaText(null)).toBe("—");
    expect(metaText(3)).toBe("3");
  });
});
