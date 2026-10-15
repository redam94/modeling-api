# mapi-pymc

PyMC model base classes for modeling-api. Plugin authors subclass `PyMCModel` and write a single
method, `build(ds, settings, ctx) -> pm.Model`. The platform provides everything else:

- **Engines**:
  - nutpie (the default when installed);
  - PyMC NUTS with segmented, resumable sampling;
  - MAP and ADVI;
  - Pathfinder and numpyro/blackjax when those libraries are installed.

  Every engine reports progress per chain.
- **Opt-in ArviZ groups**: `prior`/`prior_predictive`, `log_likelihood` and a thinned
  `posterior_predictive`.
- **Diagnostics**: r-hat, bulk/tail ESS, divergences and tree depth, as scorecard
  `Diagnostic`s with plain-language messages.
- **Scoring**:
  - the model is rebuilt on the canonicalised score data and run through
    `pm.sample_posterior_predictive(predictions=True)`;
  - `expected` outputs come from author-declared Deterministics;
  - `predictive` outputs use frozen inverse-CDF noise (common random numbers), so a
    counterfactual's deltas are not inflated by independent noise.
- **`BuildContext` helpers**:
  - `coords()`, `data()` and `observed()`;
  - `hier_effect()`, whose new levels come from the hyperprior, pooled or zero;
  - `position()` and `anticipated_coords` for `extend` dims;
  - `align()` for dims the schema does not declare.
- **`FrozenPredictor`**:
  - a compiled PyTensor function, vectorised over posterior samples and scenarios;
  - declared with `FrozenPredictorSpec`;
  - `mapi_pymc.testing.check_fast_score` checks that it matches exact scoring within MCSE.
- **Checkpoints**: `CheckpointSink` with in-memory and directory sinks. Sampler state is
  JSON-encoded and never pickled.

Reference plugins and synthetic data live in `mapi_pymc.examples`:

- `RadonModel`: E02-style, long layout, new counties;
- `TrendModel`: forecasts along an `extend` time dim;
- `PanelModel`: a hierarchical geo × time panel.

See the documentation page *Guides → Bring a PyMC model*.

```bash
uv run pytest packages/sdk-pymc -q          # ~25 s; the panel contract test is marked slow
```

License: Apache License 2.0.
