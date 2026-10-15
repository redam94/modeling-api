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

`mapi config show` prints the resolved URL and profile and where each value came from (never
the token); `mapi config path` prints the file's location. `--remote @` on `check`, `fit`,
`score` and `catalog` means "the configured API"; `--remote URL` overrides it for one command.

!!! info "Tokens"
    v0.2 is single-tenant and has no authentication: the API listens on `127.0.0.1` only.
    The client already sends `MAPI_TOKEN` as a bearer token, and refuses to send it over plain
    HTTP to anything but a loopback address. OIDC and scoped tokens arrive with multi-tenancy
    in [v0.3](../roadmap/cloud.md).

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

### Scripting with `--json`

`--json` (a global option) prints one JSON document on stdout instead of tables; `runs watch
--json` prints one JSON event per line. Errors become `{"error": {...}}` with exit status 1,
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
