#!/usr/bin/env bash
# The developer track (docs/extend/develop/index.md and part 2 of
# docs/tutorials/your-first-extensions.md): scaffold, test, validate and diagnose a plugin
# package with the mapi CLI. No server needed. conformance/tests/test_docs_extend.py runs it
# in an empty directory (`make docs-test`). Works with bash 3.2 (macOS).
set -euo pipefail

# --8<-- [start:init]
mapi plugin init acme --kind model --kind step --kind recipe --kind node --kind template
# --8<-- [end:init]

# --8<-- [start:test]
mapi plugin test acme            # the contract kits in acme/tests, with this interpreter
# --8<-- [end:test]

# --8<-- [start:validate]
mapi plugin validate acme        # load only acme's entry points and run every check
mapi --json plugin validate acme > acme-report.json   # the same report as JSON (for CI)
# --8<-- [end:validate]

# --8<-- [start:doctor]
mapi plugin doctor               # every installed extension: load problems, routing, digest
# --8<-- [end:doctor]
