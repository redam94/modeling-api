"""``causal.did`` without MCMC: event-time design, data checks and the effect groups."""

from __future__ import annotations

import numpy as np
import pymc as pm
import pytest
import xarray as xr
from mapi_plugin_causal._common import IMPACT, SAMPLE, TIME, UNIT, crn_normal
from mapi_plugin_causal.datasets_panel import DID_SPEC_YAML, did_dataset
from mapi_plugin_causal.did import (
    EVENT_DIM,
    TREATMENT,
    DiDModel,
    DiDScaling,
    DiDSettings,
    adoption_positions,
    check_treatment,
    event_dummies,
    event_times,
    fill_padded_treatment,
    fit_scaling,
    fitted_treatment,
    requested_cells,
    staggered_diagnostic,
)
from mapi_pymc.context import BuildContext
from mapi_sdk.models import FitContext
from mapi_sdk.testing import check_about
from mapi_spec import ErrorCode, MapiError, load_model_spec
from pydantic import ValidationError

# 3 units over 8 periods: adoption at 3, at 5, never
TREAT = np.array(
    [
        [0, 0, 0, 1, 1, 1, 1, 1],
        [0, 0, 0, 0, 0, 1, 1, 1],
        [0, 0, 0, 0, 0, 0, 0, 0],
    ],
    dtype=float,
)


def _canonical(ds: xr.Dataset) -> xr.Dataset:
    return ds.rename({"store": UNIT, "month": TIME, "sales": "y"})


# ======================================================================== design


def test_event_times() -> None:
    assert event_times(DiDSettings(leads=3, lags=2)) == [-3, -2, 0, 1, 2]
    assert event_times(DiDSettings(leads=0, lags=0)) == [0]
    assert event_times(DiDSettings(leads=1, lags=1)) == [0, 1]
    assert event_times(DiDSettings(design="did")) == [0]


def test_adoption_positions() -> None:
    np.testing.assert_array_equal(adoption_positions(TREAT), [3, 5, -1])


def test_binned_event_dummies_for_staggered_adoption() -> None:
    s = DiDSettings(leads=2, lags=1)  # event times [-2, 0, 1]
    d = event_dummies(TREAT, s)
    assert d.shape == (3, 8, 3)
    assert d.sum(axis=-1).max() == 1  # one dummy per cell at most
    assert (d[2] == 0).all()  # never treated
    # unit 0 adopts at 3: rel = -3..4; -2 bins rel <= -2, -1 is the reference, 1 bins rel >= 1
    np.testing.assert_array_equal(d[0, :, 0], [1, 1, 0, 0, 0, 0, 0, 0])
    np.testing.assert_array_equal(d[0, :, 1], [0, 0, 0, 1, 0, 0, 0, 0])
    np.testing.assert_array_equal(d[0, :, 2], [0, 0, 0, 0, 1, 1, 1, 1])
    # unit 1 adopts at 5: the reference period is 4
    np.testing.assert_array_equal(d[1, :, 0], [1, 1, 1, 1, 0, 0, 0, 0])
    np.testing.assert_array_equal(d[1, :, 1], [0, 0, 0, 0, 0, 1, 0, 0])
    assert d[1, 4].sum() == 0 and d[0, 2].sum() == 0
    # every treated cell (treatment = 1) has a post dummy when the end points are binned
    np.testing.assert_array_equal(d[..., 1:].sum(-1), TREAT)


def test_unbinned_dummies_leave_far_cells_out() -> None:
    d = event_dummies(TREAT, DiDSettings(leads=2, lags=1, bin_endpoints=False))
    np.testing.assert_array_equal(d[0, :, 0], [0, 1, 0, 0, 0, 0, 0, 0])
    np.testing.assert_array_equal(d[0, :, 2], [0, 0, 0, 0, 1, 0, 0, 0])
    assert d[0, 5:].sum() == 0


def test_did_design_has_one_dummy_equal_to_treatment() -> None:
    d = event_dummies(TREAT, DiDSettings(design="did"))
    assert d.shape == (3, 8, 1)
    np.testing.assert_array_equal(d[..., 0], TREAT)


def test_counterfactual_treatment_zero_removes_every_dummy() -> None:
    for s in (DiDSettings(), DiDSettings(design="did"), DiDSettings(bin_endpoints=False)):
        assert (event_dummies(np.zeros_like(TREAT), s) == 0).all()


@pytest.mark.parametrize(
    ("treat", "fit", "text"),
    [
        (np.where(TREAT == 1, 0.5, 0.0), True, "0 or 1"),
        (np.array([[0, 1, 0], [0, 0, 0]], dtype=float), True, "stay 1"),
        (np.ones((2, 3)), True, "never gets"),
        (np.zeros((2, 3)), True, "no unit"),
        (np.array([[0, np.nan], [0, 0]]), False, "missing"),
    ],
)
def test_treatment_checks(treat: np.ndarray, fit: bool, text: str) -> None:
    with pytest.raises(MapiError) as err:
        check_treatment(treat, fit=fit)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and err.value.pointer == "/data/treatment"
    assert text in str(err.value)


def test_score_time_accepts_all_untreated_and_all_treated() -> None:
    check_treatment(np.zeros((2, 3)), fit=False)
    check_treatment(np.ones((2, 3)), fit=False)


def test_staggered_diagnostic() -> None:
    assert staggered_diagnostic(np.array([3, 3, -1])) is None
    diag = staggered_diagnostic(adoption_positions(TREAT))
    assert diag is not None
    assert (diag.code, diag.severity, diag.value) == ("STAGGERED_ADOPTION", "warning", 2.0)


def test_settings_and_manifest() -> None:
    with pytest.raises(ValidationError):
        DiDSettings(covariates=["treatment"])
    with pytest.raises(ValidationError):
        DiDSettings(covariates=["x", "x"])
    m = DiDModel.manifest()
    assert (m.id, m.version) == ("causal.did", "1.0.0")
    assert m.capabilities.score_modes == frozenset({"predict", "counterfactual"})
    assert m.capabilities.owns == frozenset({"scaling"})
    assert m.plots[0] == "causal.event_study"
    check_about(DiDModel)


def test_scaling_round_trip() -> None:
    sc = DiDScaling(1.5, 2.0, [0.1], [3.0])
    assert DiDScaling.from_json(sc.to_json()) == sc


def test_did_dataset_and_spec() -> None:
    ds, truth = did_dataset(units=30, periods=24, adoption="staggered")
    assert dict(ds.sizes) == {"store": 30, "month": 24}
    adopt = adoption_positions(ds[TREATMENT].values)
    assert sorted({int(a) for a in adopt if a >= 0}) == [9, 14, 19]
    assert int((adopt >= 0).sum()) == 12
    assert truth.att == pytest.approx(5.0)
    assert sum(v is None for v in truth.adoption.values()) == 18
    spec = load_model_spec(DID_SPEC_YAML)
    assert DiDSettings.model_validate(spec.model.settings).lags == 6
    with pytest.raises(ValueError):
        did_dataset(treated_share=1.0)


# ======================================================================== effect groups


@pytest.fixture(scope="module")
def prior_fit() -> tuple[DiDModel, xr.Dataset, DiDSettings, xr.Dataset, DiDScaling]:
    """The model built at fit with prior draws standing in for the posterior."""
    ds = _canonical(did_dataset(units=8, periods=10, treated_share=0.5, seed=1)[0])
    ds["y"][0, 0] = np.nan  # a missing cell: the fit likelihood skips it
    s = DiDSettings(leads=2, lags=2)
    model = DiDModel()
    sc = fit_scaling(ds, s)
    from mapi_plugin_causal import did as did_module

    token = did_module._SCALING.set(sc)
    try:
        pm_model = model.build_model(ds, s, BuildContext(phase="fit", ds=ds))
    finally:
        did_module._SCALING.reset(token)
    assert pm_model["y"].eval().shape == (79,)
    prior = pm.sample_prior_predictive(draws=20, model=pm_model, random_seed=2)
    posterior = prior["prior"].to_dataset()
    return model, ds, s, posterior, sc


def test_event_study_layout(
    prior_fit: tuple[DiDModel, xr.Dataset, DiDSettings, xr.Dataset, DiDScaling],
) -> None:
    model, ds, s, posterior, sc = prior_fit
    treat = ds[TREATMENT].transpose(UNIT, TIME).values
    _, study = model._effect_groups(posterior, ds, treat, s, sc, 5, FitContext())
    assert study[EVENT_DIM].values.tolist() == [-2, -1, 0, 1, 2]
    assert study[EVENT_DIM].dtype == np.int64
    assert (study["coef"].sel({EVENT_DIM: -1}) == 0).all()
    assert study.attrs == {
        "reference": -1,
        "binned": 1,
        "design": "event_study",
        "model": "causal.did@1.0.0",
    }
    coef = posterior["event_coef"].stack(s=("chain", "draw"))
    for k in (-2, 0, 1, 2):  # without a pipeline the per-cell differences are the coefficients
        got = np.sort(study["coef"].sel({EVENT_DIM: k}).values)
        np.testing.assert_allclose(got, np.sort(coef.sel({EVENT_DIM: k}).values))
    assert int(study["n_cells"].sel({EVENT_DIM: -1})) == 4  # one reference cell per treated unit
    assert study["att"].dims == (SAMPLE,)
    assert int(study["n_cells"].sum()) <= 4 * 10


def test_impact_counterfactual_removes_the_dummies(
    prior_fit: tuple[DiDModel, xr.Dataset, DiDSettings, xr.Dataset, DiDScaling],
) -> None:
    model, ds, s, posterior, sc = prior_fit
    treat = ds[TREATMENT].transpose(UNIT, TIME).values
    impact, _ = model._effect_groups(posterior, ds, treat, s, sc, 5, FitContext())
    rows = np.flatnonzero(adoption_positions(treat) >= 0)
    assert impact[UNIT].values.tolist() == [str(u) for u in ds[UNIT].values[rows]]
    np.testing.assert_array_equal(impact["post"].values, treat[rows] == 1)
    assert impact.attrs["intervention_end"] == ""
    from mapi_plugin_causal._common import thin_samples

    thin = thin_samples(posterior, seed=5)
    mu = thin["mu"].transpose(SAMPLE, UNIT, TIME).values[:, rows]
    coef = thin["event_coef"].transpose(SAMPLE, EVENT_DIM).values
    d = event_dummies(treat, s)[rows]
    base = mu - np.einsum("unk,sk->sun", d, coef)
    sigma = thin["sigma"].values * sc.scale
    want = base + sigma[:, None, None] * crn_normal(5, "counterfactual", base.shape)
    np.testing.assert_allclose(impact["counterfactual"].values, want)
    assert np.isnan(impact["actual"].values).sum() == (1 if 0 in rows else 0)
    assert IMPACT == "impact"


def test_did_design_att_equals_the_coefficient(
    prior_fit: tuple[DiDModel, xr.Dataset, DiDSettings, xr.Dataset, DiDScaling],
) -> None:
    _, ds, _, _, sc = prior_fit
    s = DiDSettings(design="did")
    from mapi_plugin_causal import did as did_module

    token = did_module._SCALING.set(sc)
    try:
        pm_model = DiDModel().build_model(ds, s, BuildContext(phase="fit", ds=ds))
    finally:
        did_module._SCALING.reset(token)
    posterior = pm.sample_prior_predictive(draws=10, model=pm_model, random_seed=3)["prior"]
    treat = ds[TREATMENT].transpose(UNIT, TIME).values
    _, study = DiDModel()._effect_groups(posterior.to_dataset(), ds, treat, s, sc, 1, FitContext())
    assert study[EVENT_DIM].values.tolist() == [-1, 0]
    np.testing.assert_allclose(study["coef"].sel({EVENT_DIM: 0}).values, study["att"].values)
    assert study.attrs["design"] == "did"


def test_score_build_with_treatment_zero_has_no_effect(
    prior_fit: tuple[DiDModel, xr.Dataset, DiDSettings, xr.Dataset, DiDScaling],
) -> None:
    _, ds, s, _, sc = prior_fit
    from mapi_plugin_causal import did as did_module

    off = ds.assign({TREATMENT: xr.zeros_like(ds[TREATMENT])})
    token = did_module._SCALING.set(sc)
    try:
        ctx = BuildContext(
            phase="score", ds=off, fit_coords={str(d): ds[d].values for d in ds.dims}
        )
        DiDModel().build_model(off, s, ctx)
    finally:
        did_module._SCALING.reset(token)
    assert (ctx.data_values["event_dummies"] == 0).all()
    assert ctx.observed_vars["y"] == (UNIT, TIME)


# ======================================================================== padded score payloads


def _sent(cols: range | list[int], n: int = 8) -> np.ndarray:
    mask = np.zeros((3, n), dtype=bool)
    mask[:, list(cols)] = True
    return mask


@pytest.mark.parametrize(
    ("cols", "desc"),
    [
        (range(5, 8), "the tail: the adoption lies in the padded periods"),
        (range(1, 4), "the head: the periods after the payload are padded"),
        (range(2, 6), "the middle"),
        ([], "no period (padded units)"),
    ],
)
def test_padded_cells_take_the_fitted_adoption(cols: range | list[int], desc: str) -> None:
    sent = _sent(cols)
    padded = np.where(sent, TREAT, 0.0)  # what the harness gives the model (pad_fill 0)
    got = fill_padded_treatment(padded, sent, TREAT)
    np.testing.assert_array_equal(got, TREAT, err_msg=desc)
    np.testing.assert_array_equal(adoption_positions(got), [3, 5, -1])
    check_treatment(got, fit=False)


def test_padded_cells_follow_a_payload_that_differs_from_the_fit() -> None:
    # the counterfactual (treatment 0 in the sent periods) turns the padded periods before
    # them to 0 too, so the unit has no dummy at all
    sent = _sent(range(5, 8))
    got = fill_padded_treatment(np.zeros_like(TREAT), sent, TREAT)
    assert (got == 0).all()
    # a later adoption in the payload (period 6 for unit 0) wins over the fitted one (3)
    scenario = np.zeros_like(TREAT)
    scenario[0, 6:] = 1
    got = fill_padded_treatment(scenario, _sent(range(4, 8)), TREAT)
    np.testing.assert_array_equal(adoption_positions(got), [6, -1, -1])
    # a payload of pre-adoption periods keeps the fitted adoption (the leads stay)
    got = fill_padded_treatment(np.zeros_like(TREAT), _sent(range(0, 2)), TREAT)
    np.testing.assert_array_equal(got, TREAT)


def test_fitted_treatment_and_requested_cells() -> None:
    times = np.array(["2024-01-01", "2024-02-01", "2024-03-01"], dtype="datetime64[ns]")
    ds = xr.Dataset(
        {TREATMENT: ((UNIT, TIME), np.zeros((3, 3)))},
        coords={
            UNIT: ["a", "b", "c"],
            TIME: times,
            f"{TIME}__requested": (TIME, [False, True, True]),
            f"{UNIT}__requested": (UNIT, [True, True, False]),
        },
    )
    got = fitted_treatment(ds, {"a": "2024-02-01", "b": None, "c": "2024-01-01"})
    np.testing.assert_array_equal(got, [[0, 1, 1], [0, 0, 0], [1, 1, 1]])
    np.testing.assert_array_equal(
        requested_cells(ds), [[False, True, True], [False, True, True], [False, False, False]]
    )
    assert requested_cells(ds.drop_vars([f"{TIME}__requested", f"{UNIT}__requested"])).all()


def test_score_build_on_a_tail_payload_keeps_the_event_times(
    prior_fit: tuple[DiDModel, xr.Dataset, DiDSettings, xr.Dataset, DiDScaling],
) -> None:
    """Regression: a payload of the last periods only used to move the adoption to its first
    period (the padded periods were 0), so the cells got the lag-0 dummy."""
    _, ds, s, _, sc = prior_fit
    from mapi_plugin_causal import did as did_module
    from mapi_plugin_causal._common import time_label

    treat = ds[TREATMENT].transpose(UNIT, TIME).values
    adopt = adoption_positions(treat)
    times = ds[TIME].values
    learned = {
        str(u): (time_label(times[a]) if a >= 0 else None)
        for u, a in zip(ds[UNIT].values, adopt, strict=True)
    }
    first = int(adopt[adopt >= 0].min()) + 2  # the payload starts after every adoption
    sent = np.arange(ds.sizes[TIME]) >= first
    padded = ds.assign({TREATMENT: ds[TREATMENT].where(xr.DataArray(sent, dims=TIME), 0.0)})
    padded = padded.assign_coords({f"{TIME}__requested": (TIME, sent)})
    tokens = (did_module._SCALING.set(sc), did_module._ADOPTION.set(learned))
    try:
        ctx = BuildContext(
            phase="score", ds=padded, fit_coords={str(d): ds[d].values for d in ds.dims}
        )
        DiDModel().build_model(padded, s, ctx)
    finally:
        did_module._ADOPTION.reset(tokens[1])
        did_module._SCALING.reset(tokens[0])
    np.testing.assert_array_equal(ctx.data_values["event_dummies"], event_dummies(treat, s))
