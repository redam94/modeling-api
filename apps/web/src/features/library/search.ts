/**
 * The search params of "Make your own" (`/library`): which tab, which item is open, whether a
 * new item is being made, where a new chart template starts from, and the text of the search
 * box. Tiny (its one import has no imports), so the router can validate them without loading the
 * page.
 *
 * - `tab`: `models` (default) · `prep` · `charts` · `workflows`
 * - `item`: `lib_<ulid>` opens that item in its builder (a new revision on save)
 * - `new`: `model` · `preset` · `prep` · `chart` starts a new item
 * - `source`: a plot recipe id (`core.fit_vs_actual`) for a new chart template ("Make a chart
 *   template from this" on a plot card)
 * - `base`: a catalog model id for a new preset ("Save as preset" elsewhere)
 * - `q`: the text of the search box of the lists (v0.21)
 * - `archived`: the "Show archived" chip of the lists is on (v0.21)
 */
import { queryParam } from "@/lib/list-query";

export const LIBRARY_TABS = ["models", "prep", "charts", "workflows"] as const;
export type LibraryTab = (typeof LIBRARY_TABS)[number];

export const LIBRARY_NEW = ["model", "preset", "prep", "chart"] as const;
export type LibraryNew = (typeof LIBRARY_NEW)[number];

export interface LibrarySearch {
  tab?: LibraryTab | undefined;
  item?: string | undefined;
  new?: LibraryNew | undefined;
  source?: string | undefined;
  base?: string | undefined;
  q?: string | undefined;
  archived?: boolean | undefined;
}

const ID_CHARS = "0123456789abcdefghijklmnopqrstuvwxyz";

function libraryId(v: unknown): string | undefined {
  if (typeof v !== "string" || v.length !== 30 || !v.startsWith("lib_")) return undefined;
  for (const c of v.slice(4)) if (!ID_CHARS.includes(c)) return undefined;
  return v;
}

/** A catalog id (`core.fit_vs_actual`, `ts.sarimax`): lower-case segments joined by dots. */
function catalogId(v: unknown): string | undefined {
  if (typeof v !== "string" || !v || v.length > 128) return undefined;
  const parts = v.split(".");
  if (parts.length < 2) return undefined;
  for (const [i, p] of parts.entries()) {
    if (!p) return undefined;
    for (const [j, c] of [...p].entries()) {
      const ok = (c >= "a" && c <= "z") || (c >= "0" && c <= "9") || c === "_";
      if (!ok || (i === 0 && j === 0 && !(c >= "a" && c <= "z"))) return undefined;
    }
  }
  return v;
}

function oneOf<T extends string>(v: unknown, allowed: readonly T[]): T | undefined {
  return typeof v === "string" && (allowed as readonly string[]).includes(v) ? (v as T) : undefined;
}

export function validateLibrarySearch(s: Record<string, unknown>): LibrarySearch {
  return {
    tab: oneOf(s.tab, LIBRARY_TABS),
    item: libraryId(s.item),
    new: oneOf(s.new, LIBRARY_NEW),
    source: catalogId(s.source),
    base: catalogId(s.base),
    q: queryParam(s.q),
    archived: s.archived === true || s.archived === "true" ? true : undefined,
  };
}

/** The tab a kind lives on. */
export const TAB_OF_KIND = {
  model_recipe: "models",
  prep_preset: "prep",
  chart_template: "charts",
  workflow_template: "workflows",
} as const satisfies Record<string, LibraryTab>;

/** The URL of a Make your own view (for links outside the router's typed routes). */
export function libraryHref(search: LibrarySearch = {}): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(search)) if (typeof v === "string" && v) q.set(k, v);
  const s = q.toString();
  return s ? `/library?${s}` : "/library";
}
