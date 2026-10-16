# @mapi/plot-sandbox

The only place the platform renders Vega-Lite and Plotly specs (plan §10.3). The web app embeds
it in an iframe on a **separate origin** (`http://127.0.0.1:5174` locally) with
`sandbox="allow-scripts"` and **no** `allow-same-origin`, so a hostile spec cannot reach the
app's cookies, storage or DOM.

```bash
pnpm --filter @mapi/plot-sandbox dev       # http://127.0.0.1:5174 (strict port)
pnpm --filter @mapi/plot-sandbox build     # dist/ (static files)
pnpm --filter @mapi/plot-sandbox test      # protocol, safety, CSP, controller
```

`VITE_APP_ORIGINS` (comma separated, default `http://127.0.0.1:5173,http://localhost:5173`) lists
the origins allowed to embed the sandbox and to talk to it.

## Rendering safeguards

| Layer | What it does |
|---|---|
| Server sanitizer | `POST /v1/plots:validate` and every served `PlotSpec` are sanitized server-side first. |
| Spec check (`findUnsafeConstructs`) | Refuses URL data, `href` encodings, image marks, Plotly layout images, tile maps and markup other than `b i br sub sup`, before any library loads. |
| Vega | `vega-embed` with `ast: true` + `vega-interpreter` (no `new Function`), mode forced to `vega-lite`, actions off, a loader that rejects every URL, and a tooltip that writes `textContent` only. |
| Plotly | The `plotly.js-strict-dist-min` bundle (no `eval`), mode bar off, `$ref` columns resolved from the posted datasets. |
| CSP | `default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src data: blob:; font-src 'self' data:; connect-src 'self'; worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'`, plus `frame-ancestors <app origins>` in the header. Inline styles are needed by Plotly and the tooltip; with every fetch directive locked down they cannot exfiltrate. |

## Hosting in production

Serve `dist/` from its own origin (for example `plots.<domain>`) with these headers on every
response (see `sandboxHeaders()` in `src/csp.ts`):

- `Content-Security-Policy`: the policy above **including** `frame-ancestors <app origin>` (a
  `<meta>` tag cannot carry `frame-ancestors`).
- `Access-Control-Allow-Origin: *` and `Cross-Origin-Resource-Policy: cross-origin`: the
  sandboxed document has an opaque origin, so its module scripts are CORS fetches. The assets are
  public, static and credential-free.
- `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.

## The embed page (`/embed/<token>`)

`embed.html` (entry `src/embed-main.ts`, logic in `src/embed.ts`) shows a plot shared with a
tokenised link (plan §10.3 items 4-5). It is the only page that talks to the API:

- it fetches `GET <api>/v1/embeds/<token>` with `credentials: "omit"` and no referrer; its CSP
  is the sandbox policy with `connect-src` extended to `<api>/v1/embeds/` only
  (`buildEmbedCsp` in `src/csp.ts`), and the API answers cross-origin reads from the sandbox
  origin only;
- the answer is validated (`parseEmbedView`: inline Arrow datasets only, plain JSON, the same
  `findUnsafeConstructs` check as the frames), decoded with apache-arrow and drawn with the
  same renderers; every text goes through `textContent`;
- framed by a site outside the link's `allowed_origins` (from `location.ancestorOrigins`, else
  the referrer; no evidence fails closed) it shows a notice instead of the plot;
- it links to the static PNG fallback (`image_url`, a short-lived signed URL) when there is one.

`VITE_API_URL` (default `http://127.0.0.1:8000`) is the API origin baked into the page. The
`mapi/web` image builds with the placeholder `__MAPI_API_URL__`, which nginx replaces with
`MAPI_API_URL` when it serves `/embed/<token>` (with the embed headers:
`frame-ancestors 'self' https:` and the extended `connect-src`).

## Message protocol (`mapi.plot/v1`)

`src/protocol.ts` is shared with the web app (`@mapi/plot-sandbox/protocol`). Both sides validate
every inbound message strictly: exact key sets, types, size caps, plain JSON only.

| Direction | Type | Body |
|---|---|---|
| app → sandbox | `render` | `id`, `kind` (`vega-lite`/`plotly`), `spec`, `datasets` (name → rows of scalars), `theme` |
| app → sandbox | `theme` | `theme` (re-renders the last plot) |
| app → sandbox | `export` | `id`, `format` (`png`/`svg`) |
| sandbox → app | `ready` | sent once after load |
| sandbox → app | `rendered` | `id` |
| sandbox → app | `resize` | `height` (document height, px) |
| sandbox → app | `select` | `id`, `items` (≤ 100 flat scalar rows from a click or selection) |
| sandbox → app | `exported` | `id`, `format`, `dataUrl` (base64 PNG/SVG only) |
| sandbox → app | `error` | `id`, `code` (`INVALID_MESSAGE`, `UNSAFE_SPEC`, `RENDER_FAILED`, `EXPORT_FAILED`), `message` |

Origins: the sandbox accepts messages only from `window.parent` at a configured app origin and
answers to that exact origin. Because the frame's origin is opaque, the app checks
`event.source === iframe.contentWindow` and `event.origin === "null"`, posts with target `"*"`,
and stops posting entirely if the frame ever navigates after its first load. Messages never
trigger navigation or API calls on either side.
