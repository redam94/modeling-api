---
description: What was verified of the M3 exit criteria (Guided mode and integrations) on a laptop, the measured times, the defects the runs found, the protocol for the five non-technical testers, and what still needs people, Docker or Google Cloud.
---

# M3 verification <span class="mapi-badge">v0.4</span>

Milestone M3 (Guided mode and integrations) has three exit criteria:

1. Five non-technical testers complete **upload → report → scenario** without help in less
   than 30 minutes.
2. **An external budget optimiser works against an endpoint**.
3. **Cut line A**: public launch of declarative specs and built-ins (untrusted data, trusted
   code).

This page records how much of each criterion we verified **locally**, and the measured
numbers. It also tells what the runs found and fixed, and what remains. We cannot simulate
real testers. Thus we prepared the first criterion, but it is not met. An automated "unaided
tester" script measures the minimum of the time budget. We also examined the screens as an
analyst who uses them for the first time. The
[protocol for the five testers](#protocol-for-the-five-non-technical-testers) below is the
work that remains.

We did the verification on 2026-10-16 on branch `feat/m3-e2e` (v0.4 development), on an
Apple-silicon laptop (8 cores, 16 GB, macOS), against a live `make dev` stack:

- The API, worker, web app and plot sandbox on ports that are not the defaults (API `18000`,
  GCS shim `14443`, web `15173`, plot sandbox `15174`).
- The `dev` profile: the development identity provider and the outbox mailer.
- The GCS shim in place of Cloud Storage, and the embedded `pgserver` Postgres.
- All runs on the `local-process` executor (no Docker daemon, no cloud project).
- `MAPI_TENANT_DELETION_GRACE_S=0` and `MAPI_BUNDLE_RETENTION_DAYS=0`, so that we could
  watch a deletion to the end.
- Higher `MAPI_SIGNUP_PER_IP_PER_HOUR` and `MAPI_SIGNUP_PER_DOMAIN_PER_DAY`, because each run
  signs up new workspaces from one address.

## Summary

| Exit criterion | Verified locally | Result | Still needs |
|---|---|---|---|
| 5 non-technical testers: upload → report → scenario without help in < 30 min | The browser script `apps/web/e2e/m3-guided.e2e.spec.ts` clicks through the whole Guided flow on a realistic spreadsheet, and measures the time of each step. We examined each screen for plain language, next steps and dead ends | **135 s** of automated wall time. The system used almost all of this time (the beliefs preview, the fit, the exact runs). We found and repaired six usability defects. Two of them were dead ends | Five real testers ([protocol](#protocol-for-the-five-non-technical-testers)) |
| An external optimiser works against an endpoint | The optimiser adapter of the v0.4 client (removed in v0.5) against the live endpoint. The optimiser uses the protocol of the external framework | Under the model, the selected allocation is better than the current one: **+0.22 %** revenue for next quarter at the same budget. It came from 25 candidate allocations (22 score requests) in 220 s | The in-sandbox `optimize` job (M4) |
| Cut line A | A self-service sign-up makes a `public` workspace. The e-mail is verified, and the server refuses uploaded code (`uploaded_code_allowed: false`). Built-ins and declarative specs run. Here they ran on an `internal` workspace that an operator promoted, because the local executor admits only trusted tiers | Pass | Public workspaces that run on `docker-gvisor` or Cloud Run Jobs. Shared tiers can use only these executors |

`tests/e2e/test_m3_guided_stack.py` (15 tests, 7 minutes) also tested all M3 additions from
end to end through the API: **15 passed**.

## The API journey

```bash
make dev        # with the variables above
make e2e        # or: uv run --with-editable ../optimizer-framework python -m pytest -s -m e2e tests/e2e/test_m3_guided_stack.py
```

The tests use the workspace of one analyst, from outside the processes. Each row is one
test, in sequence. The times are the wall-clock seconds of the last run.

| Step | What is checked | Time |
|---|---|---|
| Sign-up | `POST /auth/signup` → the test reads the verification e-mail from `GET /dev/outbox` → `POST /auth/verify-email` makes a `public` workspace, with the person who signed up as admin. The operator promotes it to `internal` (the local executor runs only trusted tiers) | 0.03 |
| Guided data | All four templates are in the list. The test profiles the CSV of the analyst locally, as the browser does (5 regions × 3 channels × 104 weeks, columns `Week, Region, Revenue, TV Spend, Search Spend, Social Spend, Avg Price`). Role detection answers each question correctly, each with a plain reason. The draft validates. The upload ingests with the mapping of the draft. Detection on the stored version (`columns.json`) gives the same answers | 13.3 |
| MFF | The same data as an MFF long table (`Period, Geography, VariableName, VariableValue`) ingests to the same variables and roles | 8.3 |
| Business priors | ROI and share ranges and a baseline share (true to the simulation), and a lift test. A sandboxed `translate_priors` run translates them. The translated priors hold 81 %, 76 % and 80 % of their mass in the stated 80 % ranges. The test writes them into the spec with the id of the run | 13.3 |
| Tiny fit | 150 tuning steps and draws × 2 chains. The test measures each check of the scorecard of the template, in plain words with a docs anchor and an action. Its gate (**blocked**: the chains do not agree) is exactly what `mapi_spec.scorecard.promotion_gate` calculates. The server refuses the promotion with the checks of that gate | 30.9 |
| Endpoint fit | 1000 × 4 chains, `target_accept` 0.95. Convergence passes or gives a warning. The holdout error is below 1 %, coverage passes, and each plausibility check passes | 61.0 |
| What-if vs Run exact | The response grid for each draw answers a slider immediately. "Run exact" (compiled by the server) scores the fit window. tv × 0.5 → grid −454,590 vs exact −454,241 (sd 14,003). search × 1.5 and tv × 0.8 → −4,762 vs −4,255 (sd 14,980). The server refuses multipliers outside the grid | 10–13 for each exact run |
| Forecast, channel off | An 8-week forecast fan with history and the split line. tv off over the fit window gives −1,046,519, against −1,046,414 from the multiplier 0 of the grid | 10 each |
| Promotion | `409 OVERRIDE_REQUIRED`, which names the checks with warnings. Then an admin override with a reason, which the audit log records. History, schema with an example call, and snippets (no snippet contains a secret) | 0.01 |
| Score token | A `score:<alias>` token scores the example of the endpoint (8-week forecast) as an `xarray` tree. It gets `403` on datasets, specs, webhooks and models | 10.0 |
| Webhooks | An HTTPS receiver on `127.0.0.1` (with the certificate of the dev profile) gets `run.succeeded`, `model.version.ready`, `endpoint.promoted` and `score.completed`. The test verifies each with `mapi_client.webhooks.verify`. No body contains a data value, link or secret. The delivery log shows them. A replay comes as the same event with a new delivery id. A different secret fails verification | 0.3 |
| Embed | A saved platform-recipe plot as an embed link. The public view needs no credentials. Each write method answers `405`. The sandbox page has its CSP. After revocation, `410 LINK_EXPIRED` | < 1 |
| Budget optimisation | Refer to [An external optimiser](#an-external-optimiser-against-the-live-endpoint) | 219.6 |
| GDPR export | A zip with 49 files downloads. It holds the records and objects of the workspace. It does not contain the webhook secret, the score token or the ID token | 16.4 |
| Deletion | 645 tenant rows and 285 objects before the purge. After the purge, no table with a `tenant_id` has a row, but these are retained: `audit_events` (53), `cost_ledger` (33) and the deletion record. There is **no object** under `t/<tenant>/` in the five buckets. A signed `tenant.deletion.completed` goes to the receiver. The server refuses the admin and the score token | 13.4 |

### An external optimiser against the live endpoint

An external optimisation framework (a sibling project) has the model and optimiser
*interfaces*. The test registered the endpoint-backed model adapter of the v0.4 client as one
of its models. Then it ran an optimiser that uses its protocol. The optimiser moves a step of
budget between two channels while this increases the predicted revenue of the endpoint. When
no move increases it, the optimiser makes the step half as large. Each candidate is one score
request against the live endpoint (inline XrJSON, `score:<alias>` token).

| | Search | Social | TV | Predicted revenue (13 weeks) |
|---|---|---|---|---|
| Current plan | 16,136 | 9,737 | 37,890 | 929,612 |
| Chosen | 19,324 | 9,737 | 34,702 | 931,620 (**+0.22 %**) |

25 candidates used 22 requests (the cache answered the others). The total time was 220 s.
Each request is a sandboxed score run, so the wall time is mostly run start-up. You cannot
put the markets of a geo MMM into one request (new markets are pooled). For this reason, the
in-sandbox `optimize` job (M4) is the path for longer optimisations.

## The unaided-tester script

```bash
MAPI_E2E=1 pnpm --filter @mapi/web exec playwright test e2e/m3-guided.e2e.spec.ts
```

`tests/e2e/m3_browser_seed.py` makes a new project and writes the spreadsheet of the analyst.
The script then does the tasks that the five testers will do, without hesitation, as the
admin of the default tenant. Screenshots go to `apps/web/e2e/screenshots/m3-*.png` (git
ignores them). The times go to `m3-timings.json`.

The times below are from the release check of v0.4.0 on the integrated `develop` branch
(2026-10-16, the same laptop and settings). The first run on `feat/m3-e2e` took 137 s.

| Step | What the script does | Wall time (s) |
|---|---|---|
| 1. Template | Starts where sign-in goes (`/`) and finds **Guided**. "What do you want to learn?" → Geo marketing mix | 0.7 |
| 2. Your data | Gives the analysis a name and selects the CSV. Makes sure that each question has the correct answer. Upload and continue (upload, sandboxed check) | 4.7 |
| 3. What you believe | Selects tv, states an ROI of 1.5 to 4, shows a preview of what it means (a sandboxed run) and continues | 20.7 |
| 4. Fit the model | Fit the model. Phases and progress in plain language. See the results | 78.5 |
| 5. Results | Headline numbers, ROI and share for each channel, two charts, the sanity scorecard | 0.4 |
| 6. What if… | Search spend +20 % on the slider (immediate), Run exact, Turn off tv, a 13-week forecast | 29.6 |
| **Upload → report → scenario** | | **134.6** |
| 7. Share | The gate explained. The reason of an admin. Approve and go live. An embed link | 0.4 |

The system uses almost all of the 135 s: the beliefs preview, the fit (79 s), the exact run,
the channel-off run and the forecast. Clicks, typing and page loads take some seconds. A
person reads, hesitates and explores. Thus the 30-minute budget is mostly the time that the
testers need to *understand* each screen. The script shows that the system uses about two and
a half minutes of it on this laptop.

The second test goes through the **Integrations hub**:

- The overview cards and the endpoint page.
- Try it (validate, then score: 8.6 s), and a copy of the curl snippet.
- A `score:<alias>` token from the shortcut of the snippet.
- A webhook with its one-time secret.
- The embed link. It shows read-only in a new page (no button to push). After the hub
  revokes it, the page shows "This link has expired or was revoked".

### What the review found and fixed

We read each screenshot as an analyst who uses the app for the first time. We asked these
questions: Is the language plain? Is the next step clear? Is there a dead end? Does the app
explain each error? We fixed these items on this branch, each with a test:

| Finding | Where | Fix |
|---|---|---|
| **"Run exact" always failed** on the live stack ("User or plugin code failed"). The compiled counterfactual named no data | What if… | `POST /v1/guided/requests:compile` names the fit dataset in `data` for what-ifs and presets over the fit window. The server refuses a score request without data with `422` before a run starts |
| **"Forecast error on held-back weeks: 83 %"** for an error of 0.8 % (`mapi_sdk` reports MAPE in percent) | Results | The headline reads the measured fraction of the scorecard |
| **Dead end**: the fit of the template failed the convergence checks on two years of weekly data. The advice of the checks ("raise target_accept") names settings that Guided screens do not show. Share showed "cannot go live" | Results, Share | The `geo_mmm` template (1.1.0) samples longer and more carefully. A convergence check with a warning or failure offers **Fit again, more carefully**. A blocked Share step links to it |
| **Beliefs that the analyst did not state**: each channel started selected with an ROI range of 0.5–4 (the true ROI of search was 6.3). But the page says "leave a channel unticked if you have no view" | What you believe | Channels start not selected. When you select one, it starts from the sensible range |
| "Results without any media: between −164 % and 271 %" | Beliefs preview | Without a stated baseline: "anything from none to all of your results - the model has no view yet" |
| A revoked embed link continued to show for one minute (`Cache-Control: public, max-age=60`) | Embed page | `private, no-cache`: revocation applies on the next load |
| Jargon: "y_hat" on the axis and caption of the fit chart. "evenly spaced (W-SUN)". "the host did not resolve (gaierror)". "Sampler convergence" first under Share a chart | Report, data step, webhook dialog, Share | The name of the dataset ("revenue"). "weekly, on Sundays". "no such name". Result charts first |

We also fixed this item: `mapi_client` refused to upload an MFF table (a CSV file) with the
mapping of a Guided MFF draft.

We saw these items and left them for later. They do not block the flow, and we noted them
for the sessions of the testers:

- The fit chart showed markets under the dim name `geo`. The y axis of the forecast chart
  started at zero, and this made the fan flat. The M3 review fixed the two items: panels use
  the name of the dataset, and the axis fits the band.
- The endpoint page showed the user id of the person who promoted a version, not a name. The
  M3 review fixed this: names come from the members of the project.
- The header showed Expert mode on the Integrations hub. That test runs in a new browser
  context, and there the default tenant (not a self-service tenant) started in Expert mode.
  The browser remembers the selection. Since then, a self-service workspace starts in Guided
  mode, and Expert navigation and Home link to Guided.
- The fit takes about 80 s with the stronger settings (30 s before). This is still well in
  the budget.

## Protocol for the five non-technical testers

The exit criterion needs people. Do this protocol with **five** testers, one at a time.
Use a stack like the stack above (or staging). Record the results on the observation sheet.

### Who

- Five persons who analyse marketing or sales results in spreadsheets (marketing analysts,
  media planners, finance business partners). They must have **no** statistics or
  programming training, other than spreadsheet formulas. Select them with two questions:
  "Have you fitted a regression or written Python or R?" (no) and "Do you work with weekly
  spend and sales by market?" (yes).
- None of them has seen modeling-api before. Nobody from the team that built it.

### Setup (before each session)

1. Make a new workspace for the tester. Sign up at `/signup` with their work address, in a
   new browser profile. Sign in after the e-mail link. This opens an empty Guided page. A
   self-service workspace starts in Guided mode on all browsers. If you use an `internal`
   tenant that an operator made (a stack without a sandboxed executor), the session starts
   on Expert Home. Then the tester must find **Guided** in the sidebar as part of T1.
2. Put the spreadsheet on the desktop of the tester. `uv run python tests/e2e/m3_browser_seed.py seed --run
   <id>` writes `weekly_media_by_region.csv` (two years, five regions, three channels). You
   can also use the anonymised data of the tester in the same shape.
3. Prepare a screen recorder, a stopwatch and the observation sheet. The facilitator sits
   next to or behind the tester. The facilitator does not touch the keyboard.

### Script

Read this aloud, word for word:

> "Thank you for your help. We test the software, not you. If something is difficult, it is
> our fault. Please think aloud: say what you look for and what you think will occur. I
> cannot help you during the tasks. If you would stop in real life, tell me, and we go to the
> next task. You have a maximum of 30 minutes for all tasks."

Then give one task card at a time.

| Task | Card | Success means |
|---|---|---|
| T1 Upload | "Here is last two years' media spend and revenue by region. Load it into the tool so it can explain what each channel brings in." | The analysis gets to "What you believe" with correct answers for Revenue, Week, Region, the three spend columns and Avg Price |
| T2 Report | "Get the tool to tell you which channel returns the most per euro, and whether you can trust the numbers." | The fit finishes. The tester names the best channel and its range, and says in their own words what the scorecard concluded |
| T3 Scenario | "Next quarter you could move 20 % more into search. What would it do to revenue? How sure is the tool?" | The tester gives the change in revenue with its range (immediate or exact), from the What if… step |

The optional "What you believe" step is part of T1 to T2. Testers can skip it.

### Rules

- **Unaided**: the facilitator answers no question about the product. Permitted: say the
  task card again, "what would you do if I were not here?", "please keep thinking aloud".
  All other help is an **assist**. Record it. A task with an assist is not unaided.
- **Give-up**: the tester says that they stop, or 12 minutes go by on one task. Record it
  and give the next card. A later task can need the result of an earlier task. If this
  occurs, the facilitator does that step without comment. The time of the later task starts
  after this step.
- The time to wait for the fit is part of the 30 minutes (about 1.5 minutes on a laptop).

### Metrics and pass mark

For each tester and task, record these items:

- Completion (unaided, assisted or failed).
- Time on task.
- The number of errors (a wrong turn that the tester had to undo).
- The Single Ease Question (1 = very difficult to 7 = very easy) after each task.

After the session, record the System Usability Scale (10 items) and two open questions:
"What was most confusing?" and "What would you trust least?".

The criterion is met when **all five** testers complete T1, T2 and T3 **unaided** with a
**total under 30 minutes**. Also report the median and maximum total time, the mean SEQ for
each task and the mean SUS (target: more than 68). Each assist, give-up or error that occurs
again is a defect. Repair it before the public launch, also when the five testers pass.

### Observation sheet

Copy one sheet for each tester.

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

- **The five testers.** We did only the automated minimum and an expert review (above).
- **Real Google Cloud.** Cloud Storage (the GCS shim was in its place), Cloud Run Jobs (the
  `local-process` executor ran all runs), Pub/Sub, Cloud SQL, Identity Platform, Cloud Armor
  (the policy is Terraform, validated and never applied) and Cloud Logging retention. The
  cold start of the base images adds time to each fit, exact run and optimiser evaluation.
- **Docker.** There is no daemon here. We did not run the images, the `docker` and
  `docker-gvisor` executors or the compose stack. Thus public (shared-tier) workspaces never
  ran a fit. We did a check of the admission rule of cut line A, but not of its sandbox.
- **Plotly rendering.** Static renders and embeds were only Vega-Lite. Plotly needs kaleido
  and a Chrome in the runner image (`MAPI_RENDER_PLOTLY_ENABLED`). This laptop does not have
  them.
- **Identity verification by a vendor.** Only the fake `IdentityVerifier` and the manual
  verification of the operators are available. We did not test a vendor integration.
- **Real e-mail.** The dev outbox was in place of SMTP. We did not test deliverability,
  SPF/DKIM and bounce handling.
- **Webhooks to the internet.** Deliveries went to a receiver on `127.0.0.1` through the dev
  allow-list. Only unit tests include the full SSRF policy for public hosts.
- **Bundle retention.** With a retention lock (the default, and 30 days when deployed), a
  purge stops at the model bundles. It continues when the lock expires. Locally, we did a
  check only up to the stop, not of the continuation after 30 days. The deletion test of the
  journey does this check on a stack without `MAPI_BUNDLE_RETENTION_DAYS=0`.
- **The in-sandbox `optimize` job** came later (v0.5, on mmm-framework). Here the optimiser
  ran on the client.
