# mapi-cli

The `mapi` command line for modeling-api.

```bash
mapi new geo-mmm -o spec.yaml --with-data   # template + tiny sample data (needs mapi-conformance)
mapi check spec.yaml --data data.zarr.zip   # full static validation against installed plugins
mapi fit spec.yaml --data data.zarr.zip \
    --side-table fx=fx.zarr.zip --side-table holidays=holidays.zarr.zip   # in-process fit
mapi score request.yaml --bundle bundles/geo-mmm --data future.zarr.zip -o result.zarr.zip

mapi fit spec.yaml --data data.zarr.zip --remote @   # upload, create/revise spec, fit, follow
mapi runs ls --state running | get RUN | logs RUN | watch RUN | cancel RUN | retry RUN
mapi datasets ls | upload file.csv --name sales --mapping mapping.yaml
mapi score request.yaml --remote @ --model MODEL_ID --version 1 -o result.zarr.zip
mapi catalog [models|steps|plots] [--remote @]

mapi plugin init acme-demand && mapi plugin test acme-demand   # scaffold + contract kit
mapi spec upgrade spec.yaml --check
mapi serve                                     # delegates to `mapi-server dev` when installed
mapi config show | set KEY VALUE | unset KEY
```

Multi-tenant APIs (v0.3):

```bash
mapi login [--device | --with-token < token]  # browser OIDC (PKCE, loopback), device code, or token
mapi whoami                                   # who you are, your projects and roles
mapi use acme/growth                          # X-Mapi-Project for later commands (--project once)
mapi tokens ls | create NAME -s SCOPE | revoke TOKEN
mapi usage [--group-by resource_class] [--tenant acme]
mapi runs get RUN                             # includes the metered cost
mapi admin tenants|quotas|kill-switches|executors|audit ...   # operators; changes ask first
mapi logout [--revoke]
```

| Templates (`mapi new`) | Conformance fixture |
|---|---|
| `geo-mmm`: hierarchical geo MMM (weekly EUR, adstock, saturation) | E33 |
| `forecast`: HSGP trend + seasonality forecast | C08 |
| `tabular`: XGBoost demand with quantile uncertainty | xgb_store_demand |
| `glm`: hierarchical GLM, long layout | E02 |

**Configuration.** `--url`/`--token`/`--project`/`--profile` beat
`MAPI_URL`/`MAPI_TOKEN`/`MAPI_PROJECT`/`MAPI_PROFILE`, which beat
`~/.config/mapi/config.toml` (`$MAPI_CONFIG`, `$XDG_CONFIG_HOME`), which beats the local
default `http://127.0.0.1:8000`. The config file and `credentials.json` (sign-ins) are
written with mode `0600`; no command prints a token or secret (`mapi tokens create` prints
the new secret once, alone on stdout, unless `--save`). `--remote @` means "the configured API"; `--remote URL`
overrides it for one command.

**Scripting.** `--json` prints one JSON document on stdout (`runs watch --json` prints one
JSON event per line); errors become `{"error": {...}}` and the exit status is 1. Progress
and logs go to stderr.

**Safety.** Local `fit`/`score` run plugins in this process (isolation NONE): use them only
with code and data you trust. Text that comes from the server, plugins or logs is printed
with ANSI and control sequences removed.

License: Apache License 2.0.
