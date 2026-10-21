/**
 * The share link page (plan D13): `https://<app>/s#mpd_…`. The token rides in the fragment,
 * so it never reaches a server log or a `Referer`; the page reads it once, strips it from the
 * address bar and sends it to the server in one POST body.
 *
 * This module imports nothing at runtime but the token pattern, so the entry can check the path
 * without loading the share app.
 */
import { DASHBOARD_SHARE_TOKEN_RE } from "@/features/dashboards/types";

export const SHARE_PATH = "/s";
export type SharePath = typeof SHARE_PATH;

/** `"/s"` for the share page (exactly `/s`, a trailing slash allowed), else `null`. */
export function sharePathOf(pathname: string): SharePath | null {
  const p = pathname.replace(/\/+$/, "") || "/";
  return p === SHARE_PATH ? SHARE_PATH : null;
}

/**
 * The share token of a location fragment (`#mpd_…`), or `null` when the fragment is not
 * exactly one well-formed token (no other text, no query, no second token).
 */
export function tokenFromHash(hash: string): string | null {
  const raw = hash.startsWith("#") ? hash.slice(1) : hash;
  return DASHBOARD_SHARE_TOKEN_RE.test(raw) ? raw : null;
}

/** Drop the fragment (the token) from the address bar and the session history entry. */
export function stripFragment(win: Pick<Window, "location" | "history"> = window): void {
  if (!win.location.hash) return;
  const { pathname, search } = win.location;
  win.history.replaceState(win.history.state, "", `${pathname}${search}`);
}

/** Ask the browser to send no `Referer` from this page (set at runtime, beside the response header). */
export function applyNoReferrer(doc: Document = document): void {
  let meta = doc.head.querySelector<HTMLMetaElement>('meta[name="referrer"]');
  if (!meta) {
    meta = doc.createElement("meta");
    meta.name = "referrer";
    doc.head.prepend(meta);
  }
  meta.content = "no-referrer";
}
