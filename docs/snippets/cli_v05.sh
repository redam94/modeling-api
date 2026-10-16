#!/usr/bin/env bash
# v0.5 from the command line: Help me choose, the workflow cookbook, governance, explorers
# and budget optimisation. Sections are included by the docs; every command is real (`bash -n`
# checks the file; packages/server/tests/test_cli_m35_live.py runs the workflow part
# against a server). Needs `mapi login` (or MAPI_TOKEN) and `mapi use growth`.
set -euo pipefail

# --8<-- [start:choose]
mapi choose                                   # questions, one at a time; Enter skips one
mapi choose -a goal=forecast -a data_shape=one_series -a drivers=yes -a experience=beginner
mapi about ts.sarimax                         # one item's About card
mapi about ts.sarimax --expert                # ... with the maths and references
# --8<-- [end:choose]

# --8<-- [start:workflow]
mapi workflows templates                      # the gallery, in plain words
mapi workflows init ts_readiness_arimax --name weekly-readiness   # writes weekly-readiness.yaml
mapi workflows validate weekly-readiness.yaml # checked by the server: pointers to every problem
mapi workflows apply weekly-readiness.yaml -m "first version"     # create, or save a revision
mapi workflows run weekly-readiness \
  --input data=dataset://growth/weekly_sales@latest \
  --param 'drivers=["price"]' --param window=104 \
  --watch                                     # live node status; exit 1 if the run fails
# --8<-- [end:workflow]

# --8<-- [start:inspect]
RUN=$(mapi --json workflows runs weekly-readiness -n 1 | jq -r '.[0].run_id')
mapi workflows status "$RUN"                  # nodes, what each found, decisions, outputs
mapi workflows node "$RUN" order              # one node: parameters, result, outputs
mapi workflows output "$RUN" diff data --out diff.zarr.zip   # a derived dataset, verified
# --8<-- [end:inspect]

# --8<-- [start:automate]
mapi workflows schedules add weekly-readiness "0 6 * * 1" --tz Europe/Berlin \
  --input data=dataset://growth/weekly_sales@latest
mapi workflows triggers add weekly-readiness dataset://growth/weekly_sales --debounce 600
mapi workflows schedules ls weekly-readiness
# --8<-- [end:automate]

# --8<-- [start:governance]
mapi governance checks                        # what a policy can require
mapi governance checks --about null.permutation_target --expert
mapi governance policies ls                   # in force here: tenant-wide first
mapi governance policies create forecast-policy.yaml
mapi validate model model://growth/wf-weekly-readiness-fit@v1   # runs the checks, waits
mapi validations ls --queue                   # reviewers: what waits for a decision
mapi approve "$VALIDATION" --comment "Null tests convincing; beats naive by 25%"
mapi validations pack "$VALIDATION" --out evidence.html          # for the auditor
# --8<-- [end:governance]

# --8<-- [start:explore]
mapi explore dataset dataset://growth/weekly_sales@latest \
  -s overview -s stationarity -s ccf -s stl --kpi sales --out views/
mapi explore dataset weekly_sales --show stationarity            # one view as JSON
mapi explore model model://growth/wf-weekly-readiness-fit@v1 -s params -s residuals
mapi explore compare model://growth/arimax@v3 model://growth/ets@v1 --horizon 4
# --8<-- [end:explore]

# --8<-- [start:optimize]
mapi optimize model://growth/geo-mmm@v7 --change-pct 0 \
  --channel 'tv={"min_multiplier": 0.5, "max_multiplier": 1.5}' \
  --objective p10 --frontier 8
# --8<-- [end:optimize]
