# mapi-plugin-tabular

Tabular model plugins for modeling-api (`tabular.*`, entry point `mapi.models: tabular`).

| Plugin | Model | Representation | Score modes |
|---|---|---|---|
| `tabular.hierarchical_glm@1` | Partially pooled GLM in long layout (radon style, E02): group intercepts with group-level predictors, optional varying slopes; normal, Student-t, Poisson or Bernoulli | draws | predict, counterfactual, evaluate |
| `tabular.regressor@1` | Quantile regression over stacked rows with the registry's quantile-capable estimators (xgboost by default; store demand, plan §6.2) | quantiles | predict, forecast, counterfactual, evaluate |

* `tabular.hierarchical_glm` uses `BuildContext.hier_effect`, so unseen groups at score time
  (`county: new_levels(hyperprior)`) are drawn from the fitted hyperprior for every draw.
* `tabular.regressor` accepts exactly the quantile-capable estimators of the allow-listed
  registry in its settings schema (non-quantile estimators are rejected statically), defaults
  to xgboost with quantiles `[0.1, 0.5, 0.9]`, and lists the §6.2 plots in its manifest.

`mapi_plugin_tabular.datasets` holds synthetic data, raw schemas and example specs for both.

License: Apache License 2.0.
