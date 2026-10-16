---
description: The mapi command line - scaffold, check and fit specs in your own process, or upload data, fit, follow and score on a running platform.
---

# The command line <span class="mapi-badge">v0.2</span>

`mapi` (package `mapi-cli`, Apache-2.0) works in two places:

- **locally**, without any server: scaffold a spec, check it, fit it and score it in your own
  Python process with the installed plugins (`new`, `check`, `fit`, `score`, `catalog`,
  `plugin`, `spec`);
- **remotely**, against the [HTTP API](http-api.md) of a running platform: upload datasets,
  fit, follow runs and score model versions (`datasets`, `runs`, and `--remote` on `check`,
  `fit`, `score` and `catalog`).

Every command and option is listed in the generated [CLI reference](../reference/generated/cli-reference.md).
The examples on this page are scripts that the test suite runs: the local ones in the default
suite, the remote ones in `make e2e`.

!!! note "Running the commands"
    `make install` puts `mapi` into the workspace environment. Either activate it
    (`source .venv/bin/activate`) or prefix every command with `uv run`
    (`uv run mapi --help`).

## Without a server

**Scaffold** a spec from a template. `--with-data` also writes the matching conformance
fixture's sample data (it needs the `mapi-conformance` package, which the workspace has):

```bash
--8<-- "cli_local.sh:new"
```

| Template | What it is | Conformance fixture |
|---|---|---|
| `geo-mmm` | Hierarchical geo MMM (weekly EUR, adstock, saturation) | E33 |
| `forecast` | HSGP trend and seasonality forecast | C08 |
| `tabular` | XGBoost demand with quantile uncertainty | `xgb_store_demand` |
| `glm` | Hierarchical GLM, long layout | E02 |

**Check** it statically against the installed plugins and the data's schema. `--stages`
prints the schema after every pipeline step; the exit status is 1 when there are errors, so
it works in CI:

```bash
--8<-- "cli_local.sh:check"
```

**Fit** in-process. The bundle (zarr zip, sidecars, plots, metrics and the evaluation bundle
of `evaluate_then_refit`) goes to `bundles/<name>/`; `--side-table NAME=PATH` supplies side
tables:

```bash
--8<-- "cli_local.sh:fit"
```

`mapi score request.yaml --bundle bundles/<name> --data future.zarr.zip -o result.zarr.zip`
scores a local bundle with a `ScoreRequest` document.

!!! warning "Local means trusted"
    Local `fit` and `score` import and run the plugins in your own process (isolation
    `NONE`), exactly like a notebook. Use them with code and data you trust. On the platform,
    the same work runs in a sandbox.

**Inspect** the catalog of installed plugins, and upgrade spec documents across `apiVersion`s:

```bash
--8<-- "cli_local.sh:catalog"
--8<-- "cli_local.sh:upgrade"
```

`mapi plugin init NAME` scaffolds a plugin package (model, entry point, contract-kit tests)
and `mapi plugin test NAME` runs its tests; see [Write a plugin](plugins.md).

## Against a running platform

Start the stack first (`make dev`, then `make seed`; see the [quickstart](../getting-started/quickstart.md)).

### Choosing the API

Every remote command resolves the API the same way as the Python client, first match wins:

1. `--url` and `--token` (global options, before the command: `mapi --url http://… runs ls`);
2. `MAPI_URL` (falling back to `MAPI_API_URL`, the name `make dev` uses), `MAPI_TOKEN` and
   `MAPI_PROFILE`;
3. the selected profile in `~/.config/mapi/config.toml` (`$MAPI_CONFIG` or
   `$XDG_CONFIG_HOME/mapi/config.toml` if set);
4. `http://127.0.0.1:8000`.

```toml
# ~/.config/mapi/config.toml
profile = "local"

[profiles.local]
url = "http://127.0.0.1:8000"

[profiles.other-ports]
url = "http://127.0.0.1:18000"
timeout = 60
```

`mapi config show` prints the resolved URL, profile, project and credentials and where each
value came from (never a token: an API token appears as `mapi_<prefix>_…`, a sign-in as who
signed in and until when); `mapi config path` prints the file's location. `mapi config set
KEY VALUE` and `mapi config unset KEY` change the selected profile (`--profile`, else
`MAPI_PROFILE`, else the file's default). They rewrite the file with mode `0600` and do not
keep comments. Secrets cannot be set this way: use `mapi login --with-token`. `--remote @` on `check`, `fit`,
`score` and `catalog` means "the configured API"; `--remote URL` overrides it for one command.

!!! info "Tokens"
    v0.2 is single-tenant and has no authentication: the API listens on `127.0.0.1` only.
    The client sends `MAPI_TOKEN` as a bearer token when one is set, and refuses to send it
    over plain HTTP to anything but a loopback address.

### Sign in and choose a project <span class="mapi-badge">v0.3</span> { #sign-in-and-choose-a-project }

A multi-tenant API (v0.3) needs credentials and a project
([Tenancy and authentication](../concepts/tenancy-and-auth.md)).

```bash
--8<-- "cli_tenancy.sh:login"
```

- **`mapi login`** opens the identity provider in your browser (OpenID Connect,
  authorization code with PKCE, redirected to a one-off listener on `127.0.0.1`).
  `--device` signs in with a code typed on another device instead, and `--no-browser` only
  prints the address. `--issuer`, `--client-id` and `--discovery-url` are saved in the
  profile, so later logins need none of them. Against a local `MAPI_PROFILE=dev` stack, use
  `mapi login --issuer https://dev-idp.mapi.localhost --discovery-url
  http://127.0.0.1:8000/dev/idp` and pick an account on the page that opens. The sign-in is kept in `credentials.json` next to the config file
  (mode `0600`) and renewed automatically with its refresh token. It is only ever sent to
  the API it was made for.
- **`mapi login --with-token`** reads an API token from stdin, checks its checksum (a typo
  is caught before any request), verifies it with `GET /v1/me` and saves it in the profile
  (the config file is rewritten with mode `0600`). An API token in the profile or in
  `MAPI_TOKEN` takes precedence over a browser sign-in.
- **`mapi whoami`** shows who you are, how you signed in, and every project you can act in
  with your role there.
- **`mapi logout`** forgets the sign-in (revoking its refresh token at the identity provider)
  and the profile's token; `--revoke` also revokes that token on the server.

Every v0.2 resource (datasets, specs, runs, models, plots) now lives in a project.
`mapi use` checks that you are a member and saves the selection in the profile; the CLI sends
it as `X-Mapi-Project` on project-scoped requests only. `--project` or `MAPI_PROJECT`
override it for one command or one shell. An API token acts in its own project only.

```bash
--8<-- "cli_tenancy.sh:use"
```

### Tokens, usage and cost <span class="mapi-badge">v0.3</span> { #tokens-usage-and-cost }

```bash
--8<-- "cli_tenancy.sh:tokens"
--8<-- "cli_tenancy.sh:usage"
```

`mapi tokens create` prints the new token's secret once, alone on stdout, so redirecting it
to a file captures exactly the token; `--save` stores it in the profile instead. Nothing else
ever prints a token: listings, `whoami` and `config show` use the redacted form
`mapi_<prefix>_…`. `mapi usage` shows the exact cost of the period (integer micro-units of
the currency, priced by a named price table), the vCPU- and GiB-seconds behind it, and how
much of each quota is used and when it resets. `mapi runs get` and `mapi runs ls` show each
run's cost (`~` while it is still an estimate).

When the API answers `429`, the client handles it: `RATE_LIMITED` is waited out (the
server's `Retry-After`, then exponential back-off) and retried; `QUOTA_EXCEEDED` stops at
once with the reason, when the quota resets (`retry after 1h 30m`) and, with `--json`,
`retry_after_seconds`.

### Operators <span class="mapi-badge">v0.3</span> { #operators }

Platform operators manage tenants, quotas, kill switches and executors with `mapi admin`.
Each change shows a before-and-after table and asks for confirmation (`--yes` skips it, for
runbooks); the server audits it. Kill-switch changes need `--reason`.

```bash
--8<-- "cli_tenancy.sh:admin"
```

`mapi admin quotas set` replaces the whole policy of a subject: limits you leave out are
removed (the tier defaults and the other levels still apply).

### Upload data

`mapi datasets upload` creates the dataset if it does not exist, sends the file to the signed
URL the API returns, starts the sandboxed ingest run and waits until the version is ready.
CSV and Parquet files need `--mapping mapping.yaml`, an `IngestMapping` with the column of each
dim and variable:

```bash
--8<-- "cli_remote.sh:upload"
```

### Fit and follow

`mapi fit SPEC --remote @` creates the spec (or adds a revision when the text changed),
submits a fit run and follows its event stream until it ends, printing warnings, diagnostics
and metrics as they arrive. `--no-wait` returns after submitting; `mapi runs watch RUN`
follows any run later.

```bash
--8<-- "cli_remote.sh:fit"
```

`mapi runs cancel RUN` requests cancellation, and `mapi runs retry RUN --resource-class L`
starts a new run from a failed one.

### Score

```bash
--8<-- "cli_remote.sh:score"
```

`mapi score --remote @` sends `Prefer: wait=60` (change it with `--wait`), follows the run if it
takes longer, prints the outputs with their sizes and CRC32C, and with `-o` downloads the
result tree, checking it against that CRC32C.

### Endpoints, webhooks and sharing <span class="mapi-badge">v0.4</span> { #endpoints-webhooks-and-sharing }

```bash
mapi endpoints promote geo-mmm-prod model://growth/eu-geo-mmm@v3   # asks first; --yes skips
mapi endpoints ls
mapi endpoints schema geo-mmm-prod --example > call.json           # a valid score call
mapi endpoints score geo-mmm-prod call.json -o result.zarr.zip
mapi endpoints score geo-mmm-prod --mode forecast --periods 8 --draws 100
mapi endpoints score geo-mmm-prod --data plan.nc                   # inline XrJSON (≤ 5 MB)
mapi endpoints snippets geo-mmm-prod -l python > score.py
mapi endpoints rollback geo-mmm-prod --reason "v3 over-forecasts"
```

`promote` goes through the [promotion gate](../concepts/integrations.md#promotion-gate): a
warning scorecard needs `--override --reason TEXT` from a signed-in project admin, a failing
one is blocked. Promotions and rollbacks change what every integration calling the alias
receives, so both show the current version and ask before acting. `score` sends a JSON
`EndpointScoreCall` (a file, `-` for stdin, or `--example`), with flags for the mode, the
horizon, `--data`/`--scenario` (a `dataset://` reference, an XrJSON `.json`, a `.zarr.zip`
or a netCDF file, sent inline) and `--draws`; it waits like `mapi score --remote` and `-o`
downloads the result tree. With one `--language`, `snippets` prints only the code.

```bash
mapi webhooks create https://hooks.example.com/mapi -e endpoint.promoted -e run.failed > hook.secret
mapi webhooks ls
mapi webhooks deliveries WEBHOOK_ID --status failed
mapi webhooks replay WEBHOOK_ID DELIVERY_ID
mapi webhooks update WEBHOOK_ID --enable           # back on after --inactive or an auto-disable
mapi webhooks update WEBHOOK_ID -e run.failed --description "on-call"
mapi webhooks rotate WEBHOOK_ID > hook.secret      # asks first; the old secret signs for 24 h
mapi webhooks rm WEBHOOK_ID                        # asks first
```

Like tokens, a webhook's signing secret is printed once, alone on stdout, by `create` and
`rotate`; listings show `whsec_…<last 4>`. `update` changes the events (`-e` replaces the
whole list), `--url` or `--description`, and `--enable`/`--disable` switches deliveries on or
off: a webhook created with `--inactive`, or disabled automatically after repeated failures,
goes back on with `--enable`, which also resets its failure count. Delivery responses are the receiver's text and
are printed with control sequences removed. Verifying deliveries is described in
[Receiving webhooks](webhook-receivers.md).

```bash
mapi plots render --model MODEL_ID --version 3 --plot mmm.response_curves -f pdf -o curves.pdf
mapi plots render --saved PLOT_ID --width 1200
mapi plots embed PLOT_ID --expires-in-days 30 --allow-origin https://bi.example.com
mapi plots embeds PLOT_ID
mapi plots revoke PLOT_ID EMBED_ID                 # asks first
```

`render` draws the plot in the sandbox (vl-convert) and downloads the file; `embed` prints the
link - which carries its token - once ([Sharing plots](sharing-plots.md)).

### Guided mode and business priors <span class="mapi-badge">v0.4</span> { #guided-mode-and-business-priors }

```bash
mapi guided templates [geo_mmm]
mapi guided detect geo_mmm --csv media.csv          # or --dataset dataset://growth/media@v2
mapi guided draft geo_mmm --name eu-geo-mmm --dataset eu_media --csv media.csv --detect \
    -a spend=tv_spend,search_spend -a holdout_periods=8 --yaml-out spec.yaml
mapi priors preview --beliefs beliefs.yaml --spec SPEC_ID --revision 2 -o priors.json
```

`detect` and `draft --csv` profile the file **locally** - types, counts, ranges and a few
sample values per column - and send only those summaries, as the web app does. `--detect`
pre-fills every answer detection is sure of; `-a QUESTION=VALUE` answers (or overrides) one,
with `a,b,c` for lists and JSON for numbers. A draft stores nothing: review `spec.yaml`, then
upload the data with the mapping (`--mapping-out`) and fit. `priors preview` turns ROI or
contribution-share ranges into model priors in a sandboxed run and shows what the model then
believes, per channel ([Guided modelling](guided-modelling.md)).

### Sign-up and your workspace <span class="mapi-badge">v0.4</span> { #sign-up-and-your-workspace }

```bash
mapi --url https://api.mapi.example signup --email ana@acme.example --name "Ana Analyst" --accept-terms
mapi --url https://api.mapi.example verify-email mev_…   # the token from the e-mail link
mapi tenant status                                       # verification, any scheduled deletion
mapi tenant export -o acme-export.zip                    # GDPR export, waited for and downloaded
mapi tenant delete                                       # type the tenant id to confirm
mapi tenant delete --cancel                              # within the grace period
```

`signup` and `verify-email` send no credentials. `tenant delete` explains what is purged and
kept, and asks you to type the tenant id; scripts pass `--confirm TENANT`. The tenant is
`--tenant`, else the selected project's, else your only one
([Deleting and exporting data](data-deletion.md)).

### Workflows, governance and explorers <span class="mapi-badge">v0.5</span> { #workflows-governance-and-explorers }

**Help me choose** and **About cards**, in the terminal:

```bash
--8<-- "cli_v05.sh:choose"
```

**Workflows**: start from the gallery, check the file on the server, save it, run it and watch
every node live. `--watch` exits with status 1 when the run fails, so it works in CI:

```bash
--8<-- "cli_v05.sh:workflow"
```

```bash
--8<-- "cli_v05.sh:inspect"
```

```bash
--8<-- "cli_v05.sh:automate"
```

`-i name=ref` binds a workflow input (`@latest` is pinned to a version when the run starts);
`-p name=value` sets a parameter, as JSON when it parses (`-p 'horizons=[1,4,13]'`). The
[workflow cookbook](workflow-cookbook.md) has complete documents to start from.

**Governance**: the check catalog, policies, validations and second-person approval. `mapi
validate model` waits for the checks and prints each result in plain words with the suggested
action; `mapi approve` needs a signed-in reviewer who did not submit the model:

```bash
--8<-- "cli_v05.sh:governance"
```

**Explorers and budget plans**:

```bash
--8<-- "cli_v05.sh:explore"
```

```bash
--8<-- "cli_v05.sh:optimize"
```

Model references are `model://project/name@vN`, `name@vN`, `name` with `-v N`, or a model id;
datasets the same with `dataset://`. Every command takes `--json`.

### Scripting with `--json`

`--json` (a global option) prints one JSON document on stdout instead of tables; `runs watch
--json` and `workflows watch --json` print one JSON event per line. Errors become `{"error": {...}}` with exit status 1,
and usage errors exit with 2.

```bash
--8<-- "cli_remote.sh:json"
```

Text that came from the server, a plugin or a run log is printed with ANSI and control
sequences removed, so a hostile log line cannot rewrite your terminal.

## `mapi serve`

`mapi serve` runs `mapi-server dev`: database migrations, then the API and the orchestrator
in one process, with an embedded PostgreSQL unless `DATABASE_URL` is set and an in-process
GCS shim unless one already listens at `MAPI_GCS_ENDPOINT`. It is the smallest way to get an
API for the CLI or the client. For the web app as well, use `make dev`
([Local deployment](../operations/local-development.md)).
