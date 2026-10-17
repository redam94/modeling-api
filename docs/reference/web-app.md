# Web app and plot sandbox <span class="mapi-badge">v0.2</span>

The front end in the browser has two apps in the pnpm workspace. Each app has its own server:

| App | Package | Local origin | What it is |
|---|---|---|---|
| Web app | `@mapi/web` (`apps/web`) | `http://127.0.0.1:5173` | The SPA (React 19, TanStack Router/Query/Table, Monaco, Arrow). It has Expert mode and the Guided task flow |
| Plot sandbox | `@mapi/plot-sandbox` (`apps/plot-sandbox`) | `http://127.0.0.1:5174` | The only place where Vega-Lite and Plotly specs render. It is on a separate origin |

`make dev` starts the two apps with the local API ([Local development](../operations/local-development.md)).
In production, the apps are static files. The server sends them with the headers that this
page describes. The `mapi/web` image uses nginx for this.

## Pages

| Area | What you can do |
|---|---|
| Home | See the runs that are in progress, the runs that need attention, and the recent runs and models |
| Datasets | Create a dataset. Preview a CSV file locally. Map columns to dims and variables. Upload through a signed PUT. Monitor the ingest run. See the schema tables, the coordinate browser, the missingness heatmap and the preview (`preview.json`, `profile.json`) |
| Build | Use the Monaco YAML editor. It uses `/v1/schemas/modelspec.json` and shows live `:validate` diagnostics at their JSON pointers. See the pipeline as step cards with the schema at each stage, and markers for the split and the context. Save a revision, do a dry run, fit, and compare revisions |
| Runs | Filter by state and kind. See live progress over SSE (it continues with `Last-Event-ID`), a lane for each chain, the `STALLED` banner, the event timeline and the logs. Cancel a run. Resume a partial run. After `RESOURCE_EXHAUSTED`, retry with the next larger resource class |
| Inspect | See the scorecard, the diagnostics for each variable, the DataTree browser (`ui/tree_index.json`), the viewer for the preprocessing state (`ui/preprocess.json`), the lineage, the signature, the bundle objects and the plot gallery |
| Score | Set the mode (only the modes that the model can do), the horizon, `inputs_stage`, the input and scenario datasets, the side tables of the spec, the interventions and the output selector. Supplied side tables use the version from the fit by default. Pinned side tables use the same version again. The app sends `Prefer: wait=30`, then follows the run over SSE. It shows the plots of the run (`GET /v1/runs/{id}/plots`, sanitized) in the sandbox. It shows Arrow outputs as a table (summaries pivoted by statistic) and a quick chart |
| Guided | Use the task flow for analysts (`/guided`, `/guided/new/<template>`, `/guided/<spec>/<step>`). It has templates and questions. The browser makes a profile of the upload for the questions. Set business priors and lift tests, with a preview in a sandbox. Fit the model, with progress in plain language. See the report and the sanity scorecard. Use what-if sliders. The browser interpolates the response grid of each draw (`src/features/guided/whatif.ts`, tested against `packages/spec/tests/snapshots/whatif_golden.json`). Use "Run exact", forecasts, scenario presets, promotion and embed links. See [Your first analysis in Guided mode](../guides/guided-tutorial.md) |
| Catalog, Settings | See the steps, models and plots of the image. Set the theme and the persona (the app keeps it for each user). See the health of the connection |

## API client

`apps/web/openapi.json` is the OpenAPI 3.1 document of the control plane.
`uv run python apps/web/scripts/gen_openapi.py` writes it from
`mapi_server.app.openapi_document()`. This does not need a database.
`pnpm --filter @mapi/web openapi:types` makes `src/api/schema.d.ts` from the document.
`openapi-fetch` gives typed calls. If one of the two files is different from the server, CI
fails (`gen_openapi.py --check`, `openapi:check`). `make api-types` runs the two generators.
The document also publishes the run-event payloads (`StatePayload`, `ProgressPayload`, …) as
components. Thus, the SSE frames also have types.

| Variable | Default | Meaning |
|---|---|---|
| `VITE_API_BASE` | `http://127.0.0.1:8000` | The API. It must permit this origin (`MAPI_WEB_ORIGIN`, CORS). |
| `VITE_PLOTS_ORIGIN` | `http://127.0.0.1:5174` | The plot sandbox. It must be different from the app origin. |
| `VITE_STORAGE_ORIGINS` | `http://127.0.0.1:4443` | The origins of signed URLs that the CSP `connect-src` permits. The GCS shim answers CORS for `MAPI_WEB_ORIGIN`. |
| `VITE_APP_ORIGINS` (sandbox) | `http://127.0.0.1:5173,http://localhost:5173` | The origins that can embed the sandbox and send messages to it. |

`scripts/dev.py` sets these variables from the `MAPI_*` URLs. Thus, `make dev` also works on other ports.

## Security

- **App origin.** The app has a strict CSP with these parts:
    - `script-src 'self'`.
    - `connect-src`, which permits only the API and storage origins.
    - `frame-src`, which permits only the plot sandbox.
    - `require-trusted-types-for 'script'`, with a default policy that permits only script URLs
      from the same origin.

    Untrusted text (logs, tracebacks, `untrusted_text`, plugin messages) renders as text. The
    app removes ANSI, control and bidi characters from it. The app does a check of each
    artifact download against the size and the CRC32C in the `ArtifactLink`.
- **Plots.** Each plot renders in a `sandbox="allow-scripts"` iframe on the sandbox origin
  (no `allow-same-origin`). This rule also applies to first-party plots. Before a library
  loads, the sandbox refuses URL data, `href` encodings, image marks, Plotly layout images and
  tile maps. Vega runs with `ast: true` and the interpreter. It also has a loader that rejects
  all URLs, and a tooltip that shows only text. Plotly uses the strict bundle. The CSP of the
  sandbox is `default-src 'none'`, with `frame-ancestors` set to the app origins.
- **Messages.** The app and the sandbox do a strict validation of the `mapi.plot/v1`
  postMessage protocol in the two directions. The document in the frame has an opaque origin.
  Thus, the app accepts a message only when `event.source` is the window of the iframe and
  `event.origin` is `"null"`. If the frame goes to a different page after its first load, the
  app stops sending messages.

## Tests

```bash
pnpm -r test            # Vitest (both apps) + the Playwright smoke suite (mock API, production build)
pnpm --filter @mapi/web e2e   # @e2e specs against a running stack (MAPI_E2E=1)
```

The smoke suite runs the production build in mock mode. It fails on each CSP or Trusted Types
violation. `e2e/guided.spec.ts` runs the full Guided flow against the mock API
(`src/mocks/guided.ts`). The `@e2e` specs use a `make dev` stack that is running. If the stack
runs on other ports, set `VITE_API_BASE`, `MAPI_WEB_ORIGIN` and `MAPI_PLOTS_ORIGIN`.
