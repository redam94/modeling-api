# mapi-plugin-tabular

Tabular model plugins for modeling-api (`tabular.*`, entry point `mapi.models: tabular`).

| Plugin | Model | Representation | Score modes |
|---|---|---|---|
| `tabular.hierarchical_glm@1` | Partially pooled GLM in long layout (radon style, E02): group intercepts with group-level predictors, optional varying slopes; normal, Student-t, Poisson, Bernoulli and (1.1) negative binomial, zero-inflated Poisson and negative binomial, beta, gamma and ordinal | draws | predict, counterfactual, evaluate |
| `tabular.image_logistic@1` | Bayesian multinomial logistic regression on PCA scores (or block means) of small images in the `image_folder` layout (`pixels(image, y, x, channel)` uint8, `label(image)`) | draws | predict, evaluate |
| `tabular.regressor@1` | Quantile regression over stacked rows with the registry's quantile-capable estimators (xgboost by default; store demand, plan §6.2) | quantiles | predict, forecast, counterfactual, evaluate |

* `tabular.hierarchical_glm` uses `BuildContext.hier_effect`, so unseen groups at score time
  (`county: new_levels(hyperprior)`) are drawn from the fitted hyperprior for every draw.
* `tabular.regressor` accepts exactly the quantile-capable estimators of the allow-listed
  registry in its settings schema (non-quantile estimators are rejected statically), defaults
  to xgboost with quantiles `[0.1, 0.5, 0.9]`, and lists the §6.2 plots in its manifest.

* `tabular.image_logistic` (v0.14) fits with nutpie by default, keeps the PCA basis in its
  `features` group, scores `y_hat(chain, draw, image, class)` with at most 500 draws, and logs
  the PCA components and class means as run images when the run takes media. From model
  version 1.1.0 (v0.15), `input: features` takes a feature table in place of pixels, for
  example the output of the `dl.embed` step of `mapi-jax`.

`mapi_plugin_tabular.datasets` holds synthetic data, raw schemas and example specs for the
GLM and the regressor; `mapi_plugin_tabular.image_data` holds the synthetic shapes images
(`shapes_dataset`, `shapes_schema`, `shapes_zip`).

License: Apache License 2.0.
