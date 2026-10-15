# mapi-conformance

The conformance corpus: pymc-challenges-style fixtures that define what "supported" means,
plus a runner that takes each one through spec → static validation → fit → score →
evaluate → contract kit → plots.

```bash
python -m mapi_conformance list
python -m mapi_conformance run                       # every fixture, size tiny
python -m mapi_conformance run --fixture c08 --size full --json report.json
```

| Fixture | Model | What it exercises |
|---|---|---|
| `e02` | `tabular.hierarchical_glm` | long layout, `county_idx` remapping, hyperprior new counties, LOO, CRN counterfactual |
| `c08` | `ts.hsgp_trend` | sentinel codes, `max_extension` from anticipated coords, `HORIZON_EXCEEDS_FIT`, contiguity |
| `e24` | `ts.gaussian_hmm` | padded + mask, terminal state, forecast and filter, relabelling, LOO declared invalid |
| `xgb_store_demand` | `tabular.regressor` | plan §6.2: pooled new stores, quantile aggregation rejected, future-required covariates |

Integration tests (`pytest -m conformance`) run every fixture at size `tiny`.

A fixture is a package under `src/mapi_conformance/fixtures/<id>/` with `data.py`
(`generate(seed, size)`, optional `schema()` and `payload_<name>()` builders), `spec.yaml`,
`score_requests.yaml` and an optional `expectations.py`; list its id in
`fixtures/__init__.py`. See the docs guide *Conformance corpus* for the file formats.

License: Apache License 2.0.
