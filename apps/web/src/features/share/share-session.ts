/**
 * The share session of an open link (plan D13). It lives in memory only (the share app keeps the
 * controller in a React ref): never a store, `localStorage`, `sessionStorage` or a cookie, so a
 * closed tab forgets it and a reload needs the link again.
 *
 * The controller refreshes the session at 80 % of its lifetime and on demand (when a request
 * finds it expired); concurrent refreshes share one request. A link that ended (410) is reported
 * through `onEnded`; the dashboard the refresh answer carries goes to `onDashboard`.
 */
import { problemOf, refreshShareSession } from "@/features/dashboards/client";
import type { ClientDashboard, ShareSession, ShareSessionHandle } from "@/features/dashboards/types";

/** Refresh when this share of the session's lifetime has passed. */
export const REFRESH_AT = 0.8;
/** Treat the session as expired this long before its end (clock skew, request time). */
export const EXPIRY_MARGIN_MS = 5_000;

export interface ShareSessionOptions {
  /** `POST /v1/share-sessions:refresh` (default: the dashboards client's). */
  refresh?: (session: string) => Promise<ShareSession>;
  now?: () => number;
  onDashboard?: (dashboard: ClientDashboard) => void;
  /** The link was revoked, expired or its workspace closed (410). */
  onEnded?: () => void;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
}

export interface ShareSessionController {
  handle: ShareSessionHandle;
  /** When the current session ends (epoch ms). */
  expiresAt(): number;
  /** Stop the refresh timer (the page closed or the link ended). */
  stop(): void;
}

/** How long to wait before refreshing a session issued at `issuedAt` that ends at `expiresAt`. */
export function refreshDelay(issuedAt: number, expiresAt: number): number {
  return Math.max(0, Math.floor((expiresAt - issuedAt) * REFRESH_AT));
}

export function createShareSession(first: ShareSession, opts: ShareSessionOptions = {}): ShareSessionController {
  const now = opts.now ?? Date.now;
  const refreshCall = opts.refresh ?? ((s: string) => refreshShareSession(s));
  const setTimer = opts.setTimer ?? ((fn: () => void, ms: number) => globalThis.setTimeout(fn, ms));
  const clearTimer = opts.clearTimer ?? ((h: unknown) => globalThis.clearTimeout(h as ReturnType<typeof setTimeout>));

  let session = first.session;
  let expiresAt = Date.parse(first.expires_at);
  let timer: unknown = null;
  let inFlight: Promise<void> | null = null;
  let stopped = false;

  const schedule = () => {
    if (timer !== null) clearTimer(timer);
    timer = null;
    if (stopped) return;
    timer = setTimer(() => {
      timer = null;
      // A timed refresh that fails is not fatal: the next request refreshes on demand.
      handle.refresh().catch(() => undefined);
    }, refreshDelay(now(), expiresAt));
  };

  const handle: ShareSessionHandle = {
    current: () => session,
    expired: () => now() >= expiresAt - EXPIRY_MARGIN_MS,
    refresh() {
      if (stopped) return Promise.reject(new Error("the share session was closed"));
      inFlight ??= (async () => {
        try {
          const next = await refreshCall(session);
          session = next.session;
          expiresAt = Date.parse(next.expires_at);
          opts.onDashboard?.(next.dashboard);
          schedule();
        } catch (err) {
          if (problemOf(err)?.status === 410) {
            stopped = true;
            if (timer !== null) clearTimer(timer);
            timer = null;
            opts.onEnded?.();
          }
          throw err;
        } finally {
          inFlight = null;
        }
      })();
      return inFlight;
    },
  };

  schedule();
  return {
    handle,
    expiresAt: () => expiresAt,
    stop() {
      stopped = true;
      if (timer !== null) clearTimer(timer);
      timer = null;
    },
  };
}
