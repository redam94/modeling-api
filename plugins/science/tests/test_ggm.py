"""``sci.graphical_model`` (E19): partial correlations and edge probabilities, the dense LKJ
model and the horseshoe neighbourhood model, the score modes, typed refusals, recovery on
synthetic data and the Sachs et al. (2005) cells."""

from __future__ import annotations

import math

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_science.challenges import challenges_file
from mapi_plugin_science.ggm import (
    CENTER,
    PAIR_DIM,
    REFERENCE,
    SCALE,
    VAR_DIM,
    GraphicalModel,
    GraphicalModelSettings,
    conditional_log_density,
    edge_indicator,
    partial_correlation,
    precision_from_cholesky,
    symmetrise_neighbourhood,
)
from mapi_plugin_science.ggm_data import (
    CHALLENGES_FILE,
    CHALLENGES_REFERENCE,
    CONSENSUS_FILE,
    GGM_SCHEMA,
    GGM_SPEC_YAML,
    GGM_TRUTH,
    PROTEINS,
    SACHS_NS_SPEC_YAML,
    SACHS_SPEC_YAML,
    from_challenges,
    ggm_dataset,
    read_consensus,
    true_pcor,
)
from mapi_pymc.context import BuildContext
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import validate_dataset
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import load_model_spec, load_score_request, validate_spec
from mapi_spec.errors import ErrorCode, MapiError

SACHS = challenges_file(CHALLENGES_FILE)
HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://sci/ggm\n"
NAMES = list(GGM_TRUTH["variables"])
TRUE_EDGES = {tuple(sorted((NAMES.index(a), NAMES.index(b)))) for a, b in GGM_TRUTH["edges"]}

NS_SPEC_YAML = (
    GGM_SPEC_YAML.replace("  split: {kind: leave_group_out, dim: cell, n: 40, seed: 19}\n", "")
    .replace("mode: evaluate_then_refit", "mode: train_only")
    .replace(
        "settings: {threshold: 0.1, reference: true}",
        "settings: {kind: neighbourhood, edge_rule: and, reference: true}",
    )
    .replace("[rmse, crps, coverage_90, elpd_loo]", "[elpd_loo]")
)
"""The tiny spec with the neighbourhood model (no holdout: it cannot score new cells)."""


def _req(body: str) -> object:
    return load_score_request(HEADER + body)


def _canon(ds: xr.Dataset) -> xr.Dataset:
    return ds.rename({"protein": VAR_DIM, "protein_": PAIR_DIM, "level": "value"}).rename_vars(
        {"consensus": REFERENCE}
    )


def _new_cells(n: int = 4) -> xr.Dataset:
    return xr.Dataset(coords={"cell": np.arange(1000, 1000 + n), "protein": NAMES})


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(GraphicalModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def cells() -> xr.Dataset:
    return ggm_dataset(seed=0)


@pytest.fixture(scope="module")
def bundle(registry: Registry, cells: xr.Dataset) -> Bundle:
    return fit_spec(GGM_SPEC_YAML, cells, registry=registry, raw_schema=GGM_SCHEMA)


@pytest.fixture(scope="module")
def ns_bundle(registry: Registry) -> Bundle:
    return fit_spec(NS_SPEC_YAML, ggm_dataset(cells=200, seed=3), registry=registry,
                    raw_schema=GGM_SCHEMA)  # fmt: skip


# ------------------------------------------------------------------------------- manifest


def test_manifest_declares_matrix_grids_and_the_graph_plots() -> None:
    man = GraphicalModel.manifest()
    assert man.id == "sci.graphical_model"
    assert man.capabilities.score_modes == frozenset({"predict", "simulate", "evaluate"})
    for name, cmap in (("pcor", "diverging"), ("edge_prob", "viridis")):
        out = man.outputs[name]
        assert out.dims == [VAR_DIM, PAIR_DIM] and out.units == "1"
        assert out.grid is not None and (out.grid.y, out.grid.x) == (VAR_DIM, PAIR_DIM)
        assert out.grid.colormap == cmap
    assert man.outputs["value"].units_of == "value"
    plain = man.schema_template.resolve({})
    assert set(plain.dims) == {"cell", VAR_DIM} and set(plain.vars) == {"value"}
    assert plain.dims["cell"].policy.value == "new_levels"
    with_ref = man.schema_template.resolve({"reference": True})
    assert with_ref.vars[REFERENCE].dims == (VAR_DIM, PAIR_DIM)
    assert {"net.graph", "sci.field_map"} <= set(man.plots)


def test_static_validation_resolves_the_specs(registry: Registry) -> None:
    for text in (GGM_SPEC_YAML, SACHS_SPEC_YAML, SACHS_NS_SPEC_YAML, NS_SPEC_YAML):
        res = validate_spec(load_model_spec(text), registry.catalog(), GGM_SCHEMA)
        assert res.ok, [(i.pointer, i.message) for i in res.errors]


def test_synthetic_data_follow_the_raw_schema(cells: xr.Dataset) -> None:
    assert validate_dataset(GGM_SCHEMA, cells) == []
    assert dict(cells.sizes) == {"cell": 300, "protein": 6, "protein_": 6}
    assert int(cells["consensus"].sum()) == 2 * len(GGM_TRUTH["edges"])
    np.testing.assert_allclose(cells["level"].mean("cell"), GGM_TRUTH["mean"], atol=0.25)


# ------------------------------------------------------------------------------- numerics


def test_graph_partial_correlations_match_the_numpy_twin(cells: xr.Dataset) -> None:
    ds = _canon(cells)
    s = GraphicalModelSettings(threshold=0.2)
    model = GraphicalModel().build(ds, s, BuildContext(phase="fit", ds=ds))
    point = model.initial_point()
    rng = np.random.default_rng(4)
    packed = "chol_cholesky-cov-packed__"
    point[packed] = rng.normal(0, 0.4, point[packed].shape)
    names = ["pcor", "edge_prob", "precision"]
    graph = model.replace_rvs_by_values([model[n] for n in names])
    fn = model.compile_fn(graph, inputs=model.value_vars, on_unused_input="ignore")
    pcor, edge, omega = fn(point)
    chol_fn = model.compile_fn(
        model.replace_rvs_by_values([model["chol_stds"], model["chol_corr"]]),
        inputs=model.value_vars,
        on_unused_input="ignore",
    )
    sds, corr = chol_fn(point)
    cov = corr * np.outer(sds, sds)
    want_omega = precision_from_cholesky(np.linalg.cholesky(cov))
    np.testing.assert_allclose(omega, want_omega, rtol=1e-8, atol=1e-10)
    np.testing.assert_allclose(omega, np.linalg.inv(cov), rtol=1e-6, atol=1e-8)
    np.testing.assert_allclose(pcor, partial_correlation(want_omega), atol=1e-10)
    np.testing.assert_array_equal(edge, edge_indicator(pcor, 0.2))
    assert np.allclose(np.diag(pcor), 1.0) and np.allclose(np.diag(edge), 0.0)


def test_true_partial_correlations_are_the_generating_edges() -> None:
    rho = true_pcor()
    for (a, b), want in GGM_TRUTH["edges"].items():
        assert rho[NAMES.index(a), NAMES.index(b)] == pytest.approx(want)
    off = ~np.eye(len(NAMES), dtype=bool)
    edges = np.zeros_like(off)
    for i, j in TRUE_EDGES:
        edges[i, j] = edges[j, i] = True
    assert np.allclose(rho[off & ~edges], 0.0)


def test_conditional_log_density_is_the_full_conditional_normal() -> None:
    rng = np.random.default_rng(1)
    a = rng.normal(size=(4, 4))
    cov = a @ a.T + 4 * np.eye(4)
    mu = rng.normal(size=4)
    z = rng.normal(size=(3, 4))
    got = conditional_log_density(z, mu, np.linalg.inv(cov))
    want = np.zeros(3)
    for i in range(3):
        for j in range(4):
            rest = [k for k in range(4) if k != j]
            s12 = cov[j, rest]
            s22 = np.linalg.inv(cov[np.ix_(rest, rest)])
            m = mu[j] + s12 @ s22 @ (z[i, rest] - mu[rest])
            v = cov[j, j] - s12 @ s22 @ s12
            want[i] += -0.5 * math.log(2 * math.pi * v) - 0.5 * (z[i, j] - m) ** 2 / v
    np.testing.assert_allclose(got, want, rtol=1e-10)


def test_neighbourhood_symmetrisation_rules() -> None:
    b = np.array([[0.0, 0.5, 0.05], [0.3, 0.0, -0.2], [0.02, 0.05, 0.0]])
    rho, both = symmetrise_neighbourhood(b, 0.1, "and")
    _, either = symmetrise_neighbourhood(b, 0.1, "or")
    _, sym = symmetrise_neighbourhood(b, 0.1, "rho")
    assert rho[0, 1] == pytest.approx(math.sqrt(0.15)) and rho[1, 0] == rho[0, 1]
    assert rho[1, 2] == 0.0  # the signs disagree: no partial correlation
    np.testing.assert_array_equal(both, [[0, 1, 0], [1, 0, 0], [0, 0, 0]])
    np.testing.assert_array_equal(either, [[0, 1, 0], [1, 0, 1], [0, 1, 0]])
    np.testing.assert_array_equal(sym, [[0, 1, 0], [1, 0, 0], [0, 0, 0]])


def test_neighbourhood_graph_matches_the_numpy_twin(cells: xr.Dataset) -> None:
    ds = _canon(cells)
    for rule in ("and", "or", "rho"):
        s = GraphicalModelSettings(kind="neighbourhood", edge_rule=rule)
        model = GraphicalModel().build(ds, s, BuildContext(phase="fit", ds=ds))
        point = model.initial_point()
        point["z_raw"] = np.random.default_rng(2).normal(0, 3.0, point["z_raw"].shape)
        graph = model.replace_rvs_by_values([model["B"], model["pcor"], model["edge_prob"]])
        coef, pcor, edge = model.compile_fn(
            graph, inputs=model.value_vars, on_unused_input="ignore"
        )(point)
        assert np.allclose(np.diag(coef), 0.0)
        want_rho, want_edge = symmetrise_neighbourhood(coef, s.threshold, rule)
        np.testing.assert_allclose(pcor, want_rho, atol=1e-12)
        np.testing.assert_array_equal(edge, want_edge)


# ------------------------------------------------------------------------------- refusals


def _build(ds: xr.Dataset, **settings: object) -> None:
    s = GraphicalModelSettings(**settings)  # type: ignore[arg-type]
    GraphicalModel().build(ds, s, BuildContext(phase="fit", ds=ds))


def test_typed_refusals(cells: xr.Dataset) -> None:
    ds = _canon(cells)
    cases: list[tuple[xr.Dataset, dict[str, object], ErrorCode, str]] = [
        (ds, {"kind": "neighbourhood", "log_likelihood": "joint"}, ErrorCode.SETTINGS_INVALID,
         "log_likelihood"),
        (ds, {"kind": "neighbourhood", "expected_neighbours": 5.0}, ErrorCode.SETTINGS_INVALID,
         "expected_neighbours"),
        (ds.drop_vars(REFERENCE), {"reference": True}, ErrorCode.SCHEMA_INVALID, REFERENCE),
        (ds.isel({VAR_DIM: [0]}), {}, ErrorCode.SCHEMA_INVALID, VAR_DIM),
        (ds.assign(value=ds["value"].where(ds[VAR_DIM] != "g3", 1.0)), {},
         ErrorCode.SCHEMA_INVALID, "value"),
        (ds.assign(value=ds["value"].where(ds["cell"] != 7)), {}, ErrorCode.SCHEMA_INVALID,
         "value"),
    ]  # fmt: skip
    for data, settings, code, pointer in cases:
        with pytest.raises(MapiError) as err:
            _build(data, **settings)
        assert err.value.code is code, settings
        assert pointer in (err.value.pointer or ""), (settings, err.value.pointer)


# ------------------------------------------------------------------------------- dense fit


@pytest.mark.slow
def test_dense_fit_recovers_the_graph(bundle: Bundle, cells: xr.Dataset) -> None:
    groups = bundle.model.groups
    post = groups["posterior"]
    rho = true_pcor()
    pc = post["pcor"].transpose("chain", "draw", VAR_DIM, PAIR_DIM).values.reshape(-1, 6, 6)
    lo, hi = np.quantile(pc, [0.01, 0.99], axis=0)
    iu = np.triu_indices(6, 1)
    inside = (lo[iu] <= rho[iu]) & (rho[iu] <= hi[iu])
    assert inside.mean() >= 0.85, np.c_[lo[iu], rho[iu], hi[iu]]
    prob = post["edge_prob"].mean(("chain", "draw")).values
    for i, j in TRUE_EDGES:
        assert prob[i, j] > 0.9, (NAMES[i], NAMES[j], prob[i, j])
    null = [prob[i, j] for i, j in zip(*iu, strict=True) if (i, j) not in TRUE_EDGES]
    assert np.median(null) < 0.3
    np.testing.assert_array_equal(groups["constant_data"][REFERENCE].values, cells["consensus"])
    np.testing.assert_allclose(groups["constant_data"][CENTER], cells["level"].mean("cell"))
    np.testing.assert_allclose(groups["constant_data"][SCALE], cells["level"].std("cell"))
    learned = bundle.model.learned
    assert learned["kind"] == "dense" and learned["log_likelihood"] == "joint"
    assert 3 <= learned["edges"] <= 6


@pytest.mark.slow
def test_dense_keeps_one_log_likelihood_per_cell_for_loo(bundle: Bundle) -> None:
    ll = bundle.model.groups["log_likelihood"]["value"]
    assert ll.dims == ("chain", "draw", "cell")
    assert ll.sizes["cell"] == 300
    detail = bundle.metrics.elpd["elpd_loo"]
    assert detail.n == 300 and detail.pointwise is not None
    assert math.isfinite(bundle.metrics.values["elpd_loo"])
    # held-out cells were scored through the evaluate path
    assert bundle.metrics.values["coverage_90"] > 0.75
    assert bundle.metrics.values["crps"] is not None


@pytest.mark.slow
def test_dense_conditional_log_likelihood_matches_the_twin(
    registry: Registry, cells: xr.Dataset
) -> None:
    spec = (
        GGM_SPEC_YAML.replace("  split: {kind: leave_group_out, dim: cell, n: 40, seed: 19}\n", "")
        .replace("mode: evaluate_then_refit", "mode: train_only")
        .replace("reference: true}", "reference: true, log_likelihood: conditional}")
        .replace("[rmse, crps, coverage_90, elpd_loo]", "[elpd_loo]")
    )
    b = fit_spec(spec, cells, registry=registry, raw_schema=GGM_SCHEMA)
    ll = b.model.groups["log_likelihood"]["value"]
    assert ll.dims == ("chain", "draw", "cell")
    assert b.model.learned["log_likelihood"] == "conditional"
    post = b.model.groups["posterior"]
    x = cells["level"].values
    center, scale = x.mean(0), x.std(0)
    omega = post["precision"].isel(chain=0, draw=3).values
    mu = post["mu"].isel(chain=0, draw=3).values
    want = conditional_log_density((x - center) / scale, mu, omega) - np.log(scale).sum()
    np.testing.assert_allclose(ll.isel(chain=0, draw=3).values, want, rtol=1e-8)


@pytest.mark.slow
def test_predict_returns_the_graph_and_the_mean_of_new_cells(
    bundle: Bundle, registry: Registry
) -> None:
    out = score_bundle(bundle, _req("mode: predict\noutput: {draws: 10}\n"), _new_cells(),
                       registry=registry, return_full=True)  # fmt: skip
    pred = out["predictions"].to_dataset()
    assert set(pred.data_vars) == {"pcor", "edge_prob", "level"}
    assert pred["pcor"].dims == ("chain", "draw", "protein", "protein_")
    assert pred["level"].dims == ("chain", "draw", "cell", "protein")
    np.testing.assert_array_equal(pred["cell"].values, [1000, 1001, 1002, 1003])
    # expected values: every new cell gets the posterior mean of each variable
    level = pred["level"].mean(("chain", "draw"))
    np.testing.assert_allclose(level.isel(cell=0), level.isel(cell=3))
    np.testing.assert_allclose(level.isel(cell=0), GGM_TRUTH["mean"], atol=0.3)
    summary = out["summary"].to_dataset()
    assert summary["level"].attrs["units"] == "log_intensity"
    assert summary["edge_prob"].attrs["units"] == "1"


@pytest.mark.slow
def test_simulate_draws_new_cells_with_the_fitted_dependence(
    bundle: Bundle, registry: Registry
) -> None:
    out = score_bundle(bundle, _req("mode: simulate\noutput: {draws: 200}\n"), _new_cells(50),
                       registry=registry, return_full=True)  # fmt: skip
    sim = out["predictions"].to_dataset()["level"]
    assert sim.attrs.get("kind") == "predictive"
    x = sim.stack(s=("chain", "draw", "cell")).transpose("s", "protein").values
    corr = np.corrcoef(x.T)
    g1, g2 = NAMES.index("g1"), NAMES.index("g2")
    assert corr[g1, g2] > 0.3  # the strongest true edge shows in simulated cells
    np.testing.assert_allclose(x.std(0), GGM_TRUTH["sd"], rtol=0.25)
    again = score_bundle(bundle, _req("mode: simulate\noutput: {draws: 200}\n"),
                         _new_cells(50), registry=registry, return_full=True)  # fmt: skip
    xr.testing.assert_identical(out["predictions"].to_dataset(), again["predictions"].to_dataset())


@pytest.mark.slow
def test_unsupported_modes_are_refused(bundle: Bundle, registry: Registry) -> None:
    for body in (
        "mode: forecast\nhorizon: {dim: cell, periods: 2}\n",
        "mode: counterfactual\ninterventions: [{var: level, multiply: 1.1}]\n",
    ):
        with pytest.raises(MapiError) as err:
            score_bundle(bundle, _req(body), _new_cells(), registry=registry)
        assert err.value.code is ErrorCode.MODE_NOT_SUPPORTED


# ------------------------------------------------------------------------------- neighbourhood


@pytest.mark.slow
def test_neighbourhood_fit_finds_the_edges_and_scores_the_graph_only(
    ns_bundle: Bundle, registry: Registry
) -> None:
    post = ns_bundle.model.groups["posterior"]
    prob = post["edge_prob"].mean(("chain", "draw")).values
    for i, j in TRUE_EDGES:
        assert prob[i, j] > 0.8, (NAMES[i], NAMES[j], prob[i, j])
    iu = np.triu_indices(6, 1)
    null = [prob[i, j] for i, j in zip(*iu, strict=True) if (i, j) not in TRUE_EDGES]
    assert max(null) < 0.5
    ll = ns_bundle.model.groups["log_likelihood"]["value"]
    assert ll.dims == ("chain", "draw", "cell")
    assert ns_bundle.model.learned["log_likelihood"] == "conditional"
    assert math.isfinite(ns_bundle.metrics.values["elpd_loo"])
    out = score_bundle(ns_bundle, _req("mode: predict\n"), _new_cells(), registry=registry,
                       return_full=True)  # fmt: skip
    assert set(out["predictions"].to_dataset().data_vars) == {"pcor", "edge_prob"}
    for body in ("mode: simulate\n", "mode: predict\noutput: {kind: predictive}\n"):
        with pytest.raises(MapiError) as err:
            score_bundle(ns_bundle, _req(body), _new_cells(), registry=registry)
        assert err.value.code is ErrorCode.MODE_NOT_SUPPORTED


# ------------------------------------------------------------------------------- contract


@pytest.mark.slow
@pytest.mark.mapi_contract
def test_graphical_model_passes_the_contract_kit(registry: Registry, cells: xr.Dataset) -> None:
    fixture = ModelFixture(spec=GGM_SPEC_YAML, data=cells, raw_schema=GGM_SCHEMA, registry=registry)
    report = check_model(GraphicalModel, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "fit",
        "bundle_roundtrip_no_pickle",
        "new_level_policies",
        "unsupported_mode",
        "evaluate_path_equals_score_path",
        "output_space_and_units",
        "outputs_match_manifest",
        "seed_determinism",
    } <= ran


# ------------------------------------------------------------------------------- real data


@pytest.mark.skipif(SACHS is None, reason="pymc-challenges data not present")
def test_from_challenges_reads_the_cd3_cd28_cells() -> None:
    assert SACHS is not None
    ds = from_challenges(SACHS)
    assert validate_dataset(GGM_SCHEMA, ds) == []
    assert dict(ds.sizes) == {"cell": CHALLENGES_REFERENCE["cells"], "protein": 11, "protein_": 11}
    assert list(ds["protein"].values) == list(PROTEINS)
    adj = ds["consensus"].values
    assert (adj == adj.T).all() and int(np.triu(adj, 1).sum()) == 20
    assert adj[PROTEINS.index("erk"), PROTEINS.index("akt")] == 1
    pooled = from_challenges(SACHS, observational=False)
    assert pooled.sizes["cell"] == 7466
    consensus = SACHS.parent / CONSENSUS_FILE
    np.testing.assert_array_equal(read_consensus(consensus), adj)


def test_from_challenges_refuses_a_file_without_the_columns(tmp_path: object) -> None:
    from pathlib import Path

    path = Path(str(tmp_path)) / "sachs.txt"
    path.write_text("raf\tmek\n1\t2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing columns"):
        from_challenges(path)


@pytest.mark.slow
@pytest.mark.skipif(SACHS is None, reason="pymc-challenges data not present")
def test_dense_fit_on_the_sachs_cells_matches_the_notebook(registry: Registry) -> None:
    assert SACHS is not None
    ds = from_challenges(SACHS)
    spec = (
        SACHS_SPEC_YAML.replace(
            "  split: {kind: leave_group_out, dim: cell, n: 170, seed: 2005}\n", ""
        )
        .replace("mode: evaluate_then_refit", "mode: train_only")
        .replace("tune: 1000", "tune: 500")
        .replace("draws: 1000", "draws: 500")
        .replace("chains: 4", "chains: 2")
        .replace("engine: nutpie", "engine: pymc")
    )
    b = fit_spec(spec, ds, registry=registry, raw_schema=GGM_SCHEMA)
    prob = b.model.groups["posterior"]["edge_prob"].mean(("chain", "draw")).values
    v = list(PROTEINS)

    def p(a: str, c: str) -> float:
        return float(prob[v.index(a), v.index(c)])

    for a, c in CHALLENGES_REFERENCE["certain_edges"]:
        assert p(a, c) > 0.97, (a, c, p(a, c))
    for (a, c), want in CHALLENGES_REFERENCE["edge_prob"].items():
        assert abs(p(a, c) - want) < 0.2, (a, c, p(a, c), want)
    learned = b.model.learned
    assert abs(learned["pcor_sd_mean"] - CHALLENGES_REFERENCE["pcor_sd_mean"]) < 0.01
    assert abs(learned["edges"] - CHALLENGES_REFERENCE["edges"]) <= 1
    iu = np.triu_indices(11, 1)
    graph = prob[iu] > 0.5
    assert int((graph & (ds["consensus"].values[iu] == 1)).sum()) >= 6
