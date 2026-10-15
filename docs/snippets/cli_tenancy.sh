#!/usr/bin/env bash
# The CLI against a multi-tenant (v0.3) API (docs/guides/cli.md, docs/reference/cli.md).
#
# Needs a v0.3 stack with an identity provider; conformance/tests/test_docs_platform.py checks
# that it parses. MAPI_URL points at the API.
set -euo pipefail

# --8<-- [start:login]
mapi --profile prod --url https://api.mapi.example login \
    --issuer https://accounts.google.com --client-id 1234-abc.apps.googleusercontent.com
mapi --profile prod login --device              # no browser here: enter a code elsewhere
mapi --profile ci login --with-token < ci.token # an API token, read from stdin
mapi whoami                                     # who, how, and your projects and roles
# --8<-- [end:login]

# --8<-- [start:use]
mapi use acme/growth                            # sent as X-Mapi-Project from now on
mapi --project acme/retail runs ls              # another project, for one command
mapi use                                        # show the selection and where it came from
# --8<-- [end:use]

# --8<-- [start:tokens]
mapi tokens create atlas-optimiser -s score:geo-mmm-prod -s models:read \
    --expires-in-days 30 --rate-limit 120 > atlas.token   # the secret, once, on stdout
mapi tokens ls
mapi tokens revoke tok_01jaf3r1s2t3v4w5x6y7z8a9b0      # asks first; --yes for scripts
# --8<-- [end:tokens]

# --8<-- [start:usage]
mapi usage --group-by resource_class            # this month: cost, compute, quota headroom
mapi usage --tenant acme --period day           # the whole tenant today (tenant admins)
mapi runs get run_01jaf3q2x8m9v7k5h4g3f2e1d0    # includes the run's metered cost
# --8<-- [end:usage]

# --8<-- [start:admin]
mapi admin kill-switches set --untrusted-code off --reason "incident 2026-10-15"
mapi admin quotas set tenant acme --vcpu-seconds-per-day 172800 --concurrent-runs 40 \
    --storage 200GiB --note "beta customer"
mapi admin tenants update acme --status suspended --reason "payment overdue"
mapi admin executors ls
mapi admin executors disable cloudrun-jobs --reason "incident 2026-10-15: contain"
mapi admin audit --tenant acme --action token.create --since 2026-10-01
# --8<-- [end:admin]

# --8<-- [start:logout]
mapi logout                                     # forget the sign-in (and stored token)
mapi --profile ci logout --revoke               # ... and revoke the token on the server
# --8<-- [end:logout]
