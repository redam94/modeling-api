---
description: What was verified of the M3 exit criteria (Guided mode and integrations) on a laptop, the measured times, the defects the runs found, the protocol for the five non-technical testers, and what still needs people, Docker or Google Cloud.
---

# M3 verification <span class="mapi-badge">v0.4</span>

Milestone M3 (Guided mode and integrations) has three exit criteria:

1. five non-technical testers complete **upload → report → scenario** unaided in under 30
   minutes;
2. **Atlas optimises against an endpoint**;
3. **cut line A**: public launch of declarative specs and built-ins (untrusted data, trusted
   code).

This page records how far each was verified **locally**, the numbers measured, what the runs
found and fixed, and what remains. Real testers cannot be simulated, so the first criterion
was prepared, not met: an automated "unaided tester" script measures the floor of the time
budget and the screens were reviewed as a first-time analyst would read them; the
[protocol for the five testers](#protocol-for-the-five-non-technical-testers) below is what
remains to run.

Verified on 2026-10-16 on branch `feat/m3-e2e` (v0.4 development), on an Apple-silicon
laptop (8 cores, 16 GB, macOS), against a live `make dev` stack:

- the API, worker, web app and plot sandbox on non-default ports (API `18000`, GCS shim
  `14443`, web `15173`, plot sandbox `15174`);
- the `dev` profile: the development identity provider and the outbox mailer;
- the GCS shim instead of Cloud Storage and the embedded `pgserver` Postgres;
- every run on the `local-process` executor (no Docker daemon, no cloud project);
- `MAPI_TENANT_DELETION_GRACE_S=0` and `MAPI_BUNDLE_RETENTION_DAYS=0`, so a deletion can be
  watched to the end; higher `MAPI_SIGNUP_PER_IP_PER_HOUR` and
  `MAPI_SIGNUP_PER_DOMAIN_PER_DAY`, since every run signs up new workspaces from one address.

## Summary

| Exit criterion | Verified locally | Result | Still needs |
|---|---|---|---|
| 5 non-technical testers: upload → report → scenario unaided in < 30 min | The browser script `apps/web/e2e/m3-guided.e2e.spec.ts` clicks the whole Guided flow on a realistic spreadsheet, timing each step; every screen was reviewed for plain language, next steps and dead ends | **137 s** of automated wall time, nearly all of it the system working (the beliefs preview, the fit, the exact runs). Six usability defects found and fixed, among them two dead ends | Five real testers ([protocol](#protocol-for-the-five-non-technical-testers)) |
| Atlas optimises against an endpoint | `mapi_client.atlas.RemoteModel` against the live endpoint, with Atlas installed (`atlas.core.interfaces`), an optimiser written against Atlas's `OptimizerProtocol` | The chosen allocation beats the current one under the model: **+0.22 %** revenue for next quarter at the same budget, from 25 candidate allocations (22 score requests) in 220 s | The in-sandbox `optimize` job (M4) |
| Cut line A | A self-service sign-up creates a `public` workspace: e-mail verified, uploaded code refused (`uploaded_code_allowed: false`); built-ins and declarative specs run (here on an operator-promoted `internal` workspace, because the local executor admits only trusted tiers) | Pass | Public workspaces running on `docker-gvisor` or Cloud Run Jobs, the only executors shared tiers may use |

Everything M3 added was also exercised end to end through the API by
`tests/e2e/test_m3_guided_stack.py` (15 tests, 7 minutes): **15 passed**.

## The API journey

```bash
make dev        # with the variables above
make e2e        # or: uv run --with-editable ../optimizer-framework python -m pytest -s -m e2e tests/e2e/test_m3_guided_stack.py
```

One analyst's workspace, from outside the processes. Each row is one test, in order; the
times are wall-clock seconds of the last run.

| Step | What is checked | Time |
|---|---|---|
| Sign-up | `POST /auth/signup` → the verification e-mail read from `GET /dev/outbox` → `POST /auth/verify-email` creates a `public` workspace whose signer is admin; the operator promotes it to `internal` (the local executor runs trusted tiers only) | 0.03 |
| Guided data | All four templates listed; the analyst's CSV (5 regions × 3 channels × 104 weeks, columns `Week, Region, Revenue, TV Spend, Search Spend, Social Spend, Avg Price`) profiled locally like the browser does; role detection fills every question correctly, each with a plain reason; the draft validates; the upload ingests with the draft's mapping; detection on the stored version (`columns.json`) gives the same answers | 13.3 |
| MFF | The same data as a BayesInsight MFF long table (`Period, Geography, VariableName, VariableValue`) ingests to the same variables and roles | 8.3 |
| Business priors | ROI and share ranges and a baseline share (true to the simulation) plus a lift test, translated by a sandboxed `translate_priors` run: the translated priors hold 81 %, 76 % and 80 % of their mass inside the stated 80 % ranges; written into the spec with the run's id | 13.3 |
| Tiny fit | 150 tuning steps and draws × 2 chains: every check of the template's scorecard is measured, in plain words with a docs anchor and an action; its gate (**blocked**: chains disagree) is exactly what `mapi_spec.scorecard.promotion_gate` derives, and the server refuses the promotion with that gate's checks | 30.9 |
| Endpoint fit | 1000 × 4 chains, `target_accept` 0.95: convergence passes or warns, holdout error under 1 %, coverage passes, every plausibility check passes | 61.0 |
| What-if vs Run exact | The per-draw response grid answers a slider at once; "Run exact" (compiled by the server) scores the fit window: tv × 0.5 → grid −454,590 vs exact −454,241 (sd 14,003); search × 1.5 and tv × 0.8 → −4,762 vs −4,255 (sd 14,980). Out-of-grid multipliers are refused | 10–13 per exact run |
| Forecast, channel off | An 8-week forecast fan with history and the split line; tv off over the fit window −1,046,519, against −1,046,414 from the grid's multiplier 0 | 10 each |
| Promotion | `409 OVERRIDE_REQUIRED` naming the warned checks, then an audited admin override with a reason; history, schema with an example call, snippets (none carries a secret) | 0.01 |
| Score token | A `score:<alias>` token scores the endpoint's example (8-week forecast) as an `xarray` tree, and gets `403` on datasets, specs, webhooks and models | 10.0 |
| Webhooks | An HTTPS receiver on `127.0.0.1` (the dev profile's certificate) receives `run.succeeded`, `model.version.ready`, `endpoint.promoted` and `score.completed`, each verified with `mapi_client.webhooks.verify`; no body carries a data value, link or secret; the delivery log shows them; a replay arrives as the same event with a new delivery id; another secret fails verification | 0.3 |
| Embed | A saved platform-recipe plot as an embed link: the public view needs no credentials, every write method answers `405`, the sandbox page carries its CSP; after revocation `410 LINK_EXPIRED` | < 1 |
| Atlas | See [Atlas](#atlas-against-the-live-endpoint) | 219.6 |
| GDPR export | A 49-file zip downloads; it holds the workspace's records and objects, and none of the webhook secret, the score token or the ID token | 16.4 |
| Deletion | 645 tenant rows and 285 objects before. After the purge: no row in any table with a `tenant_id` except the retained `audit_events` (53), `cost_ledger` (33) and the deletion record; **no object** under `t/<tenant>/` in any of the five buckets; a signed `tenant.deletion.completed` reaches the receiver; the admin and the score token are refused | 13.4 |

### Atlas against the live endpoint

Atlas (`../optimizer-framework`) ships the model and optimiser *interfaces*. The test
registers `RemoteModel` as an `atlas.core.interfaces.AbstractModel` and runs an optimiser
written against `OptimizerProtocol` that returns Atlas's own `OptimizationResult`: it moves
a step of budget between two channels while that raises the endpoint's predicted revenue,
halving the step when no move does. Every candidate is one score request against the live
endpoint (inline XrJSON, `score:<alias>` token).

| | Search | Social | TV | Predicted revenue (13 weeks) |
|---|---|---|---|---|
| Current plan | 16,136 | 9,737 | 37,890 | 929,612 |
| Chosen | 19,324 | 9,737 | 34,702 | 931,620 (**+0.22 %**) |

25 candidates cost 22 requests (the cache answered the rest); 220 s in all. Each request is
a sandboxed score run, so the wall time is run start-up; a geo MMM's markets cannot be
batched into one request (new markets are pooled), which is why the in-sandbox `optimize`
job (M4) is the path for longer optimisations.

## The unaided-tester script

```bash
MAPI_E2E=1 pnpm --filter @mapi/web exec playwright test e2e/m3-guided.e2e.spec.ts
```

`tests/e2e/m3_browser_seed.py` creates a fresh project and writes the analyst's spreadsheet;
the script then does what the five testers will be asked to do, with no hesitation, as the
default tenant's admin. Screenshots land in `apps/web/e2e/screenshots/m3-*.png`
(git-ignored); timings in `m3-timings.json`.

| Step | What the script does | Wall time (s) |
|---|---|---|
| 1. Template | "What do you want to learn?" → Geo marketing mix (this run opened `/guided` directly; the script now starts where sign-in lands, `/`, and finds **Guided** in the sidebar) | 0.7 |
| 2. Your data | Names the analysis, chooses the CSV; checks that every question is pre-filled correctly; Upload and continue (upload, sandboxed check) | 4.7 |
| 3. What you believe | Ticks tv, states an ROI of 1.5 to 4, previews what it means (a sandboxed run) and continues | 22.2 |
| 4. Fit the model | Fit the model; plain-language phases and progress; See the results | 79.6 |
| 5. Results | Headline numbers, ROI and share per channel, two charts, the sanity scorecard | 0.4 |
| 6. What if… | Search spend +20 % on the slider (instant), Run exact, Turn off tv, a 13-week forecast | 29.5 |
| **Upload → report → scenario** | | **137.1** |
| 7. Share | The gate explained; an admin's reason; Approve and go live; an embed link | 0.4 |

Nearly all of the 137 s is the system working: the beliefs preview, the fit (80 s), the exact
run, the channel-off run and the forecast; clicks, typing and page loads take a few seconds. A person reads, hesitates and
explores, so the 30-minute budget is about how long the testers take to *understand* each
screen: the script shows that the system's share is about two and a half minutes on this
laptop.

The second test walks the **Integrations hub**: the overview cards, the endpoint page, Try it
(validate, then score: 8.6 s), the curl snippet copied, a `score:<alias>` token from the
snippet's shortcut, a webhook with its one-time secret, and the embed link: it renders
read-only in a new page (no button to press) and, once revoked in the hub, the page says
"This link has expired or was revoked".

### What the review found and fixed

Every screenshot was read as a first-time analyst would: is it plain language, is the next
step obvious, is there a dead end, is an error explained? Fixed on this branch, each with a
test:

| Finding | Where | Fix |
|---|---|---|
| **"Run exact" always failed** on the live stack ("User or plugin code failed"): the compiled counterfactual named no data | What if… | `POST /v1/guided/requests:compile` names the fit dataset in `data` for what-ifs and presets over the fit window; a score request without data is refused with `422` before a run starts |
| **"Forecast error on held-back weeks: 83 %"** for a 0.8 % error (`mapi_sdk` reports MAPE in percent) | Results | The headline reads the scorecard's measured fraction |
| **Dead end**: the template's fit failed the convergence checks on two years of weekly data, the checks' advice ("raise target_accept") names settings Guided screens hide, and Share said "cannot go live" | Results, Share | The `geo_mmm` template (1.1.0) samples longer and more carefully; a warned or failed convergence check offers **Fit again, more carefully**; a blocked Share step links to it |
| **Beliefs the analyst never stated**: every channel started ticked with a 0.5–4 ROI range (search's true ROI was 6.3) although the page says "leave a channel unticked if you have no view" | What you believe | Channels start unticked; ticking one starts from the sensible range |
| "Results without any media: between −164 % and 271 %" | Beliefs preview | Without a stated baseline: "anything from none to all of your results - the model has no view yet" |
| A revoked embed link kept rendering for a minute (`Cache-Control: public, max-age=60`) | Embed page | `private, no-cache`: revocation applies on the next load |
| Jargon: "y_hat" on the fit chart's axis and caption; "evenly spaced (W-SUN)"; "the host did not resolve (gaierror)"; "Sampler convergence" offered first under Share a chart | Report, data step, webhook dialog, Share | The dataset's name ("revenue"); "weekly, on Sundays"; "no such name"; result charts first |

Also fixed: `mapi_client` refused to upload an MFF table (a CSV file) with a Guided MFF
draft's mapping.

Seen and left for later (not blocking the flow, noted for the testers' sessions):

- the fit chart facets markets under the dim name `geo` (the forecast chart's y axis no
  longer starts at zero, which flattened the fan: fixed in the M3 review);
- the endpoint page shows who promoted a version as a user id, not a name;
- the header showed Expert mode on the Integrations hub: that test runs in a new browser
  context, and the default tenant (not a self-service one) started in Expert mode there.
  The choice is remembered per browser; since then a self-service workspace starts in
  Guided mode, and Expert navigation and Home link to Guided;
- the fit takes about 80 s with the sturdier settings (30 s before), still well within the
  budget.

## Protocol for the five non-technical testers

The exit criterion needs people. Run this protocol with **five** testers, one at a time,
against a stack like the one above (or staging), and record the results on the observation
sheet.

### Who

- Five people who analyse marketing or sales results in spreadsheets (marketing analysts,
  media planners, finance business partners), with **no** statistics or programming
  training beyond spreadsheet formulas. Screen them with two questions: "Have you fitted a
  regression or written Python or R?" (no) and "Do you work with weekly spend and sales by
  market?" (yes).
- None of them has seen modeling-api before. Nobody from the team that built it.

### Setup (before each session)

1. A fresh workspace for the tester: sign up at `/signup` with their work address, in a
   fresh browser profile. Signing in after the e-mail link opens an empty Guided page, and a
   self-service workspace starts in Guided mode on any browser. With an operator-created
   `internal` tenant instead (a stack without a sandboxed executor), the session starts on
   Expert Home: finding **Guided** in the sidebar is then part of T1.
2. The spreadsheet on their desktop: `uv run python tests/e2e/m3_browser_seed.py seed --run
   <id>` writes `weekly_media_by_region.csv` (two years, five regions, three channels), or
   the tester's own anonymised data in the same shape.
3. A screen recorder, a stopwatch, and the observation sheet. The facilitator sits beside or
   behind the tester and does not touch the keyboard.

### Script

Read this aloud, word for word:

> "Thank you for helping. We are testing the software, not you; anything that is hard is our
> fault. Please think aloud: say what you are looking for and what you expect to happen. I
> cannot help you during the tasks - if you would give up in real life, tell me and we move
> on. You have up to 30 minutes in all."

Then give one task card at a time.

| Task | Card | Success means |
|---|---|---|
| T1 Upload | "Here is last two years' media spend and revenue by region. Load it into the tool so it can explain what each channel brings in." | The analysis reaches "What you believe" with Revenue, Week, Region, the three spend columns and Avg Price answered correctly |
| T2 Report | "Get the tool to tell you which channel returns the most per euro, and whether you can trust the numbers." | The fit finishes; the tester names the best channel and its range, and says what the scorecard concluded, in their own words |
| T3 Scenario | "Next quarter you could move 20 % more into search. What would it do to revenue? How sure is the tool?" | The tester gives the change in revenue with its range (instant or exact), from the What if… step |

The optional "What you believe" step counts as part of T1 to T2: testers may skip it.

### Rules

- **Unaided**: the facilitator answers no question about the product. Allowed: repeating the
  task card, "what would you do if I were not here?", "please keep thinking aloud". Any other
  help is an **assist** and is recorded; a task with an assist does not count as unaided.
- **Give-up**: the tester says so, or 12 minutes pass on one task. Record it, give the next
  card; if a later task needs the earlier one's result, the facilitator performs that step
  silently and the later task's time starts afterwards.
- Waiting for the fit counts towards the 30 minutes (about 1.5 minutes on a laptop).

### Metrics and pass mark

Per tester and task: completion (unaided, assisted or failed), time on task, the number of
errors (a wrong turn the tester had to undo), and the Single Ease Question (1 = very
difficult to 7 = very easy) after each task. After the session: the System Usability Scale
(10 items) and two open questions: "What was most confusing?" and "What would you trust
least?".

The criterion is met when **all five** testers complete T1, T2 and T3 **unaided** with a
**total under 30 minutes**. Also report the median and maximum total time, the mean SEQ per
task and the mean SUS (aim: above 68). Every assist, give-up or recurring error is a defect
to fix before the public launch, even when the five pass.

### Observation sheet

Copy one per tester.

| Field | T1 Upload | T2 Report | T3 Scenario |
|---|---|---|---|
| Start / end (mm:ss) | | | |
| Completed (unaided / assisted / failed) | | | |
| Errors (wrong turns, with the screen) | | | |
| Assists (what was said) | | | |
| Words or screens the tester did not understand | | | |
| Answer given (T2: best channel, trust; T3: change and range) | | | |
| SEQ (1-7) | | | |

| Session | |
|---|---|
| Tester (role, years of experience; no name) | |
| Total time | |
| SUS score | |
| Most confusing | |
| Would trust least | |
| Facilitator notes (quotes, hesitations, where the cursor went first) | |

## What remains unverified

- **The five testers.** Only the automated floor and an expert review were done (above).
- **Real Google Cloud.** Cloud Storage (the GCS shim stood in), Cloud Run Jobs (the
  `local-process` executor ran every run), Pub/Sub, Cloud SQL, Identity Platform, Cloud
  Armor (the policy is Terraform, validated and never applied) and Cloud Logging retention.
  The cold start of the base images adds to every fit, exact run and Atlas evaluation.
- **Docker.** No daemon here: the images, the `docker` and `docker-gvisor` executors and
  the compose stack were not run, so public (shared-tier) workspaces never ran a fit; cut
  line A's admission rule was checked, its sandbox was not.
- **Plotly rendering.** Static renders and embeds were Vega-Lite only; Plotly needs kaleido
  and a Chrome in the runner image (`MAPI_RENDER_PLOTLY_ENABLED`), which this laptop does not
  provide.
- **Identity verification by a vendor.** Only the fake `IdentityVerifier` and the
  operators' manual verification exist; no vendor integration was exercised.
- **Real e-mail.** The dev outbox stood in for SMTP: deliverability, SPF/DKIM and bounce
  handling are untested.
- **Webhooks to the internet.** Deliveries went to a receiver on `127.0.0.1` through the
  dev allow-list; the full SSRF policy for public hosts is covered by unit tests only.
- **Bundle retention.** With a retention lock (the default, and 30 days when deployed) a
  purge stops at the model bundles and resumes when the lock expires; locally this was
  checked only up to the stop (the journey's deletion test does so on a stack without
  `MAPI_BUNDLE_RETENTION_DAYS=0`), not the resumption after 30 days.
- **The Atlas in-sandbox `optimize` job** is M4; here Atlas ran on the client.
