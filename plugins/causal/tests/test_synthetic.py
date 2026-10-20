"""``causal.synthetic_control`` without MCMC: settings, data checks, numerics and group layouts."""

from __future__ import annotations

import numpy as np
import pymc as pm
import pytest
import xarray as xr
from mapi_plugin_causal._common import IMPACT, SAMPLE, TIME, UNIT, effect_summary
from mapi_plugin_causal.datasets_panel import (
    GEO_LIFT_SPEC_YAML,
    geo_lift_dataset,
)
from mapi_plugin_causal.synthetic import (
    DONOR_WEIGHTS,
    TREATED,
    SyntheticControlModel,
    SyntheticControlSettings,
    counterfactual_draws,
    unit_scales,
)
from mapi_sdk.models import FitContext, Fitted, ScoreContext
from mapi_sdk.testing import check_about
from mapi_spec import ErrorCode, MapiError, load_model_spec, load_score_request
from mapi_spec.spec import FitConfig
from pydantic import JsonValue, ValidationError

START, END = "2024-02-26", "2024-05-13"  # weeks 60 and 71 of geo_lift_dataset()
_REQ = load_score_request(
    "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://p/m\nmode: predict\n"
)


def _settings(**kw: object) -> SyntheticControlSettings:
    base: dict[str, object] = {"treated": ["geo_03"], "intervention_start": START}
    return SyntheticControlSettings.model_validate({**base, **kw})


def _canonical(ds: xr.Dataset) -> xr.Dataset:
    return ds.rename({"geo": UNIT, "week": TIME, "sales": "y"})


@pytest.fixture(scope="module")
def geo() -> xr.Dataset:
    return _canonical(geo_lift_dataset(geos=6, weeks=40, start_week=30)[0])


# ======================================================================== settings


def test_treated_and_donors_accept_comma_text() -> None:
    s = _settings(treated="geo_03, geo_04 ,", donors="geo_00,geo_01")
    assert s.treated == ["geo_03", "geo_04"]
    assert s.donors == ["geo_00", "geo_01"]
    assert _settings(donors="").donors is None


@pytest.mark.parametrize(
    "kw",
    [
        {"treated": ""},
        {"treated": ["a", "a"]},
        {"donors": ["geo_03", "geo_01"]},
        {"concentration": 0},
        {"intervention_end": "2024-01-01"},
    ],
)
def test_bad_settings_are_rejected(kw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _settings(**kw)


def test_bad_settings_give_settings_invalid() -> None:
    with pytest.raises(MapiError) as err:
        SyntheticControlModel().parse_settings({"treated": [], "intervention_start": START})
    assert err.value.code is ErrorCode.SETTINGS_INVALID


def test_manifest_and_about() -> None:
    m = SyntheticControlModel.manifest()
    assert (m.id, m.version) == ("causal.synthetic_control", "1.0.0")
    caps = m.capabilities
    assert caps.score_modes == frozenset({"predict"})
    assert not caps.loo_valid and caps.loo_invalid_reason
    assert caps.owns == frozenset({"scaling"}) and not caps.deterministic_given_seed
    assert m.outputs["y_hat"].dims == [UNIT, TIME]
    assert list(m.plots)[:3] == ["causal.impact", "causal.pre_fit", "causal.donor_weights"]
    assert m.family == "causal" or m.id.split(".")[0] == "causal"
    check_about(SyntheticControlModel)


# ======================================================================== data checks


def _panel_error(ds: xr.Dataset, **kw: object) -> MapiError:
    with pytest.raises(MapiError) as err:
        SyntheticControlModel().panel(ds, _settings(**kw))
    return err.value


def test_panel_splits_treated_and_donors(geo: xr.Dataset) -> None:
    p = SyntheticControlModel().panel(geo, _settings(intervention_start="2023-07-31"))
    assert p.treated == ["geo_03"]
    assert p.donors == ["geo_00", "geo_01", "geo_02", "geo_04", "geo_05"]
    assert p.y_treated.shape == (1, 40) and p.y_donors.shape == (5, 40)
    assert p.window.start == 30 and p.window.open_end


def test_panel_data_checks(geo: xr.Dataset) -> None:
    start = {"intervention_start": "2023-07-31"}
    err = _panel_error(geo, treated=["geo_99"], **start)
    assert (err.code, err.pointer) == (ErrorCode.SETTINGS_INVALID, "/model/settings/treated")
    err = _panel_error(geo, donors=["geo_00", "nowhere"], **start)
    assert (err.code, err.pointer) == (ErrorCode.SETTINGS_INVALID, "/model/settings/donors")
    err = _panel_error(geo, donors=["geo_00"], **start)
    assert (err.code, err.pointer) == (ErrorCode.SETTINGS_INVALID, "/model/settings/donors")
    holes = geo.copy(deep=True)
    holes["y"][1, 5] = np.nan
    err = _panel_error(holes, **start)
    assert (err.code, err.pointer) == (ErrorCode.SCHEMA_INVALID, "/data/y")
    assert err.untrusted_text == "geo_01"
    err = _panel_error(geo, intervention_start="2023-01-09")
    assert err.code is ErrorCode.SETTINGS_INVALID
    sparse = geo.copy(deep=True)
    sparse["y"][3, 2:30] = np.nan
    err = _panel_error(sparse, **start)
    assert err.code is ErrorCode.SCHEMA_INVALID and err.untrusted_text == "geo_03"


def test_unit_scales_round_trip() -> None:
    y = np.array([[2.0, 4.0, np.nan, 10.0], [1.0, 1.0, 1.0, 5.0]])
    pre = np.array([True, True, True, False])
    scales = unit_scales(y, pre, "mean")
    np.testing.assert_allclose(scales, [3.0, 1.0])
    np.testing.assert_allclose((y / scales[:, None]) * scales[:, None], y)
    np.testing.assert_allclose(unit_scales(y, pre, "none"), [1.0, 1.0])
    with pytest.raises(MapiError) as err:
        unit_scales(-y, pre, "mean", labels=["a", "b"])
    assert err.value.code is ErrorCode.SCHEMA_INVALID and err.value.untrusted_text == "a, b"


# ======================================================================== numerics


def test_simplex_prior_weights_are_on_the_simplex() -> None:
    model = SyntheticControlModel().build_model(
        np.ones((2, 5)),
        np.ones((4, 5)),
        _settings(treated=["a", "b"]),
        ["a", "b"],
        ["d0", "d1", "d2", "d3"],
    )
    w = pm.draw(model["weights"], draws=50, random_seed=1)
    assert w.shape == (50, 2, 4)
    assert (w >= 0).all()
    np.testing.assert_allclose(w.sum(-1), 1.0)


def test_horseshoe_and_student_t_build() -> None:
    s = _settings(treated=["a"], weights="horseshoe", likelihood="student_t", intercept=False)
    y = np.ones((1, 6))
    y[0, 2] = np.nan
    model = SyntheticControlModel().build_model(y, np.ones((3, 6)), s, ["a"], ["d0", "d1", "d2"])
    assert {"weights", "tau", "nu", "sigma"} <= set(model.named_vars)
    assert "alpha" not in model.named_vars
    assert model["y"].eval().shape == (5,)  # the missing cell is left out
    assert SyntheticControlModel.kept_vars(s) == ["weights", "sigma", "nu", "tau"]


def test_counterfactual_is_alpha_plus_donors_times_weights() -> None:
    rng = np.random.default_rng(0)
    w = rng.dirichlet(np.ones(3), size=(7, 2))  # (S, J, D)
    alpha = rng.normal(size=(7, 2))
    sigma = np.abs(rng.normal(size=(7, 2)))
    donors = rng.normal(size=(3, 9))
    mu, pred = counterfactual_draws(w, alpha, sigma, donors, seed=3, name="counterfactual")
    want = alpha[:, :, None] + np.einsum("sjd,dn->sjn", w, donors)
    np.testing.assert_allclose(mu, want)
    _, again = counterfactual_draws(w, alpha, sigma, donors, seed=3, name="counterfactual")
    np.testing.assert_array_equal(pred, again)  # common random numbers
    _, other = counterfactual_draws(w, alpha, sigma, donors, seed=3, name="y_hat")
    assert not np.allclose(pred, other)
    z = (pred - mu) / sigma[:, :, None]
    _, shifted = counterfactual_draws(w, alpha + 1.0, sigma, donors, seed=3, name="counterfactual")
    np.testing.assert_allclose(shifted - pred, 1.0)  # same noise, shifted mean
    assert abs(float(z.mean())) < 0.5
    _, heavy = counterfactual_draws(
        w, None, sigma, donors, seed=3, name="counterfactual", nu=np.full(7, 3.0)
    )
    assert heavy.shape == (7, 2, 9)


# ======================================================================== groups and scoring


def _posterior(s: SyntheticControlSettings, donors: list[str], seed: int = 0) -> xr.Dataset:
    """A hand-built posterior (2 chains x 5 draws) with near-zero noise."""
    rng = np.random.default_rng(seed)
    j, d = len(s.treated), len(donors)
    w = rng.dirichlet(np.ones(d), size=(2, 5, j))
    return xr.Dataset(
        {
            "weights": (("chain", "draw", TREATED, "donor"), w),
            "alpha": (("chain", "draw", TREATED), rng.normal(0, 0.05, (2, 5, j))),
            "sigma": (("chain", "draw", TREATED), np.full((2, 5, j), 1e-9)),
        },
        coords={"chain": [0, 1], "draw": np.arange(5), TREATED: s.treated, "donor": donors},
    )


def test_effect_groups_layout_and_exact_counterfactual(geo: xr.Dataset) -> None:
    model = SyntheticControlModel()
    s = _settings(treated=["geo_03", "geo_05"], intervention_start="2023-07-31")
    p = model.panel(geo, s)
    pre = p.window.pre_mask(p.n)
    scale_t = unit_scales(p.y_treated, pre, "mean")
    scale_d = unit_scales(p.y_donors, pre, "mean")
    yd_n = p.y_donors / scale_d[:, None]
    post = _posterior(s, p.donors)
    impact, weights = model._effect_groups(post, p, yd_n, scale_t, s, 11, FitContext())
    assert dict(impact.sizes) == {SAMPLE: 10, UNIT: 2, TIME: 40}
    assert impact["counterfactual"].dims == (SAMPLE, UNIT, TIME)
    assert impact["post"].dims == (UNIT, TIME) and int(impact["post"].sum()) == 20
    assert impact.attrs["intervention_start"] == "2023-07-31"
    assert impact.attrs["model"] == "causal.synthetic_control@1.0.0"
    # counterfactual = (alpha + Y_d @ w) * unit scale, sample by sample
    from mapi_plugin_causal._common import thin_samples

    thin = thin_samples(post, seed=11)
    w = thin["weights"].transpose(SAMPLE, TREATED, "donor").values
    a = thin["alpha"].transpose(SAMPLE, TREATED).values
    want = (a[:, :, None] + np.einsum("sjd,dn->sjn", w, yd_n)) * scale_t[None, :, None]
    np.testing.assert_allclose(impact["counterfactual"].values, want, atol=1e-5)
    np.testing.assert_allclose(
        impact["effect"].values, impact["actual"].values[None] - impact["counterfactual"].values
    )
    assert np.isnan(impact["cumulative_effect"].values[:, :, :30]).all()
    assert dict(weights["weight"].sizes) == {SAMPLE: 10, UNIT: 2, "donor": 4}
    np.testing.assert_allclose(weights["weight"].sum("donor"), 1.0)
    assert weights["pre_rmse"].dims == (UNIT,) and (weights["pre_rmse"] > 0).all()
    assert weights.attrs["prior"] == "simplex"
    summary = effect_summary(impact)
    assert summary["n_treated"] == 2 and set(summary["by_unit"]) == {"geo_03", "geo_05"}


def _fitted(s: SyntheticControlSettings, geo: xr.Dataset) -> Fitted:
    p = SyntheticControlModel().panel(geo, s)
    pre = p.window.pre_mask(p.n)
    units = [*p.treated, *p.donors]
    scales = unit_scales(np.vstack([p.y_treated, p.y_donors]), pre, "mean")
    return Fitted(
        manifest=SyntheticControlModel.manifest(),
        settings=s.model_dump(mode="json"),
        fit_config=FitConfig(),
        groups={"posterior": _posterior(s, p.donors)},
        learned={
            "treated": list[JsonValue](p.treated),
            "donors": list[JsonValue](p.donors),
            "scales": {u: float(v) for u, v in zip(units, scales, strict=True)},
        },
    )


def test_score_returns_the_synthetic_copy_of_treated_units(geo: xr.Dataset) -> None:
    s = _settings(intervention_start="2023-07-31")
    fitted = _fitted(s, geo)
    req = _REQ
    tree = SyntheticControlModel().score(fitted, geo, req, ScoreContext(kind="expected"))
    y_hat = tree["predictions"].to_dataset()["y_hat"]
    assert y_hat.dims == ("chain", "draw", UNIT, TIME)
    assert np.isnan(y_hat.sel({UNIT: "geo_00"})).all()
    post = fitted.posterior
    scales = fitted.learned["scales"]
    assert isinstance(scales, dict)
    donors = fitted.learned["donors"]
    assert isinstance(donors, list)
    yd = np.stack([geo["y"].sel({UNIT: d}).values / scales[str(d)] for d in donors])
    w = post["weights"].sel({TREATED: "geo_03"}).values  # (chain, draw, D)
    a = post["alpha"].sel({TREATED: "geo_03"}).values
    want = (a[..., None] + np.einsum("cdk,kn->cdn", w, yd)) * float(scales["geo_03"])  # type: ignore[arg-type]
    np.testing.assert_allclose(y_hat.sel({UNIT: "geo_03"}).values, want)
    pred = SyntheticControlModel().score(fitted, geo, req, ScoreContext(kind="predictive", seed=4))[
        "predictions"
    ]
    assert pred["y_hat"].attrs["noise"] == "crn_seeded"


def test_score_needs_complete_donors(geo: xr.Dataset) -> None:
    s = _settings(intervention_start="2023-07-31")
    fitted = _fitted(s, geo)
    holes = geo.copy(deep=True)
    holes["y"][0, 3] = np.nan
    req = _REQ
    with pytest.raises(MapiError) as err:
        SyntheticControlModel().score(fitted, holes, req, ScoreContext())
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    # a payload of donors only (treated rows not requested) scores to NaN instead
    flagged = holes.assign_coords({f"{UNIT}__requested": (UNIT, geo[UNIT].values == "geo_01")})
    tree = SyntheticControlModel().score(fitted, flagged, req, ScoreContext())
    assert np.isnan(tree["predictions"].to_dataset()["y_hat"].values).all()
    with pytest.raises(MapiError) as err:
        SyntheticControlModel().score(fitted, geo.drop_vars("y"), req, ScoreContext())
    assert err.value.code is ErrorCode.SCHEMA_INVALID


# ======================================================================== data + spec


def test_geo_lift_dataset_and_spec() -> None:
    ds, truth = geo_lift_dataset()
    assert dict(ds.sizes) == {"geo": 12, "week": 78}
    assert (truth.start, truth.end) == (START, END)
    assert truth.treated == ("geo_03",)
    np.testing.assert_allclose(sum(truth.weights["geo_03"].values()), 1.0)
    assert "geo_03" not in truth.weights["geo_03"]
    eff = truth.effect[0]
    assert (eff[:60] == 0).all() and (eff[72:] == 0).all() and (eff[60:72] > 0).all()
    np.testing.assert_allclose(
        ds["sales"].sel(geo="geo_03").values, truth.counterfactual[0] + truth.effect[0]
    )
    spec = load_model_spec(GEO_LIFT_SPEC_YAML)
    settings = SyntheticControlSettings.model_validate(spec.model.settings)
    assert (settings.intervention_start, settings.intervention_end) == (START, END)
    assert "mapi_schema" in ds.attrs
    _, t2 = geo_lift_dataset(treated=("geo_01", "geo_02"), test_weeks=None)
    assert t2.effect.shape == (2, 78) and (t2.effect[:, -1] > 0).all()
    with pytest.raises(ValueError):
        geo_lift_dataset(treated=("nowhere",))


def test_impact_is_a_known_group_name() -> None:
    assert IMPACT == "impact" and DONOR_WEIGHTS == "donor_weights"
