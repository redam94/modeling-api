/**
 * Runtime configuration from Vite env (`VITE_*`, baked in at build time).
 *
 * - `VITE_API_BASE`: control-plane base URL (default `http://127.0.0.1:8000`, the M1 port).
 * - `VITE_PLOTS_ORIGIN`: origin of apps/plot-sandbox (default `http://127.0.0.1:5174`).
 *   It must be a different origin from the app itself (plan §10.3).
 * - `VITE_AUTH_BASE`: where the backend-for-frontend `/auth/*` routes live (default: the API
 *   base). In mock mode sign-in goes to the app's own development identity provider page.
 * - `VITE_TERMS_URL`, `VITE_ACCEPTABLE_USE_URL`: the documents sign-up asks people to accept
 *   (default: the summaries the app ships at `/terms` and `/acceptable-use`). An `http(s)` URL
 *   or a path on the app's own origin.
 */

const ORIGIN_RE = /^https?:\/\/[A-Za-z0-9.-]+(?::\d{1,5})?$|^https?:\/\/\[[0-9a-fA-F:]+\](?::\d{1,5})?$/;

export function normaliseBase(value: string | undefined, fallback: string): string {
  const raw = (value ?? "").trim() || fallback;
  const url = new URL(raw);
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`API base must be http(s): ${raw}`);
  }
  return raw.replace(/\/+$/, "");
}

export function normaliseOrigin(value: string | undefined, fallback: string): string {
  const raw = ((value ?? "").trim() || fallback).replace(/\/+$/, "");
  if (!ORIGIN_RE.test(raw)) throw new Error(`invalid origin: ${raw}`);
  return raw;
}

/** A document link: an http(s) URL or a same-origin path (`/terms`), else `fallback`. */
export function normaliseDocUrl(value: string | undefined, fallback: string): string {
  const raw = (value ?? "").trim();
  if (!raw) return fallback;
  if (/^\/(?![/\\])/.test(raw)) return raw;
  const url = new URL(raw);
  if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error(`document URL must be http(s) or a path: ${raw}`);
  return url.toString();
}

export const env = {
  apiBase: normaliseBase(import.meta.env.VITE_API_BASE, "http://127.0.0.1:8000"),
  plotsOrigin: normaliseOrigin(import.meta.env.VITE_PLOTS_ORIGIN, "http://127.0.0.1:5174"),
  authBase: normaliseBase(import.meta.env.VITE_AUTH_BASE || import.meta.env.VITE_API_BASE, "http://127.0.0.1:8000"),
  mock: import.meta.env.MODE === "mock",
  termsUrl: normaliseDocUrl(import.meta.env.VITE_TERMS_URL, "/terms"),
  acceptableUseUrl: normaliseDocUrl(import.meta.env.VITE_ACCEPTABLE_USE_URL, "/acceptable-use"),
} as const;

if (typeof window !== "undefined" && window.location.origin === env.plotsOrigin) {
  // A misconfiguration that would defeat the plot isolation entirely.
  throw new Error("VITE_PLOTS_ORIGIN must differ from the app origin");
}
