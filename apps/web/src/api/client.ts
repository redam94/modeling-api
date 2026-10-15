import createClient from "openapi-fetch";

import { env } from "@/env";

import type { paths } from "./schema";
import type { Problem } from "./types";

/** The typed API client (openapi-fetch over the generated `paths`). */
export const api = createClient<paths>({
  baseUrl: env.apiBase,
  // M1 has no auth; M2 adds a BFF session cookie (credentials: "include" then).
  credentials: "omit",
  headers: { Accept: "application/json" },
  // Resolve fetch at call time (not import time) so interceptors installed later apply.
  fetch: (request: Request) => globalThis.fetch(request),
});

/** An HTTP error from the API, carrying the RFC 7807 problem when the server sent one. */
export class ApiError extends Error {
  override name = "ApiError";

  constructor(
    readonly status: number,
    readonly problem: Problem | null,
    message?: string,
  ) {
    super(message ?? problem?.detail ?? problem?.title ?? `HTTP ${status}`);
  }

  get code(): string | null {
    return this.problem?.code ?? null;
  }
}

function isProblem(value: unknown): value is Problem {
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
  throw new ApiError(response.status, isProblem(error) ? error : null);
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
