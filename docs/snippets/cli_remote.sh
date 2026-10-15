#!/usr/bin/env bash
# The CLI against a running stack (docs/guides/cli.md, docs/getting-started/quickstart.md).
#
# Needs `make dev` and `make seed`; writes the demo files into demo/ itself. `make e2e` runs
# it (conformance/tests/test_docs_platform.py). MAPI_API_URL (or MAPI_URL) points at the API.
set -euo pipefail

# --8<-- [start:upload]
python scripts/seed.py --export demo/      # the E33 tables, score payloads and spec
mapi datasets upload demo/eu_media_future.zarr.zip --name eu_media_future
mapi datasets ls
# --8<-- [end:upload]

# --8<-- [start:fit]
mapi check demo/eu-geo-mmm.yaml --remote @   # the server's static validation
mapi fit demo/eu-geo-mmm.yaml --remote @     # create or revise the spec, fit, follow
mapi runs ls --kind fit --limit 3
# --8<-- [end:fit]

# --8<-- [start:score]
cat > forecast.yaml <<'EOF'
apiVersion: mapi/v1
kind: ScoreRequest
model: model://default/eu-geo-mmm@latest
mode: forecast
horizon: {dim: time, periods: 8}
data: dataset://default/eu_media_future@latest
side_tables: {fx: dataset://default/fx_daily@latest}
output: {kind: expected, summary: [mean, hdi_90]}
EOF
# the CLI has no `models` command yet: take the model id and version from the API
MODEL=$(curl -s "${MAPI_API_URL:-http://127.0.0.1:8000}/v1/models" \
  | jq -c '.items[] | select(.name == "eu-geo-mmm")')
mapi score forecast.yaml --remote @ --model "$(jq -r .id <<<"$MODEL")" \
  --version "$(jq -r .latest_version <<<"$MODEL")" -o forecast.zarr.zip
# --8<-- [end:score]

# --8<-- [start:json]
RUN=$(mapi --json runs ls --kind score --limit 1 | jq -r '.[0].id')
mapi --json runs get "$RUN" | jq '{state, attempt, kind}'
mapi runs logs "$RUN" | tail -n 5
# --8<-- [end:json]
rm -f forecast.yaml forecast.zarr.zip
