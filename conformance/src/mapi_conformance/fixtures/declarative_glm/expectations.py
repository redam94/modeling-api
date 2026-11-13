"""Declarative GLM checks: the spec is the recipe's compiled output, effects carry their signs,
rows are matched by label (shuffle invariance) and unseen stores are more uncertain."""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import default_registry
from mapi_sdk.schema import infer_schema
from mapi_spec.library import ModelRecipeDoc, parse_library_doc
from mapi_spec.model_recipes import compile_model_recipe

from mapi_conformance.fixtures.declarative_glm.data import RECIPE, TRUTH, schema

TINY_FIT = {"tune": 300, "draws": 150, "chains": 2}

_IN_SAMPLE: dict[str, xr.Dataset] = {}


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    doc = parse_library_doc(RECIPE)
    assert isinstance(doc, ModelRecipeDoc)
    compiled = compile_model_recipe(doc, default_registry().catalog(), schema=schema())
    assert compiled.ok, [i.message for i in compiled.issues]
    spec = bundle.spec
    assert spec.model.plugin_settings == compiled.model.plugin_settings, "spec.yaml is stale"
    assert [s.model_dump(mode="json") for s in spec.pipeline] == [
        s.model_dump(mode="json") for s in compiled.pipeline
    ], "spec.yaml pipeline is stale"
    assert infer_schema(data, targets=["sales"]).vars["store"].dtype == "str"

    post = bundle.model.posterior
    beta = post["beta"].mean(("chain", "draw"))
    price, promo, temp = (float(beta.sel(covariate=v)) for v in ("price", "promo", "temp"))
    assert (post["beta"].sel(covariate="price") <= 0).all(), "price is constrained negative"
    assert (post["beta"].sel(covariate="promo") >= 0).all(), "promo is constrained positive"
    assert abs(price - TRUTH["price"]) < 0.2, f"price effect {price:.2f}"
    assert abs(promo - TRUTH["promo"]) < 0.3, f"promo effect {promo:.2f}"
    assert abs(temp - TRUTH["temp"]) < 0.2, f"temp effect {temp:.2f}"
    levels = sorted({str(v) for v in data["store"].values})
    assert bundle.model.learned["group_levels"] == {"store": levels}
    assert post["a_store"].sizes["store"] == len(levels)


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    summary = tree["summary"].to_dataset()
    if name == "predict_in_sample":
        _IN_SAMPLE[bundle.bundle_id] = summary
    elif name == "shuffled_rows":
        base = _IN_SAMPLE.get(bundle.bundle_id)
        assert base is not None, "predict_in_sample must run first"
        xr.testing.assert_allclose(summary.sortby("row"), base.sortby("row"))
    elif name == "new_store_from_group_spread":
        draws = tree["draws"].to_dataset()["y_hat"]
        sd = draws.std("sample").values
        # rows 2-5 belong to the new store: their spread includes the store spread
        assert sd[2:].mean() > sd[:2].mean(), "a new store should be more uncertain"
        assert np.isfinite(summary["y_hat"]).all()
    elif name == "counterfactual_promo":
        delta = tree["delta/summary"].to_dataset()["y_hat"].sel(stat="mean").values
        promo = np.array([0.0, 1.0, 0.0, 1.0, 0.0, 0.0])
        assert (delta[promo == 0] > 0).all() and np.allclose(delta[promo == 1], 0.0)
