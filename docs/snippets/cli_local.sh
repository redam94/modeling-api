#!/usr/bin/env bash
# The CLI without a server (docs/guides/cli.md): scaffold, check and fit in your own process.
# Run by conformance/tests/test_docs_platform.py in the default test suite.
set -euo pipefail

# --8<-- [start:new]
mapi new tabular -o spec.yaml --with-data     # an XGBoost demand model plus its sample data
# --8<-- [end:new]

# --8<-- [start:check]
mapi check spec.yaml --data data.zarr.zip --stages
# --8<-- [end:check]

# --8<-- [start:fit]
mapi fit spec.yaml --data data.zarr.zip       # in-process: writes bundles/<name>/
ls bundles/tabular
# --8<-- [end:fit]

# --8<-- [start:catalog]
mapi catalog models
mapi --json catalog steps | jq -r '.steps[].id' | head -n 5
# --8<-- [end:catalog]

# --8<-- [start:upgrade]
mapi spec upgrade spec.yaml --check           # exit 0: already the current apiVersion
# --8<-- [end:upgrade]
