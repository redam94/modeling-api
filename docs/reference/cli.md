# CLI and Python client <span class="mapi-badge">v0.2</span>

Two Apache-2.0 packages talk to the `/v1` [HTTP API](http-api.md):

- **`mapi-client`** (`mapi_client`): a typed Python client, sync and async, built on `httpx`;
- **`mapi-cli`** (`mapi`): the command line, built on the client and on the SDK for local runs.

Both use the Pydantic DTOs from `mapi_spec.api` for every request and response, and raise
`mapi_spec.errors.MapiError` subclasses rebuilt from RFC 7807 problem documents.

This page is a compact summary. The guides walk through each with tested examples
([command line](../guides/cli.md), [Python client](../guides/python-client.md)), and the
[CLI reference](generated/cli-reference.md) lists every command's `--help`, generated from the
installed CLI.

## Python client

```python
from mapi_client import Client

with Client() as api:  # MAPI_URL / MAPI_TOKEN / ~/.config/mapi/config.toml / 127.0.0.1:8000
    version = api.datasets.upload("eu_media.zarr.zip", dataset="eu_media_daily")
    rev = api.specs.upsert(open("spec.yaml").read())
    run = api.runs.fit(rev.spec_id, rev.revision)
    for item in api.runs.stream_events(run.id):  # SSE, resumes with Last-Event-ID
        print(item)
    run = api.runs.wait(run.id, raise_on_failure=True)
    result = api.models.score(
        model_id,
        1,
        {
            "model": "model://default/eu-geo-mmm@v1",
            "mode": "forecast",
            "horizon": {"dim": "time", "periods": 8},
        },
    )
    result.tree  # xr.DataTree with the `xarray` extra
```

| Resource | Calls |
|---|---|
| `projects` | `list`, `get` |
| `catalog` | `get`, `models`, `model`, `steps`, `plots`, `schema` |
| `datasets` | `create`, `list`, `get`, `find`, `ensure`, `create_upload`, `create_version`, `versions`, `version`, `wait_version`, `upload`, `artifact`, `download` |
| `specs` | `validate`, `create`, `list`, `get`, `find`, `revise`, `upsert`, `revisions`, `revision`, `dry_run` |
| `runs` | `create`, `fit`, `get`, `list`, `wait`, `stream_events`, `events`, `logs`, `plots`, `cancel`, `resume`, `retry` |
| `models` | `list`, `get`, `versions`, `version`, `artifact`, `download`, `score` |
| `plots` | `list`, `get`, `validate` |
| `tenants` (v0.3) | `list`, `get`, `rename`, `projects`, `create_project`, `update_project`, `delete_project`, `members`, `add_member`, `set_role`, `remove_member`, `usage`, `audit` |
| `tokens` (v0.3) | `list`, `create`, `get`, `revoke` |
| `usage` (v0.3) | `get` (the project), `tenant` |
| `admin` (v0.3, operators) | `tenants`, `tenant`, `create_tenant`, `update_tenant`, `quotas`, `set_quota`, `delete_quota`, `kill_switches`, `set_kill_switches`, `executors`, `executor`, `audit` |

- `AsyncClient` has the same resources with `await` and `async for`.
- List calls return lazy paginators that follow `next_cursor`.
- Calls that accept an `Idempotency-Key` get a fresh UUID unless you pass one. Idempotent
  calls are retried on 502, 503, 504 and connection errors, honouring `Retry-After`.
- `429 RATE_LIMITED` is waited out (`Retry-After`, then exponential back-off) and resent for
  every method, because the server refused it before acting; `RateLimited` is raised after
  `RetryPolicy.rate_limit_retries` attempts. `429 QUOTA_EXCEEDED` raises `QuotaExceeded` at
  once, and `retry_after_of(exc)` gives the seconds until the quota resets (v0.3).
- `api.tokens.create` returns the token's secret once (`TokenCreated.secret`, kept out of
  `repr`) and is never resent after a lost response, so it cannot mint two tokens.
- `datasets.upload` accepts a path, bytes or an `xr.Dataset` (with the `xarray` extra). It
  creates the dataset if needed, PUTs the file to the signed URL, starts the ingest run and
  waits for the version to be ready. A dataset that carries its raw schema in the
  `mapi_schema` attribute keeps its dim policies and roles.
- `runs.stream_events` yields `RunSnapshot`, `RunEventFrame` (persisted events, with an id),
  `RunLiveFrame` (live `progress` and `log` frames, without an id) and finally `RunEnd`.
- `models.score` sends `Prefer: wait=N`. While the run is still going it repeats the same
  request with the same `Idempotency-Key`; the server's idempotent replay returns the finished
  `ScoreCallResult` with fresh output links.
- Signed storage URLs are fetched without the API's `Authorization` header, and every
  transfer is checked against the storage-computed CRC32C and size.

**Configuration.** Arguments beat `MAPI_URL` (fallback `MAPI_API_URL`), `MAPI_TOKEN`,
`MAPI_PROJECT` and `MAPI_PROFILE`, which beat a profile in `~/.config/mapi/config.toml`
(`$MAPI_CONFIG`, `$XDG_CONFIG_HOME`), which beats `http://127.0.0.1:8000`. A token is never
sent over plain HTTP to a non-loopback host. v0.2 has no authentication.

**Credentials and project (v0.3).** The client authenticates with an API token (argument,
`MAPI_TOKEN` or the profile's `token`) or, when none is set, with the OpenID Connect sign-in
`mapi login` stored for the profile in `credentials.json` (mode `0600`, next to the config
file, or `$MAPI_CREDENTIALS`). A stored sign-in is used only for the API URL it was made for;
its ID token is renewed with the refresh token shortly before it expires and after a `401`.
`project` (`Client(project=…)`, `MAPI_PROJECT`, the profile's `project`) is sent as
`X-Mapi-Project: <tenant>/<project>` on project-scoped requests only: not on identity, tenant
and operator routes, nor where the path already names the project.

| File | Mode | Holds |
|---|---|---|
| `~/.config/mapi/config.toml` | `0600` when written by `mapi` | Profiles: `url`, `project`, `token`, `timeout`, `max_retries`, `verify`, `oidc_issuer`, `oidc_client_id`, `oidc_client_secret` |
| `~/.config/mapi/credentials.json` | `0600` | Per profile: the API URL, issuer, client id, ID token, refresh token and expiry of a `mapi login` sign-in |

## Command line

```bash
mapi new geo-mmm -o spec.yaml --with-data   # template + tiny sample data (needs mapi-conformance)
mapi check spec.yaml --data data.zarr.zip   # full static validation against installed plugins
mapi fit spec.yaml --data data.zarr.zip \
    --side-table fx=fx.zarr.zip --side-table holidays=holidays.zarr.zip   # in-process fit
mapi score request.yaml --bundle bundles/geo-mmm --data future.zarr.zip -o result.zarr.zip

mapi serve                                  # the local stack in one process (see below)
mapi fit spec.yaml --data data.zarr.zip --remote @   # upload, create/revise spec, fit, follow
mapi runs ls --state running | get RUN | logs RUN | watch RUN | cancel RUN | retry RUN
mapi datasets ls | upload file.csv --name sales --mapping mapping.yaml
mapi score request.yaml --remote @ --model MODEL_ID --version 1 -o result.zarr.zip
mapi catalog [models|steps|plots] [--remote @]

mapi plugin init acme-demand && mapi plugin test acme-demand   # scaffold + contract kit
mapi spec upgrade spec.yaml --check
mapi config show | path | set KEY VALUE | unset KEY
```

Multi-tenant commands (v0.3):

```bash
mapi login [--device | --no-browser | --with-token] [--issuer URL --client-id ID]
mapi whoami
mapi use [TENANT/PROJECT | --unset]          # --project / MAPI_PROJECT for one command
mapi logout [--revoke]
mapi tokens ls [--all] | get TOKEN | create NAME -s SCOPE... [--save] | revoke TOKEN
mapi usage [--period day|month] [--start DATE] [--group-by KEY] [--tenant TENANT]
mapi admin tenants ls | get | create | update     # operators; changes ask first (--yes)
mapi admin quotas ls | set SCOPE SUBJECT --LIMIT... | rm SCOPE SUBJECT
mapi admin kill-switches get | set --reason TEXT [--untrusted-code on|off] [--spend-cap N]
                                  [--cancel-untrusted-runs]  # with --untrusted-code off
mapi admin executors ls | get NAME | disable NAME --reason TEXT | enable NAME --reason TEXT
mapi admin audit [--tenant] [--action] [--since] ...
```

| Command | Notes |
|---|---|
| `login` | Browser sign-in: authorization code + PKCE (S256) with a loopback redirect to `127.0.0.1:<port>/callback`, `state` and `nonce` checked. `--device`: the device authorization grant. `--with-token`: an API token from stdin, checksum-checked and verified with `GET /v1/me`. `--url`, `--issuer` and `--client-id` are saved in the profile |
| `whoami` | `GET /v1/me`: principal, how you signed in, operator flag, tenants and projects with roles |
| `use` | Checks membership with `GET /v1/me` (a bare project id is qualified when one tenant has it) and saves `project` in the profile; a token only acts in its own project |
| `logout` | Forgets the profile's sign-in (revoking its refresh token at the identity provider) and token; `--revoke` also revokes the token on the server |
| `tokens create` | Prints the secret once, alone on stdout (or `--save` stores it in the profile); `--json` includes it, as requested |
| `usage` | Exact cost (micro-units, price table named), vCPU- and GiB-seconds, groups, and each quota's use and reset time |
| `runs get`, `runs ls` | Show the run's metered cost (`~` while estimated) |
| `admin ...` | Every change prints a before/after table on stderr and asks for confirmation; `--yes` skips it. `quotas set` replaces the whole policy |

Errors: `QUOTA_EXCEEDED` and `RATE_LIMITED` print when to retry (`retry after 1h 30m`); with
`--json` the error document carries `retry_after_seconds`. No command prints a token: they
appear as `mapi_<prefix>_…`.

| Template (`mapi new`) | What it is | Conformance fixture |
|---|---|---|
| `geo-mmm` | Hierarchical geo MMM (weekly EUR, adstock, saturation) | E33 |
| `forecast` | HSGP trend + seasonality forecast | C08 |
| `tabular` | XGBoost demand with quantile uncertainty | `xgb_store_demand` |
| `glm` | Hierarchical GLM, long layout | E02 |

- `--remote @` means "the configured API"; `--remote URL` overrides it for one command.
- `--json` prints one JSON document on stdout (`runs watch --json` prints one JSON event per
  line). Errors become `{"error": {...}}` with exit status 1; usage errors exit 2.
- Local `fit` and `score` run plugins in-process (isolation `NONE`): use them only with code
  and data you trust. Server, plugin and log text is printed with ANSI and control sequences
  and bidi overrides removed.

### `mapi serve`

`mapi serve` runs `mapi-server dev`: database migrations, then the API and the orchestrator
(dispatcher, reconciler, finalizer and event pump) in one process. Postgres is the embedded
`pgserver` unless `DATABASE_URL` is set, and the [GCS shim](gcs-shim.md) is started in-process
when nothing listens at `MAPI_GCS_ENDPOINT`. Runs execute through the `local-process`
executor with the real `mapi-runner`. For the web app as well, use `make dev`
([Local development](../operations/local-development.md)).
