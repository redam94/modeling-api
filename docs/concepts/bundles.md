# Bundles

A **bundle** contains all the results of a fit that scoring needs. Its form is safe to store,
move and read again. A bundle is one `bundle.zarr.zip` and four JSON sidecars.

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

`Bundle.read` calculates the size, CRC32C, MD5 and SHA-256 of each file again. Then it
compares them with the manifest. If a bundle is damaged or changed without permission,
`Bundle.read` raises `BundleError` (`INTEGRITY_FAILURE`).

## What is inside

```python
--8<-- "quickstart.py:inspect"
```

| Node | Holds |
|---|---|
| `/spec` | The canonical spec and the resolved spec (pinned versions, the schema of each stage) |
| `/schema/raw`, `/schema/model` | The data contract at the input and at the model stage |
| `/binding` | Canonical ↔ dataset names, reversed on each output |
| `/preprocess/<step>` | For each step: the learned JSON state, labelled `arrays` and the `context` tail for each group |
| `/model` | The manifest, settings, fit config and learned JSON |
| `/model/<group>` | ArviZ-named groups: `posterior`, `sample_stats`, `log_likelihood`, `observed_data`, … |
| `/terminal_state` | Sequential models: the filtered state at the last fitted step |
| `/native` | Safe native artifacts: skops files, xgboost JSON, … |
| `/derived/<name>` | Derived quantities cached at fit (contributions, response curves) |
| `/fit_coords/<stage>` | The fitted coordinates at each stage (CoordPlan checks against them) |
| `/side_tables/<name>` | `pinned` side tables, frozen with the fit |
| `/thin` | The thinning index for draws, for the full bundle |

## No pickle, ever

Pickle runs code when it loads. Thus pickle must not be in an artifact that moves between
users, services and sandboxes.

- Model groups are labelled arrays in zarr. Settings, states and manifests are JSON.
- Native estimators are stored as **skops** or **xgboost JSON**. When skops loads a file, it
  first lists the types in the file. It refuses all types that are not on the allow list of
  that estimator.
- Some `FitOutput.native` entries look like pickle (`.pkl`, `.joblib`, `.pt`, pickle magic
  bytes, …). The platform rejects these entries when it writes a bundle **and** when it reads
  a bundle. The contract kit checks this for each plugin.

## Hashes in the forms object stores report

The platform records CRC32C and MD5 in base64, the same as Google Cloud Storage calculates
them on upload. The platform binds a signed manifest to the object generations and to the
hashes that the server calculated. Thus a person cannot replace or replay an uploaded
artifact (see [Storage and integrity](architecture.md#storage-and-integrity)). The platform
also records SHA-256, for content addressing.

## Lineage

The manifest records the spec hash, the data hash, the version of the plugin and of each step,
and the library versions of the fit. `manifest.lineage` links a production bundle to its
evaluation bundle. Thus you can always find which data and which code made a forecast.

Next: [Coord plans and typed errors](coord-plans.md).
