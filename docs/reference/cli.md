# CLI and Python client <span class="mapi-badge">v0.5</span>

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
| `admin` (v0.3, operators) | `tenants`, `tenant`, `create_tenant`, `update_tenant`, `quotas`, `set_quota`, `delete_quota`, `kill_switches`, `set_kill_switches`, `executors`, `executor`, `audit`; v0.4: `signups`, `review_signup`, `set_verification`, `retry_deletion` |
| `guided` (v0.4) | `templates`, `template`, `detect_roles`, `draft`, `compile`, `translate_priors`, `prior_translation`, `wait_prior_translation`, `preview_priors` |
| `endpoints` (v0.4) | `list`, `get`, `promote`, `delete`, `history`, `rollback`, `schema`, `validate`, `submit`, `score`, `snippets` |
| `webhooks` (v0.4) | `list`, `create`, `get`, `update`, `delete`, `rotate_secret`, `deliveries`, `replay` |
| `plots` sharing (v0.4) | `save`, `saved`, `get_saved`, `delete_saved`, `render`, `render_status`, `render_output`, `download_render`, `create_embed`, `embeds`, `revoke_embed`, `embed` |
| `tenants` trust (v0.4) | `verification`, `request_deletion`, `deletion`, `cancel_deletion`, `export`, `export_status`, `wait_export`, `download_export` |
| `catalog` About cards (v0.5) | `abouts`, `about`, `chooser`, `recommend`, `workflow_nodes`, `workflow_node`, `governance_checks` |
| `datasets` About and explorer (v0.5) | `about`, `describe`, `explore` |
| `models` explorers and plans (v0.5) | `explore`, `compare`, `optimize`, `optimization`, `validate`, `validations` |
| `explorations` (v0.5) | `get`, `wait`, `link`, `view` |
| `workflows` (v0.5) | `list`, `find`, `create`, `validate`, `get`, `update`, `delete`, `revisions`, `revision`, `revise`, `upsert`, `run`, `runs`, `get_run`, `wait`, `cancel`, `stream_events`, `events`, `nodes`, `node`, `output_link`, `download_output`, `schedules`, `schedule`, `update_schedule`, `delete_schedule`, `triggers`, `trigger`, `delete_trigger`, `templates`, `template` |
| `governance` (v0.5) | `checks`, `policies`, `policy`, `create_policy`, `update_policy`, `retire_policy`, `policy_versions` (`tenant_id=` for tenant-wide policies) |
| `validations` (v0.5) | `request`, `list`, `for_model`, `get`, `wait`, `evidence`, `cancel`, `resubmit`, `decide`, `approve`, `reject`, `deprecate`, `evidence_pack`, `download_evidence_pack` |

**v0.4.** `api.endpoint(alias)` is a `RemoteEndpoint` (`schema()`, `validate(ds)`,
`score(ds) -> xr.Dataset` with inline XrJSON); `api.signup()` and `api.verify_email()` are
public calls sent without credentials. `mapi_client.webhooks.verify(headers, body, secret)`
checks a delivery's `Mapi-Signature` ([Receiving webhooks](../guides/webhook-receivers.md));
`mapi_client.xrjson` encodes and decodes inline payloads; budgets are optimised by the
platform ([Optimise a budget](../tutorials/optimise-a-budget.md)).
Every operation of the endpoint table has a request builder, checked against the
contract by the client's tests.

**v0.5.** `api.workflows.stream_events(run_id)` follows a workflow run's server-sent events:
`WorkflowSnapshot` (the run with every node, first on each connection),
`WorkflowEventFrame` (`workflow.node.*`, `workflow.branch.decided`, ...; its `id` is the
event's `seq`) and `WorkflowEnd`; a dropped connection resumes with `Last-Event-ID` and no
event is yielded twice. Workflow documents are accepted as a `WorkflowSpec`, a mapping, YAML
text or a file path (`api.workflows.validate(Path("wf.yaml"))`); `upsert` adds a revision only
when the document's hash changed. `models.optimize` repeats a keyed `Prefer: wait` request
until the plan is ready; `datasets.explore`, `models.explore` and `models.compare` wait for
the explore run and `explorations.view(exploration, section)` downloads one view.
`catalog.recommend(goal=..., data_shape=[...])` checks the answers against the chooser's
questions before sending. `validations.decide` (and `approve`, `reject`) is never resent
after a lost response; reviewers sign in (`mapi login`), since no API token can approve.
Failed workflow runs raise the failing node's error from `wait` and `run(wait=True)`
(`raise_on_failure=False` returns the run instead).

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

Multi-tenant commands (v0.3; walked through in [Authentication](../guides/authentication.md#the-command-line)
and [Administration](../guides/administration.md), both run by the test suite):

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

Guided mode and integrations (v0.4; walked through in the
[command-line guide](../guides/cli.md#endpoints-webhooks-and-sharing)):

```bash
mapi endpoints ls | get ALIAS | history ALIAS | schema ALIAS [--example] | snippets ALIAS [-l LANG]
mapi endpoints promote ALIAS MODEL_REF [--override --reason TEXT] [--expected-revision N]
mapi endpoints rollback ALIAS --reason TEXT [--to-revision N]
mapi endpoints score ALIAS [CALL.json | --example] [--mode M] [--periods N] [--data FILE|REF] [-o OUT]
mapi webhooks ls | get ID | create URL -e EVENT... [--inactive] | rotate ID | deliveries ID [--status S]
mapi webhooks update ID [--enable|--disable] [-e EVENT...] [--url URL] [--description TEXT]
mapi webhooks replay ID DELIVERY_ID | rm ID
mapi plots render (--saved ID | --model M --version V --plot P | --spec FILE) [-f png|svg|pdf] [-o OUT]
mapi plots embed PLOT_ID [--expires-in-days N] [--allow-origin URL] | embeds PLOT_ID | revoke PLOT_ID EMBED_ID
mapi guided templates [ID] | detect ID (--csv FILE | --dataset REF) | draft ID --name N --dataset D ...
mapi priors preview --beliefs FILE (--spec ID --revision N | --document FILE) [--lift-tests FILE]
mapi signup --email E --name N [--accept-terms] | verify-email TOKEN
mapi tenant status | export [-o FILE] | delete [--confirm TENANT] [--cancel]
```

| Command | Notes |
|---|---|
| `endpoints promote`, `endpoints rollback` | Show the current version and ask first (`--yes` skips); `--override` needs `--reason` |
| `webhooks create`, `webhooks rotate` | Print the signing secret once, alone on stdout; `rotate` and `rm` ask first |
| `webhooks update` | `--enable` switches a paused or automatically disabled webhook back on and resets its failure count; `-e` replaces the event list |
| `plots embed` | Prints the embed URL (it carries the token) once; `revoke` asks first |
| `guided detect`, `guided draft --csv` | Profile the CSV locally and send only column summaries |
| `signup`, `verify-email` | Public calls: no credentials are sent |
| `tenant delete` | Lists what is purged and kept and asks you to type the tenant id (`--confirm TENANT` in scripts) |

Workflows, governance, explorers and choosing (v0.5; walked through in the
[command-line guide](../guides/cli.md#workflows-governance-and-explorers) and the
[workflow cookbook](../guides/workflow-cookbook.md)):

```bash
mapi workflows templates | template ID [--expert] | init ID [--name N] [--into PROJECT] [-o FILE] [--offline]
mapi workflows validate FILE | apply FILE [-m MSG] | create [FILE | --template ID] | ls [--archived]
mapi workflows get WF [--yaml] [-r N] | revisions WF | archive WF [--undo] | rm WF [--yes]
mapi workflows run WF [-i NAME=REF]... [-p NAME=VALUE]... [-r N] [--no-cache] [--watch]
mapi workflows watch RUN [--from N] | status RUN | runs WF [--status S] | nodes RUN | node RUN NODE
mapi workflows output RUN NODE PORT -o FILE | cancel RUN | nodes-catalog [--kind K] [--about ID]
mapi workflows schedules ls WF | add WF CRON [--tz TZ] [-i ...] [-p ...] | pause | resume | rm WF ID
mapi workflows triggers ls WF | add WF DATASET [--input NAME] [--debounce S] | rm WF ID
mapi governance checks [--about ID] [--expert]
mapi governance policies ls [--scope tenant|project] [--tenant T] | get ID | versions ID
mapi governance policies create FILE [--tenant T] | update ID FILE [--expected-version N] | retire ID
mapi validate model MODEL_REF [-v N] [--policy ID]... [--note TEXT] [--no-wait] | workflow FILE
mapi validations ls [--queue | --status S] | get ID | evidence ID | cancel ID | resubmit ID
mapi validations deprecate ID --reason TEXT | pack ID -o evidence.html|evidence.json
mapi approve VALIDATION [--reject] [--comment TEXT] [--yes]
mapi explore dataset REF [-s SECTION]... [--kpi V] [--facet-by D] [--max-lag N] [-o DIR] [--show S]
mapi explore model REF [-s SECTION]... [--horizon N] [--compare-with REF] [-o DIR] [--show S]
mapi explore compare REF REF... [--horizon N] | get RUN
mapi optimize MODEL_REF [--budget N | --change-pct P] [--channel C='{...}'] [--objective O]
              [--frontier N] [--by-geo] [--request FILE]
mapi choose [-a QUESTION=ANSWER[,ANSWER]]... [--kind K] [-n N] [--about N] [--expert]
mapi about ID [--kind K] [--expert]
```

| Command | Notes |
|---|---|
| `workflows run --watch`, `workflows watch` | A live node board fed by the SSE stream (redrawn on a terminal, one line per change otherwise), then the summary; exit status 1 when the run failed or was cancelled. `--json`: one JSON event per line |
| `workflows apply` | Creates the workflow named by `metadata.name`, or saves the file as the next revision; an unchanged file adds nothing |
| `workflows init` | Writes a gallery template as YAML under your name and project (`--offline` uses the templates bundled with the CLI) |
| `-i`, `-p` | `name=value`; `-p` values are JSON when they parse (`-p 'horizons=[1,4]'`) |
| `validate model` | `MODEL_REF` is `model://project/name@vN`, `name@vN`, `name` with `-v N` or a model id; waits for the checks and prints each result with its plain message and suggested action; exit status 1 on `checks_failed` or `rejected` |
| `approve` | Shows the validation and asks first (`--yes` skips); `--reject` needs `--comment`; refused for the submitter (`SELF_APPROVAL_FORBIDDEN`) and for API tokens |
| `governance policies retire`, `validations deprecate`, `workflows rm` | Ask first (`--yes` skips) |
| `explore …` | Waits for the explore run; `--out DIR` writes every view as JSON, `--show SECTION` prints one |
| `optimize` | Prints the plan (current vs recommended spend, the next unit's return, notes), the uplift and its probability, and the frontier |
| `choose` | Asks the chooser's questions (or takes `-a` answers and asks nothing) and prints the ranking with reasons, cautions and the top About cards |

Errors: `QUOTA_EXCEEDED` and `RATE_LIMITED` print when to retry (`retry after 1h 30m`); with
`--json` the error document carries `retry_after_seconds`. No command prints a token: they
appear as `mapi_<prefix>_…`.

| Template (`mapi new`) | What it is | Conformance fixture |
|---|---|---|
| `geo-mmm` | Geo marketing-mix model on mmm-framework (weekly EUR, adstock, saturation) | E33 |
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
