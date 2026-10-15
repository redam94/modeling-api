/**
 * Content-Security-Policy for the plot sandbox origin (plan §10.3).
 *
 * The policy is emitted twice: as a `<meta http-equiv>` in index.html (works on any static
 * host) and as an HTTP header by the Vite dev/preview servers. `frame-ancestors` is only
 * honoured in the header, so production hosting must send the header too (see README).
 *
 * `style-src 'unsafe-inline'` is required by Plotly and Vega tooltips, which inject style
 * elements and attributes. It cannot be used to exfiltrate: every fetch directive
 * (`connect-src`, `img-src`, `font-src`, ...) only allows same-origin, `data:` or `blob:`.
 */

export interface CspOptions {
  /** Origins allowed to embed the sandbox (the web app). */
  appOrigins: readonly string[];
  /** Vite dev server: allow its HMR websocket. */
  dev?: boolean;
  /** Origin of the sandbox itself (needed for the dev websocket URL). */
  selfOrigin?: string;
  /** Include `frame-ancestors` (header only; ignored and warned about in a meta tag). */
  includeFrameAncestors?: boolean;
}

const ORIGIN_RE = /^https?:\/\/[A-Za-z0-9.-]+(?::\d{1,5})?$|^https?:\/\/\[[0-9a-fA-F:]+\](?::\d{1,5})?$/;

export function parseOrigins(value: string | undefined, fallback: readonly string[]): string[] {
  const items = (value ?? "")
    .split(",")
    .map((s) => s.trim().replace(/\/+$/, ""))
    .filter(Boolean);
  const origins = items.length ? items : [...fallback];
  for (const o of origins) {
    if (!ORIGIN_RE.test(o)) throw new Error(`invalid origin ${JSON.stringify(o)}`);
  }
  return origins;
}

export function buildCsp(opts: CspOptions): string {
  const connect = ["'self'"];
  if (opts.dev && opts.selfOrigin) {
    connect.push(opts.selfOrigin.replace(/^http/, "ws"));
  }
  const directives: [string, string[]][] = [
    ["default-src", ["'none'"]],
    ["script-src", ["'self'"]],
    ["style-src", ["'self'", "'unsafe-inline'"]],
    ["img-src", ["data:", "blob:"]],
    ["font-src", ["'self'", "data:"]],
    ["connect-src", connect],
    ["worker-src", ["'none'"]],
    ["object-src", ["'none'"]],
    ["base-uri", ["'none'"]],
    ["form-action", ["'none'"]],
  ];
  if (opts.includeFrameAncestors) {
    directives.push(["frame-ancestors", opts.appOrigins.length ? [...opts.appOrigins] : ["'none'"]]);
  }
  return directives.map(([name, values]) => `${name} ${values.join(" ")}`).join("; ");
}

/** HTTP headers the sandbox host must send (dev and preview servers use these). */
export function sandboxHeaders(opts: CspOptions): Record<string, string> {
  return {
    "Content-Security-Policy": buildCsp({ ...opts, includeFrameAncestors: true }),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    // The sandboxed document has an opaque origin, so its module scripts are cross-origin
    // CORS fetches. Assets are public, static and credential-free.
    "Access-Control-Allow-Origin": "*",
    "Cross-Origin-Resource-Policy": "cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
  };
}
