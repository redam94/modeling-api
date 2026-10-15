/**
 * Content-Security-Policy for the app origin (plan §9: "CSP with Trusted Types on the app
 * origin"). Applied to production builds as a `<meta>` tag and to `vite preview` as a header
 * (which also carries `frame-ancestors`). The dev server is exempt: Vite injects inline scripts.
 */

export interface AppCspOptions {
  apiBase: string;
  /** Base of the `/auth` backend-for-frontend routes when it is not the API base. */
  authBase?: string;
  plotsOrigin: string;
  /** Origins of signed storage URLs (GCS shim locally, storage.googleapis.com in the cloud). */
  storageOrigins: readonly string[];
  /** Include `frame-ancestors` (header only). */
  header?: boolean;
}

export function originOf(url: string): string {
  return new URL(url).origin;
}

export function buildAppCsp(o: AppCspOptions): string {
  const connect = ["'self'", originOf(o.apiBase), ...(o.authBase ? [originOf(o.authBase)] : []), ...o.storageOrigins.map(originOf)];
  const directives: [string, string[]][] = [
    ["default-src", ["'self'"]],
    ["script-src", ["'self'"]],
    // Monaco and Radix inject style elements/attributes; no fetch directive allows exfiltration.
    ["style-src", ["'self'", "'unsafe-inline'"]],
    ["img-src", ["'self'", "data:", "blob:"]],
    ["font-src", ["'self'", "data:"]],
    ["connect-src", [...new Set(connect)]],
    ["frame-src", [originOf(o.plotsOrigin)]],
    ["worker-src", ["'self'", "blob:"]],
    ["object-src", ["'none'"]],
    ["base-uri", ["'none'"]],
    ["form-action", ["'self'"]],
    ["require-trusted-types-for", ["'script'"]],
  ];
  if (o.header) directives.push(["frame-ancestors", ["'none'"]]);
  return directives.map(([k, v]) => `${k} ${v.join(" ")}`).join("; ");
}
