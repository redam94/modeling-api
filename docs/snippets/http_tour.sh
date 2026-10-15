#!/usr/bin/env bash
# The HTTP API guide (docs/guides/http-api.md) with curl and jq, against a running stack.
#
# Needs `make dev` and `make seed` (the eu-geo-mmm spec and its datasets) plus the demo files
# in demo/ (`uv run python scripts/seed.py --export demo/`) and the eu_media_future dataset.
# `make e2e` runs it (conformance/tests/test_docs_platform.py). Works with bash 3.2 (macOS).
set -euo pipefail

# --8<-- [start:health]
API=${MAPI_API_URL:-http://127.0.0.1:8000}
curl -s "$API/readyz" | jq -c '{status, version, checks: .checks | map_values(.status)}'
curl -s "$API/v1/catalog/models" \
  | jq -r '.items[].manifest | "\(.id)@\(.version)  \(.capabilities.representation)"'
# --8<-- [end:health]

# --8<-- [start:dataset]
DS=$(curl -s "$API/v1/datasets?limit=200" | jq -r '.items[] | select(.name == "weekly_sales") | .id')
if [ -z "$DS" ]; then
  DS=$(curl -s -X POST "$API/v1/datasets" -H 'Content-Type: application/json' \
    -d '{"name": "weekly_sales", "description": "a CSV upload from the HTTP guide"}' | jq -r .id)
fi
echo "dataset $DS"
# --8<-- [end:dataset]

# --8<-- [start:upload]
printf 'week,sales\n2026-01-04,120\n2026-01-11,131\n2026-01-18,127\n2026-01-25,140\n' > sales.csv
SIZE=$(wc -c < sales.csv | tr -d ' ')
UP=$(curl -s -X POST "$API/v1/datasets/$DS/uploads" -H 'Content-Type: application/json' \
  -d "{\"filename\": \"sales.csv\", \"content_type\": \"text/csv\", \"size_bytes\": $SIZE}")
HEADERS=()  # the signed URL binds these headers: send them exactly
while IFS= read -r h; do HEADERS+=(-H "$h"); done \
  < <(jq -r '.upload.headers | to_entries[] | "\(.key): \(.value)"' <<<"$UP")
curl -s -o /dev/null -w 'PUT %{http_code}\n' -X PUT "${HEADERS[@]}" \
  --data-binary @sales.csv "$(jq -r .upload.url <<<"$UP")"
# --8<-- [end:upload]

# --8<-- [start:ingest]
MAPPING='{
  "format": "csv",
  "dims": {"week": {"column": "week", "spec": {"kind": "time", "policy": "extend", "freq": "W-SUN"}}},
  "vars": {"sales": {"column": "sales", "role": "target", "units": "units"}}
}'
DV=$(curl -s -X POST "$API/v1/datasets/$DS/versions" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d "{\"upload_id\": $(jq .id <<<"$UP"), \"mapping\": $MAPPING}")
V=$(jq -r .version <<<"$DV")
STATUS=ingesting
while [ "$STATUS" = ingesting ]; do  # a sandboxed ingest run parses and checks the file
  sleep 1
  STATUS=$(curl -s "$API/v1/datasets/$DS/versions/$V" | jq -r '.status // "ingesting"')
done
curl -s "$API/v1/datasets/$DS/versions/$V" | jq '{ref, status, dims: .data_schema.dims | keys}'
# --8<-- [end:ingest]

# --8<-- [start:validate]
jq -Rs '{yaml: .}' demo/eu-geo-mmm.yaml \
  | curl -s -X POST "$API/v1/specs:validate" -H 'Content-Type: application/json' -d @- \
  | jq '{valid, issues: [.issues[] | {code, pointer}], stages: [.stages[] | .name]}'
# --8<-- [end:validate]

# --8<-- [start:fit]
SPEC=$(curl -s "$API/v1/specs?limit=200" | jq -c '.items[] | select(.name == "eu-geo-mmm")')
RUN=$(curl -s -X POST "$API/v1/runs" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d "{\"kind\": \"fit\", \"spec_id\": $(jq .id <<<"$SPEC"), \"revision\": $(jq .latest_revision <<<"$SPEC")}" \
  | jq -r .id)
echo "run $RUN"
# --8<-- [end:fit]

# --8<-- [start:events]
# server-sent events: a snapshot, the persisted events, live progress, then `end`
curl -sN -H 'Accept: text/event-stream' "$API/v1/runs/$RUN/events" | grep '^event:' | uniq -c
curl -s "$API/v1/runs/$RUN" | jq '{state, output_model_version_id}'
# --8<-- [end:events]

# --8<-- [start:model]
MODEL=$(curl -s "$API/v1/models?limit=200" | jq -c '.items[] | select(.name == "eu-geo-mmm")')
M=$(jq -r .id <<<"$MODEL")
MV=$(jq -r .latest_version <<<"$MODEL")
curl -s "$API/v1/models/$M/versions/$MV" \
  | jq '{ref, scorecard: .scorecard.overall, metrics, signed: .manifest.signature.alg}'
# --8<-- [end:model]

# --8<-- [start:score]
RES=$(curl -s -X POST "$API/v1/models/$M/versions/$MV:score" \
  -H 'Content-Type: application/json' -H 'Prefer: wait=60' -H "Idempotency-Key: $(uuidgen)" \
  -d '{"request": {
        "apiVersion": "mapi/v1", "kind": "ScoreRequest",
        "model": "model://default/eu-geo-mmm@latest",
        "mode": "forecast",
        "horizon": {"dim": "time", "periods": 8},
        "data": "dataset://default/eu_media_future@latest",
        "side_tables": {"fx": "dataset://default/fx_daily@latest"}}}')
jq '{done, state: .run.state, outputs: [.outputs[] | {name, size, crc32c}]}' <<<"$RES"
curl -s "$(jq -r '.outputs[] | select(.name == "result.json") | .url' <<<"$RES")" \
  | jq '{mode, representation, output_space}'
# --8<-- [end:score]

# --8<-- [start:problem]
curl -s -X POST "$API/v1/models/$M/versions/$MV:score" -H 'Content-Type: application/json' \
  -d '{"request": {"apiVersion": "mapi/v1", "kind": "ScoreRequest",
       "model": "model://default/eu-geo-mmm@latest", "mode": "filter",
       "data": "dataset://default/eu_media_future@latest"}}' | jq '{status, code, detail}'
# --8<-- [end:problem]
rm -f sales.csv
