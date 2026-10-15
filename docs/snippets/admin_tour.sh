#!/usr/bin/env bash
# The Administration guide (docs/guides/administration.md): tenants, projects, members, tokens,
# quotas, usage, kill switches and the audit log, with the mapi CLI, curl and jq.
#
# Needs a v0.3 stack in the dev profile (`make dev`): it signs in through the development IdP
# as its operator (admin@dev.local) and as a tenant admin (analyst@dev.local). Against a real
# deployment each person signs in with `mapi login` instead; operator commands need a sign-in,
# never an API token. `make e2e` runs it (conformance/tests/test_docs_platform.py) with a fresh
# TENANT id each time. Works with bash 3.2 (macOS).
set -euo pipefail

# --8<-- [start:signin]
API=${MAPI_URL:-${MAPI_API_URL:-http://127.0.0.1:8000}}
export MAPI_URL=$API
dev_login() {  # dev profile only: an ID token for one seeded account, valid for an hour
  python -c 'import sys; from mapi_client.oidc import dev_idp_login
print(dev_idp_login(sys.argv[1], sys.argv[2]))' "$API" "$1"
}
OPERATOR=$(dev_login admin@dev.local)          # a platform operator
TENANT_ADMIN=$(dev_login analyst@dev.local)    # becomes the new tenant's admin below
MAPI_TOKEN=$OPERATOR mapi whoami
# --8<-- [end:signin]

TENANT=${TENANT:-acme}
as_admin() {  # curl the API as the tenant admin
  curl -sS -H "Authorization: Bearer $TENANT_ADMIN" -H 'Content-Type: application/json' "$@"
}

# --8<-- [start:tenant]
export MAPI_TOKEN=$OPERATOR
mapi admin tenants create "$TENANT" --name "Acme Retail" --tier verified \
    --residency eu --admin-email analyst@dev.local --yes
mapi admin tenants ls
# --8<-- [end:tenant]

# --8<-- [start:projects]
as_admin -X POST "$API/v1/tenants/$TENANT/projects" \
    -d '{"id": "growth", "name": "Growth marketing"}' | jq -c '{tenant_id, id, name}'
as_admin -X POST "$API/v1/tenants/$TENANT/projects/growth/members" \
    -d '{"email": "viewer@dev.local", "role": "runner"}' | jq -c '{project_id, email, role}'
as_admin "$API/v1/tenants/$TENANT/members" \
    | jq -r '.items[] | "\(.email // .principal_id)\t\(.role)\t(tenant-wide)"'
as_admin "$API/v1/tenants/$TENANT/projects/growth/members" \
    | jq -r '.items[] | "\(.email // .principal_id)\t\(.role)\t\(.project_id // "(tenant-wide)")"'
# --8<-- [end:projects]

# --8<-- [start:tokens]
export MAPI_TOKEN=$TENANT_ADMIN
mapi use "$TENANT/growth"
mapi tokens create nightly-report -s models:read --expires-in-days 30 --rate-limit 60 \
    --budget 3600 --budget-period day > nightly-report.token   # the secret, once
mapi tokens ls
# --8<-- [end:tokens]

# --8<-- [start:usage]
mapi usage --group-by resource_class             # the selected project, this UTC month
mapi usage --tenant "$TENANT" --period day       # the whole tenant today (tenant admins)
# --8<-- [end:usage]

# --8<-- [start:quotas]
export MAPI_TOKEN=$OPERATOR
mapi admin quotas set tenant "$TENANT" --vcpu-seconds-per-day 172800 \
    --concurrent-runs 40 --max-fanout-tasks 256 --storage 200GiB \
    --note "private beta customer" --yes
mapi admin quotas ls
# --8<-- [end:quotas]

# --8<-- [start:kill]
mapi admin kill-switches set --spend-cap 500 --spend-cap-period month \
    --reason "private beta budget" --yes
mapi admin kill-switches get
# --8<-- [end:kill]

# --8<-- [start:audit]
mapi admin audit --tenant "$TENANT" -n 10        # operators: any tenant
# tenant admins: their own tenant's log
as_admin "$API/v1/tenants/$TENANT/audit?limit=5" \
    | jq -r '.items[] | "\(.ts)  \(.action)  \(.resource_kind):\(.resource_id)  \(.outcome)"'
# --8<-- [end:audit]

# --8<-- [start:suspend]
mapi admin tenants update "$TENANT" --status suspended --reason "contract ended" --yes
mapi admin tenants update "$TENANT" --status active --yes
# --8<-- [end:suspend]

# leave the stack as it was: no spend cap, the token revoked
mapi admin kill-switches set --clear-spend-cap --reason "docs walkthrough done" --yes >/dev/null
export MAPI_TOKEN=$TENANT_ADMIN
mapi --json tokens ls | jq -r '.[] | select(.name == "nightly-report") | .id' \
    | while read -r id; do mapi tokens revoke "$id" --yes; done
rm -f nightly-report.token
echo "admin tour: done"
