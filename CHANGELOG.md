# Changelog

All notable changes are documented here. The project follows [Semantic Versioning](https://semver.org/);
milestone releases are tagged on `main`.

## [Unreleased]
- Repository scaffold: uv workspace, package skeletons, split Apache-2.0 / AGPLv3+commercial licensing.

### Added
- **`mapi-spec`**: the pure-Pydantic contract layer.
  - DataSchema, DimSpec and VarSpec, with schema templates.
  - SchemaEffect propagation; ModelSpec, ScoreRequest and the Sweep, Backtest, SBC and Campaign wrappers.
  - A strict YAML 1.2 loader, canonical JSON and spec hashes.
  - Static validation: split stage, `LEAKAGE_RISK`, dim policies, recursion and the CoordPlan checks.
  - Capabilities, representation rules, the PlotSpec envelope, stable RFC 7807 error codes, the Executor protocol shapes, JSON Schema export and the `mapi-spec` CLI.
- **`mapi-sdk`**: the xarray runtime.
  - Wire formats: XrJSON, single-object zarr v3 zips, DataTree zips and Arrow IPC.
  - Array-level schema validation; the step contract, pipeline engine and stages.
  - Split and per-group context replay; inverse chains with output-space labels.
  - CoordPlan and coordinate canonicalisation, the representation-aware summariser and metrics.
  - Bundle IO with integrity sidecars and no pickle; the `fit_spec`/`score_bundle` harness with `evaluate_then_refit`.
  - The entry-point registry, the in-process executor, and the `mapi_sdk.testing` contract kit (`check_step`, `check_model`, pytest plugin).
- **`mapi-steps`**: 28 built-in `core.*` steps (reshape, value, feature, formula, adstock and saturation).
  - Each exports a templated SchemaEffect and a shipped catalog JSON.
  - Catalog entries for the five split kinds.
- **`mapi-pymc`**: `PyMCModel`, where an author writes only `build()`.
  - `BuildContext` with `hier_effect` new levels.
  - Engines: nutpie, pymc, MAP and ADVI, plus guarded numpyro, blackjax and pathfinder.
  - Segmented, resumable sampling with checkpoints; CRN counterfactuals.
  - Convergence diagnostics, FrozenPredictor fast scoring and `check_fast_score`.
- **`mapi-sklearn`**: `SklearnModel` with an allow-listed estimator registry (sklearn and xgboost).
  - skops and xgboost-JSON persistence.
  - Point, quantile, bootstrap, conformal and classifier plugins (`sklearn.tabular_*`).
  - Pooled new levels; exact or zeroing contributions.
- **`mapi-plots`**: a JSON-only PlotSpec sanitizer (pinned Vega-Lite 6.4.1 and plotly.js 4.1.1 schemas, an expression allow-list, HTML stripping) and role-bound templates.
  - The standard `core.*` recipe catalog, plus the `mmm.*` and `ml.*` domain packs.
  - Altair and Plotly converters with named Arrow datasets, and the `render_plots` batch entry point.
- Cross-package conformance suites:
  - the plan's xgboost spec through real steps, models and plots;
  - an E33-shaped `core.*` pipeline in front of a PyMC model.
- Docs: spec format, SDK, built-in steps and plots references; guides for plugins, PyMC, tabular models and plots.

### Fixed (integration)
- The plan's xgboost example names the real quantile plugin (`sklearn.tabular_quantiles`). A new `ml.feature_importance` recipe covers its plot list.
- `core.fit_vs_actual` and `core.residuals_over_time` compare quantile and interval summaries cell by cell instead of refusing them.
- The contract kit flags step settings whose defaults are missing from the exported JSON Schema (`default_factory`), and finds reverse-bound outputs when checking units.
