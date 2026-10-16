/**
 * Pages that work without a session (`/signup`, `/verify-email` and the default terms the
 * sign-up links to): the app renders them outside the workspace gate, from their own lazily
 * loaded chunk.
 */
export const PUBLIC_PATHS = ["/signup", "/verify-email", "/terms", "/acceptable-use"] as const;
export type PublicPath = (typeof PUBLIC_PATHS)[number];

export function publicPathOf(pathname: string): PublicPath | null {
  const p = pathname.replace(/\/+$/, "") || "/";
  return (PUBLIC_PATHS as readonly string[]).includes(p) ? (p as PublicPath) : null;
}

/** `mev_` and 40 lowercase base32 characters (`mapi_spec.trust.EmailVerify.token`). */
export const VERIFY_TOKEN_RE = /^mev_[0-9a-hjkmnp-tv-z]{40}$/;
