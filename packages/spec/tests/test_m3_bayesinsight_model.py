"""``mmm.bayesinsight`` settings (BayesInsight's ``model_def.json``, typed) and the importer
for the dedicated plugin (``import_bayesinsight(..., target="mmm.bayesinsight")``)."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import pytest
from mapi_spec.bayesinsight import import_bayesinsight
from mapi_spec.bayesinsight_model import (
    BayesInsightModel,
    BayesInsightSettings,
    BIControl,
    BIExog,
    BILocalTrend,
    BIMedia,
    BISeason,
    CoeffPrior,
    DeterministicTransform,
    Likelihood,
    SShapedPrior,
    WeibullAdstockPrior,
    coeff_param_problem,
)
from mapi_spec.errors import ErrorCode, MapiError, Severity
from mapi_spec.guided import BayesInsightImport
from mapi_spec.ingest import BayesInsightMetadata, BayesInsightModelDef
from mapi_spec.spec import ModelSpec
from pydantic import ValidationError

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "bayesinsight"


def _load(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    md = json.loads((FIXTURES / f"{name}_model_def.json").read_text())
    meta = json.loads((FIXTURES / f"{name}_metadata.json").read_text())
    return md, meta


def _import(md: dict[str, Any], meta: dict[str, Any]) -> BayesInsightImport:
    return BayesInsightImport(
        name="bi",
        dataset="bi_mff",
        model_def=BayesInsightModelDef.model_validate(md),
        metadata=BayesInsightMetadata.model_validate(meta),
        target="mmm.bayesinsight",
    )


# ================================================================================ model_def


def test_bayesinsight_documents_parse_with_their_defaults() -> None:
    md, _ = _load("complex")
    model = BayesInsightModel.model_validate(md)  # artifact / fitted / VOF are dropped
    assert [type(v) for v in model.variable_details] == [BISeason, BIControl, BIMedia, BIExog]
    assert model.kpi.likelihood.type == "Poisson" and model.kpi.likelihood.link == "log"
    bare = BayesInsightModel.model_validate(
        {"variable_details": [{"variable_name": "s", "variable_type": "exog"},
                              {"variable_name": "m", "variable_type": "media"},
                              {"variable_name": "q", "variable_type": "season"},
                              {"variable_name": "t", "variable_type": "localtrend"}]}
    )  # fmt: skip
    kpi, media, season, trend = bare.variable_details
    assert isinstance(kpi, BIExog) and kpi.likelihood.type == "Poisson"  # BayesInsight's default
    assert kpi.intercept_prior == CoeffPrior(
        coeff_dist="Normal", coeff_params={"mu": 0, "sigma": 3}
    )
    assert isinstance(media, BIMedia)
    assert media.coeff_prior.coeff_params == pytest.approx(
        {"mu": math.log(0.05), "sigma": math.log(1.3)}
    )
    assert (media.adstock, media.media_transform, media.index_to) == ("delayed", "hill", "median")
    assert media.adstock_prior.type == "Delayed" and media.media_transform_prior.type == "Hill"
    assert isinstance(season, BISeason) and season.n_fourier == 5
    assert season.period == pytest.approx(365.25 / 7) and season.partial_pooling_sigma == 1.0
    assert isinstance(trend, BILocalTrend) and trend.random_coeff_dims == [] and trend.order == 3
    nulls = BISeason.model_validate({"variable_name": "q", "n_fourier": None, "period": None})
    assert nulls.n_fourier == 5  # BayesInsight writes null for 'use the default'


@pytest.mark.parametrize(
    ("dist", "params", "problem"),
    [
        ("Normal", {"mu": 0, "sigma": 1}, None),
        ("Beta", {"mu": 0.3, "nu": 10}, None),
        ("Gamma", {"mu": 2, "sigma": 1}, None),
        ("Exponential", {"scale": 2}, None),
        ("Normal", {"mu": 0}, "needs the parameters"),
        ("Normal", {"mu": 0, "sigma": -1}, "must be > 0"),
        ("Beta", {"mu": 0.5, "sigma": 0.6}, "sigma^2"),
        ("Uniform", {"lower": 1, "upper": 0}, "below upper"),
        ("MvNormal", {"mu": 0}, "not a supported"),
        ("Laplace", {"mu": float("inf"), "b": 1}, "finite"),
    ],
)
def test_coefficient_priors_are_checked(
    dist: str, params: dict[str, float], problem: str | None
) -> None:
    got = coeff_param_problem(dist, params)
    assert (got is None) if problem is None else (got is not None and problem in got)


def test_priors_transforms_and_likelihoods_validate() -> None:
    alias = CoeffPrior.model_validate(
        {"coeff_dist": "Normal", "coef_params": {"mu": 0, "sigma": 1}}
    )
    assert alias.coeff_params == {"mu": 0, "sigma": 1}  # BayesInsight's README spelling
    with pytest.raises(ValidationError, match="takes the parameters"):
        DeterministicTransform(functional_form="power")
    assert DeterministicTransform(functional_form="indexp", params={"alpha": 2, "mean": 5})
    lik = Likelihood(type="NegativeBinomial", dispersion_dims=["Geography"])
    assert lik.dispersion == ("alpha", "Gamma", {"alpha": 2.0, "beta": 0.001})
    assert Likelihood(type="Normal").dispersion == ("sigma", "HalfNormal", {"sigma": 1.0})
    assert Likelihood(type="ZeroInflatedPoisson").dispersion == (
        "psi", "Beta", {"alpha": 1.0, "beta": 1.0},
    )  # fmt: skip
    with pytest.raises(ValidationError, match="no likelihood_params"):
        Likelihood(type="Poisson", likelihood_params={"sigma": 1})
    with pytest.raises(ValidationError, match="not psi"):
        Likelihood(type="ZeroInflatedPoisson", dispersion_dims=["Geography"])
    with pytest.raises(ValidationError, match="must be > 0"):
        Likelihood(type="Normal", likelihood_params={"sigma": 0})


def test_media_curves_and_carry_overs_pair_with_their_priors() -> None:
    s = BIMedia.model_validate({"variable_name": "m", "media_transform_prior": {"type": "SShaped"}})
    assert s.media_transform == "sshaped" and isinstance(s.media_transform_prior, SShapedPrior)
    s2 = BIMedia.model_validate({"variable_name": "m", "media_transform": "sshaped"})
    assert isinstance(s2.media_transform_prior, SShapedPrior)
    w = BIMedia.model_validate({"variable_name": "m", "adstock": "weibull"})
    assert isinstance(w.adstock_prior, WeibullAdstockPrior)
    assert w.adstock_prior.resolved() == (2.0, 1.0, 3.0, 1.0)  # pymc-marketing's PDF defaults
    assert WeibullAdstockPrior(kind="CDF", lam_mean=4).resolved() == (4.0, 2.5, 2.0, 2.5)
    w2 = BIMedia.model_validate({"variable_name": "m", "adstock_prior": {"type": "Weibull"}})
    assert w2.adstock == "weibull"
    with pytest.raises(ValidationError):
        BIMedia.model_validate({"variable_name": "m", "media_transform_prior": {"n_ave": 0.5}})
    with pytest.raises(ValidationError, match="Beta prior"):
        SShapedPrior(beta_ave=2.0, beta_std=1e8)


def test_signs_time_transforms_dims_and_grouping() -> None:
    with pytest.raises(ValidationError, match="no negative values"):
        BIControl.model_validate({"variable_name": "c", "sign": "negative", "coeff_prior": {
            "coeff_dist": "Uniform", "coeff_params": {"lower": 0, "upper": 1}}})  # fmt: skip
    with pytest.raises(ValidationError, match="not implemented"):
        BIControl.model_validate({"variable_name": "c", "time_transform": {"t": "movingAverage"}})
    with pytest.raises(ValidationError, match="must not share"):
        BIControl.model_validate({"variable_name": "c", "fixed_ind_coeff_dims": ["Geography"],
                                  "random_coeff_dims": ["Geography"]})  # fmt: skip
    with pytest.raises(ValidationError, match="grouping_name"):
        BILocalTrend.model_validate({"variable_name": "t", "grouping_map": {"a": ["US"]}})
    with pytest.raises(ValidationError, match="exactly one"):
        BILocalTrend.model_validate({"variable_name": "t", "grouping_map": {"a": ["US"]},
                                     "grouping_name": "region"})  # fmt: skip
    with pytest.raises(ValidationError, match="exactly one 'exog'"):
        BayesInsightModel.model_validate(
            {"variable_details": [{"variable_name": "m", "variable_type": "media"},
                                  {"variable_name": "c", "variable_type": "control"}]}
        )  # fmt: skip


# ================================================================================= settings


def _settings(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "model_def": {"variable_details": [
            {"variable_name": "Sales_T", "variable_type": "exog",
             "random_coeff_dims": ["Geography"]},
            {"variable_name": "TV_T", "variable_type": "media"},
            {"variable_name": "Price_T", "variable_type": "control"},
        ]},
        "variables": {"Sales_T": "y", "TV_T": "tv", "Price_T": "price"},
        "media": ["tv"],
        "controls": ["price"],
        "row_dims": {"Geography": "geo", "Product": "product"},
        "panel": ["geo", "product"],
    }  # fmt: skip
    doc.update(over)
    return doc


def test_settings_tie_model_def_to_the_data() -> None:
    s = BayesInsightSettings.model_validate(_settings())
    assert s.var("TV_T") == "tv" and s.l_max == 12 and s.lag_dim == "lag"
    schema = BayesInsightSettings.model_json_schema()
    assert schema["properties"]["model_def"]["x-ui-level"] == "advanced"


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"variables": {"Sales_T": "sales", "TV_T": "tv", "Price_T": "price"}}, "to 'y'"),
        ({"media": ["price"], "controls": ["tv"]}, "media must list"),
        ({"panel": ["product", "geo"]}, "row order"),
        ({"row_dims": {"Product": "product"}, "panel": ["product"]}, "not dims of the model"),
        ({"variables": {"Sales_T": "y", "TV_T": "mu", "Price_T": "price"}, "media": ["mu"]},
         "reserved"),
        ({"variables": {"Sales_T": "y", "TV_T": "tv", "Price_T": "price", "X": "x"}},
         "does not define"),
    ],
)  # fmt: skip
def test_inconsistent_settings_are_refused(over: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        BayesInsightSettings.model_validate(_settings(**over))


# ================================================================================ importer


def test_the_complex_model_imports_for_the_dedicated_plugin() -> None:
    md, meta = _load("complex")
    draft = import_bayesinsight(_import(md, meta), project="p")
    spec = ModelSpec.model_validate(draft.spec)
    assert str(spec.model.use) == "mmm.bayesinsight@^1"
    s = BayesInsightSettings.model_validate(spec.model.settings)
    assert s.panel == ["geo", "product"] and s.media == ["media_var_0"]
    assert s.controls == ["promotion"] and s.var("Stock_Total_Total_Total") == "y"
    assert spec.data.bind.vars == {"y": "stock", "media_var_0": "media_var_0",
                                   "promotion": "promotion"}  # fmt: skip
    [lags] = spec.pipeline
    assert lags.with_["pad"] == "nan" and lags.with_["within"] == ["geo", "product"]
    assert draft.mapping.dims["geo"].spec.policy.value == "new_levels"
    assert set(draft.mapping.vars) == {"stock", "media_var_0", "promotion"}
    assert not [i for i in draft.issues if i.severity is not Severity.INFO]
    # the KPI keeps BayesInsight's priors in its own units
    kpi = s.model_def.kpi
    assert kpi.intercept_prior is not None and kpi.intercept_prior.coeff_params["sigma"] == 1.0


def test_features_the_plugin_cannot_run_fall_back_with_warnings() -> None:
    md, meta = _load("complex")
    md = copy.deepcopy(md)
    details = md["variable_details"]
    details[3]["likelihood"] = {"type": "MvNormal"}
    details[1]["time_transform"] = {"transform_type": "movingAverage"}
    details[1]["coeff_prior"] = {"coeff_dist": "Dirichlet", "coeff_params": {"a": 1.0}}
    details[1]["random_coeff_dims"] = ["Geography", "Outlet"]
    details[1]["wobble"] = 3
    details[2]["fixed_ind_coeff_dims"] = ["Geography"]
    details[2]["adstock"] = "weibull"
    draft = import_bayesinsight(_import(md, meta), project="p")
    by_pointer = {i.pointer: i for i in draft.issues}
    base = "/model_def/variable_details"
    assert by_pointer[f"{base}/3/likelihood"].severity is Severity.WARNING
    assert "MvNormal" in by_pointer[f"{base}/3/likelihood"].message
    assert by_pointer[f"{base}/1/time_transform"].severity is Severity.WARNING
    assert "Dirichlet" in by_pointer[f"{base}/1/coeff_prior"].message
    assert "Outlet" in by_pointer[f"{base}/1/random_coeff_dims"].message
    assert "dropped" in by_pointer[f"{base}/1/wobble"].message
    assert by_pointer[f"{base}/2/adstock"].severity is Severity.INFO
    s = BayesInsightSettings.model_validate(draft.spec["model"]["settings"])
    assert s.model_def.kpi.likelihood.type == "Normal"
    assert s.model_def.controls[0].coeff_prior.coeff_dist == "Normal"  # BayesInsight's default
    assert s.model_def.media[0].adstock == "weibull"
    # a coefficient per geo has nothing for a new geo: the dim is fixed
    assert draft.mapping.dims["geo"].spec.policy.value == "fixed"
    assert draft.mapping.dims["product"].spec.policy.value == "new_levels"


def test_series_split_outside_the_row_ids_and_clashing_names_are_refused() -> None:
    md, meta = _load("complex")
    split = copy.deepcopy(meta)
    split["metadata"]["allowed_campaigns"] = ["A", "B"]
    with pytest.raises(MapiError) as exc:
        import_bayesinsight(_import(md, split), project="p")
    assert exc.value.code is ErrorCode.IMPORT_UNSUPPORTED and "Campaign" in exc.value.detail
    clash = copy.deepcopy(md)
    clash["variable_details"][1]["variable_name"] = "mu_Total_Total_Total"
    with pytest.raises(MapiError, match="taken"):
        import_bayesinsight(_import(clash, meta), project="p")
    unknown = copy.deepcopy(meta)
    unknown["metadata"]["allowed_outlets"] = None
    draft = import_bayesinsight(_import(md, unknown), project="p")
    assert any("Outlet" in i.message and i.severity is Severity.INFO for i in draft.issues)


def test_a_national_model_has_no_panel() -> None:
    md, meta = _load("complex")
    meta["metadata"]["row_ids"] = ["Period"]
    meta["metadata"]["allowed_geos"] = meta["metadata"]["allowed_products"] = ["Total"]
    for v in md["variable_details"]:
        v["random_coeff_dims"] = []
        if v["variable_type"] != "season":
            v["variable_name"] = v["variable_name"].replace("_Total_Total_Total", "") + (
                "_Total" * 5
            )
    draft = import_bayesinsight(_import(md, meta), project="p")
    s = BayesInsightSettings.model_validate(draft.spec["model"]["settings"])
    assert s.panel == [] and s.row_dims == {}
    assert draft.spec["pipeline"][0]["with"]["within"] == []  # type: ignore[index]
