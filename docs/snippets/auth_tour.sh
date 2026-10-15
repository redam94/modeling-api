#!/usr/bin/env bash
# The Authentication guide (docs/guides/authentication.md): a sign-in, an API token, what its
# scopes allow, and revocation, with curl and jq.
#
# Needs a v0.3 stack in the dev profile (`make dev`), whose development IdP lets a script sign
# in without a browser. `make e2e` runs it (conformance/tests/test_docs_platform.py). Works with
# bash 3.2 (macOS).
set -euo pipefail

# --8<-- [start:signin]
API=${MAPI_URL:-${MAPI_API_URL:-http://127.0.0.1:8000}}
curl -s -o /dev/null -w 'anonymous: %{http_code}\n' "$API/v1/me"      # 401: sign in first
# dev profile only: what `mapi login` gets from a real identity provider, without a browser
ID_TOKEN=$(python -c 'import sys; from mapi_client.oidc import dev_idp_login
print(dev_idp_login(sys.argv[1], "analyst@dev.local"))' "$API")
curl -s -H "Authorization: Bearer $ID_TOKEN" "$API/v1/me" \
  | jq -c '{email: .principal.email, auth, operator,
            projects: [.tenants[].projects[] | "\(.tenant_id)/\(.project_id) \(.role)"]}'
# --8<-- [end:signin]

# --8<-- [start:mint]
CREATED=$(curl -s -X POST "$API/v1/tokens" -H "Authorization: Bearer $ID_TOKEN" \
  -H 'X-Mapi-Project: default/default' -H 'Content-Type: application/json' \
  -d '{"name": "auth-guide", "scopes": ["models:read"], "expires_in_days": 1}')
TOKEN=$(jq -r .secret <<<"$CREATED")             # shown once: store it now
TOKEN_ID=$(jq -r .token.id <<<"$CREATED")
jq -c '.token | {id, display, scopes, project: "\(.tenant_id)/\(.project_id)", expires_at}' \
  <<<"$CREATED"
# --8<-- [end:mint]

# --8<-- [start:use]
curl -s -H "Authorization: Bearer $TOKEN" "$API/v1/me" | jq -c '{auth, operator}'
curl -s -H "Authorization: Bearer $TOKEN" "$API/v1/runs?limit=2" \
  | jq -r '.items[] | "\(.id) \(.kind) \(.state)"'   # bound to its project: no header needed
# --8<-- [end:use]

# --8<-- [start:narrow]
# models:read cannot spend compute, even though its owner is a runner
curl -s -X POST "$API/v1/runs" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"kind": "fit", "spec": "spec://default/eu-geo-mmm"}' \
  | jq -c '{status, code}'
# a token never acts outside its project, or as an operator
curl -s -H "Authorization: Bearer $TOKEN" -H 'X-Mapi-Project: acme/growth' "$API/v1/runs" \
  | jq -c '{status, code}'
curl -s -H "Authorization: Bearer $TOKEN" "$API/v1/admin/tenants" | jq -c '{status, code}'
# --8<-- [end:narrow]

# --8<-- [start:revoke]
curl -s -o /dev/null -w 'revoke: %{http_code}\n' -X DELETE "$API/v1/tokens/$TOKEN_ID" \
  -H "Authorization: Bearer $ID_TOKEN" -H 'X-Mapi-Project: default/default'
curl -s -H "Authorization: Bearer $TOKEN" "$API/v1/me" | jq -c '{status, code}'   # at once
# --8<-- [end:revoke]
