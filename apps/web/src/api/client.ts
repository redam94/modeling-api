import createClient, { type Middleware } from "openapi-fetch";

import { env } from "@/env";
import { useAuth } from "@/stores/auth";
import { activeScopeKey } from "@/stores/scope";

import type { paths } from "./schema";
import type { Problem } from "./types";

/** `mapi_spec.tenancy.PROJECT_HEADER`: which project a project-scoped request acts in. */
export const PROJECT_HEADER = "X-Mapi-Project";

/**
 * Sent on every state-changing request of a signed-in browser. A custom header cannot be
 * attached by a cross-site form or image and forces a CORS preflight, which the API only
 * grants to the web origin; with the `SameSite=Strict` session cookie and JSON bodies this is
 * the third CSRF layer (the server may also require it).
 */
export const CSRF_HEADER = "X-Requested-With";
export const CSRF_VALUE = "mapi-web";

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/**
 * Routes that act in the selected project (every v0.2 resource plus tokens and usage).
 * Tenant, operator, identity and catalog routes name their scope in the path or have none;
 * sending the header there could only conflict with the path (422).
 */
export function isProjectScoped(pathname: string): boolean {
  if (!pathname.startsWith("/v1/")) return false;
  return !/^\/v1\/(?:me|tenants|admin|projects|catalog|schemas)(?:[/:]|$)/.test(pathname);
}

/** Headers naming the project for `scope` (none outside a v0.3 session). */
export function projectHeaders(scope: string | null): Record<string, string> {
  return scope && useAuth.getState().mode === "session" ? { [PROJECT_HEADER]: scope } : {};
}

/**
 * Adds the tenancy headers a v0.3 server expects (never to a v0.2 server, whose CORS policy
 * would refuse them) and raises the session-expired flag on a 401.
 */
export const sessionMiddleware: Middleware = {
  onRequest({ request }) {
    if (useAuth.getState().mode !== "session") return undefined;
    const path = new URL(request.url).pathname;
    if (!SAFE_METHODS.has(request.method.toUpperCase())) request.headers.set(CSRF_HEADER, CSRF_VALUE);
    if (isProjectScoped(path) && !request.headers.has(PROJECT_HEADER)) {
      const scope = activeScopeKey();
      if (scope) request.headers.set(PROJECT_HEADER, scope);
    }
    return request;
  },
  onResponse({ request, response }) {
    if (response.status === 401 && !new URL(request.url).pathname.startsWith("/auth/")) {
      useAuth.getState().markExpired();
    }
    return undefined;
  },
};

/** The typed API client (openapi-fetch over the generated `paths`). */
export const api = createClient<paths>({
  baseUrl: env.apiBase,
  // The BFF session cookie (SameSite=Strict) rides along; the API allows only the web origin.
  credentials: "include",
  headers: { Accept: "application/json" },
  // Resolve fetch at call time (not import time) so interceptors installed later apply.
  fetch: (request: Request) => globalThis.fetch(request),
});
api.use(sessionMiddleware);

/** Seconds from a `Retry-After` header (delta-seconds or an HTTP date), else `null`. */
export function parseRetryAfter(value: string | null, now: number = Date.now()): number | null {
  if (!value) return null;
  const v = value.trim();
  if (/^\d{1,9}$/.test(v)) return Number(v);
  const t = Date.parse(v);
  return Number.isNaN(t) ? null : Math.max(0, Math.ceil((t - now) / 1000));
}

/** An HTTP error from the API, carrying the RFC 7807 problem when the server sent one. */
export class ApiError extends Error {
  override name = "ApiError";

  constructor(
    readonly status: number,
    readonly problem: Problem | null,
    message?: string,
    /** Seconds to wait before retrying (`Retry-After`), for 429 and 503. */
    readonly retryAfter: number | null = null,
  ) {
    super(message ?? problem?.detail ?? problem?.title ?? `HTTP ${status}`);
  }

  get code(): string | null {
    return this.problem?.code ?? null;
  }
}

export function isProblem(value: unknown): value is Problem {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return typeof v.code === "string" && typeof v.title === "string" && typeof v.status === "number";
}

interface FetchResult<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/**
 * Unwrap an openapi-fetch result: return `data` on 2xx, throw {@link ApiError} otherwise.
 * Network failures surface as `TypeError` from fetch and are rethrown unchanged.
 */
export async function unwrap<T>(promise: Promise<FetchResult<T>>): Promise<T> {
  const { data, error, response } = await promise;
  if (response.ok) {
    if (data === undefined && response.status !== 204) {
      throw new ApiError(response.status, null, "empty response body");
    }
    return data as T;
  }
  throw new ApiError(
    response.status,
    isProblem(error) ? error : null,
    undefined,
    parseRetryAfter(response.headers.get("Retry-After")),
  );
}

/** Like {@link unwrap} for endpoints that answer 204 No Content. */
export async function unwrapVoid(promise: Promise<FetchResult<unknown>>): Promise<void> {
  const { error, response } = await promise;
  if (response.ok) return;
  throw new ApiError(
    response.status,
    isProblem(error) ? error : null,
    undefined,
    parseRetryAfter(response.headers.get("Retry-After")),
  );
}

/** Like {@link unwrap} but also returns the status (for 200-vs-202 and replay handling). */
export async function unwrapWithStatus<T>(
  promise: Promise<FetchResult<T>>,
): Promise<{ data: T; status: number; headers: Headers }> {
  const result = await promise;
  const data = await unwrap(Promise.resolve(result));
  return { data, status: result.response.status, headers: result.response.headers };
}

/** A fresh Idempotency-Key for one user intent (reuse it on network retries). */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

/** Human-readable message for any thrown value. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const p = err.problem;
    if (p) return p.detail ? `${p.title}: ${p.detail}` : p.title;
    return err.message;
  }
  if (err instanceof TypeError) return `Could not reach the API at ${env.apiBase}.`;
  if (err instanceof Error) return err.message;
  return String(err);
}
