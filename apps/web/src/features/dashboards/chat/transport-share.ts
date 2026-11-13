/**
 * The chat transport of a share link (v0.20): `GET` / `POST /v1/shared/chat` with plain
 * `fetch`, exactly like the share client's other requests: never cookies (`credentials:
 * "omit"`), never the CSRF or project header, no referrer, no cache. The share session rides
 * in `X-Mapi-Share` only; a session that ended between two requests is refreshed once and the
 * request sent again (a revoked link answers 410 `LINK_EXPIRED`, which the share app turns
 * into the link-ended page).
 *
 * The share app's static closure has little room (plan D22), so this module is small and
 * reuses the share client's error type.
 */
import type { Problem } from "@/api/types";
import { env } from "@/env";

import { ShareApiError } from "../client";
import { SHARE_SESSION_HEADER, type ShareSessionHandle } from "../types";

import type { ChatTransport } from "./types";

export interface ShareChatTransportOptions {
  /** The API base (default `env.apiBase`). */
  baseUrl?: string;
}

function isProblem(v: unknown): v is Problem {
  const p = v as Partial<Problem> | null;
  return typeof p === "object" && p !== null && typeof p.code === "string" && typeof p.title === "string" && typeof p.status === "number";
}

function retryAfterOf(value: string | null): number | null {
  return value && /^\d{1,9}$/.test(value.trim()) ? Number(value.trim()) : null;
}

/** The chat transport of the share link whose session `session` holds. */
export function shareChatTransport(session: ShareSessionHandle, opts: ShareChatTransportOptions = {}): ChatTransport {
  const url = `${opts.baseUrl ?? env.apiBase}/v1/shared/chat`;

  const once = (body: unknown, signal: AbortSignal | undefined) =>
    globalThis.fetch(url, {
      method: body === undefined ? "GET" : "POST",
      headers: {
        Accept: "application/json",
        [SHARE_SESSION_HEADER]: session.current(),
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? null : JSON.stringify(body),
      credentials: "omit",
      referrerPolicy: "no-referrer",
      cache: "no-store",
      mode: "cors",
      signal: signal ?? null,
    });

  async function call<T>(body: unknown, signal: AbortSignal | undefined): Promise<T> {
    if (session.expired()) await session.refresh();
    let res = await once(body, signal);
    let problem: unknown = res.ok ? null : await res.json().catch(() => null);
    if (res.status === 401 && isProblem(problem) && problem.code === "UNAUTHENTICATED") {
      // The session ended between two requests: refresh once (a revoked link answers 410).
      await session.refresh();
      res = await once(body, signal);
      problem = res.ok ? null : await res.json().catch(() => null);
    }
    if (res.ok) return (await res.json()) as T;
    throw new ShareApiError(res.status, isProblem(problem) ? problem : null, undefined, retryAfterOf(res.headers.get("Retry-After")));
  }

  return {
    availability: (signal) => call(undefined, signal),
    send: (req, signal) => call(req, signal),
  };
}
