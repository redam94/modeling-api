"""``sci.areal_bym2`` (E18): the graph (contiguity, islands, scaling factor), the ICAR and CAR
samplers, the kinds of area effect, leave-areas-out, every score mode, typed refusals, the
plots fed from the bundle, recovery on a synthetic map and the Scottish lip-cancer data."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
import scipy.special
import xarray as xr
from mapi_plots.recipes import render_plots
from mapi_plugin_science.areal import (
    ADJACENCY,
    OBS_DIM,
    ArealBYM2Model,
    ArealBYM2Settings,
    area_graph,
    boundary_distance,
    contiguity,
    draw_car,
    draw_icar,
    morans_i,
    scaling_factor,
)
from mapi_plugin_science.areal_data import (
    AREAL_SCHEMA,
    AREAL_SPEC_YAML,
    AREAL_TRUTH,
    CHALLENGES_FILE,
    SCOTLAND_SPEC_YAML,
    areal_dataset,
    challenges_reference,
    from_challenges,
    square_rings,
)
from mapi_plugin_science.challenges import challenges_file
from mapi_pymc.context import BuildContext
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import validate_dataset
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import load_model_spec, load_score_request, validate_spec
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import ScoreRequest

CHALLENGES = challenges_file(CHALLENGES_FILE)
HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://sci/areal\n"
CANON = {"district": "area", "district_": "area_", "aff": "covariate"}


def _req(body: str) -> ScoreRequest:
    return load_score_request(HEADER + body)


def _spec(settings: str, *, split: bool = False, fit: str = "") -> str:
    """The tiny spec with other settings (and no holdout unless ``split``)."""
    spec = AREAL_SPEC_YAML.replace("{kind: bym2, islands: link_nearest}", settings)
    if not split:
        spec = spec.replace(
            "  split: {kind: mask_block, dims: [area], fraction: 0.15, seed: 18}\n", ""
        )
        spec = spec.replace("mode: evaluate_then_refit, ", "mode: train_only, ")
    if fit:
        spec = spec.replace("tune: 300, draws: 150", fit)
    return spec


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(ArealBYM2Model, replace=True)
    return reg


@pytest.fixture(scope="module")
def dataset() -> xr.Dataset:
    return areal_dataset(seed=0)


@pytest.fixture(scope="module")
def bundle(registry: Registry, dataset: xr.Dataset) -> Bundle:
    return fit_spec(AREAL_SPEC_YAML, dataset, registry=registry, raw_schema=AREAL_SCHEMA)


def _canon(ds: xr.Dataset) -> xr.Dataset:
    return ds.rename(CANON)


def _build(ds: xr.Dataset, **settings: Any) -> Any:
    canon = _canon(ds)
    s = ArealBYM2Settings(**settings)
    return ArealBYM2Model().build(canon, s, BuildContext(phase="fit", ds=canon))


# ------------------------------------------------------------------------------- manifest


def test_manifest_declares_area_outputs_and_the_map_plots() -> None:
    man = ArealBYM2Model.manifest()
    assert man.id == "sci.areal_bym2"
    assert man.capabilities.score_modes == frozenset({"predict", "simulate", "evaluate"})
    assert man.capabilities.loo_valid
    assert list(man.outputs) == ["cases", "rr", "p_exceed", "effect"]
    assert man.outputs["cases"].units_of == "cases"
    assert man.outputs["rr"].units == "1" and man.outputs["p_exceed"].units == "probability"
    assert all(o.grid is None for o in man.outputs.values())  # areas, not a raster
    schema = man.schema_template.resolve({})
    assert schema.dims["area"].kind == "categorical" and schema.dims["area"].policy == "fixed"
    assert schema.vars["cases"].missing == "allow"
    assert set(schema.vars) == {"cases", "expected", "covariate", "adjacency", "ring_x", "ring_y"}
    assert {"sci.area_map", "net.graph", "core.ridge"} <= set(man.plots)
    props: Any = man.settings_schema["properties"]
    assert set(props["kind"]["enum"]) == {
        "poisson", "iid", "icar", "car", "bym2",
    }  # fmt: skip


@pytest.mark.parametrize("yaml", [AREAL_SPEC_YAML, SCOTLAND_SPEC_YAML])
def test_static_validation_resolves_the_specs(registry: Registry, yaml: str) -> None:
    res = validate_spec(load_model_spec(yaml), registry.catalog(), AREAL_SCHEMA)
    assert res.ok, [(i.pointer, i.message) for i in res.errors]


def test_synthetic_data_matches_the_raw_schema(dataset: xr.Dataset) -> None:
    assert validate_dataset(AREAL_SCHEMA, dataset) == []
    assert dataset.sizes["district"] == 43  # 6 x 7 squares and one island
    assert (dataset["adjacency"].values == dataset["adjacency"].values.T).all()
    assert int(dataset["adjacency"].values[-1].sum()) == 0  # the island touches nothing


# ------------------------------------------------------------------------------- graph maths


def test_queen_and_rook_contiguity_differ_at_corners() -> None:
    rx, ry = square_rings(2, 2)
    queen = contiguity(rx, ry)
    rook = contiguity(rx, ry, rule="rook")
    assert queen.sum() // 2 == 6 and rook.sum() // 2 == 4  # two diagonal corner contacts
    np.testing.assert_array_equal(rook, [[0, 1, 1, 0], [1, 0, 0, 1], [1, 0, 0, 1], [0, 1, 1, 0]])
    with pytest.raises(ValueError):
        contiguity(rx, ry, rule="bishop")


def test_islands_link_to_the_closest_boundary_and_fix_the_scaling_factor() -> None:
    ds = areal_dataset(n_row=3, n_col=3, seed=1)
    rx, ry = ds["ring_x"].values, ds["ring_y"].values
    w0 = ds["adjacency"].values
    assert np.isnan(scaling_factor(w0))  # E18 failure 1: an island makes s undefined
    g = area_graph(w0, rx, ry, "link_nearest")
    assert g.n_components_input == 2 and g.islands_input == (9,) and g.n_components == 1
    (i, j, d), = g.links  # fmt: skip
    near = min(range(9), key=lambda k: boundary_distance(rx, ry, 9, k))
    assert (i, j) == (9, near) and d == pytest.approx(1500.0)
    s = scaling_factor(g.w)
    assert 0 < s < 1
    q = np.diag(g.w.sum(1)) - g.w
    assert s == pytest.approx(np.exp(np.mean(np.log(np.diag(np.linalg.pinv(q))))))
    kept = area_graph(w0, rx, ry, "isolate")
    assert kept.n_components == 2 and not kept.links
    with pytest.raises(MapiError) as err:
        area_graph(w0, rx, ry, "error", ds["district"].values)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and "D10" in str(err.value)


def test_separate_pieces_are_linked_until_the_graph_is_connected() -> None:
    rx, ry = square_rings(1, 6)
    w = contiguity(rx, ry)
    w[2, 3] = w[3, 2] = 0  # two chains of three, no island
    g = area_graph(w, rx, ry, "link_nearest")
    assert g.n_components == 1 and len(g.links) == 1 and {g.links[0][0], g.links[0][1]} == {2, 3}


def test_the_icar_sampler_has_the_generalised_inverse_as_covariance() -> None:
    rx, ry = square_rings(3, 4)
    w = contiguity(rx, ry, rule="rook")
    draws = draw_icar(w, 40_000, np.random.default_rng(0))
    np.testing.assert_allclose(draws.sum(1), 0.0, atol=1e-9)  # sum-to-zero
    q_plus = np.linalg.pinv(np.diag(w.sum(1)) - w)
    np.testing.assert_allclose(np.cov(draws.T), q_plus, atol=0.04)
    scaled = draw_icar(w, 20_000, np.random.default_rng(1)) / np.sqrt(scaling_factor(w))
    # E18 check: the scaled field has a typical (geometric mean) marginal variance of 1
    assert np.exp(np.mean(np.log(scaled.var(0)))) == pytest.approx(1.0, abs=0.05)


def test_the_car_sampler_has_the_inverse_precision_as_covariance() -> None:
    rx, ry = square_rings(2, 3)
    w = contiguity(rx, ry, rule="rook")
    n = 30_000
    draws = draw_car(w, np.full(n, 0.9), np.full(n, 4.0), np.random.default_rng(2))
    want = np.linalg.inv(4.0 * (np.diag(w.sum(1)) - 0.9 * w))
    np.testing.assert_allclose(np.cov(draws.T), want, atol=0.01)


def test_morans_i_sees_a_gradient() -> None:
    rx, ry = square_rings(4, 4)
    w = contiguity(rx, ry, rule="rook")
    assert morans_i(rx[:, 0], w) > 0.5
    checker = np.indices((4, 4)).sum(0).ravel() % 2
    assert morans_i(checker.astype(float), w) == pytest.approx(-1.0)


# ------------------------------------------------------------------------------- the model


def test_bym2_graph_matches_the_numpy_twin(dataset: xr.Dataset) -> None:
    model = _build(dataset)
    rng = np.random.default_rng(3)
    n = dataset.sizes["district"]
    point = {
        "b0": 0.2, "b1": -0.3, "sigma_log__": np.log(0.7), "rho_logodds__": 0.4,
        "phi": rng.normal(size=n), "theta": rng.normal(size=n),
    }  # fmt: skip
    (rr_graph,) = model.replace_rvs_by_values([model["rr"]])
    fn = model.compile_fn(rr_graph, inputs=model.value_vars, on_unused_input="ignore")
    rr = fn(point)
    g = area_graph(dataset["adjacency"].values, dataset["ring_x"].values,
                   dataset["ring_y"].values, "link_nearest")  # fmt: skip
    s = scaling_factor(g.w)
    rho = scipy.special.expit(0.4)
    u = 0.7 * (np.sqrt(rho / s) * point["phi"] + np.sqrt(1 - rho) * point["theta"])
    x = (dataset["aff"].values - dataset["aff"].values.mean()) / 10.0
    np.testing.assert_allclose(rr, np.exp(0.2 - 0.3 * x + u), rtol=1e-10)


@pytest.mark.parametrize(
    ("kind", "free"),
    [
        ("poisson", {"b0", "b1"}),
        ("iid", {"b0", "b1", "sigma", "theta"}),
        ("icar", {"b0", "b1", "sigma", "phi"}),
        ("car", {"b0", "b1", "sigma", "alpha", "phi"}),
        ("bym2", {"b0", "b1", "sigma", "rho", "phi", "theta"}),
    ],
)
def test_every_kind_builds_with_a_finite_density(
    dataset: xr.Dataset, kind: str, free: set[str]
) -> None:
    model = _build(dataset, kind=kind)
    assert {rv.name for rv in model.free_RVs} == free
    logp = model.point_logps()
    assert all(np.isfinite(v) for v in logp.values()), logp


def test_islands_isolate_is_refused_where_the_graph_must_be_connected(dataset: xr.Dataset) -> None:
    for kind in ("bym2", "car"):
        with pytest.raises(MapiError) as err:
            _build(dataset, kind=kind, islands="isolate")
        assert err.value.code is ErrorCode.SETTINGS_INVALID
        assert err.value.pointer == "/model/settings/islands"
    # the ICAR samples with the island alone: a flat prior for it (E18 failure 1, quietly)
    model = _build(dataset, kind="icar", islands="isolate")
    assert np.isfinite(model.point_logps()["phi"])


@pytest.mark.parametrize(
    ("change", "pointer"),
    [
        ({"cases": -1.0}, "/data/cases"),
        ({"cases": 2.5}, "/data/cases"),
        ({"expected": 0.0}, "/data/expected"),
    ],
)
def test_bad_values_raise_typed_errors(
    dataset: xr.Dataset, change: dict[str, float], pointer: str
) -> None:
    (name, value), = change.items()  # fmt: skip
    bad = dataset.copy(deep=True)
    bad[name].values[0] = value
    with pytest.raises(MapiError) as err:
        _build(bad)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and err.value.pointer == pointer


def test_all_counts_missing_is_refused(dataset: xr.Dataset) -> None:
    with pytest.raises(MapiError) as err:
        _build(dataset.assign(cases=dataset["cases"] * np.nan))
    assert err.value.code is ErrorCode.SCHEMA_INVALID


# ------------------------------------------------------------------------------- fit


def test_fit_recovers_the_truth_and_keeps_the_map_in_the_bundle(
    bundle: Bundle, dataset: xr.Dataset
) -> None:
    post = bundle.model.groups["posterior"]
    for name in ("b0", "b1", "sigma"):
        lo, hi = np.quantile(post[name].values, [0.01, 0.99])
        assert lo < AREAL_TRUTH[name] < hi, (name, lo, hi)
    const = bundle.model.groups["constant_data"]
    n = dataset.sizes["district"]
    assert const["ring_x"].dims == ("area", "vertex") and const.sizes["area"] == n
    adj = const[ADJACENCY]
    assert (
        adj.dims == ("area", "area_") and int(adj.values.sum()) // 2 == 132
    )  # 131 queen edges + 1 link
    assert const["centroid"].dims == ("area", "xy") and bool(np.isfinite(const["centroid"]).all())
    np.testing.assert_allclose(
        const["smr"].values, dataset["cases"].values / dataset["expected"].values
    )
    learned = bundle.model.learned
    assert learned["kind"] == "bym2" and learned["links"] == [["D43", "D35", 1500.0]]
    assert learned["n_components_input"] == 2 and learned["n_components"] == 1
    assert 0 < float(learned["scaling_factor"]) < 1  # type: ignore[arg-type]
    # the pointwise log-likelihood is always kept: elpd_loo works without a keep block
    assert bundle.model.groups["log_likelihood"]["cases"].dims == ("chain", "draw", "area")
    assert np.isfinite(float(bundle.metrics.values["elpd_loo"] or np.nan))
    assert bundle.metrics.elpd["elpd_loo"].n == n


def test_leave_areas_out_keeps_the_held_out_areas_in_the_graph(
    bundle: Bundle, dataset: xr.Dataset
) -> None:
    ev = bundle.evaluation
    assert ev is not None
    obs = ev.model.groups["observed_data"]["cases"]
    n = dataset.sizes["district"]
    assert obs.dims == (OBS_DIM,) and 0 < obs.sizes[OBS_DIM] < n
    # the latent field still covers every area, held out or not
    assert ev.model.groups["posterior"]["effect"].sizes["area"] == n
    held = sorted(set(dataset["district"].values) - set(obs[OBS_DIM].values))
    assert ev.model.learned["n_observed"] == n - len(held)
    values = bundle.metrics.values
    assert bundle.metrics.n_holdout == len(held)
    assert (values["coverage_90"] or 0.0) >= 0.5 and (values["rmse"] or 0.0) > 0


# ------------------------------------------------------------------------------- score


def _score(bundle: Bundle, registry: Registry, body: str, data: xr.Dataset) -> xr.Dataset:
    out = score_bundle(bundle, _req(body), data, registry=registry, return_full=True)
    return out["predictions"].to_dataset()


def test_predict_returns_the_posterior_relative_risk(
    bundle: Bundle, dataset: xr.Dataset, registry: Registry
) -> None:
    pred = _score(bundle, registry, "mode: predict\n", dataset.drop_vars("cases"))
    post = bundle.model.groups["posterior"]
    np.testing.assert_allclose(pred["rr"].values, post["rr"].values, rtol=1e-10)
    np.testing.assert_allclose(
        pred["cases"].values, post["rr"].values * dataset["expected"].values, rtol=1e-10
    )
    np.testing.assert_allclose(
        pred["p_exceed"].mean(("chain", "draw")), (post["rr"] > 1).mean(("chain", "draw"))
    )
    assert set(np.unique(pred["p_exceed"].values)) <= {0.0, 1.0}
    assert pred["rr"].dims == ("chain", "draw", "district")


def test_predict_needs_only_the_area_labels(
    bundle: Bundle, dataset: xr.Dataset, registry: Registry
) -> None:
    some = dataset["district"].values[[4, 1]]
    labels_only = xr.Dataset(coords={"district": some})
    pred = _score(bundle, registry, "mode: predict\n", labels_only)
    full = _score(bundle, registry, "mode: predict\n", dataset.drop_vars("cases"))
    xr.testing.assert_allclose(pred["rr"], full["rr"].sel(district=some))
    # a payload can change the covariate of an area (what if)
    more = dataset.drop_vars("cases").assign(aff=dataset["aff"] + 10.0)
    moved = _score(bundle, registry, "mode: predict\n", more)
    ratio = (moved["rr"] / full["rr"]).values
    b1 = bundle.model.groups["posterior"]["b1"].values[..., None]
    np.testing.assert_allclose(ratio, np.broadcast_to(np.exp(b1), ratio.shape), rtol=1e-8)


def test_predictive_counts_use_frozen_noise_per_area(
    bundle: Bundle, dataset: xr.Dataset, registry: Registry
) -> None:
    body = "mode: predict\noutput: {kind: predictive}\nseed: 4\n"
    pred = _score(bundle, registry, body, dataset.drop_vars("cases"))
    cases = pred["cases"].values
    assert (cases >= 0).all() and set(np.unique(cases % 1)) == {0.0}
    assert pred["cases"].attrs.get("noise") in (None, "crn_ppf")
    again = _score(bundle, registry, body, dataset.drop_vars("cases").isel(district=[7, 3]))
    np.testing.assert_array_equal(again["cases"].values, pred["cases"].isel(district=[7, 3]).values)


def test_simulate_draws_new_maps_from_the_fitted_prior(
    bundle: Bundle, dataset: xr.Dataset, registry: Registry
) -> None:
    sim = _score(bundle, registry, "mode: simulate\nseed: 2\n", dataset.drop_vars("cases"))
    post = bundle.model.groups["posterior"]
    eff = sim["effect"].values
    assert not np.allclose(eff, post["effect"].values)
    # new BYM2 maps: the area effects keep the fitted scale on average
    sd = np.sqrt((eff**2).mean())
    sigma = float(post["sigma"].mean())
    assert 0.5 * sigma < sd < 1.6 * sigma
    assert set(np.unique(sim["cases"].values % 1)) == {0.0}  # counts, always with noise
    again = _score(bundle, registry, "mode: simulate\nseed: 2\n", dataset.drop_vars("cases"))
    xr.testing.assert_identical(sim, again)


@pytest.mark.parametrize("kind", ["poisson", "iid", "icar", "car"])
def test_simulate_for_every_kind(registry: Registry, dataset: xr.Dataset, kind: str) -> None:
    b = fit_spec(
        _spec(f"{{kind: {kind}}}", fit="tune: 100, draws: 40"),
        dataset,
        registry=registry,
        raw_schema=AREAL_SCHEMA,
    )
    sim = _score(b, registry, "mode: simulate\n", dataset.drop_vars("cases"))
    eff = sim["effect"].values
    assert np.isfinite(eff).all() and np.isfinite(sim["cases"].values).all()
    if kind == "poisson":
        assert not eff.any()
    if kind == "icar":
        np.testing.assert_allclose(eff.sum(-1), 0.0, atol=1e-8)


def test_simulate_refuses_an_icar_with_islands(registry: Registry, dataset: xr.Dataset) -> None:
    b = fit_spec(
        _spec("{kind: icar, islands: isolate}", fit="tune: 100, draws: 40"),
        dataset,
        registry=registry,
        raw_schema=AREAL_SCHEMA,
    )
    assert b.model.learned["n_components"] == 2 and b.model.learned["scaling_factor"] is None
    with pytest.raises(MapiError) as err:
        _score(b, registry, "mode: simulate\n", dataset.drop_vars("cases"))
    assert err.value.code is ErrorCode.SETTINGS_INVALID


def test_unsupported_modes_and_unknown_areas_are_refused(
    bundle: Bundle, dataset: xr.Dataset, registry: Registry
) -> None:
    body = "mode: counterfactual\ninterventions: [{var: aff, multiply: 1.1}]\n"
    with pytest.raises(MapiError) as err:
        _score(bundle, registry, body, dataset.drop_vars("cases"))
    assert err.value.code is ErrorCode.MODE_NOT_SUPPORTED
    new = (
        dataset.drop_vars("cases")
        .isel(district=[0])
        .assign_coords(district=["Atlantis"], district_=dataset["district"].values)
    )
    with pytest.raises(MapiError) as err:
        _score(bundle, registry, "mode: predict\n", new)
    assert err.value.code is ErrorCode.UNSEEN_LEVELS


def test_the_map_and_graph_plots_render_from_the_bundle(bundle: Bundle) -> None:
    out = render_plots(
        bundle.to_tree(),
        ["sci.area_map", "net.graph"],
        options={"sci.area_map": {"var": "rr", "observed": "smr", "show_edges": True}},
    )
    assert not out.issues, [i.message for i in out.issues]
    assert len(out.plots) == 2


@pytest.mark.mapi_contract
def test_areal_bym2_passes_the_contract_kit(registry: Registry, dataset: xr.Dataset) -> None:
    fixture = ModelFixture(
        spec=AREAL_SPEC_YAML, data=dataset, raw_schema=AREAL_SCHEMA, registry=registry
    )
    report = check_model(ArealBYM2Model, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "fit",
        "bundle_roundtrip_no_pickle",
        "shuffled_scoring",
        "subset_scoring",
        "new_level_policies",
        "unsupported_mode",
        "output_space_and_units",
        "outputs_match_manifest",
        "seed_determinism",
        "plots_sanitize",
    } <= ran


# ------------------------------------------------------------------------------- real data


@pytest.mark.skipif(CHALLENGES is None, reason="pymc-challenges data not present")
def test_from_challenges_reads_the_scottish_districts() -> None:
    assert CHALLENGES is not None
    ds = from_challenges(CHALLENGES)
    assert validate_dataset(AREAL_SCHEMA, ds) == []
    assert ds.sizes["district"] == 56 and int(ds["cases"].sum()) == 536
    assert ds["district"].values[0] == "Skye-Lochalsh" and ds["cases"].values[0] == 9
    assert int(ds["adjacency"].sum()) // 2 == 117  # queen contiguity of the polygons
    rook = from_challenges(CHALLENGES, graph="rook")
    assert int(rook["adjacency"].sum()) // 2 == 115
    published = from_challenges(CHALLENGES, graph="published")
    assert int(published["adjacency"].sum()) // 2 == 132
    g = area_graph(ds["adjacency"].values, ds["ring_x"].values, ds["ring_y"].values,
                   "link_nearest", ds["district"].values)  # fmt: skip
    names = ds["district"].values
    links = {(str(names[i]), str(names[j])) for i, j, _ in g.links}
    assert links == {
        ("Okney", "Caithness"),
        ("Shetland", "Okney"),
        ("WesternIsles", "Ross-Cromarty"),
    }
    assert g.w.sum() // 2 == 120 and scaling_factor(g.w) == pytest.approx(0.632, abs=1e-3)


@pytest.mark.slow
@pytest.mark.skipif(CHALLENGES is None, reason="pymc-challenges data not present")
def test_real_data_bym2_reproduces_the_notebook(registry: Registry) -> None:
    assert CHALLENGES is not None
    ds = from_challenges(CHALLENGES)
    spec = (
        SCOTLAND_SPEC_YAML.replace("mode: evaluate_then_refit", "mode: train_only")
        .replace("  split: {kind: mask_block, dims: [area], fraction: 0.125, seed: 1987}\n", "")
        .replace("engine: nutpie", "engine: pymc")
        .replace("tune: 1000, draws: 1000", "tune: 1000, draws: 500")
    )
    b = fit_spec(spec, ds, registry=registry, raw_schema=AREAL_SCHEMA)
    post = b.model.groups["posterior"]
    ref = challenges_reference()["bym2"]
    assert abs(float(post["b1"].mean()) - ref["b1"]) < 0.05
    assert abs(float(post["b0"].mean()) - ref["b0"]) < 0.03
    assert abs(float(post["sigma"].mean()) - ref["sigma"]) < 0.05
    assert abs(float(post["rho"].mean()) - ref["rho"]) < 0.08
    p_exceed = (post["rr"] > 1).mean(("chain", "draw")).values
    assert 17 <= int((p_exceed > 0.95).sum()) <= 21  # the notebook: 19
    assert -156 < (b.metrics.values["elpd_loo"] or 0.0) < -150  # the notebook: -152.7
    assert int(b.model.groups["sample_stats"]["diverging"].sum()) == 0
