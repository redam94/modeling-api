/**
 * The `?q=` of a list page (v0.21): the text of the page's search box. Import-free, so the
 * router can validate it without loading a page.
 */

/** Characters of a query in the URL, in the search box of a list page and in the global search. */
export const MAX_QUERY_CHARS = 200;
/** Characters the API takes in the `name` filter of `/v1/runs` and `/v1/experiments`. */
export const MAX_NAME_FILTER_CHARS = 200;
/** Characters the API takes in the `q` of `/v1/library`. */
export const MAX_LIBRARY_QUERY_CHARS = 120;

/**
 * The query of a list page from its URL search, or `undefined` (the key is then left out of
 * the URL). The router reads `?q=2025` as a number and `?q=true` as a boolean: their text is
 * the query.
 */
export function queryParam(v: unknown): string | undefined {
  const text = typeof v === "string" ? v.trim() : typeof v === "number" || typeof v === "boolean" ? String(v) : "";
  return text && text.length <= MAX_QUERY_CHARS ? text : undefined;
}
