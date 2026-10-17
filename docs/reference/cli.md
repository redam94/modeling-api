# CLI and Python client <span class="mapi-badge">v0.5</span>

Two Apache-2.0 packages send requests to the `/v1` [HTTP API](http-api.md):

- **`mapi-client`** (`mapi_client`): a typed Python client. It has a sync and an async
  version. It uses `httpx`.
- **`mapi-cli`** (`mapi`): the command line. It uses the client, and the SDK for local runs.

The two packages use the Pydantic DTOs from `mapi_spec.api` for each request and response.
For errors, they raise `mapi_spec.errors.MapiError` subclasses. They make these subclasses from
RFC 7807 problem documents.

This page is a short summary. The guides give tested examples for each package
([command line](../guides/cli.md), [Python client](../guides/python-client.md)). The
[CLI reference](generated/cli-reference.md) shows the `--help` text of each command. The
installed CLI makes that page.

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
`score(ds) -> xr.Dataset` with inline XrJSON). `api.signup()` and `api.verify_email()` are
public calls. The client sends them without credentials.
`mapi_client.webhooks.verify(headers, body, secret)` does a check of the `Mapi-Signature` of a
delivery ([Receiving webhooks](../guides/webhook-receivers.md)). `mapi_client.xrjson` encodes
and decodes inline payloads. The platform optimises budgets
([Optimise a budget](../tutorials/optimise-a-budget.md)).
Each operation in the endpoint table has a request builder. The tests of the client do a check
of each builder against the contract.

**v0.5.** `api.workflows.stream_events(run_id)` follows the server-sent events of a workflow
run. It gives these items:

- `WorkflowSnapshot`: the run with all its nodes. It is the first item on each connection.
- `WorkflowEventFrame`: `workflow.node.*`, `workflow.branch.decided` and other events. Its `id`
  is the `seq` of the event.
- `WorkflowEnd`.

If the connection stops, the client continues with `Last-Event-ID`. It does not give an event
two times.

The client accepts a workflow document as a `WorkflowSpec`, a mapping, YAML text or a file
path (`api.workflows.validate(Path("wf.yaml"))`). `upsert` adds a revision only when the hash
of the document changed. `models.optimize` sends a keyed `Prefer: wait` request again until
the plan is ready. `datasets.explore`, `models.explore` and `models.compare` wait for the
explore run. `explorations.view(exploration, section)` downloads one view.

`catalog.recommend(goal=..., data_shape=[...])` does a check of the answers against the
questions of the chooser before it sends them. The client does not send
`validations.decide` (and `approve`, `reject`) again after a lost response. Reviewers sign in
(`mapi login`), because an API token cannot approve. If a workflow run fails, `wait` and
`run(wait=True)` raise the error of the node that failed. With `raise_on_failure=False`, they
return the run.

- `AsyncClient` has the same resources with `await` and `async for`.
- List calls return lazy paginators that follow `next_cursor`.
- If a call accepts an `Idempotency-Key` and you do not give one, the client makes a new
  UUID. The client tries idempotent calls again after 502, 503, 504 and connection errors. It
  obeys `Retry-After`.
- For `429 RATE_LIMITED`, the client waits (`Retry-After`, then exponential back-off). Then it
  sends the request again, for all methods. This is safe because the server refused the
  request before it did an action. After `RetryPolicy.rate_limit_retries` attempts, the client
  raises `RateLimited`.
- `429 QUOTA_EXCEEDED` raises `QuotaExceeded` immediately. `retry_after_of(exc)` gives the
  number of seconds until the quota resets (v0.3).
- `api.tokens.create` returns the secret of the token one time (`TokenCreated.secret`). The
  secret is not in the `repr`. The client does not send the call again after a lost response.
  Thus it cannot make two tokens.
- `datasets.upload` accepts a path, bytes or an `xr.Dataset` (with the `xarray` extra). It
  makes the dataset if necessary. Then it PUTs the file to the signed URL and starts the ingest
  run. Then it waits until the version is ready. If a dataset has its raw schema in the
  `mapi_schema` attribute, the dataset keeps its dim policies and roles.
- `runs.stream_events` gives `RunSnapshot`, `RunEventFrame` (persisted events, with an id),
  `RunLiveFrame` (live `progress` and `log` frames, without an id) and at the end `RunEnd`.
- `models.score` sends `Prefer: wait=N`. While the run continues, the client sends the same
  request again with the same `Idempotency-Key`. The idempotent replay of the server returns
  the completed `ScoreCallResult` with new output links.
- The client gets signed storage URLs without the `Authorization` header of the API. It does a
  check of each transfer against the CRC32C and size that the storage calculated.

**Configuration.** The client uses the first value that it finds in this sequence:

1. Arguments.
2. `MAPI_URL` (fallback `MAPI_API_URL`), `MAPI_TOKEN`, `MAPI_PROJECT` and `MAPI_PROFILE`.
3. A profile in `~/.config/mapi/config.toml` (`$MAPI_CONFIG`, `$XDG_CONFIG_HOME`).
4. `http://127.0.0.1:8000`.

The client does not send a token over plain HTTP to a host that is not a loopback host. v0.2
has no authentication.

**Credentials and project (v0.3).** The client authenticates with an API token (argument,
`MAPI_TOKEN` or the `token` of the profile). If there is no token, the client uses the OpenID
Connect sign-in from `mapi login`. `mapi login` keeps this sign-in for the profile in
`credentials.json` (mode `0600`, next to the config file, or `$MAPI_CREDENTIALS`). The client
uses a stored sign-in only for the API URL of that sign-in. The client renews the ID token with
the refresh token a short time before it expires, and after a `401`.

The client sends `project` (`Client(project=…)`, `MAPI_PROJECT`, the `project` of the profile)
as `X-Mapi-Project: <tenant>/<project>`. It sends this header only on project-scoped requests.
It does not send it on identity, tenant and operator routes, or when the path names the
project.

| File | Mode | Holds |
|---|---|---|
| `~/.config/mapi/config.toml` | `0600` when `mapi` writes it | Profiles: `url`, `project`, `token`, `timeout`, `max_retries`, `verify`, `oidc_issuer`, `oidc_client_id`, `oidc_client_secret` |
| `~/.config/mapi/credentials.json` | `0600` | For each profile: the API URL, issuer, client id, ID token, refresh token and expiry of a `mapi login` sign-in |

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

Multi-tenant commands (v0.3). [Authentication](../guides/authentication.md#the-command-line)
and [Administration](../guides/administration.md) show these commands step by step. The test
suite runs the two guides:

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
| `login` | Browser sign-in: authorization code + PKCE (S256) with a loopback redirect to `127.0.0.1:<port>/callback`. The CLI does a check of `state` and `nonce`. `--device`: the device authorization grant. `--with-token`: an API token from stdin. The CLI does a check of its checksum and verifies it with `GET /v1/me`. The CLI saves `--url`, `--issuer` and `--client-id` in the profile |
| `whoami` | `GET /v1/me`: the principal, how you signed in, the operator flag, and the tenants and projects with roles |
| `use` | Does a check of membership with `GET /v1/me`. If only one tenant has a bare project id, the CLI adds the tenant to it. The CLI saves `project` in the profile. A token acts only in its own project |
| `logout` | Removes the sign-in and token of the profile. It also revokes the refresh token at the identity provider. `--revoke` also revokes the token on the server |
| `tokens create` | Prints the secret one time, alone on stdout. With `--save`, the CLI keeps it in the profile. `--json` includes it, because you asked for it |
| `usage` | The exact cost (micro-units, with the name of the price table), vCPU-seconds and GiB-seconds, groups, and the use and reset time of each quota |
| `runs get`, `runs ls` | Show the metered cost of the run (`~` while it is an estimate) |
| `admin ...` | Each change prints a before/after table on stderr and asks you to confirm. `--yes` skips this step. `quotas set` replaces the full policy |

Guided mode and integrations (v0.4). The
[command-line guide](../guides/cli.md#endpoints-webhooks-and-sharing) shows these commands step
by step:

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
| `endpoints promote`, `endpoints rollback` | Show the current version and ask you first (`--yes` skips this step). `--override` needs `--reason` |
| `webhooks create`, `webhooks rotate` | Print the signing secret one time, alone on stdout. `rotate` and `rm` ask you first |
| `webhooks update` | `--enable` turns on a webhook that is paused or that the server disabled. It also sets the failure count to zero. `-e` replaces the event list |
| `plots embed` | Prints the embed URL one time. The URL contains the token. `revoke` asks you first |
| `guided detect`, `guided draft --csv` | Profile the CSV file locally and send only summaries of the columns |
| `signup`, `verify-email` | Public calls. The CLI sends no credentials |
| `tenant delete` | Shows the data that the server purges and the data that it keeps. Then it asks you to type the tenant id (`--confirm TENANT` in scripts) |

Workflows, governance, explorers and the chooser (v0.5). The
[command-line guide](../guides/cli.md#workflows-governance-and-explorers) and the
[workflow cookbook](../guides/workflow-cookbook.md) show these commands step by step:

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
| `workflows run --watch`, `workflows watch` | Shows a live board of the nodes. The SSE stream supplies the data. On a terminal, the CLI draws the board again for each change. Otherwise, it prints one line for each change. Then it prints the summary. The exit status is 1 if the run failed or was cancelled. `--json`: one JSON event on each line |
| `workflows apply` | Makes the workflow that `metadata.name` names, or saves the file as the next revision. If the file did not change, the CLI adds nothing |
| `workflows init` | Writes a gallery template as YAML with your name and project. `--offline` uses the templates that come with the CLI |
| `-i`, `-p` | `name=value`. If a `-p` value parses as JSON, the CLI uses it as JSON (`-p 'horizons=[1,4]'`) |
| `validate model` | `MODEL_REF` is `model://project/name@vN`, `name@vN`, `name` with `-v N`, or a model id. The CLI waits for the checks. Then it prints each result with its plain message and suggested action. The exit status is 1 on `checks_failed` or `rejected` |
| `approve` | Shows the validation and asks you first (`--yes` skips this step). `--reject` needs `--comment`. The server refuses the submitter (`SELF_APPROVAL_FORBIDDEN`) and API tokens |
| `governance policies retire`, `validations deprecate`, `workflows rm` | Ask you first (`--yes` skips this step) |
| `explore …` | Waits for the explore run. `--out DIR` writes each view as JSON. `--show SECTION` prints one view |
| `optimize` | Prints the plan (the current spend and the recommended spend, the return of the next unit, notes), the uplift and its probability, and the frontier |
| `choose` | Asks the questions of the chooser, or uses the `-a` answers and asks nothing. Then it prints the ranking with reasons, cautions and the top About cards |

Errors: for `QUOTA_EXCEEDED` and `RATE_LIMITED`, the CLI prints when to try again
(`retry after 1h 30m`). With `--json`, the error document contains `retry_after_seconds`. No
command prints a token. Tokens show as `mapi_<prefix>_…`.

| Template (`mapi new`) | What it is | Conformance fixture |
|---|---|---|
| `geo-mmm` | Geo marketing-mix model on mmm-framework (weekly EUR, adstock, saturation) | E33 |
| `forecast` | HSGP trend + seasonality forecast | C08 |
| `tabular` | XGBoost demand with quantile uncertainty | `xgb_store_demand` |
| `glm` | Hierarchical GLM, long layout | E02 |

- `--remote @` means "the configured API". `--remote URL` overrides it for one command.
- `--json` prints one JSON document on stdout. `runs watch --json` prints one JSON event on
  each line. Errors become `{"error": {...}}` with exit status 1. Usage errors have exit
  status 2.
- Local `fit` and `score` run plugins in the same process (isolation `NONE`). Use them only
  with code and data that you trust.
- The CLI removes ANSI sequences, control sequences and bidi overrides from server, plugin and
  log text before it prints the text.

### `mapi serve`

`mapi serve` runs `mapi-server dev`. This command runs the database migrations. Then it runs
the API and the orchestrator (dispatcher, reconciler, finalizer and event pump) in one process.
If `DATABASE_URL` is not set, Postgres is the embedded `pgserver`. If nothing listens at
`MAPI_GCS_ENDPOINT`, the command starts the [GCS shim](gcs-shim.md) in the same process. Runs
use the `local-process` executor with the real `mapi-runner`. To also run the web app, use
`make dev` ([Local development](../operations/local-development.md)).
