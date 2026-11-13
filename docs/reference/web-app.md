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
| Datasets | Create a dataset. Preview a CSV file locally. Map columns to dims and variables. Upload through a signed PUT. Monitor the ingest run. See the schema tables, the coordinate browser, the missingness heatmap and the preview (`preview.json`, `profile.json`). Upload a zip of images (`image_folder`, v0.14). Upload a folder of images as an image collection with the step **Folder of images**: labels from the folders or from a manifest column, progress, retry and continue (v0.15). The version page of a collection shows its summary (v0.15). The Data Explorer has the **Image gallery** section for image datasets (v0.14). See [Image data](../guides/image-data.md) |
| Build | Use the Monaco YAML editor. It uses `/v1/schemas/modelspec.json` and shows live `:validate` diagnostics at their JSON pointers. See the pipeline as step cards with the schema at each stage, and markers for the split and the context. Save a revision, do a dry run, fit, and compare revisions |
| Runs | Filter by state, kind and experiment. From v0.14, the filter bar takes the [search grammar](http-api.md#search-runs), the column picker adds tracked metrics, params and tags as columns, and you can sort by a column and save the search as a view. Select 2 to 10 runs with the check boxes to compare them or to add them to an experiment. The run page has the tabs **Overview**, **Metrics**, **Params**, **Media** (v0.14), **Logs** and **Notes** (v0.11). It shows live progress over SSE (it continues with `Last-Event-ID`), a lane for each chain, the `STALLED` banner, the event timeline and a **Plots** card for a finished run (v0.10). Cancel a run. Resume a partial run. After `RESOURCE_EXHAUSTED`, retry with the next larger resource class. See [Track metrics, params and tags](../guides/tracking.md) |
| Compare runs | `/runs/compare` (v0.14): the metric curves, the metric bars, the params that are different, the plots side by side, and the images with a step slider, for 2 to 10 runs. See [Compare runs and use experiments](../guides/run-comparison.md) |
| Experiments | `/experiments` and the page of an experiment (v0.14): make, rename, archive and delete experiments, and see their runs. The **Notes** tab of a run moves the run to an experiment |
| Inspect | See the scorecard, the diagnostics for each variable, the DataTree browser (`ui/tree_index.json`), the viewer for the preprocessing state (`ui/preprocess.json`), the lineage, the signature, the bundle objects and the plot gallery |
| Score | Set the mode (only the modes that the model can do), the horizon, `inputs_stage`, the input and scenario datasets, the side tables of the spec, the interventions and the output selector. Supplied side tables use the version from the fit by default. Pinned side tables use the same version again. The app sends `Prefer: wait=30`, then follows the run over SSE. It shows the plots of the run (`GET /v1/runs/{id}/plots`, sanitized) in the sandbox. It shows a view for each output by its shape (`ScoreSummaryTable.dims` and `time_dim`): a map with a frame slider for a grid output (from `ui/fields.json`, which then replaces the `sci.field_map` and `sci.vector_field` plots), a line with a band for a time series, intervals for a categorical output and tiles for a scalar. It shows Arrow outputs as a table (summaries pivoted by statistic) and a quick chart. The quick chart has a **Cell** picker for a table with more dims than the axis and the colour, for example a grid (v0.10). *Results by output* also shows a map of areas and a classification view from the plots of the run (v0.10). The table keeps the dim columns that the `mapi.dims` metadata names as keys, also when they are numeric (v0.9). The **No new data** option shows only for Forecast, or when a scenario dataset gives the values (v0.9). For the other modes, a note tells why the option is not there |
| Guided | Use the task flow for analysts (`/guided`, `/guided/new/<template>`, `/guided/<spec>/<step>`). It has templates and questions. The browser makes a profile of the upload for the questions. Set business priors and lift tests, with a preview in a sandbox. Fit the model, with progress in plain language. See the report and the sanity scorecard. Use what-if sliders. The browser interpolates the response grid of each draw (`src/features/guided/whatif.ts`, tested against `packages/spec/tests/snapshots/whatif_golden.json`). Use "Run exact", forecasts, scenario presets, promotion and embed links. See [Your first MMM in Guided mode](../guides/guided-tutorial.md) |
| Workflows | See the gallery of templates, edit a workflow and run it. The run view shows the outputs of each node: tables, reports, governance evidence and links to files. It draws the `plot` outputs of nodes and the `plot` sections of reports in the sandbox (v0.9) |
| Dashboards | `/dashboards` and `/dashboards/<id>` (v0.16): make a client dashboard and edit its draft on the tabs **Setup**, **Inputs**, **Access**, **History** and **Preview**. Publish it, give it to client users, make and revoke share links, publish or hide uploads, and approve or reject refit requests. See [Build a client dashboard](../guides/client-dashboards.md) |
| Search | Open the search with **Search** in the top bar, `Cmd/Ctrl+K` or `/` (v0.21). It finds pages, actions, items by name, ids and pages of the docs (`src/features/search/`, `src/components/shell/command-palette.tsx`). It follows the navigation mode and the role. See [Search](../guides/web-ui.md#search) |
| Catalog, Settings | See the steps, models and plots of the image. The plot catalog has a filter for each pack: All models, Marketing mix, Machine learning, Science and Networks. Set the theme and the persona (the app keeps it for each user). See the health of the connection |

## The client portal and the share page <span class="mapi-badge">v0.16</span>

Two surfaces show client dashboards without the full app:

| Surface | Path | Who | What loads |
|---|---|---|---|
| Client portal | `/c`, `/c/<dashboard id>?tab=` | A signed-in user whose role in the current project is `client` | A small app with its own router: the list of granted dashboards and the readout page. No navigation, no search, no shortcuts. Any other path goes to `/c` |
| Share page | `/s#mpd_…` | Anyone with a share link | A separate chunk with its own query client and a plain `fetch` client. It sends no cookie, no CSRF header and no `X-Mapi-Project`. It sets `<meta name="referrer" content="no-referrer">` |

The workspace gate loads the portal in place of the main router when the role is `client`.
Thus no expert page can be opened with a deep link. A person with more than one project
changes project with the project switcher in the portal header. The server enforces the
access in all cases.

The share page reads the token from the fragment, removes it with `history.replaceState`,
and exchanges it for a session in a POST. It keeps the session in memory only (never in
`localStorage` or a cookie) and gets a new one at 80% of its life. See
[Share dashboards securely with a share link](../guides/sharing-dashboards-securely.md).

The readout charts are dependency-free SVG (`components/readout-charts.tsx`). They do not
use the plot sandbox. The build has a size budget for each chunk (`bundle-budget.ts`,
`vite.config.ts`). The share app must stay below 288 KiB and must not import TanStack Router,
the data tables, `api/queries.ts`, Monaco, Arrow or XYFlow. The client portal must stay below
260 KiB and must not import the data tables, the shell of the main app, Monaco, Arrow or
XYFlow. A build that breaks a budget fails.

## Theme { #theme }

From v0.21, the app, the client pages and the plot sandbox use the Mono look. One rule makes
it: the controls are monochrome, and colour is for data. It replaces the looks of v0.16 and
v0.18. The documentation site uses the same look
([The docs theme](../project/docs-theme.md)).

- **Tokens.** `apps/web/src/app.css` keeps the semantic names of shadcn (`--primary`,
  `--card`, …) in `:root` (light) and `.dark`. `--primary` is the ink colour. The file also
  has ramps (`--grey-000` to `--grey-1000`, and `blue`, `orange`, `green` and `red`), series
  colours (`--series-1` to `--series-8`), chart tokens (`--chart-band`, `--chart-gain`, …)
  and the zone tokens of the readouts (`--zone-headroom`, `--zone-optimal`, `--zone-over`,
  `--zone-nopay`). New components use only token classes, never colour literals.
- **Colour for meaning.** Orange is new or needs attention, blue is information, green is
  good and red is danger. The series colours start with blue, orange, green and red. Their
  sequence was tested with a simulation of red-green colour blindness.
- **Dark theme.** The dark theme comes from the same tokens. Select it in **Settings** or the
  **Theme** menu, or follow the system. The class `band-dark` gives the dark tokens to one
  block in the two themes.
- **Type.** The text uses the system font. Inter and JetBrains Mono (self-hosted, no Google
  Fonts) are the fonts for a system that has none. The body text is 14/22 px (`text-sm`).
  Numbers in tiles and tables use tabular figures.
- **Focus ring.** The focus ring is the ink colour (`--ring` is `#1d1d1f` in the light theme
  and `#f5f5f7` in the dark theme). In the light theme, status text colours are darker than
  the data hues, so they pass AA on their tints.
- **Art.** The sign-in, Start here, not-found and empty-list images are generated files in
  `apps/web/src/assets/brand/`, with their source in `PROVENANCE.md`. The components in
  `components/brand/` draw them. One file serves the two themes (the utility
  `art-on-black`). The share page and the client portal must not load the empty-list art.
- **Plots.** The plot sandbox and the server-side plot palette (`mapi_plots.palette`) use the
  same series colours. The sandbox renders in the system font. For a plot that a model
  version stored before v0.21, the sandbox draws each old fixed recipe colour as the new
  colour of the same role ([What's new in 0.21](../whats-new-0.21.md#chart-colours)).

Tests (`theme-contrast.test.ts`, `theme-tokens.test.ts`, `theme-series-parity.test.ts`) make
sure that the tokens pass the contrast rules in the two themes and agree with the palettes.

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
