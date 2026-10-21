# v0.17 dashboard fixtures

Test data for layout 2 dashboards (any model type). Two layers:

1. **Captures** (WP0, `<plugin>/`): the real outputs of score runs on the isolated probe stack,
   as the API served them. Do not edit them. `index.json` gives the outcome of each probe case.
2. **Golden bundles** (WP4, `golden/<case>.json`): one file per preset case. A bundle groups
   the captures that a v2 readout is built from, adds derived actuals and adds facts that tests
   can assert against. `_derive.py` writes the bundles from the captures.

## Captures

Each plugin directory holds:

| File | Content | Loads as |
|---|---|---|
| `probe.json` | request per case, outcome, refusal text and issues, fit metric rows, scorecard, input contract, capabilities | plain JSON |
| `<case>.summary.json` | `summary.json` of the score run | `ScoreSummary` |
| `<case>.result.json` | `result.json` (kind, names, representation, `aggregation_flagged`, issues) | plain JSON |
| `<case>.forecast.json` | `ui/guided/forecast.json` | `ForecastView` |
| `<case>.counterfactual.json` | `ui/guided/counterfactual.json` | `CounterfactualView` |
| `<case>.fields.json` | `ui/fields.json` | `FieldsView` |

Case name suffixes give the output selector:

- `__readout`: the selector that v0.16 sends (`inputs.readout_output`). Draws use `mean,hdi_90`,
  quantiles use `median,eti_90`, interval uses `point,interval` and point uses `mean`.
- `__predictive`: the same selector with `kind: predictive`.
- `__canonical`: the same selector with `names: canonical`.

The probe notes (`plans/v017-probe.md`, not committed) record the findings. The bundles repeat
the findings that tests need (see "Facts").

## Golden bundles

| Case | Preset | Plugin | Representation | Roles |
|---|---|---|---|---|
| `forecast_bsts` | forecast | ts.bsts | draws | baseline, predictive, aggregate |
| `forecast_sarimax` | forecast | ts.sarimax | quantiles | baseline, predictive, in_sample |
| `regression_conformal` | regression | sklearn.tabular_conformal | interval | baseline, predictive, rows |
| `regression_point` | regression | sklearn.tabular_regressor | point | baseline, aggregate, rows_two, rows_new_store, rows_next_week |
| `classifier_binary` | classifier | sklearn.tabular_classifier | point | as regression_point |
| `classifier_multiclass` | classifier | sklearn.multiclass_classifier | point | as regression_point |
| `causal_impact` | causal_impact | causal.impact | draws | baseline, predictive, forecast, forecast_predictive |
| `causal_did` | causal_impact | causal.did | draws | baseline, predictive, counterfactual |
| `field_lgcp` | field_map | sci.lgcp_grid | draws | baseline, predictive |
| `field_lgcp_forecast` | field_map | sci.lgcp_grid | draws | forecast, forecast_predictive, aggregate |
| `field_areal` | field_map | sci.areal_bym2 | draws | baseline, predictive, simulate |
| `image_logistic` | image | tabular.image_logistic | draws | baseline, predictive |

There is no `mmm_planner` case. MMM keeps the v0.16 path and its fixtures.
`tabular.image_logistic` stands in for `dl.cnn_classifier`, because the probe did not fit the
CNN.

### Roles

- `baseline`: the expected-value score that a v2 baseline readout sends (the `__readout` selector).
- `predictive`: the same score with `kind: predictive`. This is the `band: "outcome"` path.
- `aggregate`: a score with `output.aggregate`. Draws aggregate per draw. Point models return
  column `point` with `aggregation_flagged: true`.
- `forecast`, `forecast_predictive`: forecast-mode scores of a model whose baseline is a
  predict score.
- `counterfactual`, `simulate`, `in_sample`: the other modes of the plugin.
- `rows*`: inline scenario-row scores. The probe did not store the inline payloads; the result
  coords are in the summary.

### Bundle layout (`format: mapi.test.dashboard-material/v1`)

| Key | Content |
|---|---|
| `case`, `preset`, `plugin`, `representation`, `primary_output`, `note` | identity and a one-line summary of the special points |
| `derived_from` | the capture files that the bundle copies |
| `manifest` | the installed plugin's `ModelManifest` (`exclude_defaults`), from the registry when the bundle was derived |
| `capabilities`, `input_contract`, `scorecard` | from `probe.json` (`input_contract` = the input table and snapshot of the version) |
| `fit_metrics` | the `RunMetricSummary` rows of the fit run, as `MetricSeries`. Rows with `labels == {}` are the scalar values. Refit copies carry `{fit: ...}` |
| `scores.<role>` | `probe_case`, `mode`, `horizon`, `output` (the request selector), `inline`, `trimmed`, `result`, `summary`, `forecast`, `counterfactual`, `fields` (null when the run did not write it) |
| `refusals` | probe cases that were refused before a run: `code`, `refused`, `issues` (with pointers) |
| `actuals` | derived actuals (see below) or null |
| `facts` | values computed from the bundle (see below) |

`causal_did` is trimmed: its summaries keep 4 of 20 stores (`store_00`, `store_01` control;
`store_04`, `store_05` treated), and `scores.<role>.trimmed` says so. The counterfactual view
and the fit metrics stay complete, so `delta_total` and `effect_total` are over all stores.
Every bundle is at most 200 KiB.

### Facts

- `facts.tables.<role>.<variable>`: `dims`, `time_dim`, `centre` (first of
  `mean, median, q50, p50, point`), `band` (the lower/upper columns or null), `n_rows`,
  `classes`, and `sum_centre`, `sum_low`, `sum_high`, `mean_band_width`.
  **`sum_low`/`sum_high` are the wrong band of a total** (summed bounds). A test asserts that a
  snapshot never shows them. The correct band of a total is in `facts.aggregates`.
- `facts.aggregates`: `<role>.forecast_total` (`ForecastView.total`),
  `<role>.counterfactual_delta_total`, `<role>.aggregate` (the aggregate tables and
  `aggregation_flagged`).
- `facts.predictive.<role>.<variable>`: the mean band width, expected and predictive, and
  `widens`. Findings: observed outputs of draws models widen. Latent outputs (bsts `level`,
  areal `rr`/`effect`/`p_exceed`), lgcp `total` and class probabilities do not widen.
  Quantiles and interval do not change, because their band is already an outcome band. Point
  models refuse `kind: predictive` before a run (`refusals`, `CAPABILITY_UNSUPPORTED`).
- `facts.scalar_fit_metrics`: `{key: last}` for the rows with `labels == {}`.

### Actuals (synthetic)

The probe has no new observations. The actuals are therefore **synthetic and deterministic**:

- `time` (forecast_bsts, forecast_sarimax): actual = centre + k x half band width of the
  `predictive` score, with k cycling over `-0.5, 0.2, 1.3, -1.6, 0.0, 0.7, -0.9, 0.4`
  (|k| > 1 is outside the band). Rounded to 3 decimals.
- `row` value (regression_conformal, regression_point): the same offsets on the `baseline`
  rows. Point models have no band, so the half width is `0.1 x |centre| + 1`.
- `row` binary (classifier_binary): y = 1 when a seeded draw (`random.Random(17017)`) is less
  than p.
- `row` multiclass (classifier_multiclass): one label per case, drawn from the case's class
  probabilities with the same seed.
- effect (causal_impact): the 4 forecast weeks, actual = centre + 3 + k x half band width.
  `cf_total` is the `forecast_predictive` `ForecastView.total`. `effect_low = actual_total -
  q0.95` and `effect_high = actual_total - q0.05`. The `naive_*` values (from summed bounds)
  are the wrong answer.

`actuals` holds:

- `match`, `against` (the role whose predictions are compared), and `kpi_column` or
  `label_column` (the target name in the input table).
- `names`: the dataset dim name mapped to the canonical name.
- `table`: long format, the summary's dim columns followed by `actual`.
- `inputs`: an `XrJsonDataset` in the input table's canonical names. Time actuals are dense on
  the dims, as a v1 `ActualsSubmit` upload holds them. Row actuals have one `row` dim with the
  coordinates as data variables. WP8 can reshape them for its `id_column` rule.
- `expected`: the metrics. Error and classification metrics come from
  `mapi_sdk.metrics.compute_metrics` (point representation), the reference that
  `mapi_spec.dashboard_metrics` must match within 1e-9. `band_hit_rate` (inclusive bounds),
  `rate_pred`/`rate_obs`, `confusion` (rows = actual `[0, 1]`, columns = predicted at
  threshold 0.5), `label_counts` and `top_class_counts` are counted directly.

## Use in tests

```python
import sys
from importlib import util
from pathlib import Path

_p = Path(__file__).parent / "fixtures" / "dashboards_v017" / "loader.py"
_spec = util.spec_from_file_location("dashboards_v017_loader", _p)
golden = sys.modules.setdefault(_spec.name, util.module_from_spec(_spec))
_spec.loader.exec_module(golden)

m = golden.load_case("classifier_binary")      # Material (validated models)
m.scores["baseline"].summary                    # ScoreSummary
m.scores["baseline"].table("y_hat").rows
m.scalar_metrics()["auc"]
m.actuals_submit("week 41")                     # ActualsSubmit body
golden.raw_case("causal_did")                   # plain JSON copy, safe to mutate
golden.load_capture("ts.sarimax", "probe.json")
```

`test_golden_fixtures.py` (in this directory) checks these points:

- Every capture and bundle validates.
- Each bundle is at most 200 KiB.
- The facts agree with a plain-Python recomputation.
- The bundles match `_derive.py --check`. A changed plugin manifest (for example WP3's
  `meaning`) is reported, but it does not make a bundle stale.

## Regenerate

After a new probe run, or when a plugin manifest changes:

```sh
.venv/bin/python packages/server/tests/fixtures/dashboards_v017/_derive.py
```
