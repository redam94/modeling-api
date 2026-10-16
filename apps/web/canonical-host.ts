import type { Connect, Plugin } from "vite";

/**
 * Redirects `localhost` to the canonical host the stack is configured for (`127.0.0.1`).
 *
 * The API allows exactly one browser origin (`MAPI_WEB_ORIGIN`) and the session cookie is
 * `SameSite=strict` on the API's host, so a page opened as `http://localhost:5173` can neither
 * pass CORS nor send the cookie: every call fails and the app reports the API as unreachable.
 * Sending the browser to the same path on `127.0.0.1` makes both spellings work.
 */
export function canonicalHostRedirect(host: string, from: readonly string[] = ["localhost"]): Connect.NextHandleFunction {
  return (req, res, next) => {
    const header = req.headers.host;
    if (!header) return next();
    const url = new URL(`http://${header}`);
    if (!from.includes(url.hostname) || url.hostname === host) return next();
    res.statusCode = 307;
    res.setHeader("Location", `http://${host}${url.port ? `:${url.port}` : ""}${req.url ?? "/"}`);
    res.setHeader("Cache-Control", "no-store");
    res.end();
  };
}

export function canonicalHost(host: string): Plugin {
  const redirect = canonicalHostRedirect(host);
  return {
    name: "mapi-canonical-host",
    configureServer(server) {
      server.middlewares.use(redirect);
    },
    configurePreviewServer(server) {
      server.middlewares.use(redirect);
    },
  };
}
