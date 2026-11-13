/**
 * The documentation group of the global search (v0.21). The index is made at build time from
 * the docs (`scripts/gen_docs_search_index.py`) and loads as its own lazy chunk: the app's CSP
 * does not let it fetch the docs site's search index.
 */
import { compareRanked, matchText, type Range, type Ranked } from "./rank";

/** One page of the docs: title, section tab, site path (`""` or it ends in `/`), H2 headings `[text, anchor]`. */
export interface DocsPage {
  t: string;
  s: string;
  p: string;
  h: [heading: string, anchor: string][];
}

export interface DocsHit {
  title: string;
  /** The section tab, and the page for a heading. */
  secondary: string;
  /** The path in the docs site, with the anchor of a heading. */
  path: string;
  ranges: Range[];
}

let cache: Promise<DocsPage[]> | null = null;

/** The index, loaded one time for each page load. */
export function loadDocsIndex(): Promise<DocsPage[]> {
  cache ??= import("./docs-index.gen.json").then((m) => m.default as unknown as DocsPage[]);
  return cache;
}

interface DocRow {
  title: string;
  secondary: string;
  path: string;
  /** Pages go before headings when the score is equal. */
  heading: boolean;
}

/** The pages and headings whose title matches `query`, best first, pages before headings. */
export function searchDocs(index: readonly DocsPage[], query: string, limit = 5): DocsHit[] {
  const hits: Ranked<DocRow>[] = [];
  const add = (row: DocRow) => {
    const m = matchText(query, row.title, undefined, false);
    if (m) hits.push({ row, score: m.score, ranges: m.ranges });
  };
  for (const page of index) {
    add({ title: page.t, secondary: page.s, path: page.p, heading: false });
    for (const [text, anchor] of page.h) add({ title: text, secondary: `${page.s} · ${page.t}`, path: `${page.p}#${anchor}`, heading: true });
  }
  hits.sort((a, b) => (a.score !== b.score ? b.score - a.score : a.row.heading !== b.row.heading ? Number(a.row.heading) - Number(b.row.heading) : compareRanked(a, b)));
  return hits.slice(0, limit).map((h) => ({ title: h.row.title, secondary: h.row.secondary, path: h.row.path, ranges: h.ranges }));
}

/** The URL of a docs page. `base` is `env.docsUrl`, which ends in `/`. */
export function docsHref(base: string, path: string): string {
  return `${base}${path}`;
}

/** The docs site with its search open on `query` (MkDocs Material reads `?q=`). */
export function docsSearchHref(base: string, query: string): string {
  return `${base}?q=${encodeURIComponent(query)}`;
}
