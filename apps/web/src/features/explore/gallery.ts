/**
 * Pure helpers of the gallery section (`mapi_spec.views.GalleryView`, v0.14): facet filters,
 * paging and the counts of the thumbnails. No React and no DOM here. Every label and facet
 * value comes from the user's data: render it as text only.
 */
import type { GalleryFacet, GalleryItem, GalleryView } from "./views";

/** Thumbnails on one page of the gallery. */
export const GALLERY_PAGE_SIZE = 48;
/** The bucket of the levels past the runner's cap (`MAX_GALLERY_LEVELS`). */
export const OTHER_LEVEL = "(other)";
/** Thumbnails smaller than this (pixels a side) are drawn with sharp pixels when scaled up. */
export const PIXELATED_BELOW = 32;

/** The chosen level of each facet (`null` or absent: all levels). */
export type GalleryFilters = Readonly<Record<string, string | null | undefined>>;

const B64_RE = /^[A-Za-z0-9+/]+={0,2}$/;

/** The level of an item in a facet: its value as text, `(other)` when the facet counts it there. */
export function itemLevel(item: GalleryItem, facet: GalleryFacet): string {
  const v = item.meta?.[facet.name];
  const text = v === null || v === undefined ? "" : String(v);
  if (text in facet.counts) return text;
  return OTHER_LEVEL in facet.counts ? OTHER_LEVEL : text;
}

/** The items that match every chosen facet level. */
export function filterItems(view: Pick<GalleryView, "items" | "facets">, filters: GalleryFilters): GalleryItem[] {
  const active = view.facets.filter((f) => filters[f.name] != null);
  if (!active.length) return view.items;
  return view.items.filter((item) => active.every((f) => itemLevel(item, f) === filters[f.name]));
}

/** The number of pages of `n` items (at least 1). */
export function pageCount(n: number, size = GALLERY_PAGE_SIZE): number {
  return Math.max(1, Math.ceil(n / size));
}

/** The items of page `page` (0-based; clamped to the last page). */
export function pageItems<T>(items: readonly T[], page: number, size = GALLERY_PAGE_SIZE): T[] {
  const last = pageCount(items.length, size) - 1;
  const p = Math.max(0, Math.min(page, last));
  return items.slice(p * size, (p + 1) * size);
}

/** The levels of a facet in the order of the view (`(other)` last), with the image counts. */
export function facetLevels(facet: GalleryFacet): { level: string; count: number }[] {
  const out = Object.entries(facet.counts).map(([level, count]) => ({ level, count }));
  return [...out.filter((x) => x.level !== OTHER_LEVEL), ...out.filter((x) => x.level === OTHER_LEVEL)];
}

/** The number of shown thumbnails of each level of a facet (after the other facets' filters). */
export function shownCounts(view: Pick<GalleryView, "items" | "facets">, filters: GalleryFilters, facet: GalleryFacet): Record<string, number> {
  const others = { ...filters, [facet.name]: null };
  const out: Record<string, number> = {};
  for (const item of filterItems(view, others)) {
    const level = itemLevel(item, facet);
    out[level] = (out[level] ?? 0) + 1;
  }
  return out;
}

/** What the gallery shows, in plain words (the sample rule when it does not show every image). */
export function galleryNote(view: Pick<GalleryView, "items" | "n_images" | "selection" | "facets">): string {
  const items = view.items.length;
  if (view.selection === "all") return `Shows all ${items.toLocaleString("en-US")} images.`;
  const shown = `Shows ${items.toLocaleString("en-US")} of ${view.n_images.toLocaleString("en-US")} images`;
  if (view.selection === "even") return `${shown}: an even sample.`;
  const names = view.facets.map((f) => f.name);
  const by = names.includes("split") && names[0] !== "split" ? `${names[0]} and split` : (names[0] ?? "label");
  return `${shown}: an even sample of each ${by}.`;
}

/** A `data:` URL of a thumbnail, only for base64 text (anything else gives `null`). */
export function thumbSrc(item: Pick<GalleryItem, "png">): string | null {
  return typeof item.png === "string" && item.png.length > 0 && B64_RE.test(item.png) ? `data:image/png;base64,${item.png}` : null;
}

/** The text of a label or a facet value ("—" when absent). */
export function metaText(v: string | number | boolean | null | undefined): string {
  return v === null || v === undefined || v === "" ? "—" : String(v);
}
