# Bundles

A **bundle** is everything a fit produced that scoring needs, in a form that is safe to store,
move and read back: one `bundle.zarr.zip` plus four JSON sidecars.

```text
fruit-demand-v1/
├── bundle.zarr.zip      # one zarr v3 zip: the whole fitted model as an xr.DataTree
├── manifest.json        # identity, provenance, lineage and the hashes of every file
├── metrics.json         # holdout metrics (computed through the score path)
├── diagnostics.json     # convergence and sanity checks, expert and plain-language
├── plots.json           # rendered, sanitized plots for outputs.plots
└── evaluation/          # evaluate_then_refit: the train-only bundle behind the metrics
```

## Write, read, verify

```python
--8<-- "quickstart.py:bundle"
```

`Bundle.read` recomputes the size, CRC32C, MD5 and SHA-256 of every file and compares them with
the manifest. A damaged or tampered bundle raises `BundleError` (`INTEGRITY_FAILURE`).

## What is inside

```python
--8<-- "quickstart.py:inspect"
```

| Node | Holds |
|---|---|
| `/spec` | The canonical spec and the resolved spec (pinned versions, every stage's schema) |
| `/schema/raw`, `/schema/model` | The data contract at the input and at the model stage |
| `/binding` | Canonical ↔ dataset names, reversed on every output |
| `/preprocess/<step>` | Each step's learned JSON state, labelled `arrays` and per-group `context` tail |
| `/model` | The manifest, settings, fit config and learned JSON |
| `/model/<group>` | ArviZ-named groups: `posterior`, `sample_stats`, `log_likelihood`, `observed_data`, … |
| `/terminal_state` | Sequential models: the filtered state at the last fitted step |
| `/native` | Safe native artifacts: skops files, xgboost JSON, … |
| `/derived/<name>` | Derived quantities cached at fit (contributions, response curves) |
| `/fit_coords/<stage>` | The fitted coordinates at every stage (what CoordPlan checks against) |
| `/side_tables/<name>` | `pinned` side tables, frozen with the fit |
| `/thin` | The bundle-wide thinning index for draws |

## No pickle, ever

Pickle executes code when it is loaded, so it has no place in an artifact that moves between
users, services and sandboxes.

- Model groups are labelled arrays in zarr; settings, states and manifests are JSON.
- Native estimators are stored as **skops** (loading lists the file's types first and refuses
  anything outside that estimator's allow-list) or **xgboost JSON**.
- `FitOutput.native` entries that look like pickle (`.pkl`, `.joblib`, `.pt`, pickle magic
  bytes, …) are rejected when a bundle is written **and** when it is read. The contract kit
  checks it for every plugin.

## Hashes in the forms object stores report

CRC32C and MD5 are recorded base64-encoded, the way Google Cloud Storage computes them on
upload. The hosted platform will bind a manifest to the object generations and server-computed
hashes, so an uploaded artifact cannot be swapped or replayed. SHA-256 is recorded too, for
content addressing.

## Lineage

The manifest records the spec hash, the data hash, the plugin and every step version, and the
library versions a bundle was fitted with; `manifest.lineage` links a production bundle to its
evaluation bundle. "Which data and which code made this forecast?" always has an answer.

Next: [Coord plans and typed errors](coord-plans.md).
