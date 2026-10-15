# @mapi/web

The modeling-api web app: Expert mode for M1 (v0.2) plus the Guided-mode shell (task flows land
in v0.4). React 19, TypeScript (strict), Vite, TanStack Router/Query/Table, Zustand for UI-only
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

## API client

`openapi.json` drives `src/api/schema.d.ts` (openapi-typescript) and the typed client
(openapi-fetch). Until the server publishes its own document, `scripts/gen_openapi.py` builds it
from `mapi_spec.api.M1_ENDPOINTS` and the Pydantic DTO schemas with the same machinery FastAPI
uses, so component names match:

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
