# @mapi/web

The modeling-api web app: Expert mode (v0.2), the multi-tenant workspace (v0.3: sign-in, tenant
and project switcher, API tokens, members, usage and cost, operator console), and in v0.4 Guided mode
(the task flow for analysts), the Integrations hub and the trust pages (endpoints, snippets, webhooks,
share links, sign-up, Settings > Data). React 19, TypeScript (strict), Vite, TanStack Router/Query/Table, Zustand for UI-only
state, Tailwind v4 with shadcn/ui (new-york, neutral) components, Monaco + monaco-yaml, Apache
Arrow JS and lucide icons.

```bash
pnpm install                              # from the repo root (pnpm workspace)
pnpm --filter @mapi/web dev               # http://127.0.0.1:5173 against VITE_API_BASE
pnpm --filter @mapi/web dev:mock          # same, with the API mocked by MSW (no backend)
pnpm --filter @mapi/plot-sandbox dev      # http://127.0.0.1:5174, needed for every plot
pnpm --filter @mapi/web test              # Vitest + Playwright smoke (mock-mode production build, chromium)
pnpm --filter @mapi/web e2e               # @e2e specs against a running local stack
```

| Variable | Default | Meaning |
|---|---|---|
| `VITE_API_BASE` | `http://127.0.0.1:8000` | Control-plane base URL. The API must allow this app's origin (CORS, `MAPI_WEB_ORIGIN`). |
| `VITE_PLOTS_ORIGIN` | `http://127.0.0.1:5174` | Origin of `apps/plot-sandbox`; must differ from the app origin. |
| `VITE_STORAGE_ORIGINS` | `http://127.0.0.1:4443` | Origins of signed storage URLs (CSP `connect-src`), comma separated. |
| `VITE_AUTH_BASE` | `VITE_API_BASE` | Where the backend-for-frontend `/auth/*` routes live, when not on the API base. |
| `VITE_TERMS_URL` | `/terms` | The terms of service sign-up links to (`http(s)` URL or same-origin path). |
| `VITE_ACCEPTABLE_USE_URL` | `/acceptable-use` | The acceptable-use policy sign-up links to. |

The Playwright smoke suite serves on 5173/5174. If those ports are taken (a running `make dev`),
use others: `MAPI_SMOKE_WEB_PORT=5293 MAPI_SMOKE_PLOTS_PORT=5294 pnpm --filter @mapi/web test:smoke`.

## Sign-in, projects and permissions (v0.3)

- **Session.** On load the app asks `GET /auth/session`. Signed out, it shows a sign-in screen
  whose button navigates to `/auth/login?return_to=<path>`; the API runs the OIDC
  Authorization Code + PKCE flow and sets an HttpOnly `SameSite=Strict` cookie. The app never
  holds a token. A server without `/auth` (404, the v0.2 single-tenant profile) puts the app in
  a local mode with one `default/default` project and no tenancy headers.
- **Requests.** Every API call sends the cookie (`credentials: "include"`). Project-scoped
  routes (every v0.2 resource, `/v1/tokens`, `/v1/usage`) carry `X-Mapi-Project:
  <tenant>/<project>`; tenant, admin, identity and catalog routes never do. Every mutation
  carries the session's CSRF token in `X-CSRF-Token` (handed out by `/auth/session`). A 401 opens a "session ended" dialog
  that keeps the page (and its unsaved edits) instead of navigating away.
- **Projects.** The header switcher lists `/v1/me` tenants and projects with the caller's
  effective role and remembers the choice per browser. Every project-scoped query key starts
  with `["p", "<tenant>/<project>"]`, and its request names the same project, so two projects'
  caches never mix; switching cancels in-flight requests and returns to the section's list.
- **Permissions.** `src/api/access.gen.json` (generated from `mapi_spec.tenancy.PERMISSIONS`)
  lets the UI disable what the role cannot do and explain why; the server still decides.
- **Problems.** 401, 403, `RATE_LIMITED` and `QUOTA_EXCEEDED` (including the kill switches)
  are shown as plain-language advice with next steps and a `Retry-After` countdown
  (`src/lib/problems.ts`); everything else keeps the technical RFC 7807 view.

In mock mode (`dev:mock`, the smoke suite) the identity provider is a page in the app
(`/auth/login`, `src/mocks/dev-idp.tsx`, never in production builds) offering three personas:
Ana (organisation admin and platform operator, the default), Rui (runner) and Vic (viewer).

## Guided mode (v0.4)

`src/routes/guided.tsx` and `src/features/guided/` implement the task flow of plan §11: templates,
the question flow, business priors and lift tests, the fit, the report and scorecard, what-if
sliders, forecasts and sharing. The user guide is `docs/guides/guided-tutorial.md`.

- **Sessions are specs.** `POST /v1/guided/drafts` stores nothing; the draft becomes a spec when
  the data is uploaded (its `metadata.labels.guided` names the template), and every later step
  writes a revision whose message starts with `Guided:`. Each step is a URL
  (`/guided/<spec>/<step>`) rebuilt from server state: the spec, its dataset, the spec's fit runs
  (`GET /v1/runs?kind=fit&spec_id=…`) and the model version the latest fit produced. Only the
  answers of a session that has not uploaded yet live in `sessionStorage`.
- **Local profiling.** `features/guided/profile.ts` reads the head of the file (8 MB), pivots MFF
  long tables, recognises Meridian tables and sends `ColumnProfile`s only.
- **What-if.** `features/guided/whatif.ts` ports `mapi_spec.whatif.evaluate_scenario`;
  `whatif.test.ts` replays `packages/spec/tests/snapshots/whatif_golden.json`.
- **Persona.** The Guided/Expert choice is stored per user id (`stores/ui.ts`, `personas`);
  `Project.guided_locked` hides the toggle and the Expert navigation for members below admin.
- **Mocks.** `src/mocks/guided.ts` answers every Guided call in mock mode and the smoke suite
  (the templates in `guided-templates.gen.json` come from `mapi_spec.guided`:
  `uv run python apps/web/scripts/gen_guided_fixtures.py [--check]`). A project can be locked
  in mock mode with `localStorage["mapi.mock.guidedLocked"] = "acme/growth"`.

## Workflows and governance (M3.5)

`features/workflows` and `features/governance` hold the M3.5 screens (routes in
`routes/workflows.tsx` and `routes/governance.tsx`); the user guides are the "In the web app"
sections of `docs/guides/workflows.md` and `docs/guides/governance.md`.

- **One document.** The canvas, the node editor and the YAML/JSON view all edit one
  `WorkflowSpec` draft through the pure functions of `features/workflows/doc.ts`; the server
  validates every draft (`POST /v1/workflows:validate`) and its issues are pinned to nodes by
  their JSON pointers. Conditions and `$expr` values are only scanned for node ids, never
  evaluated. Node positions are a per-browser preference (`localStorage`), not part of the
  document.
- **Typed ports.** `ports.ts` mirrors the server's port checks so an incompatible edge is refused
  with its reason before anything is sent; `canvas-model.ts` turns a document into the graph
  (data, gate and "reads" edges) without importing React Flow.
- **React Flow** (`@xyflow/react`, MIT, pinned) loads with `canvas.tsx` only; `vite.config.ts`
  forbids it in the entry closure. Selection flows one way: user selections arrive as `select`
  node changes, the editor's own selection is pushed into the nodes.
- **Runs.** `run-events.ts` follows `GET /v1/workflow-runs/{id}/events` (SSE, `Last-Event-ID`).
  `cron.ts` ports `mapi_spec.cron` for the schedule preview; `workflows-lib.test.ts` replays the
  server's firing times.
- **DTO aliases** for these screens live beside them (`features/workflows/types.ts`,
  `features/governance/types.ts`), built from `schema.d.ts` like `src/api/types.ts`.
- **Mocks.** `src/mocks/workflows.ts`, `src/mocks/governance.ts` and the validator stand-in
  `src/mocks/workflow-validate.ts` (checked against the server's answers for broken documents).
  Their data, `src/mocks/workflows.gen.json`, comes from `mapi_spec` and a real local run of the
  canonical template and two governed fits:
  `uv run python apps/web/scripts/gen_workflow_fixtures.py [--check]`. Mock runs replay that
  run's events, one every `localStorage["mapi.mock.wfStepMs"]` ms (150 by default).

## API client

`openapi.json` drives `src/api/schema.d.ts` (openapi-typescript) and the typed client
(openapi-fetch). `scripts/gen_openapi.py` writes the server's own document (every M1 and M2
operation; the same document a running server serves at `/openapi.json`). It also writes
`src/api/access.gen.json`, the role order and permission matrix the UI uses to hide actions:

```bash
uv run python apps/web/scripts/gen_openapi.py          # regenerate (or --check)
pnpm --filter @mapi/web openapi:types                   # regenerate schema.d.ts
pnpm --filter @mapi/web openapi:fetch                   # take /openapi.json from a running server
```

Code refers to DTOs only through `src/api/types.ts`.

## Security rules the UI follows

- Production builds carry a strict CSP (`csp.ts`): `script-src 'self'`, `connect-src` limited to
  the API and storage origins, `frame-src` the plot sandbox only, and
  `require-trusted-types-for 'script'` with a default policy that admits same-origin script URLs
  only (`src/trusted-types.ts`). Serve the same policy as a header with
  `frame-ancestors 'none'` (as `vite preview` does). The Playwright smoke suite runs this build
  in mock mode and fails on any violation.
- Every Vega-Lite / Plotly plot, first-party ones included, renders in the plot-sandbox iframe
  (`sandbox="allow-scripts"`, separate origin). See `apps/plot-sandbox/README.md` for the
  protocol; the parent side is `src/features/plots/plot-host.ts`.
- Untrusted text (log lines, tracebacks, `untrusted_text`, plugin messages) is rendered as text
  only, with ANSI and control/bidi characters stripped (`src/lib/ansi.ts`). ESLint forbids
  `dangerouslySetInnerHTML` and `innerHTML`.
- Monaco hovers are untrusted markdown (no command links); markers are plain text.
- Artifact downloads are checked against the size and CRC32C in the `ArtifactLink`.
- Uploads go straight to the signed PUT URL; CSV files are previewed locally first and never
  parsed by the server.

## Precomputed views (proposed formats)

The runner writes small JSON views next to datasets and bundles. They are not in `mapi_spec` yet;
`src/lib/views.ts` defines and validates the formats the app reads (unknown formats degrade to
"not available"):

| Object | `format` | Shape |
|---|---|---|
| `preview.json` (dataset) | `mapi.preview/v1` | `columns: [{name, dtype, role: dim\|var}]`, `rows: scalar[][]`, `total_rows` |
| `profile.json` (dataset) | `mapi.profile/v1` | `vars: {name: {dtype, count, missing, min, max, mean, std}}`, `missingness: {along, labels[], coords[], fraction[label][coord]}` |
| `ui/tree_index.json` (bundle) | `mapi.tree-index/v1` | `groups: {path: {dims: {d: n}, vars: {v: {dims, shape, dtype, units?, description?}}, coords: {c: {dims, dtype, size, values?}}, attrs}}` |
| `ui/preprocess.json` (bundle) | `mapi.preprocess/v1` | `steps: [{id, use, kind, stateful, learned, arrays: [{name, dims (≤2), coords, values}]}]` |

Plot data (`ui/plotdata/*.arrow`) and score outputs are Arrow IPC streams decoded in the app
(`src/lib/arrow.ts`) and posted to the sandbox as rows.
