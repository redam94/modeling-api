/**
 * Trusted Types default policy (the app's CSP sets `require-trusted-types-for 'script'`).
 *
 * Only script URLs on this origin (bundled Monaco workers, the MSW worker in mock mode) are
 * allowed. There is deliberately no `createHTML` / `createScript`: any library that assigns a
 * raw string to an HTML or script sink fails loudly instead of creating an XSS hole. Monaco
 * brings its own named policies for its internal rendering.
 */

interface TrustedTypePolicyFactoryLike {
  createPolicy(name: string, rules: { createScriptURL?: (input: string) => string }): unknown;
}

export function sameOriginScriptUrl(input: string, origin: string = window.location.origin): string {
  const url = new URL(input, origin);
  if (url.origin !== origin) throw new TypeError(`blocked script URL from ${url.origin}`);
  return url.href;
}

export function installDefaultPolicy(): boolean {
  const tt = (window as unknown as { trustedTypes?: TrustedTypePolicyFactoryLike }).trustedTypes;
  if (!tt) return false;
  try {
    tt.createPolicy("default", { createScriptURL: (input) => sameOriginScriptUrl(input) });
    return true;
  } catch {
    // A default policy already exists (hot reload); keep the first one.
    return false;
  }
}

installDefaultPolicy();
