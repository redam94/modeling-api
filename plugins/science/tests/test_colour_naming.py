"""``sci.colour_naming`` (E77): the naming graph against its NumPy twin, recovery on simulated
speakers, every score mode, typed refusals, the plots and the World Color Survey."""

from __future__ import annotations

import json

import numpy as np
import pytest
import xarray as xr
from mapi_plots.recipes import render_plots
from mapi_plugin_science.challenges import challenges_file
from mapi_plugin_science.colour_naming import (
    FEATURE_NAMES,
    ColourNamingModel,
    ColourNamingSettings,
    _Data,
    chip_features,
    effective_categories,
    lab_to_srgb,
    naming_probabilities,
)
from mapi_plugin_science.colour_naming_data import (
    CHALLENGES_FILE,
    NAFAANRA_HELD_OUT,
    NAFAANRA_SPEC_YAML,
    NAMING_SCHEMA,
    NAMING_SPEC_YAML,
    NAMING_TRUTH,
    from_challenges,
    naming_dataset,
)
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import validate_dataset
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import load_model_spec, load_score_request, validate_spec
from mapi_spec.errors import ErrorCode, MapiError

WCS = challenges_file(CHALLENGES_FILE)
HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://sci/colour\n"
CANON = {
    "response": "naming",
    "cie_l": "lab_l",
    "cie_a": "lab_a",
    "cie_b": "lab_b",
    "wcs_row": "grid_row",
    "wcs_col": "grid_col",
}


def _req(body: str) -> object:
    return load_score_request(HEADER + body)


def _spec(**settings: object) -> str:
    """The tiny spec with other model settings."""
    body = ", ".join(f"{k}: {v}" for k, v in settings.items())
    return NAMING_SPEC_YAML.replace(
        "settings: {kind: hierarchical, new_speakers: 5, score_draws: 50}",
        f"settings: {{{body}, new_speakers: 5, score_draws: 50}}",
    )


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(ColourNamingModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def naming() -> xr.Dataset:
    return naming_dataset(seed=0)


@pytest.fixture(scope="module")
def bundle(registry: Registry, naming: xr.Dataset) -> Bundle:
    return fit_spec(NAMING_SPEC_YAML, naming, registry=registry, raw_schema=NAMING_SCHEMA)


@pytest.fixture(scope="module")
def pooled(registry: Registry, naming: xr.Dataset) -> Bundle:
    return fit_spec(_spec(kind="pooled"), naming, registry=registry, raw_schema=NAMING_SCHEMA)


# ------------------------------------------------------------------------------- manifest


def test_manifest_declares_a_palette_grid_and_categorical_outputs() -> None:
    man = ColourNamingModel.manifest()
    assert man.id == "sci.colour_naming"
    assert man.capabilities.score_modes == frozenset({"predict", "simulate", "evaluate"})
    assert man.capabilities.loo_valid
    grid = man.outputs["p_term"].grid
    assert grid is not None and (grid.y, grid.x, grid.frame) == ("lightness", "hue", "term")
    assert man.outputs["p_name"].dims == ["speaker", "chip", "term"]
    # every output declares its own units: none is read as a numeric prediction of the target
    assert all(o.units is not None and o.units_of is None for o in man.outputs.values())
    schema = man.schema_template.resolve({})
    assert schema.vars["naming"].dtype == "str" and schema.vars["naming"].role.value == "target"
    assert schema.dims["speaker"].new_level_strategy == "hyperprior"
    assert schema.dims["chip"].policy.value == "fixed"
    assert set(man.plots) >= {"sci.swatches", "sci.field_map"}


def test_static_validation_resolves_both_specs(registry: Registry) -> None:
    for text in (NAMING_SPEC_YAML, NAFAANRA_SPEC_YAML):
        res = validate_spec(load_model_spec(text), registry.catalog(), NAMING_SCHEMA)
        assert res.ok, [(i.pointer, i.message) for i in res.errors]
    assert validate_dataset(NAMING_SCHEMA, naming_dataset()) == []


# ------------------------------------------------------------------------------- numerics


def test_lab_to_srgb_matches_known_colours() -> None:
    lab = np.array([[100.0, 0.0, 0.0], [0.0, 0.0, 0.0], [53.24, 80.09, 67.2]])
    rgb = lab_to_srgb(lab)
    np.testing.assert_allclose(rgb[0], 1.0, atol=2e-3)
    np.testing.assert_allclose(rgb[1], 0.0, atol=1e-9)
    np.testing.assert_allclose(rgb[2], [1.0, 0.0, 0.0], atol=6e-3)  # sRGB red
    assert ((rgb >= 0) & (rgb <= 1)).all()


def test_feature_sets_are_standardised_and_know_the_hue_circle() -> None:
    ds = naming_dataset(rows=3, hues=6)
    lab = np.c_[ds["cie_l"].values, ds["cie_a"].values, ds["cie_b"].values]
    row, col = ds["wcs_row"].values, ds["wcs_col"].values
    for kind, names in FEATURE_NAMES.items():
        f = chip_features(lab, row, col, kind)  # type: ignore[arg-type]
        assert f.shape == (ds.sizes["chip"], len(names))
        np.testing.assert_allclose(f[:, 0], 1.0)
        np.testing.assert_allclose(f[:, 1:].mean(0), 0.0, atol=1e-12)
    circle = chip_features(lab, row, col, "grid_circle")
    line = chip_features(lab, row, col, "grid_line")
    # on a circle, column 6 is as close to column 1 as column 2 is; on a line it is far
    c1, c2, c6 = (np.flatnonzero((row == 1) & (col == c))[0] for c in (1, 2, 6))

    def gap(f: np.ndarray, i: int, j: int) -> float:
        return float(np.linalg.norm(f[i] - f[j]))

    assert gap(circle, c1, c6) == pytest.approx(gap(circle, c1, c2))
    assert gap(line, c1, c6) > 3 * gap(line, c1, c2)


def test_the_pymc_graph_matches_the_numpy_twin(naming: xr.Dataset) -> None:
    ds = naming.rename(CANON)
    rng = np.random.default_rng(3)
    for kind in ("pooled", "hierarchical"):
        s = ColourNamingSettings(kind=kind)  # type: ignore[arg-type]
        data = _Data.from_dataset(ds, s)
        model = ColourNamingModel().build_model(data, s)
        point = {k: v + rng.normal(0, 0.5, np.shape(v)) for k, v in model.initial_point().items()}
        names = ["W", "sd", "z"] if kind == "hierarchical" else ["W"]
        values = model.compile_fn(
            model.replace_rvs_by_values([model[n] for n in names]),
            inputs=model.value_vars,
            on_unused_input="ignore",
        )(point)
        (pot,) = model.replace_rvs_by_values([model["loglik"]])
        got = model.compile_fn(pot, inputs=model.value_vars, on_unused_input="ignore")(point)
        p = naming_probabilities(data.features, *values)
        sp, ch = np.nonzero(data.codes >= 0)
        y = data.codes[sp, ch]
        want = np.log(p[sp, ch, y] if kind == "hierarchical" else p[ch, y]).sum()
        np.testing.assert_allclose(got, want, rtol=1e-9)


def test_effective_categories_count_crisp_equal_categories() -> None:
    p = np.zeros((90, 3))
    p[np.arange(90), np.arange(90) % 3] = 1.0
    assert effective_categories(p) == pytest.approx(3.0)
    assert effective_categories(np.full((90, 3), 1 / 3)) == pytest.approx(1.0)


# ------------------------------------------------------------------------------- refusals


def _fit_raw(ds: xr.Dataset, **settings: object) -> None:
    s = ColourNamingSettings(**settings)  # type: ignore[arg-type]
    _Data.from_dataset(ds.rename(CANON), s)


def test_typed_refusals(naming: xr.Dataset) -> None:
    one_term = naming.assign(response=xr.full_like(naming["response"], "light"))
    with pytest.raises(MapiError) as err:
        _fit_raw(one_term)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and "at least 2" in str(err.value)

    with pytest.raises(MapiError) as err:
        _fit_raw(naming.isel(speaker=[0]), kind="hierarchical")
    assert err.value.code is ErrorCode.SETTINGS_INVALID
    _fit_raw(naming.isel(speaker=[0]), kind="pooled")  # one speaker is fine when pooled

    with pytest.raises(MapiError) as err:
        _fit_raw(naming, features="grid_line", n_random=8)
    assert err.value.code is ErrorCode.SETTINGS_INVALID and "n_random" in str(err.value)

    hole = naming.copy(deep=True)
    hole["cie_a"][3] = np.nan
    with pytest.raises(MapiError) as err:
        _fit_raw(hole)
    assert err.value.code is ErrorCode.SCHEMA_INVALID

    twin = naming.copy(deep=True)
    twin["wcs_col"][7] = int(twin["wcs_col"][6])
    twin["wcs_row"][7] = int(twin["wcs_row"][6])
    with pytest.raises(MapiError) as err:
        _fit_raw(twin)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and "same grid" in str(err.value)

    with pytest.raises(MapiError) as err:
        ColourNamingModel().parse_settings({"kind": "mixture"})
    assert err.value.code is ErrorCode.SETTINGS_INVALID


# ------------------------------------------------------------------------------- fit


def test_fit_recovers_the_language_shares(bundle: Bundle, naming: xr.Dataset) -> None:
    """The single quadratic weights are confounded on a small palette; the identified
    quantity is the share of speakers who give each term to each chip."""
    post = bundle.model.posterior
    terms = [str(t) for t in post["term"].values]
    truth = np.asarray(NAMING_TRUTH["W"], dtype=float)
    truth -= truth.mean(1, keepdims=True)
    order = [list(NAMING_TRUTH["terms"]).index(t) for t in terms]
    lab = np.c_[naming["cie_l"].values, naming["cie_a"].values, naming["cie_b"].values]
    row, col = naming["wcs_row"].values, naming["wcs_col"].values
    z = np.random.default_rng(1).standard_normal((4000, 4, 3))
    z -= z.mean(-1, keepdims=True)
    sd_true = np.full((4, 3), NAMING_TRUTH["sd"])
    true = naming_probabilities(chip_features(lab, row, col), truth, sd_true, z).mean(0)
    true = true[:, order]  # (chip, term) in the fitted term order
    p = post["p_term"].stack(s=("chain", "draw")).transpose("s", "term", "lightness", "hue")
    draws = p.values[:, :, row - int(p["lightness"][0]), col - int(p["hue"][0])]
    assert np.abs(draws.mean(0).T - true).mean() < 0.1
    lo, hi = np.quantile(draws, [0.01, 0.99], axis=0)
    assert ((true + 1e-3 >= lo.T) & (true - 1e-3 <= hi.T)).mean() > 0.8
    # the signs the truth fixes: light rises with L*, dark falls, warm rises with a*
    w = post["W"].mean(("chain", "draw"))
    assert float(w.sel(feat="L", term="light")) > 0 > float(w.sel(feat="L", term="dark"))
    assert float(w.sel(feat="a", term="warm")) > 0
    sd = post["sd"].mean(("chain", "draw")).values
    assert 0.05 < float(np.median(sd)) < 1.5
    obs = bundle.model.groups["observed_data"]
    assert obs["naming"].dims == ("speaker", "chip") and int(obs["naming"].min()) == -1
    assert bundle.model.learned["terms"] == terms
    assert bundle.model.groups["log_likelihood"]["naming"].dims == ("chain", "draw", "chip")


def test_language_level_outputs_live_in_the_posterior(bundle: Bundle, naming: xr.Dataset) -> None:
    post = bundle.model.posterior
    p = post["p_term"]
    assert p.dims == ("chain", "draw", "term", "lightness", "hue")
    total = p.sum("term", skipna=False)
    filled = np.isfinite(total.values)
    assert filled.sum() == naming.sizes["chip"] * p.sizes["chain"] * p.sizes["draw"]
    np.testing.assert_allclose(total.values[filled], 1.0, atol=1e-4)
    rgb = post["mode_term_rgb"]
    assert rgb.dims == ("chain", "draw", "chip", "rgb") and {"row", "col"} <= set(rgb.coords)
    assert float(rgb.min()) >= 0 and float(rgb.max()) <= 1
    k_spk = float(post["k_eff_speaker"].mean())
    k_lang = float(post["k_eff_language"].mean())
    assert 1.0 < k_lang <= k_spk + 0.05 < 3.05  # speakers disagree: the language is blurrier
    const = bundle.model.groups["constant_data"]
    assert const["chip_rgb"].dims == ("chip", "rgb")


def test_evaluate_scores_held_out_speakers_and_elpd_is_per_chip(
    bundle: Bundle, pooled: Bundle, naming: xr.Dataset
) -> None:
    for b in (bundle, pooled):
        values = b.metrics.values
        assert -1.2 < values["log_score"] < -0.1
        assert values["accuracy_lift"] > 0.1
        detail = b.metrics.elpd["elpd_loo"]
        assert detail.n == naming.sizes["chip"] and detail.pointwise is not None
    assert pooled.model.groups["posterior"].get("sd") is None
    assert pooled.model.learned["kind"] == "pooled"


# ------------------------------------------------------------------------------- score


def test_predict_new_and_fitted_speakers(
    bundle: Bundle, naming: xr.Dataset, registry: Registry
) -> None:
    payload = naming.isel(speaker=[0, 1]).drop_vars("response")
    payload = payload.assign_coords(speaker=["s01", "visitor"])
    out = score_bundle(
        bundle, _req("mode: predict\noutput: {draws: 5}\n"), payload, registry=registry,
        return_full=True,
    )  # fmt: skip
    pred = out["predictions"].to_dataset()
    p = pred["p_name"]
    assert p.dims == ("chain", "draw", "speaker", "chip", "term")
    np.testing.assert_allclose(p.sum("term").values, 1.0, atol=1e-9)
    # a fitted speaker keeps their own effects; a new one gets fresh ones in every draw
    spread = p.std(("chain", "draw")).mean(("chip", "term"))
    assert float(spread.sel(speaker="visitor")) > float(spread.sel(speaker="s01"))
    assert pred["mode_term_rgb"].attrs["units"] == "sRGB"
    assert {"row", "col"} <= set(pred["mode_term_rgb"].coords)
    assert out["summary"].to_dataset()["p_term"].attrs["units"] == "probability"


def test_predictive_names_are_one_hot_and_reproducible(
    bundle: Bundle, naming: xr.Dataset, registry: Registry
) -> None:
    payload = naming.isel(speaker=[2]).drop_vars("response")
    req = _req("mode: predict\noutput: {kind: predictive}\n")
    a = score_bundle(bundle, req, payload, registry=registry, return_full=True)
    b = score_bundle(bundle, req, payload, registry=registry, return_full=True)
    pa = a["predictions"].to_dataset()["p_name"]
    assert set(np.unique(pa.values)) == {0.0, 1.0}
    np.testing.assert_array_equal(pa.sum("term").values, 1.0)
    xr.testing.assert_identical(pa, b["predictions"].to_dataset()["p_name"])


def test_simulate_draws_fresh_speakers_even_for_fitted_labels(
    bundle: Bundle, naming: xr.Dataset, registry: Registry
) -> None:
    payload = naming.isel(speaker=[0]).drop_vars("response")
    pred = score_bundle(
        bundle, _req("mode: predict\n"), payload, registry=registry, return_full=True
    )["predictions"].to_dataset()["p_name"]
    sim = score_bundle(
        bundle, _req("mode: simulate\n"), payload, registry=registry, return_full=True
    )["predictions"].to_dataset()["p_name"]
    assert sim.dims == pred.dims
    assert float(sim.std(("chain", "draw")).mean()) > float(pred.std(("chain", "draw")).mean())


def test_evaluate_mode_reports_categorical_metrics(
    bundle: Bundle, naming: xr.Dataset, registry: Registry
) -> None:
    tree = score_bundle(
        bundle,
        _req("mode: evaluate\ndata: dataset://x/naming@v1\noutput: {kind: predictive}\n"),
        naming.isel(speaker=slice(0, 4)),
        registry=registry,
        metrics=["log_score", "accuracy", "brier"],
    )
    values = json.loads(tree.attrs["metrics"])["values"]
    # evaluate keeps probabilities (not one-hot draws), so the log score stays finite
    assert -1.0 < values["log_score"] < 0 and 0.5 < values["accuracy"] <= 1
    # the fit-time holdout metrics come back through the score path (new speakers)
    ev = bundle.evaluation
    assert ev is not None
    seen = set(ev.model.posterior["speaker"].values.tolist())
    held = [s for s in naming["speaker"].values.tolist() if s not in seen]
    assert len(held) == 2
    names = ["log_score", "accuracy", "brier"]
    again = score_bundle(
        ev,
        _req("mode: evaluate\ndata: dataset://x/naming@v1\nseed: 77\n"),
        naming.sel(speaker=held),
        registry=registry,
        metrics=names,
    )
    got = json.loads(again.attrs["metrics"])["values"]
    for n in names:
        assert got[n] == pytest.approx(bundle.metrics.values[n], rel=1e-9)


@pytest.mark.parametrize(
    ("body", "payload", "code"),
    [
        ("mode: forecast\nhorizon: {dim: chip, periods: 1}\n", "fit", "MODE_NOT_SUPPORTED"),
        ("mode: counterfactual\ninterventions: [{var: cie_a, multiply: 1.1}]\n", "fit",
         "MODE_NOT_SUPPORTED"),
        ("mode: predict\n", "new_chips", "UNSEEN_LEVELS"),
    ],
)  # fmt: skip
def test_score_refusals(
    bundle: Bundle, naming: xr.Dataset, registry: Registry, body: str, payload: str, code: str
) -> None:
    data = naming.isel(speaker=[0]).drop_vars("response")
    if payload == "new_chips":
        data = data.isel(chip=[0, 1]).assign_coords(chip=[901, 902])
    with pytest.raises(MapiError) as err:
        score_bundle(bundle, _req(body), data, registry=registry)
    assert err.value.code.value == code


# ------------------------------------------------------------------------------- plots


def test_the_manifest_plots_render_from_the_bundle(bundle: Bundle) -> None:
    report = render_plots(bundle.to_tree(), ColourNamingModel.plots)
    assert not report.issues, [(i.code, i.message, i.untrusted_text) for i in report.issues]
    by_id = {p.id: p for p in report.plots}
    swatch = by_id["sci.swatches"]
    caption = swatch.caption_plain or ""
    assert "map of the" in caption and "observed colours" in caption
    assert "p_term" in json.dumps(by_id["sci.field_map"].model_dump(mode="json"))[:200_000]


@pytest.mark.mapi_contract
def test_colour_naming_passes_the_contract_kit(registry: Registry, naming: xr.Dataset) -> None:
    fixture = ModelFixture(
        spec=NAMING_SPEC_YAML, data=naming, raw_schema=NAMING_SCHEMA, registry=registry
    )
    report = check_model(ColourNamingModel, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "fit",
        "bundle_roundtrip_no_pickle",
        "new_level_policies",
        "shuffled_scoring",
        "subset_scoring",
        "outputs_match_manifest",
        "seed_determinism",
        "plots_sanitize",
    } <= ran


# ------------------------------------------------------------------------------- real data


@pytest.mark.skipif(WCS is None, reason="pymc-challenges data not present")
def test_from_challenges_reads_nafaanra() -> None:
    assert WCS is not None
    ds = from_challenges(WCS)
    assert validate_dataset(NAMING_SCHEMA, ds) == []
    assert dict(ds.sizes) == {"speaker": 29, "chip": 330}
    labels, counts = np.unique(ds["response"].values, return_counts=True)
    got = dict(zip(labels.tolist(), counts.tolist(), strict=True))
    assert {k: got[k] for k in ("W", "N", "F", "B")} == {"W": 5082, "N": 2757, "F": 1550, "B": 173}
    assert set(NAFAANRA_HELD_OUT) <= set(ds["speaker"].values.tolist())
    assert ds.attrs["language"] == "Nafaanra"
    rgb = lab_to_srgb(np.c_[ds["cie_l"].values, ds["cie_a"].values, ds["cie_b"].values])
    white = int(np.flatnonzero((ds["wcs_row"].values == 0) & (ds["wcs_col"].values == 0))[0])
    assert rgb[white].min() > 0.9
    with pytest.raises(ValueError):
        from_challenges(WCS, language=999)


@pytest.mark.slow
@pytest.mark.skipif(WCS is None, reason="pymc-challenges data not present")
def test_nafaanra_pooled_held_out_log_score_matches_the_notebook(registry: Registry) -> None:
    """E77 part B: pooled CIELAB fit on 20 speakers, the 9 others scored (-0.392)."""
    assert WCS is not None
    ds = from_challenges(WCS)
    spec = NAFAANRA_SPEC_YAML.replace("kind: hierarchical", "kind: pooled").replace(
        "tune: 1000, draws: 1000, chains: 4,\n        engine: nutpie", "tune: 500, draws: 500, "
        "chains: 2, engine: pymc"
    ).replace("evaluate_then_refit", "train_only")  # fmt: skip
    b = fit_spec(spec, ds, registry=registry, raw_schema=NAMING_SCHEMA)
    assert b.model.learned["n_speakers"] == 20
    assert b.metrics.values["log_score"] == pytest.approx(-0.392, abs=0.004)
    k = float(b.model.posterior["k_eff_language"].mean())
    assert 1.6 < k < 2.1


@pytest.mark.slow
@pytest.mark.skipif(WCS is None, reason="pymc-challenges data not present")
def test_nafaanra_hierarchical_matches_the_notebook(registry: Registry) -> None:
    """E77 parts C and E: all 29 speakers; B is the term only some speakers use."""
    assert WCS is not None
    ds = from_challenges(WCS)
    groups = ", ".join(NAFAANRA_HELD_OUT)
    split = f"  split: {{kind: leave_group_out, dim: speaker, groups: [{groups}]}}\n"
    spec = NAFAANRA_SPEC_YAML.replace(split, "").replace(
        "tune: 1000, draws: 1000, chains: 4", "tune: 500, draws: 500, chains: 2"
    )
    assert "split" not in spec
    b = fit_spec(spec, ds, registry=registry, raw_schema=NAMING_SCHEMA)
    post = b.model.posterior
    sd = post["sd"].mean(("chain", "draw"))
    assert float(sd.sel(rfeat="1", term="B")) > 1.8  # notebook: 2.61
    assert float(sd.sel(rfeat="1", term="W")) < 0.5  # notebook: 0.25
    assert float(post["k_eff_speaker"].mean()) == pytest.approx(1.92, abs=0.05)
    assert float(post["k_eff_language"].mean()) == pytest.approx(1.84, abs=0.05)
    assert int(b.model.groups["sample_stats"]["diverging"].sum()) == 0
