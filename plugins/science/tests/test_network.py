"""``sci.network_latent_space`` (E21): dyads of a graph, the three kinds, Procrustes-aligned
positions, pointwise log-likelihood per dyad, scores and simulated graphs, typed refusals,
recovery on a synthetic graph and the real Lazega network."""

from __future__ import annotations

import os

import numpy as np
import pymc as pm
import pytest
import xarray as xr
from mapi_plugin_science.challenges import challenges_file
from mapi_plugin_science.network import (
    ADJACENCY,
    DYAD_DIM,
    DYAD_I,
    DYAD_J,
    GRAPH_STATS,
    GROUP,
    POSITION_REF,
    NetworkLatentSpaceModel,
    NetworkLatentSpaceSettings,
    align_to,
    dyad_eta,
    dyads,
    graph_stats,
    procrustes,
    simulate_graph,
)
from mapi_plugin_science.network_data import (
    CHALLENGES_FILE,
    LAZEGA_SPEC_YAML,
    NETWORK_SCHEMA,
    NETWORK_SPEC_YAML,
    NETWORK_TRUTH,
    challenges_reference,
    from_challenges,
    network_dataset,
    network_latent,
)
from mapi_pymc.context import BuildContext
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import validate_dataset
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import load_model_spec, load_score_request, validate_spec
from mapi_spec.errors import ErrorCode, MapiError
from scipy.special import expit

LAZEGA = challenges_file(CHALLENGES_FILE)
HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://sci/network\n"
CANON = {"lawyer": "node", "lawyer_": "node_", "covariate": "cov", "cowork": "tie",
         "homophily": "dyad_cov", "office": "node_group"}  # fmt: skip


def _req(body: str) -> object:
    return load_score_request(HEADER + body)


def _canon(ds: xr.Dataset) -> xr.Dataset:
    return ds.rename(CANON)


def _codes(fn: object) -> ErrorCode:
    with pytest.raises(MapiError) as err:
        fn()  # type: ignore[operator]
    return err.value.code


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(NetworkLatentSpaceModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def graph() -> xr.Dataset:
    return network_dataset(seed=0)


@pytest.fixture(scope="module")
def bundle(registry: Registry, graph: xr.Dataset) -> Bundle:
    return fit_spec(NETWORK_SPEC_YAML, graph, registry=registry, raw_schema=NETWORK_SCHEMA)


# ------------------------------------------------------------------------------- manifest


def test_manifest_declares_grids_kinds_and_plots() -> None:
    man = NetworkLatentSpaceModel.manifest()
    assert man.id == "sci.network_latent_space"
    assert man.capabilities.score_modes == frozenset({"predict", "simulate", "evaluate"})
    assert man.capabilities.loo_valid
    for name in ("tie_prob", "tie"):
        grid = man.outputs[name].grid
        assert grid is not None and (grid.y, grid.x) == ("node", "node_")
    assert man.outputs["tie"].units_of == "tie"
    assert man.outputs["tie_prob"].units == "probability"
    assert man.outputs["position"].dims == ["node", "latent_dim"]
    assert man.outputs["graph_stat"].dims == ["statistic"]
    full = man.schema_template.resolve({"covariates": True, "node_groups": True})
    assert set(full.vars) == {"tie", "dyad_cov", "node_group"}
    assert full.vars["tie"].missing == "allow"
    bare = man.schema_template.resolve({"covariates": False})
    assert set(bare.vars) == {"tie"} and "cov" not in bare.dims
    assert "net.graph" in man.plots and "sci.field_map" in man.plots
    schema = man.settings_schema["properties"]["kind"]
    assert set(schema["enum"]) == {"covariates", "sociality", "latent_space"}


def test_static_validation_resolves_both_specs(registry: Registry) -> None:
    for text in (NETWORK_SPEC_YAML, LAZEGA_SPEC_YAML):
        res = validate_spec(load_model_spec(text), registry.catalog(), NETWORK_SCHEMA)
        assert res.ok, [(i.pointer, i.message) for i in res.errors]


def test_synthetic_graph_follows_the_schema(graph: xr.Dataset) -> None:
    assert validate_dataset(NETWORK_SCHEMA, graph) == []
    a = graph["cowork"].values
    assert (a == a.T).all() and np.diag(a).sum() == 0 and 0.05 < a.mean() < 0.5


# ------------------------------------------------------------------------------- numerics


def test_dyads_skip_missing_cells_and_refuse_bad_values() -> None:
    a = np.array([[0, 1, 0, 1], [1, 0, 0, 0], [0, 0, 0, 1], [1, 0, 1, 0]], dtype=float)
    i, j, y = dyads(a)
    assert list(zip(i.tolist(), j.tolist(), strict=True)) == [
        (0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)
    ]  # fmt: skip
    np.testing.assert_array_equal(y, [1, 0, 1, 0, 0, 1])
    b = a.copy()
    b[2, 0] = np.nan  # one missing cell holds out the whole pair
    i, j, _ = dyads(b)
    assert (0, 2) not in set(zip(i.tolist(), j.tolist(), strict=True)) and i.size == 5
    asym = a.copy()
    asym[1, 0] = 0
    assert _codes(lambda: dyads(asym)) is ErrorCode.SCHEMA_INVALID
    bad = a.copy()
    bad[0, 1] = bad[1, 0] = 2
    assert _codes(lambda: dyads(bad)) is ErrorCode.SCHEMA_INVALID


@pytest.mark.parametrize(
    ("kind", "covariates"),
    [("covariates", True), ("covariates", False), ("sociality", True), ("latent_space", True),
     ("latent_space", False)],
)  # fmt: skip
def test_pymc_graph_matches_the_numpy_twin(kind: str, covariates: bool) -> None:
    """The Bernoulli log density of each dyad in build() equals the NumPy log odds."""
    ds = _canon(network_dataset(n_nodes=12, seed=3))
    s = NetworkLatentSpaceSettings(kind=kind, covariates=covariates)  # type: ignore[arg-type]
    model = NetworkLatentSpaceModel().build(ds, s, BuildContext(phase="fit", ds=ds))
    rng = np.random.default_rng(0)
    point = model.initial_point()
    point = {k: v + rng.normal(0, 0.5, np.shape(v)) for k, v in point.items()}
    (lp,) = model.replace_rvs_by_values([model.logp(vars=[model["tie"]], sum=False)[0]])
    got = model.compile_fn(lp, inputs=model.value_vars, on_unused_input="ignore")(point)
    i, j, y = dyads(ds["tie"].values)
    beta = point["beta"][None] if covariates else None
    x = ds["dyad_cov"].values[i, j] if covariates else None
    soc = z = None
    if kind != "covariates":
        soc = (np.exp(point["sigma_s_log__"]) * point["s_raw"])[None]
    if kind == "latent_space":
        z = (np.exp(point["sigma_z_log__"]) * point["z_raw"])[None]
    eta = dyad_eta(np.atleast_1d(point["alpha"]), i, j, x=x, beta=beta, sociality=soc, z=z)[0]
    want = np.where(y == 1, np.log(expit(eta)), np.log(expit(-eta)))
    np.testing.assert_allclose(got, want, rtol=1e-8, atol=1e-10)
    assert model["tie"].ndim == 1 and len(model.coords[DYAD_DIM]) == i.size
    assert ("z_raw" in model.named_vars) == (kind == "latent_space")
    assert ("beta" in model.named_vars) == covariates


def test_procrustes_removes_rotation_reflection_and_translation() -> None:
    rng = np.random.default_rng(1)
    base = rng.normal(size=(15, 2))
    draws = []
    for k in range(40):
        th = rng.uniform(0, 2 * np.pi)
        rot = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
        if k % 2:
            rot = rot @ np.diag([1.0, -1.0])  # a mirror image
        draws.append(base @ rot + rng.normal(size=2) * 3 + rng.normal(0, 0.01, (15, 2)))
    z = np.stack(draws)
    aligned, ref = procrustes(z, 10)
    assert aligned.std(0).max() < 0.05  # every draw lands on the same configuration
    np.testing.assert_allclose(align_to(z, ref), aligned, atol=1e-12)
    d_raw = np.linalg.norm(z[:, :, None] - z[:, None], axis=-1)
    d_al = np.linalg.norm(aligned[:, :, None] - aligned[:, None], axis=-1)
    np.testing.assert_allclose(d_al, d_raw, atol=1e-10)  # distances are untouched


def test_graph_stats_of_a_known_graph() -> None:
    m = np.zeros((5, 5))
    for a, b in [(0, 1), (1, 2), (0, 2), (2, 3)]:  # a triangle with a tail, node 4 isolated
        m[a, b] = m[b, a] = 1
    st = dict(zip(GRAPH_STATS, graph_stats(m), strict=True))
    assert st["triangles"] == 1 and st["isolates"] == 1 and st["max_degree"] == 3
    assert st["transitivity"] == pytest.approx(3 / 5)  # 3 closed of 5 two-paths
    assert st["esp0"] == 1  # the tail (2, 3) has no shared neighbour
    assert st["density"] == pytest.approx(4 / 10)
    assert st["unreachable"] == pytest.approx(4 / 10)
    u = np.random.default_rng(0).uniform(size=(5, 5))
    g = simulate_graph(np.full((5, 5), 0.5), u)
    assert (g == g.T).all() and np.diag(g).sum() == 0


def test_build_refuses_bad_data() -> None:
    ds = _canon(network_dataset(n_nodes=8, seed=1))
    s = NetworkLatentSpaceSettings()
    model = NetworkLatentSpaceModel()

    def build(d: xr.Dataset, st: NetworkLatentSpaceSettings = s) -> object:
        return model.build(d, st, BuildContext(phase="fit", ds=d))

    assert _codes(lambda: build(ds.isel(node=[0, 1], node_=[0, 1]))) is ErrorCode.SCHEMA_INVALID
    shifted = ds.assign_coords(node_=ds["node_"].values + 100)
    assert _codes(lambda: build(shifted)) is ErrorCode.SCHEMA_INVALID
    assert _codes(lambda: build(ds.drop_vars("dyad_cov"))) is ErrorCode.SCHEMA_INVALID
    empty = ds.assign(tie=ds["tie"] * 0)
    assert _codes(lambda: build(empty)) is ErrorCode.SCHEMA_INVALID
    holes = ds.assign(dyad_cov=ds["dyad_cov"].where(ds["node"] != 1))
    assert _codes(lambda: build(holes)) is ErrorCode.SCHEMA_INVALID
    # without covariates the pair covariates are not needed
    bare = NetworkLatentSpaceSettings(covariates=False)
    assert isinstance(build(ds.drop_vars("dyad_cov"), bare), pm.Model)


# ------------------------------------------------------------------------------- fit


@pytest.mark.slow
def test_fit_keeps_dyads_positions_and_loglik(bundle: Bundle, graph: xr.Dataset) -> None:
    groups = bundle.model.groups
    n = graph.sizes["lawyer"]
    obs = groups["observed_data"]["tie"]
    assert obs.dims == (DYAD_DIM,)
    const = groups["constant_data"]
    i, j = const[DYAD_I].values, const[DYAD_J].values
    assert (i < j).all() and obs.size == n * (n - 1) // 2 == i.size
    np.testing.assert_array_equal(obs.values, graph["cowork"].values[i, j])
    assert const[ADJACENCY].dims == ("node", "node_") and GROUP in const
    ll = groups["log_likelihood"]["tie"]
    assert ll.dims == ("chain", "draw", DYAD_DIM) and ll.sizes[DYAD_DIM] == obs.size
    assert float(ll.max()) <= 0.0
    detail = bundle.metrics.elpd["elpd_loo"]
    assert detail.n == obs.size and detail.pointwise is not None
    assert len(detail.pointwise) == obs.size
    post = groups["posterior"]
    pos = post["position"]
    assert pos.dims == ("chain", "draw", "node", "latent_dim")
    np.testing.assert_allclose(pos.mean("node").values, 0.0, atol=1e-10)  # centred draws
    z = (post["z_raw"] * post["sigma_z"]).transpose("chain", "draw", "node", "z_axis").values
    s, d = z.shape[0] * z.shape[1], z.shape[-1]
    again = align_to(z.reshape(s, n, d), const[POSITION_REF].values)
    np.testing.assert_allclose(again, pos.values.reshape(s, n, d), atol=1e-10)
    diag_vars = {dg.var for dg in bundle.diagnostics if dg.code == "RHAT"}
    assert "z_raw" not in diag_vars  # convergence is judged on identified quantities
    assert bundle.model.learned["kind"] == "latent_space"
    assert bundle.evaluation is not None
    assert bundle.evaluation.model.learned["n_dyads_held_out"] > 0


@pytest.mark.slow
def test_fit_recovers_the_truth(bundle: Bundle) -> None:
    post = bundle.model.posterior
    for name, true in NETWORK_TRUTH["beta"].items():
        draws = post["beta"].sel(cov=name).values.ravel()
        lo, hi = np.quantile(draws, [0.01, 0.99])
        assert lo < true < hi, (name, lo, hi)
    lo, hi = np.quantile(post["alpha"].values, [0.01, 0.99])
    assert lo < NETWORK_TRUTH["alpha"] < hi, (lo, hi)
    # the latent map: posterior mean distances follow the true distances
    z = network_latent(seed=0)["z"]
    i, j = np.triu_indices(z.shape[0], 1)
    true_d = np.linalg.norm(z[i] - z[j], axis=-1)
    m = post["position"].mean(("chain", "draw")).values
    est_d = np.linalg.norm(m[i] - m[j], axis=-1)
    assert np.corrcoef(true_d, est_d)[0, 1] > 0.3
    soc = network_latent(seed=0)["sociality"]
    assert np.corrcoef(soc, post["sociality"].mean(("chain", "draw")).values)[0, 1] > 0.3


# ------------------------------------------------------------------------------- score


@pytest.mark.slow
def test_predict_gives_symmetric_tie_probabilities(
    bundle: Bundle, graph: xr.Dataset, registry: Registry
) -> None:
    out = score_bundle(
        bundle, _req("mode: predict\n"), graph.drop_vars("cowork"), registry=registry,
        return_full=True,
    )  # fmt: skip
    pred = out["predictions"].to_dataset()
    p = pred["tie_prob"].transpose("chain", "draw", "lawyer", "lawyer_").values
    assert ((p >= 0) & (p <= 1)).all()
    np.testing.assert_allclose(p, np.swapaxes(p, -1, -2), atol=1e-12)
    assert np.diagonal(p, axis1=-2, axis2=-1).max() == 0.0
    np.testing.assert_allclose(pred["cowork"].values, pred["tie_prob"].values)
    # one pair by hand from the posterior
    post = bundle.model.posterior
    a, b = 2, 7
    z = (post["z_raw"] * post["sigma_z"]).isel(node=[a, b])
    eta = (
        post["alpha"]
        + (post["beta"] * graph["homophily"].isel(lawyer=a, lawyer_=b).values).sum("cov")
        + post["sociality"].isel(node=a)
        + post["sociality"].isel(node=b)
        - np.sqrt(((z.isel(node=0) - z.isel(node=1)) ** 2).sum("z_axis") + 1e-6)
    )
    np.testing.assert_allclose(p[:, :, a, b], expit(eta.values), rtol=1e-10)
    deg = pred["degree"].mean(("chain", "draw")).values
    np.testing.assert_allclose(deg, p.mean((0, 1)).sum(-1), rtol=1e-10)
    np.testing.assert_allclose(
        pred["position"].values,
        post["position"].values,
        atol=0,  # the aligned draws
    )
    assert "graph_stat" not in pred  # only for simulated graphs


@pytest.mark.slow
def test_simulate_draws_graphs_and_their_statistics(
    bundle: Bundle, graph: xr.Dataset, registry: Registry
) -> None:
    out = score_bundle(
        bundle, _req("mode: simulate\noutput: {kind: predictive}\n"), graph.drop_vars("cowork"),
        registry=registry, return_full=True,
    )  # fmt: skip
    pred = out["predictions"].to_dataset()
    g = pred["cowork"].transpose("chain", "draw", "lawyer", "lawyer_").values
    assert set(np.unique(g)) <= {0.0, 1.0}
    np.testing.assert_array_equal(g, np.swapaxes(g, -1, -2))
    st = pred["graph_stat"]
    assert list(st["statistic"].values) == list(GRAPH_STATS)
    # the simulated graphs look like the observed one (posterior predictive check)
    obs = dict(zip(GRAPH_STATS, graph_stats(graph["cowork"].values), strict=True))
    dens = st.sel(statistic="density").values
    assert np.quantile(dens, 0.01) <= obs["density"] <= np.quantile(dens, 0.99)
    np.testing.assert_allclose(
        pred["degree"].values, g.sum(-1), atol=0
    )  # degree of each simulated graph
    # a subset of the nodes gets the same ties (common random numbers on the pair)
    sub = graph.drop_vars("cowork").isel(lawyer=slice(0, 10), lawyer_=slice(0, 10))
    part = score_bundle(
        bundle, _req("mode: simulate\noutput: {kind: predictive}\n"), sub, registry=registry,
        return_full=True,
    )["predictions"].to_dataset()  # fmt: skip
    np.testing.assert_array_equal(
        part["cowork"].transpose("chain", "draw", "lawyer", "lawyer_").values, g[:, :, :10, :10]
    )


@pytest.mark.slow
def test_held_out_pairs_are_predicted(graph: xr.Dataset, registry: Registry) -> None:
    holes = graph.copy()
    a = holes["cowork"].values.copy()
    a[0, 1:6] = np.nan  # the ties of node 1 with nodes 2-6 are unknown
    holes["cowork"] = (("lawyer", "lawyer_"), a)
    spec = (
        NETWORK_SPEC_YAML.replace(
            "  split: {kind: mask_block, dims: [node, node_], fraction: 0.1, seed: 4}\n", ""
        )
        .replace("mode: evaluate_then_refit", "mode: train_only")
        .replace("{kind: latent_space, node_groups: true}", "{kind: sociality}")
    )
    b = fit_spec(spec, holes, registry=registry, raw_schema=NETWORK_SCHEMA)
    assert b.model.learned["n_dyads_held_out"] == 5
    assert b.model.groups["observed_data"].sizes[DYAD_DIM] == 30 * 29 // 2 - 5
    assert "position" not in b.model.posterior and "sociality" in b.model.posterior
    out = score_bundle(
        b, _req("mode: predict\noutput: {summary: [mean]}\n"), holes.drop_vars("cowork"),
        registry=registry, return_full=True,
    )  # fmt: skip
    p = out["summary"].to_dataset()["tie_prob"].sel(stat="mean")
    held = p.isel(lawyer=0, lawyer_=slice(1, 6)).values
    assert np.isfinite(held).all() and ((held > 0) & (held < 1)).all()
    assert "position" not in out["predictions"].to_dataset()


@pytest.mark.slow
def test_kinds_share_the_dyads_for_elpd(graph: xr.Dataset, registry: Registry) -> None:
    """A workflow ladder compares kinds by elpd_loo: same dyads in the same order."""
    spec = (
        NETWORK_SPEC_YAML.replace("{kind: latent_space, node_groups: true}", "{kind: covariates}")
        .replace("mode: evaluate_then_refit", "mode: train_only")
        .replace("  split: {kind: mask_block, dims: [node, node_], fraction: 0.1, seed: 4}\n", "")
    )
    b = fit_spec(spec, graph, registry=registry, raw_schema=NETWORK_SCHEMA)
    ll = b.model.groups["log_likelihood"]["tie"]
    assert ll.sizes[DYAD_DIM] == 30 * 29 // 2
    assert b.metrics.values["elpd_loo"] is not None
    assert "sociality" not in b.model.posterior and "z_raw" not in b.model.posterior


@pytest.mark.slow
def test_typed_refusals_at_score(bundle: Bundle, graph: xr.Dataset, registry: Registry) -> None:
    def code(body: str, payload: xr.Dataset | None) -> ErrorCode:
        return _codes(lambda: score_bundle(bundle, _req(body), payload, registry=registry))

    payload = graph.drop_vars("cowork")
    assert code("mode: forecast\nhorizon: {dim: node, periods: 2}\n", None) in (
        ErrorCode.MODE_NOT_SUPPORTED,
        ErrorCode.CAPABILITY_UNSUPPORTED,
    )
    assert (
        code("mode: counterfactual\ninterventions: [{var: homophily, multiply: 2}]\n", payload)
        is ErrorCode.MODE_NOT_SUPPORTED
    )
    assert code("mode: predict\n", payload.drop_vars("homophily")) in (
        ErrorCode.MISSING_FUTURE_COVARIATE,
        ErrorCode.SCHEMA_INVALID,
    )
    one = payload.isel(covariate=[1])  # a pair needs every covariate of the fit
    assert code("mode: predict\n", one) is ErrorCode.MISSING_FUTURE_COVARIATE
    stranger = payload.assign_coords(
        lawyer=payload["lawyer"].values + 1000, lawyer_=payload["lawyer_"].values + 1000
    )
    assert code("mode: predict\n", stranger) is ErrorCode.UNSEEN_LEVELS


CONTRACT_CHECKS = (
    "manifest",
    "about",
    "fit",
    "bundle_roundtrip_no_pickle",
    "shuffled_scoring",
    "new_level_policies",
    "unsupported_mode",
    "evaluate_path_equals_score_path",
    "output_space_and_units",
    "outputs_match_manifest",
    "representation_aggregation",
    "seed_determinism",
    "plots_sanitize",
)
"""Every check but ``subset_scoring``: it scores one level of the categorical ``covariate``
dim and expects outputs on that dim, but a pair needs all its covariates, so the model
refuses a subset of them (see ``test_typed_refusals_at_score``)."""


@pytest.mark.mapi_contract
@pytest.mark.slow
def test_network_passes_the_contract_kit(registry: Registry, graph: xr.Dataset) -> None:
    fixture = ModelFixture(
        spec=NETWORK_SPEC_YAML, data=graph, raw_schema=NETWORK_SCHEMA, registry=registry
    )
    report = check_model(
        NetworkLatentSpaceModel, fixture, checks=CONTRACT_CHECKS, raise_on_failure=False
    )
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "fit",
        "bundle_roundtrip_no_pickle",
        "shuffled_scoring",
        "new_level_policies",
        "unsupported_mode",
        "outputs_match_manifest",
        "seed_determinism",
        "plots_sanitize",
    } <= ran


# ------------------------------------------------------------------------------- real data


@pytest.mark.skipif(LAZEGA is None, reason="pymc-challenges data not present")
def test_from_challenges_reads_the_lazega_network() -> None:
    assert LAZEGA is not None
    ds = from_challenges(LAZEGA)
    assert validate_dataset(NETWORK_SCHEMA, ds) == []
    assert dict(ds.sizes) == {"lawyer": 71, "lawyer_": 71, "covariate": 6}
    a = ds["cowork"].values
    assert int(a.sum()) // 2 == 378 and (a == a.T).all()
    i, j, y = dyads(a)
    assert y.size == 2485 and y.mean() == pytest.approx(0.152, abs=1e-3)
    x = ds["homophily"].values[i, j]
    # the notebook's tie rates when same vs different office and practice
    office = list(ds["covariate"].values).index("same_office")
    assert y[x[:, office] == 1].mean() == pytest.approx(0.233, abs=1e-3)
    assert y[x[:, office] == 0].mean() == pytest.approx(0.063, abs=1e-3)
    st = dict(zip(GRAPH_STATS, graph_stats(a), strict=True))
    assert st["transitivity"] == pytest.approx(0.307, abs=1e-3)
    assert st["triangles"] == 493 and st["isolates"] == 1
    assert st["mean_geodesic"] == pytest.approx(2.104, abs=1e-3)
    assert set(np.unique(ds["office"].values)) == {1, 2, 3}


@pytest.mark.skipif(LAZEGA is None, reason="pymc-challenges data not present")
def test_from_challenges_refuses_a_bad_file(tmp_path: object) -> None:
    from pathlib import Path

    assert LAZEGA is not None
    d = Path(str(tmp_path))
    (d / "lazega_cowork.txt").write_text("0 1\n1 0\n")
    (d / "lazega_attributes.txt").write_text(Path(LAZEGA).with_name(
        "lazega_attributes.txt").read_text())  # fmt: skip
    with pytest.raises(ValueError, match="71 x 71"):
        from_challenges(d / "lazega_cowork.txt")


@pytest.mark.slow
@pytest.mark.skipif(LAZEGA is None, reason="pymc-challenges data not present")
def test_real_data_sociality_fit_matches_the_notebook(registry: Registry) -> None:
    """Model (b) on the 71 lawyers: the homophily effects and sigma_s of the E21 notebook."""
    assert LAZEGA is not None
    ds = from_challenges(LAZEGA)
    draws = int(os.environ.get("MAPI_E21_DRAWS", "300"))
    spec = LAZEGA_SPEC_YAML.replace(
        "{kind: latent_space, node_groups: true}", "{kind: sociality, node_groups: true}"
    ).replace(
        "tune: 1000, draws: 1000, chains: 4, engine: nutpie",
        f"tune: 500, draws: {draws}, chains: 2, engine: pymc",
    )
    b = fit_spec(spec, ds, registry=registry, raw_schema=NETWORK_SCHEMA)
    ref = challenges_reference()["sociality"]
    post = b.model.posterior
    for name in ("same_office", "same_practice", "same_status"):
        got = float(post["beta"].sel(cov=name).mean())
        assert abs(got - ref[name]) < 0.25, (name, got, ref[name])
    assert abs(float(post["sigma_s"].mean()) - ref["sigma_s"]) < 0.15
    assert abs(float(post["alpha"].mean()) - ref["alpha"]) < 0.6
    elpd = b.metrics.values["elpd_loo"]
    assert elpd is not None and abs(elpd - ref["elpd_loo"]) < 15
