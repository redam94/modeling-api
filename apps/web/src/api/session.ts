/**
 * The browser session (backend-for-frontend, `mapi_spec.auth`). The SPA never holds an OAuth
 * token: `/auth/login` runs the OIDC Authorization Code + PKCE flow server-side and sets an
 * HttpOnly `SameSite=Strict` cookie; the app only asks `/auth/session` who is signed in.
 *
 * A server without `/auth` (404, the v0.2 single-tenant profile) puts the app in the
 * `none` auth mode: no sign-in, no tenancy headers.
 */
import createClient from "openapi-fetch";

import { env } from "@/env";
import { useAuth } from "@/stores/auth";

import { sessionMiddleware, unwrap, unwrapVoid } from "./client";
import type { paths } from "./schema";
import type { SessionInfo } from "./types";

/** The `/auth/*` routes, which may live on another base than the versioned API. */
export const authApi = createClient<paths>({
  baseUrl: env.authBase,
  credentials: "include",
  headers: { Accept: "application/json" },
  fetch: (request: Request) => globalThis.fetch(request),
});
authApi.use(sessionMiddleware);

export type SessionState = { mode: "session"; info: SessionInfo } | { mode: "none" };

/** `GET /auth/session`; also records the auth mode for the API client's middleware. */
export async function fetchSession(signal?: AbortSignal): Promise<SessionState> {
  const res = await authApi.GET("/auth/session", signal ? { signal } : {});
  if (res.response.status === 404) {
    useAuth.getState().setMode("none");
    return { mode: "none" };
  }
  const info = await unwrap(Promise.resolve(res));
  useAuth.getState().setMode("session");
  if (info.authenticated) useAuth.getState().clearExpired();
  return { mode: "session", info };
}

/** `POST /auth/logout` (works without a valid session). */
export function logout(): Promise<void> {
  return unwrapVoid(authApi.POST("/auth/logout", {}));
}

/** `mapi_spec.auth.safe_return_to`: a same-origin absolute path, else `/`. */
const RETURN_TO_RE = /^\/(?![/\\])[A-Za-z0-9\-._~!$&'()*+,;=:@%/?#]{0,2047}$/;

export function safeReturnTo(value: string | null | undefined): string {
  return value && RETURN_TO_RE.test(value) ? value : "/";
}

/** The current location as a `return_to` path. */
export function currentPath(): string {
  if (typeof window === "undefined") return "/";
  const { pathname, search, hash } = window.location;
  return safeReturnTo(`${pathname}${search}${hash}`);
}

/**
 * Where "Sign in" sends the browser: the BFF login route with `return_to`. In mock mode it is
 * the app's own development identity-provider page (MSW cannot answer a top-level navigation
 * to another origin).
 */
export function loginHref(returnTo: string, info?: Pick<SessionInfo, "login_url"> | null): string {
  const path = safeReturnTo(returnTo);
  const query = `return_to=${encodeURIComponent(path)}`;
  if (env.mock) return `/auth/login?${query}`;
  const configured = info?.login_url ?? "/auth/login";
  let target: string;
  if (/^\/(?![/\\])/.test(configured)) {
    target = `${env.authBase}${configured}`;
  } else {
    // An absolute login URL is only followed on the auth origin itself.
    try {
      const u = new URL(configured);
      target = u.origin === new URL(env.authBase).origin ? u.toString() : `${env.authBase}/auth/login`;
    } catch {
      target = `${env.authBase}/auth/login`;
    }
  }
  return `${target}${target.includes("?") ? "&" : "?"}${query}`;
}

/** Navigate to sign-in (a full page load: the identity provider is another site). */
export function signIn(returnTo: string = currentPath(), info?: Pick<SessionInfo, "login_url"> | null): void {
  window.location.assign(loginHref(returnTo, info));
}
