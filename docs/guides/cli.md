---
description: The mapi command line - scaffold, check and fit specs in your own process, or upload data, fit, follow and score on a running platform.
---

# The command line <span class="mapi-badge">v0.2</span>

`mapi` (package `mapi-cli`, Apache-2.0) works in two locations:

- **Locally**, without a server. You scaffold a spec, check it, fit it and score it in your
  own Python process, with the installed plugins (`new`, `check`, `fit`, `score`, `catalog`,
  `plugin`, `spec`).
- **Remotely**, against the [HTTP API](http-api.md) of a platform that runs. You upload
  datasets, fit, follow runs and score model versions (`datasets`, `runs`, and `--remote` on
  `check`, `fit`, `score` and `catalog`).

The generated [CLI reference](../reference/generated/cli-reference.md) lists all commands and
options. The examples on this page are scripts that the test suite runs. The default suite
runs the local scripts. `make e2e` runs the remote scripts.

!!! note "Running the commands"
    `make install` puts `mapi` into the workspace environment. You can activate the
    environment (`source .venv/bin/activate`). Alternatively, you can put `uv run` before
    each command (`uv run mapi --help`).

## Without a server

**Scaffold** a spec from a template. `--with-data` also writes the sample data of the
conformance fixture that matches. This option needs the `mapi-conformance` package. The
workspace has this package.

```bash
--8<-- "cli_local.sh:new"
```

| Template | What it is | Conformance fixture |
|---|---|---|
| `geo-mmm` | Hierarchical geo MMM (weekly EUR, adstock, saturation) | E33 |
| `forecast` | HSGP trend and seasonality forecast | C08 |
| `tabular` | XGBoost demand with quantile uncertainty | `xgb_store_demand` |
| `glm` | Hierarchical GLM, long layout | E02 |

**Check** the spec statically against the installed plugins and the schema of the data.
`--stages` shows the schema after each pipeline step. If there are errors, the exit status
is 1. Thus the command works in CI:

```bash
--8<-- "cli_local.sh:check"
```

**Fit** in-process. The bundle goes to `bundles/<name>/`. It contains the zarr zip,
sidecars, plots, metrics and the evaluation bundle of `evaluate_then_refit`.
`--side-table NAME=PATH` supplies side tables:

```bash
--8<-- "cli_local.sh:fit"
```

`mapi score request.yaml --bundle bundles/<name> --data future.zarr.zip -o result.zarr.zip`
scores a local bundle with a `ScoreRequest` document.

!!! warning "Local means trusted"
    Local `fit` and `score` import and run the plugins in your own process (isolation
    `NONE`), exactly as a notebook does. Use them only with code and data that you trust. On
    the platform, the same work runs in a sandbox.

**Inspect** the catalog of installed plugins. **Upgrade** spec documents across
`apiVersion`s:

```bash
--8<-- "cli_local.sh:catalog"
--8<-- "cli_local.sh:upgrade"
```

`mapi plugin init NAME` scaffolds a plugin package (model, entry point, contract-kit tests).
`mapi plugin test NAME` runs its tests. See [Write a plugin](plugins.md).

## With a server

First, start the stack (`make dev`, then `make seed`). See the
[quickstart](../getting-started/quickstart.md).

### Select the API

Each remote command finds the API in the same way as the Python client. The first match in
this list wins:

1. `--url` and `--token`. These are global options. Put them before the command:
   `mapi --url http://… runs ls`.
2. `MAPI_URL`, `MAPI_TOKEN` and `MAPI_PROFILE`. If `MAPI_URL` is not set, the CLI uses
   `MAPI_API_URL`, the name that `make dev` uses.
3. The selected profile in `~/.config/mapi/config.toml`. If `$MAPI_CONFIG` or
   `$XDG_CONFIG_HOME/mapi/config.toml` is set, the CLI uses that file.
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

`mapi config show` shows the resolved URL, profile, project and credentials. It also shows
the source of each value. It never shows a token. An API token shows as `mapi_<prefix>_…`. A
sign-in shows as the person who signed in and the time when the sign-in ends.
`mapi config path` shows the location of the file.

`mapi config set KEY VALUE` and `mapi config unset KEY` change the selected profile. The
selected profile is `--profile`. If you do not give it, it is `MAPI_PROFILE`, and then the
default of the file. These commands write the file again with mode `0600`. They do not keep
comments. You cannot set secrets in this way. Use `mapi login --with-token`.

`--remote @` on `check`, `fit`, `score` and `catalog` means "the configured API".
`--remote URL` overrides it for one command.

!!! info "Tokens"
    v0.2 is single-tenant and has no authentication. The API listens only on `127.0.0.1`.
    When `MAPI_TOKEN` is set, the client sends it as a bearer token. The client refuses to
    send it over plain HTTP to an address that is not a loopback address.

### Sign in and choose a project <span class="mapi-badge">v0.3</span> { #sign-in-and-choose-a-project }

A multi-tenant API (v0.3) needs credentials and a project
([Tenancy and authentication](../concepts/tenancy-and-auth.md)).

```bash
--8<-- "cli_tenancy.sh:login"
```

- **`mapi login`** opens the identity provider in your browser. It uses OpenID Connect and
  the authorization code with PKCE. The provider redirects to a one-time listener on
  `127.0.0.1`.
    - `--device` signs in with a code that you type on a different device.
    - `--no-browser` only shows the address.
    - The CLI saves `--issuer`, `--client-id` and `--discovery-url` in the profile. Thus
      later logins do not need them.
    - Against a local `MAPI_PROFILE=dev` stack, use `mapi login --issuer
      https://dev-idp.mapi.localhost --discovery-url http://127.0.0.1:8000/dev/idp`. Then
      select an account on the page that opens.
    - The CLI keeps the sign-in in `credentials.json` next to the config file (mode
      `0600`). It renews the sign-in automatically with its refresh token. It sends the
      sign-in only to the API that it was made for.
- **`mapi login --with-token`** reads an API token from stdin and checks its checksum. Thus
  it finds an incorrect character before it sends a request. Then it verifies the token with
  `GET /v1/me` and saves it in the profile. It writes the config file again with mode
  `0600`. An API token in the profile or in `MAPI_TOKEN` has priority over a browser sign-in.
- **`mapi whoami`** shows who you are and how you signed in. It also shows each project
  where you can act, with your role there.
- **`mapi logout`** removes the sign-in and revokes its refresh token at the identity
  provider. It also removes the token of the profile. `--revoke` also revokes that token on
  the server.

Each v0.2 resource (datasets, specs, runs, models, plots) is now in a project. `mapi use`
checks that you are a member. Then it saves the selection in the profile. The CLI sends the
project as `X-Mapi-Project`, but only on project-scoped requests. `--project` overrides it
for one command. `MAPI_PROJECT` overrides it for one shell. An API token acts only in its own
project.

```bash
--8<-- "cli_tenancy.sh:use"
```

### Tokens, usage and cost <span class="mapi-badge">v0.3</span> { #tokens-usage-and-cost }

```bash
--8<-- "cli_tenancy.sh:tokens"
--8<-- "cli_tenancy.sh:usage"
```

`mapi tokens create` shows the secret of the new token one time, alone on stdout. Thus, if
you redirect stdout to a file, the file contains exactly the token. `--save` stores the
token in the profile instead. No other command ever shows a token. Lists, `whoami` and
`config show` use the redacted form `mapi_<prefix>_…`.

`mapi usage` shows these data:

- The exact cost of the period, in integer micro-units of the currency. A named price table
  gives the prices.
- The vCPU-seconds and GiB-seconds behind the cost.
- How much of each quota is used, and when it resets.

`mapi runs get` and `mapi runs ls` show the cost of each run. While the cost is still an
estimate, it has a `~`.

When the API answers `429`, the client does these actions:

- For `RATE_LIMITED`, the client waits and then tries again. It waits for the `Retry-After`
  of the server, and then uses exponential back-off.
- For `QUOTA_EXCEEDED`, the client stops immediately. It shows the reason and the time when
  the quota resets (`retry after 1h 30m`). With `--json`, it also gives
  `retry_after_seconds`.

### Operators <span class="mapi-badge">v0.3</span> { #operators }

Platform operators use `mapi admin` to manage tenants, quotas, kill switches and executors.
Each change shows a before-and-after table and asks for confirmation. For runbooks, `--yes`
skips the confirmation. The server audits each change. Kill-switch changes need `--reason`.

```bash
--8<-- "cli_tenancy.sh:admin"
```

`mapi admin quotas set` replaces the full policy of a subject. It removes the limits that you
do not give. The tier defaults and the other levels still apply.

### Upload data

`mapi datasets upload` does these steps:

1. If the dataset does not exist, it creates the dataset.
2. It sends the file to the signed URL that the API returns.
3. It starts the sandboxed ingest run.
4. It waits until the version is ready.

CSV and Parquet files need `--mapping mapping.yaml`. This file is an `IngestMapping` that
gives the column of each dim and variable:

```bash
--8<-- "cli_remote.sh:upload"
```

### Fit and follow

`mapi fit SPEC --remote @` creates the spec. If the text changed, it adds a revision. Then it
submits a fit run and follows its event stream until the run ends. It shows warnings,
diagnostics and metrics when they arrive. With `--no-wait`, the command returns after it
submits the run. `mapi runs watch RUN` follows a run at a later time.

```bash
--8<-- "cli_remote.sh:fit"
```

`mapi runs cancel RUN` asks for cancellation. `mapi runs retry RUN --resource-class L` starts
a new run from a run that failed.

### Score

```bash
--8<-- "cli_remote.sh:score"
```

`mapi score --remote @` sends `Prefer: wait=60`. To change the time, use `--wait`. If the run
takes longer, the command follows the run. It shows the outputs with their sizes and CRC32C.
With `-o`, it downloads the result tree and checks it against that CRC32C.

The request must give data to score: `data`, inline values, a `scenario` dataset or a
forecast `horizon`. If it gives none of them, the server refuses the request before a run
starts (`SCHEMA_INVALID` on `/data`, "the score request has no data").

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

`promote` goes through the [promotion gate](../concepts/integrations.md#promotion-gate). A
scorecard with warnings needs `--override --reason TEXT` from a signed-in project admin. The
gate blocks a scorecard that fails. Promotions and rollbacks change what each integration
that calls the alias receives. Thus both commands show the current version and ask before
they act.

`score` sends a JSON `EndpointScoreCall`. The call comes from a file, from stdin (`-`) or
from `--example`. Flags set these values:

- The mode and the horizon.
- `--data`/`--scenario`: a `dataset://` reference, an XrJSON `.json`, a `.zarr.zip` or a
  netCDF file. The command sends the file inline.
- `--draws`.

`score` waits in the same way as `mapi score --remote`. `-o` downloads the result tree. With
one `--language`, `snippets` shows only the code.

```bash
mapi endpoints schema ts-forecast                           # the Input table's columns
mapi endpoints forecast ts-forecast input.csv               # actuals + blank-target future rows
mapi endpoints forecast ts-forecast --actuals new.csv --periods 6
mapi endpoints forecast ts-forecast --actuals new.csv --future 1961-04-01 --future 1961-05-01
mapi endpoints forecast ts-forecast input.csv --dry-run     # print the call, send nothing
```

`forecast` sends [actuals and the dates to forecast](../concepts/forecasting-with-actuals.md)
without a new fit. The CSV has a header row with the columns of the input table of the
endpoint: the time column, the target and the drivers. `schema` lists them.

- Rows with a target value are actuals.
- Rows after the actuals with an empty target are the forecast.
- If there are no such rows, the command adds `--periods` (default 8) after the last actual.
- The command carries empty drivers on future rows forward (`--future-covariates`).

The command shows the data that the forecast used ("Forecast updated with 3 actuals …") and
the summary table. `score` also shows them.

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

`create` and `rotate` show the signing secret of a webhook one time, alone on stdout, as for
tokens. Lists show `whsec_…<last 4>`. `update` changes the events, `--url` or
`--description`. `-e` replaces the full list of events. `--enable`/`--disable` turns
deliveries on or off.

A webhook that you created with `--inactive` goes back on with `--enable`. This also applies
to a webhook that the platform disabled automatically after many failures. `--enable` also
resets the failure count. Delivery responses are the text of the receiver. The CLI shows them
with control sequences removed. [Receiving webhooks](webhook-receivers.md) tells how to
verify deliveries.

```bash
mapi plots render --model MODEL_ID --version 3 --plot mmm.response_curves -f pdf -o curves.pdf
mapi plots render --saved PLOT_ID --width 1200
mapi plots embed PLOT_ID --expires-in-days 30 --allow-origin https://bi.example.com
mapi plots embeds PLOT_ID
mapi plots revoke PLOT_ID EMBED_ID                 # asks first
```

`render` draws the plot in the sandbox (vl-convert) and downloads the file. `embed` shows the
link one time. The link contains its token ([Sharing plots](sharing-plots.md)).

### Guided mode and business priors <span class="mapi-badge">v0.4</span> { #guided-mode-and-business-priors }

```bash
mapi guided templates [geo_mmm]
mapi guided detect geo_mmm --csv media.csv          # or --dataset dataset://growth/media@v2
mapi guided draft geo_mmm --name eu-geo-mmm --dataset eu_media --csv media.csv --detect \
    -a spend=tv_spend,search_spend -a holdout_periods=8 --yaml-out spec.yaml
mapi priors preview --beliefs beliefs.yaml --spec SPEC_ID --revision 2 -o priors.json
```

`detect` and `draft --csv` profile the file **locally**. They find the types, counts, ranges
and some sample values of each column. They send only those summaries, as the web app does.

- `--detect` fills each answer that detection is sure of.
- `-a QUESTION=VALUE` answers one question, or overrides the answer. Use `a,b,c` for lists
  and JSON for numbers.

A draft stores nothing. Examine `spec.yaml`. Then upload the data with the mapping
(`--mapping-out`) and fit. `priors preview` changes ROI ranges or contribution-share ranges
into model priors in a sandboxed run. It shows what the model then believes, for each channel
([Guided modelling](guided-modelling.md)).

### Sign-up and your workspace <span class="mapi-badge">v0.4</span> { #sign-up-and-your-workspace }

```bash
mapi --url https://api.mapi.example signup --email ana@acme.example --name "Ana Analyst" --accept-terms
mapi --url https://api.mapi.example verify-email mev_…   # the token from the e-mail link
mapi tenant status                                       # verification, any scheduled deletion
mapi tenant export -o acme-export.zip                    # GDPR export, waited for and downloaded
mapi tenant delete                                       # type the tenant id to confirm
mapi tenant delete --cancel                              # within the grace period
```

`signup` and `verify-email` send no credentials. `tenant delete` tells what the platform
purges and what it keeps. Then it asks you to type the tenant id. Scripts give
`--confirm TENANT`. The tenant is `--tenant`. If you do not give it, the tenant is the tenant
of the selected project. If there is no selected project, it is your only tenant
([Deleting and exporting data](data-deletion.md)).

### Workflows, governance and explorers <span class="mapi-badge">v0.5</span> { #workflows-governance-and-explorers }

**Help me choose** and **About cards**, in the terminal:

```bash
--8<-- "cli_v05.sh:choose"
```

**Workflows**: start from the gallery and check the file on the server. Then save it, run it
and watch each node live. If the run fails, `--watch` exits with status 1. Thus it works in
CI:

```bash
--8<-- "cli_v05.sh:workflow"
```

`validate` and `apply` show the sequence in which the nodes will run. This output is from a
local stack, with the document of the [cookbook](workflow-cookbook.md#readiness-to-arimax):

```text
weekly-readiness.yaml: valid (23 nodes)
order: adf → kpss → pp → za → seas → folds → holdout → wanders → seasonal → naive → order → sdiff → backtest → fit → diff → compare → dm → acf → select → governance → report → trusted → pack
```

The [cookbook](workflow-cookbook.md#run-it-with-the-cli) shows the live board of `--watch`
and the summary at its end. Then examine a run, one node at a time:

```bash
--8<-- "cli_v05.sh:inspect"
```

```text
 node        order
 uses        search.arma_order@1.0.0
 status      succeeded
 attempt     1
 child runs  run_01m53hawz719exdgb6kcw5ysp3
 params      {"d": 1, "exog": ["price"], "max_p": 3, "max_q": 3, "trend": "c", "criterion": "aic"}
 result      {"best_p": 1, "best_q": 1, "best_order": [1, 1, 1], "criterion": "aic",
              "best_score": 430.4823790539326, "n_failed": 0, "suggested_p": 0, "suggested_q": 0}
 outputs     table: table
```

This is the output of `mapi workflows node "$RUN" order`. Here, we folded the JSON onto fewer
lines. The differencing decision gave `d = 1` to the search, and the run gave the `price`
driver. The search selected ARIMA(1, 1, 1) by AIC.

```bash
--8<-- "cli_v05.sh:automate"
```

`-i name=ref` binds a workflow input. When the run starts, the platform pins `@latest` to a
version. `-p name=value` sets a parameter. If the value parses as JSON, the CLI sends it as
JSON (`-p 'horizons=[1,4,13]'`). The [workflow cookbook](workflow-cookbook.md) has complete
documents that you can start from.

Since v0.9, `mapi workflows templates` also lists the recipes for Bayesian and science models.
Start each one with `mapi workflows init <id>`. The cookbook shows the commands for each one:

| Template | Cookbook section |
|---|---|
| `sci_spatial_ladder` | [Disease map: which spatial model?](workflow-cookbook.md#disease-map-which-spatial-model) |
| `sci_network_ladder` | [Network ties: which model?](workflow-cookbook.md#network-ties-which-model) |
| `sci_phylo_signal` | [Trait regression across species](workflow-cookbook.md#trait-regression-across-species) |
| `sci_calibrate_then_design` | [Calibrate, then design](workflow-cookbook.md#calibrate-then-design) |
| `bayes_workflow_audit` | [Bayesian workflow audit](workflow-cookbook.md#bayesian-workflow-audit) |
| `sci_image_model_check` | [Image model check](workflow-cookbook.md#image-model-check) |

To get the data of a science case study, use `python -m mapi_plugin_science.export <case>`.
Then upload the file with `mapi datasets upload`.

**Governance**: the check catalog, policies, validations and approval by a second person.
`mapi validate model` waits for the checks. It shows each result in plain words with the
suggested action. `mapi approve` needs a signed-in reviewer who did not submit the model:

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

These are the forms of a model reference: `model://project/name@vN`, `name@vN`, `name` with
`-v N`, or a model id. Dataset references have the same forms with `dataset://`. All
commands accept `--json`.

### Use `--json` in scripts

`--json` is a global option. It shows one JSON document on stdout instead of tables.
`runs watch --json` and `workflows watch --json` show one JSON event on each line. Errors
become `{"error": {...}}` with exit status 1. Usage errors exit with status 2.

```bash
--8<-- "cli_remote.sh:json"
```

The CLI removes ANSI and control sequences from text that came from the server, a plugin or a
run log. Thus a hostile log line cannot write over your terminal.

## `mapi serve`

`mapi serve` runs `mapi-server dev`. It does the database migrations. Then it runs the API and
the orchestrator in one process. It uses an embedded PostgreSQL, unless `DATABASE_URL` is set.
It uses an in-process GCS shim, unless a shim already listens at `MAPI_GCS_ENDPOINT`. This is
the smallest way to get an API for the CLI or the client. If you also want the web app, use
`make dev` ([Local deployment](../operations/local-development.md)).
