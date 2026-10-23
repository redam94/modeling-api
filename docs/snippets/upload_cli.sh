#!/usr/bin/env bash
# Upload your own code with the CLI (docs/extend/upload.md). Needs a server that allows
# uploads and a sign-in (`mapi login`) or a token with the plugins:publish scope.
# conformance/tests/test_docs_upload.py checks that every command and option here exists.
set -euo pipefail

# --8<-- [start:init]
mapi login                                   # once: sign in with your browser
mapi use acme/growth                         # the project that runs the checks
mapi plugin init acme --upload --kind step --kind recipe
cd acme
mapi plugin test                             # the contract kit, on your machine
# --8<-- [end:init]

# --8<-- [start:push]
mapi plugin push --wait
# --8<-- [end:push]

# --8<-- [start:approval]
mapi plugin push --request-approval --wait   # after a version bump
mapi plugin status acme                      # every version and its state
mapi plugin status acme@0.1.0                # one version in full
# --8<-- [end:approval]

# --8<-- [start:yank]
mapi plugin yank acme@0.1.0 --reason superseded --note "Use 0.2.0."
# --8<-- [end:yank]

# --8<-- [start:json]
mapi --json plugin push --wait > push.json
jq -r '.version.state' push.json             # kit_passed when the checks passed
# --8<-- [end:json]
