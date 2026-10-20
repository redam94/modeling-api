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
| `/model/fit_history` | Fits in steps (ADVI): `loss(step)`, the loss of each optimization step, at most 5,000 steps |
| `/model/metrics_history` | v0.11: the metrics, params and tags that the model logged during the fit (see [below](#metrics-history)) |
| `/terminal_state` | Sequential models: the filtered state at the last fitted step. For example, a forecast of `ts.bsts` (v0.13) starts from it. |
| `/native` | Safe native artifacts: skops files, xgboost JSON and, from v0.15, the safetensors weights of a network |
| `/derived/<name>` | Derived quantities cached at fit (contributions, response curves) |
| `/fit_coords/<stage>` | The fitted coordinates at each stage (CoordPlan checks against them) |
| `/side_tables/<name>` | `pinned` side tables, frozen with the fit |
| `/thin` | The thinning index for draws, for the full bundle |

## The model groups of a fit in steps and of a state model

Some plugins keep more groups under `/model`. The standard plots read them:

| Group | Plugins | Holds | Plot |
|---|---|---|---|
| `fit_history` | PyMC models with `engine: advi` | `loss(step)`: the negative ELBO of each step. The var attrs give `objective` and `engine`. When the fit has more steps than 5,000, the dataset attr `thinned_from` gives the number of steps. | `core.loss_curve` |
| `hidden_states` | `ts.gaussian_hmm` | `state_prob(chain, draw, animal, step, state)` | `core.state_sequence` |
| `regimes` | statsmodels Markov switching | `smoothed_prob(time, regime)`, `filtered_prob`, the transition matrix | `core.state_sequence` |
| `components` | unobserved components, ETS, `ts.hsgp_trend`, `ts.bsts` (v0.13) | One variable for each part of the series. A variable with the attr `decomposition: exclude` (a trend slope) is not an additive part. | `core.decomposition` |

Each data variable of `fit_history` is one curve over `step`. The other dims of a variable are
labels: the plot draws one line for each level.

## The groups of the model packs <span class="mapi-badge">v0.12</span> { #model-pack-groups }

The marketing-mix, causal and deep-learning models keep more groups under `/model`. Their
plots and workflow nodes read these groups, not the posterior:

| Group | Plugins | Holds | Read by |
|---|---|---|---|
| `lift_tests` (v0.12) | `mmm.bayesian`, when the fit used lift tests | Each lift test (`observed(test)`, `observed_sd(test)`) and the estimate of the model for each draw (`model(chain, draw, test)`) | `mmm.lift_tests` |
| `impact` (v0.13) | `causal.impact`, `causal.synthetic_control`, `causal.did` | `actual`, `counterfactual`, `effect`, `cumulative_effect` and `post`, in the units of the target ([Measure the effect of a change](causal-effects.md)) | `causal.impact`, `causal.pre_fit`, `model.lift_test` |
| `donor_weights` (v0.13) | `causal.synthetic_control` | `weight(sample, unit, donor)` and the error before the test, `pre_rmse(unit)` | `causal.donor_weights` |
| `event_study` (v0.13) | `causal.did` | `coef(sample, event_time)`, `n_cells(event_time)` and the average effect after the change, `att(sample)` | `causal.event_study` |
| `training` (v0.15) | `dl.mlp`, `dl.cnn_classifier`, `dl.finetune_classifier` | Values along `epoch`: `train_loss`, `val_loss`, the training and validation metric (`accuracy` or `rmse`) and `lr`. A classifier also keeps `confusion(epoch, true_class, pred_class)`, and then only the epochs with a confusion matrix. The attrs give `best_epoch`, `stopped_early`, `epochs_run` and `steps`. The group keeps a maximum of 200 epochs (50 for a classifier), always with the first, the best and the last. The attr `thinned_from` gives the number of epochs before. | `ml.training_curves`, `ml.confusion_epochs` |
| `misclassified` (v0.15) | `dl.cnn_classifier`, `dl.finetune_classifier` | Up to 32 validation images that the network classified incorrectly at the best epoch, as small thumbnails with the true and the predicted label | `ml.misclassified` |
| `posterior` (v0.15) | the `dl.*` models | `draw_id(chain, draw)`: one entry for each draw of the Laplace approximation or of MC dropout. The harness thins the draws of a score request from this group. | the score path |

The `dl.*` models keep the trained network in `/native`: `weights.safetensors` and, for the
Laplace draws, `laplace.safetensors`. These are safetensors files, not pickle. The
architecture of the network is in the metadata of the weights file.

## The `metrics_history` group <span class="mapi-badge">v0.11</span> { #metrics-history }

When model code logs metrics during a fit (`ctx.log_metric`, see
[Track metrics, params and tags](../guides/tracking.md)), the fit also keeps them in the
bundle, in the group `/model/metrics_history`. The plot `core.loss_curve` reads this group in
the same way as `fit_history`. The group has this layout:

| Part | Content |
|---|---|
| Data variables | One variable for each metric key that has steps (a maximum of 64 keys). The attr `mapi_dropped_keys` counts the keys that are not kept. The variable of a key named `step` is `metric_step` (var attr `mapi_key`), because `step` is the name of the dim. |
| Dim `step` | `int64`, the steps of all the series of the key |
| Label dims | One dim for each label key (for example `chain`). The coords are the label values, sorted. |
| Empty cells | NaN where a (step, labels) cell has no point |
| Long series | Each series keeps a maximum of 5,000 points, evenly spaced, with the last point. The var attr `thinned_from` gives the number of points before. |
| Attr `mapi_scalars` | The points without a step, as JSON text: `{key: {labels_key: value}}`. `labels_key` is `k1=v1,k2=v2`, or `""` for no labels. |
| Attr `mapi_params` | The params, as JSON text |
| Attr `mapi_tags` | The tags, as JSON text |

All the attrs are text, thus the group is safe in zarr. A fit without logged values has no
`metrics_history` group. The group of an `evaluate_then_refit` fit has only the values of the
final fit. The evaluation fit keeps its values in the evaluation bundle.

## Pareto k in `metrics.json` <span class="mapi-badge">v0.10</span> { #pareto-k }

When the fit calculates `elpd_loo`, `metrics.json` keeps the PSIS Pareto k of each observation
in `elpd.elpd_loo.pareto_k`. The values are in the same sequence as `pointwise`. The fit keeps
them only for data of up to 10,000 points. The fit keeps no `pareto_k` if one value is not
finite. A k of more than 0.7 shows that the LOO estimate of that observation is not reliable.
The plot `core.loo_pit` shows the k of each observation.

```json
{"elpd": {"elpd_loo": {"elpd": -412.3, "se": 11.8, "p": 6.1, "n": 120,
                       "pointwise": [-3.1, -2.9, ...], "pareto_k": [0.12, 0.08, ...]}}}
```

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
