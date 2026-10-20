"""``ts.gaussian_hmm`` end to end: terminal state, relabelling, forecast and filter (E24)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_ts import GaussianHMMModel
from mapi_plugin_ts.datasets import (
    TRACKS_SCHEMA,
    TRACKS_SPEC_YAML,
    TRACKS_TRUTH,
    tracks_dataset,
)
from mapi_plugin_ts.hmm import HIDDEN_STATES, TERMINAL_STATE
from mapi_plugin_ts.hmm_math import stationary
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import score_bundle
from mapi_sdk.models import FitOutput, ScoreContext
from mapi_sdk.registry import Registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import ErrorCode, MapiError, load_model_spec, load_score_request, validate_spec

pytestmark = pytest.mark.slow

HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://ts/hmm\n"


def _req(body: str) -> Any:
    return load_score_request(HEADER + body)


def _future(steps: int = 6) -> xr.Dataset:
    """The next ``steps`` steps of every track (same generator, longer series)."""
    return tracks_dataset(animals=4, steps=40 + steps, seed=0).isel(step=slice(40, None))


def test_manifest_declares_terminal_state_modes_and_no_loo() -> None:
    caps = GaussianHMMModel.manifest().capabilities
    assert {"forecast", "filter"} <= caps.score_modes
    assert not caps.loo_valid and caps.loo_invalid_reason
    assert "transition" in caps.identified_quantities


def test_elpd_loo_is_rejected_statically(registry: Registry) -> None:
    spec = load_model_spec(TRACKS_SPEC_YAML.replace("[rmse, mae, coverage_90]", "[elpd_loo]"))
    res = validate_spec(spec, registry.catalog(), TRACKS_SCHEMA)
    assert any(i.code is ErrorCode.CAPABILITY_UNSUPPORTED for i in res.errors)


def test_fit_recovers_states_and_stores_terminal_state(hmm_bundle: Bundle) -> None:
    post = hmm_bundle.model.posterior
    mu = post["mu"].mean(("chain", "draw")).values
    assert np.all(np.diff(post["mu"].values, axis=-1) > 0)  # every draw ordered
    np.testing.assert_allclose(mu, TRACKS_TRUTH["mu"], atol=0.25)
    trans = post["transition"].mean(("chain", "draw")).values
    assert trans[0, 0] > 0.6 and trans[1, 1] > 0.5
    term = hmm_bundle.model.terminal_state
    assert term is not None and dict(term["alpha_T"].sizes) == {
        "chain": 2,
        "draw": 100,
        "animal": 4,
        "state": 2,
    }
    np.testing.assert_allclose(term["alpha_T"].sum("state"), 1.0)
    assert term.attrs["step"] == 39
    assert list(term["last_observed"].values) == [39, 35, 39, 33]
    hidden = hmm_bundle.model.groups[HIDDEN_STATES]["state_prob"]
    np.testing.assert_allclose(
        hidden.isel(step=-1), term["alpha_T"].transpose(*hidden.dims[:-2], "state")
    )
    assert "log_likelihood" not in hmm_bundle.model.groups
    assert hmm_bundle.metrics.skipped.get("elpd_loo") is None


def test_bundle_roundtrip_keeps_the_terminal_state(
    hmm_bundle: Bundle, registry: Registry, tmp_path: Any
) -> None:
    hmm_bundle.write(tmp_path)
    back = Bundle.read(tmp_path, registry)
    assert back.model.terminal_state is not None
    xr.testing.assert_identical(back.model.terminal_state, hmm_bundle.model.terminal_state)
    req = _req("mode: forecast\nhorizon: {dim: step, periods: 3}\n")
    a = score_bundle(hmm_bundle, req, registry=registry)
    b = score_bundle(back, req, registry=registry)
    sa, sb = a["summary"].to_dataset(), b["summary"].to_dataset()
    xr.testing.assert_allclose(sa, sb, rtol=1e-12)


def test_forecast_propagates_the_terminal_state_to_stationarity(
    hmm_bundle: Bundle, registry: Registry
) -> None:
    out = score_bundle(
        hmm_bundle,
        _req("mode: forecast\nhorizon: {dim: step, periods: 60}\n"),
        registry=registry,
        return_full=True,
    )
    pred = out["predictions"].to_dataset()
    probs = pred["state_prob"]
    assert probs.attrs["units"] == "probability"
    np.testing.assert_allclose(probs.sum("state"), 1.0)  # never inverse-transformed
    assert pred["y_hat"].attrs["units"] == "km"
    post = hmm_bundle.model.posterior
    trans = post["transition"].transpose("chain", "draw", "state", "state_next").values
    target = stationary(trans.reshape(-1, 2, 2)).reshape(2, 100, 2)
    far = probs.isel(step=-1).transpose("chain", "draw", "animal", "state").values
    np.testing.assert_allclose(far, np.broadcast_to(target[:, :, None], far.shape), atol=1e-3)
    near = probs.isel(step=0).mean(("chain", "draw"))
    assert float(np.abs(near.sel(animal="a0") - near.sel(animal="a2")).max()) > 0.0


def test_predict_never_uses_payload_targets(
    hmm_bundle: Bundle, tracks: xr.Dataset, registry: Registry
) -> None:
    req = _req("mode: predict\n")
    a = score_bundle(hmm_bundle, req, tracks, registry=registry)
    b = score_bundle(hmm_bundle, req, tracks.drop_vars("step_length"), registry=registry)
    xr.testing.assert_identical(a["summary"].to_dataset(), b["summary"].to_dataset())


def test_filter_updates_the_terminal_state(hmm_bundle: Bundle, registry: Registry) -> None:
    new = _future(6)
    out = score_bundle(
        hmm_bundle,
        _req("mode: filter\ndata: dataset://ecology/tracks_live@v1\n"),
        new,
        registry=registry,
        return_full=True,
    )
    assert TERMINAL_STATE in out.children
    term = out[TERMINAL_STATE].to_dataset()
    assert term.attrs["step"] == 45 and term.attrs["previous_step"] == 39
    np.testing.assert_allclose(term["alpha_T"].sum("state"), 1.0)
    assert list(term["last_observed"].values) == [45, 41, 45, 33]
    probs = out["predictions"].to_dataset()["state_prob"]
    assert probs.attrs["kind"] == "filtered"
    # a long move is read as the travelling state once observed
    lengths = new["step_length"].sel(animal="a0").values
    filtered = probs.sel(animal="a0").mean(("chain", "draw")).sel(state=1).values
    assert np.corrcoef(np.log1p(lengths), filtered)[0, 1] > 0.5


def test_filter_in_two_batches_equals_one_batch(hmm_bundle: Bundle) -> None:
    """Filtering from an updated terminal state continues exactly where the last filter
    stopped (the plugin-owned state is all a sequential model needs)."""
    model = GaussianHMMModel()
    fitted = hmm_bundle.model
    canon = _future(6).rename({"step_length": "y", "observed": "mask"})
    canon["y"] = np.log1p(canon["y"])
    ctx = ScoreContext(mode="filter", kind="expected", seed=1, schema=fitted.schema)
    req = _req("mode: filter\ndata: dataset://x/y@v1\n")
    whole = model.score(fitted, canon, req, ctx)[TERMINAL_STATE].to_dataset()
    first = model.score(fitted, canon.isel(step=slice(0, 3)), req, ctx)[TERMINAL_STATE].to_dataset()
    updated = replace(fitted, terminal_state=first)
    second = model.score(updated, canon.isel(step=slice(3, None)), req, ctx)[TERMINAL_STATE]
    np.testing.assert_allclose(second.to_dataset()["alpha_T"], whole["alpha_T"], atol=1e-10)


def test_filter_rejects_steps_inside_the_fit_and_missing_targets(
    hmm_bundle: Bundle, tracks: xr.Dataset, registry: Registry
) -> None:
    req = _req("mode: filter\ndata: dataset://x/y@v1\n")
    with pytest.raises(MapiError) as err:
        score_bundle(hmm_bundle, req, tracks.isel(step=slice(-5, None)), registry=registry)
    assert err.value.code is ErrorCode.COORD_NOT_ALLOWED
    with pytest.raises(MapiError) as err2:
        score_bundle(hmm_bundle, req, _future().drop_vars("step_length"), registry=registry)
    assert err2.value.code is ErrorCode.SCHEMA_INVALID


def test_new_animals_start_from_pi(hmm_bundle: Bundle, registry: Registry) -> None:
    window = tracks_dataset(animals=4, steps=40).isel(step=slice(0, 3))
    newbie = window.sel(animal=["a0"]).assign_coords(animal=["z_new"])
    payload = xr.concat([window, newbie], dim="animal")
    out = score_bundle(
        hmm_bundle, _req("mode: predict\n"), payload, registry=registry, return_full=True
    )
    probs = out["predictions"].to_dataset()["state_prob"].sel(animal="z_new", step=0)
    pi = hmm_bundle.model.posterior["pi"]
    np.testing.assert_allclose(probs.transpose(*pi.dims).values, pi.values)


def test_postprocess_relabels_switched_draws(hmm_bundle: Bundle, tracks: xr.Dataset) -> None:
    model = GaussianHMMModel()
    fitted = hmm_bundle.model
    post = fitted.posterior
    swap = xr.DataArray([1, 0], dims="state")
    switched = post.copy()
    for name in ("pi", "mu", "sigma"):
        flipped = post[name].isel(state=swap).assign_coords(state=post["state"].values)
        switched[name] = flipped.where(post["chain"] == 1, post[name])
    t = post["transition"].isel(state=swap, state_next=swap.rename(state="state_next"))
    t = t.assign_coords(state=post["state"].values, state_next=post["state_next"].values)
    switched["transition"] = t.where(post["chain"] == 1, post["transition"])
    out = FitOutput(
        groups={"posterior": switched}, learned={}, terminal_state=fitted.terminal_state
    )
    fixed = model.postprocess(out, tracks, model.parse_settings({}))
    assert fixed.learned["relabelled_draws"] == 100
    for name in ("pi", "mu", "sigma", "transition"):
        xr.testing.assert_allclose(fixed.groups["posterior"][name], post[name])
    unchanged = model.postprocess(
        FitOutput(groups={"posterior": post}), tracks, model.parse_settings({})
    )
    assert unchanged.groups["posterior"] is post


def test_ordered_identification_builds_an_ordered_mean() -> None:
    model = GaussianHMMModel()
    data = tracks_dataset(animals=2, steps=5)
    y = np.log1p(data["step_length"].values)
    pm_model = model.build_model(
        y, data["observed"].values, model.parse_settings({"identification": "ordered"})
    )
    transforms = {rv.name: type(tr).__name__ for rv, tr in pm_model.rvs_to_transforms.items() if tr}
    assert transforms["mu"] == "Ordered"


@pytest.mark.mapi_contract
def test_gaussian_hmm_passes_the_contract_kit(registry: Registry, tracks: xr.Dataset) -> None:
    fixture = ModelFixture(
        spec=TRACKS_SPEC_YAML, data=tracks, raw_schema=TRACKS_SCHEMA, registry=registry
    )
    report = check_model(GaussianHMMModel, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "shuffled_scoring",
        "subset_scoring",
        "new_level_policies",
        "context_window_equivalence",
        "evaluate_path_equals_score_path",
        "seed_determinism",
    } <= ran


def test_manifest_lists_the_state_sequence_plot() -> None:
    plots = GaussianHMMModel.manifest().plots
    assert list(plots) == ["core.convergence", "core.posterior_forest", "core.state_sequence"]


def test_an_advi_fit_keeps_its_loss_history(registry: Registry, tracks: xr.Dataset) -> None:
    from mapi_pymc.engines import FIT_HISTORY
    from mapi_sdk.harness import fit_spec

    spec = TRACKS_SPEC_YAML.replace(
        "tune: 150, draws: 100, chains: 2, engine: pymc", "tune: 300, draws: 50, engine: advi"
    ).replace("mode: evaluate_then_refit", "mode: train_only")
    b = fit_spec(spec, tracks, registry=registry, raw_schema=TRACKS_SCHEMA)
    hist = b.model.groups[FIT_HISTORY]
    assert hist["loss"].dims == ("step",) and hist.sizes["step"] == 300
    assert hist["loss"].attrs["engine"] == "advi"
