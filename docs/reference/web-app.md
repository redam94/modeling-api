# Web app and plot sandbox <span class="mapi-badge">v0.2</span>

The browser front end is two separately served apps in the pnpm workspace:

| App | Package | Local origin | What it is |
|---|---|---|---|
| Web app | `@mapi/web` (`apps/web`) | `http://127.0.0.1:5173` | The Expert-mode SPA (React 19, TanStack Router/Query/Table, Monaco, Arrow) plus the Guided-mode shell |
| Plot sandbox | `@mapi/plot-sandbox` (`apps/plot-sandbox`) | `http://127.0.0.1:5174` | The only place Vega-Lite and Plotly specs render, on a separate origin |

`make dev` starts both against the local API ([Local development](../operations/local-development.md)).
In production they are static files, served with the headers described below (the
`mapi/web` image does this with nginx).

## Pages

| Area | What you can do |
|---|---|
| Home | Runs in flight, runs that need attention, recent runs and models |
| Datasets | Create a dataset, preview a CSV locally, map columns to dims and variables, upload through a signed PUT, follow the ingest run; schema tables, coordinate browser, missingness heatmap and preview (`preview.json`, `profile.json`) |
| Build | Monaco YAML editor bound to `/v1/schemas/modelspec.json` with live `:validate` diagnostics at their JSON pointers; the pipeline as step cards with the schema at each stage, the split and context markers; save a revision, dry-run, fit, revision diff |
| Runs | Filters by state and kind; live progress over SSE (resumes with `Last-Event-ID`), per-chain lanes, the `STALLED` banner, event timeline, logs; cancel, resume a partial run, retry one resource class up after `RESOURCE_EXHAUSTED` |
| Inspect | Scorecard, per-variable diagnostics, the DataTree browser (`ui/tree_index.json`), lineage, signature and bundle objects, the plot gallery |
| Score | Mode limited to the model's capabilities, horizon, `inputs_stage`, input and scenario datasets, the spec's side tables (supplied ones default to the version the fit used; pinned ones are replayed), interventions, output selector; `Prefer: wait=30`, then the run over SSE; the run's own plots (`GET /v1/runs/{id}/plots`, sanitized) in the sandbox, Arrow outputs as a table (summaries pivoted by statistic) and a quick chart |
| Catalog, Settings | Steps, models and plots of the image; theme, persona and connection health |

## API client

`apps/web/openapi.json` is the control plane's own OpenAPI 3.1 document, written by
`uv run python apps/web/scripts/gen_openapi.py` from `mapi_server.app.openapi_document()`
(no database needed). `pnpm --filter @mapi/web openapi:types` generates
`src/api/schema.d.ts` from it, and `openapi-fetch` gives typed calls. CI fails when either
file drifts from the server (`gen_openapi.py --check`, `openapi:check`). Run-event payloads
(`StatePayload`, `ProgressPayload`, …) are published as components so SSE frames are typed
too.

| Variable | Default | Meaning |
|---|---|---|
| `VITE_API_BASE` | `http://127.0.0.1:8000` | The API. It must allow this origin (`MAPI_WEB_ORIGIN`, CORS). |
| `VITE_PLOTS_ORIGIN` | `http://127.0.0.1:5174` | The plot sandbox; must differ from the app origin. |
| `VITE_STORAGE_ORIGINS` | `http://127.0.0.1:4443` | Signed-URL origins allowed by the CSP `connect-src`. The GCS shim answers CORS for `MAPI_WEB_ORIGIN`. |
| `VITE_APP_ORIGINS` (sandbox) | `http://127.0.0.1:5173,http://localhost:5173` | Origins allowed to embed the sandbox and talk to it. |

`scripts/dev.py` sets these from the `MAPI_*` URLs, so `make dev` on other ports just works.

## Security

- **App origin.** A strict CSP with `script-src 'self'`, `connect-src` limited to the API and
  storage origins, `frame-src` the plot sandbox only, and `require-trusted-types-for 'script'`
  with a default policy that admits same-origin script URLs only. Untrusted text (logs,
  tracebacks, `untrusted_text`, plugin messages) renders as text with ANSI, control and bidi
  characters stripped. Artifact downloads are checked against the `ArtifactLink` size and
  CRC32C.
- **Plots.** Every plot, first-party included, renders in a `sandbox="allow-scripts"` iframe
  on the sandbox origin (no `allow-same-origin`). The sandbox refuses URL data, `href`
  encodings, image marks, Plotly layout images and tile maps before any library loads; Vega
  runs with `ast: true` and the interpreter, a loader that rejects every URL and a text-only
  tooltip; Plotly uses the strict bundle. Its CSP is `default-src 'none'` with
  `frame-ancestors` set to the app origins.
- **Messages.** The `mapi.plot/v1` postMessage protocol is validated strictly in both
  directions. Because the framed document has an opaque origin, the app accepts a message
  only when `event.source` is the iframe's window and `event.origin` is `"null"`, and stops
  posting if the frame ever navigates after its first load.

## Tests

```bash
pnpm -r test            # Vitest (both apps) + the Playwright smoke suite (mock API, production build)
pnpm --filter @mapi/web e2e   # @e2e specs against a running stack (MAPI_E2E=1)
```

The smoke suite runs the mock-mode production build and fails on any CSP or Trusted Types
violation. The `@e2e` specs reuse a running `make dev` stack; set `VITE_API_BASE`,
`MAPI_WEB_ORIGIN` and `MAPI_PLOTS_ORIGIN` when it runs on other ports.
