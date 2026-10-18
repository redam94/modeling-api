"""``sci.phylo_regression`` (E23): the tree as node arrays, the four residual kinds, the
leave-one-out conditionals, prediction from relatives, recovery on synthetic data, the R and
Newick readers and the real primates."""

from __future__ import annotations

import gzip
import struct
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_science.challenges import challenges_file
from mapi_plugin_science.phylo import (
    PhyloRegressionModel,
    PhyloRegressionSettings,
    PhyloTree,
    conditional_loo,
    conditional_predict,
    covariance,
    parse_newick,
    to_newick,
)
from mapi_plugin_science.phylo_data import (
    CHALLENGES_FILE,
    CHALLENGES_TREE,
    PHYLO_SCHEMA,
    PHYLO_SPEC_YAML,
    PHYLO_TRUTH,
    PRIMATES_SPEC_YAML,
    challenges_reference,
    from_challenges,
    phylo_dataset,
    random_tree,
    read_r_object,
    read_tree,
)
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import validate_dataset
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import load_model_spec, load_score_request, validate_spec
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import ValidationError
from scipy import stats

CSV = challenges_file(CHALLENGES_FILE)
TREE = challenges_file(CHALLENGES_TREE)
HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://sci/phylo\n"


def _req(body: str) -> object:
    return load_score_request(HEADER + body)


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(PhyloRegressionModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def species() -> xr.Dataset:
    return phylo_dataset(n_species=60, missing=6, seed=0)


@pytest.fixture(scope="module")
def bundle(registry: Registry, species: xr.Dataset) -> Bundle:
    return fit_spec(PHYLO_SPEC_YAML, species, registry=registry, raw_schema=PHYLO_SCHEMA)


def _canonical(ds: xr.Dataset) -> xr.Dataset:
    return ds.rename({"brain": "trait"})


# ------------------------------------------------------------------------------- manifest


def test_manifest_declares_tips_slopes_and_the_tree_plot() -> None:
    man = PhyloRegressionModel.manifest()
    assert man.id == "sci.phylo_regression"
    assert man.capabilities.score_modes == frozenset({"predict", "simulate", "evaluate"})
    assert man.outputs["trait"].dims == ["species"] and man.outputs["trait"].units_of == "trait"
    assert man.outputs["slope"].dims == ["predictor"]
    assert all(o.grid is None for o in man.outputs.values())
    schema = man.schema_template.resolve({"predictors": ["mass", "range"]})
    assert {"trait", "mass", "range", "parent", "branch_length", "is_tip", "tip_label"} == set(
        schema.vars
    )
    assert schema.vars["trait"].missing == "allow"
    assert schema.vars["tip_label"].dtype == "str"
    assert schema.dims["species"].policy.value == "fixed"
    assert "sci.phylogeny" in man.plots


def test_static_validation_resolves_both_specs(registry: Registry) -> None:
    for text in (PHYLO_SPEC_YAML, PRIMATES_SPEC_YAML):
        res = validate_spec(load_model_spec(text), registry.catalog(), PHYLO_SCHEMA)
        assert res.ok, [(i.pointer, i.message) for i in res.errors]


def test_settings_refuse_reserved_and_duplicate_predictors() -> None:
    with pytest.raises(ValidationError):
        PhyloRegressionSettings(predictors=["parent"])
    with pytest.raises(ValidationError):
        PhyloRegressionSettings(predictors=["body", "body"])
    with pytest.raises(ValidationError):
        PhyloRegressionSettings(kind="pagel")  # type: ignore[arg-type]


# ------------------------------------------------------------------------------- the tree


def test_shared_length_is_the_depth_of_the_common_ancestor() -> None:
    #        root(6)
    #       /       \
    #    5 (1.0)    4 (tip d, 3.0)
    #    /     \
    # 0 a 2.0  3 (0.5)
    #          /   \
    #       1 b 1.5  2 c 1.5
    tree = PhyloTree.build(
        [5, 3, 3, 5, 6, 6, -1],
        [2.0, 1.5, 1.5, 0.5, 3.0, 1.0, 0.0],
        labels=["a", "b", "c", "", "d", "", ""],
    )
    tips = tree.tip_positions(["a", "b", "c", "d"])
    c = tree.shared_length(tips)
    want = np.array(
        [[3.0, 1.0, 1.0, 0.0], [1.0, 3.0, 1.5, 0.0], [1.0, 1.5, 3.0, 0.0], [0, 0, 0, 3.0]]
    )
    np.testing.assert_allclose(c, want)
    assert tree.tip_depth() == pytest.approx(3.0)
    # Newick both ways keeps the shared lengths
    back = parse_newick(to_newick(tree))
    np.testing.assert_allclose(back.shared_length(back.tip_positions(["a", "b", "c", "d"])), want)
    # pruning a tip keeps the others' shared lengths
    pruned = tree.prune(["a", "c", "d"])
    np.testing.assert_allclose(
        pruned.shared_length(pruned.tip_positions(["a", "c", "d"])), want[np.ix_([0, 2, 3], [0, 2, 3])]
    )


def test_invalid_trees_are_refused() -> None:
    with pytest.raises(ValueError, match="one root"):
        PhyloTree.build([-1, -1, 0], [0, 1, 1])
    with pytest.raises(ValueError, match="cycle"):
        PhyloTree.build([-1, 2, 1, 0], [0, 1, 1, 1])
    with pytest.raises(ValueError, match="negative"):
        PhyloTree.build([-1, 0, 0], [0, -1, 1])
    with pytest.raises(ValueError, match="unique"):
        PhyloTree.build([-1, 0, 0], [0, 1, 1], labels=["", "x", "x"])


def test_random_tree_is_ultrametric_and_binary() -> None:
    tree = random_tree(25, depth=10.0, seed=3)
    depth = tree.depth()[tree.tips]
    np.testing.assert_allclose(depth, 10.0)
    kids = tree.children()
    assert all(len(k) in (0, 2) for k in kids)
    assert tree.tips.size == 25 and tree.n == 49


# ------------------------------------------------------------------------------- numerics


def test_covariance_kinds_and_the_conditionals() -> None:
    tree = random_tree(8, seed=1)
    c = tree.shared_length(tree.tips)
    t = tree.tip_depth()
    d = np.diag(c)
    r, dn = c / t, (d[:, None] + d[None] - 2 * c) / (2 * t)
    p = {"sigma": np.array([0.5]), "lam": np.array([0.7]), "eta2": np.array([2.0]),
         "rho": np.array([0.3])}  # fmt: skip
    np.testing.assert_allclose(covariance("iid", r, dn, p)[0], 0.25 * np.eye(8))
    np.testing.assert_allclose(covariance("brownian", r, dn, p)[0], 0.25 * r)
    lam = covariance("lambda", r, dn, p)[0]
    np.testing.assert_allclose(lam, 0.25 * (0.7 * r + 0.3 * np.eye(8)))
    ou = covariance("ou", r, dn, p)[0]
    np.testing.assert_allclose(ou, 2.0 * np.exp(-0.3 * dn) + 1e-6 * np.eye(8))
    # the LOO conditional equals conditioning on the other species directly
    rng = np.random.default_rng(0)
    y, mu = rng.normal(size=8), rng.normal(size=(1, 8))
    mean, var, ll = conditional_loo(y, mu, lam[None])
    for i in range(8):
        rest = np.delete(np.arange(8), i)
        m1, v1 = conditional_predict(
            y[rest], mu[:, rest], mu[:, [i]], lam[None][:, rest][:, :, rest],
            lam[None][:, [i]][:, :, rest], lam[None][:, [i], [i]],
        )  # fmt: skip
        assert mean[0, i] == pytest.approx(m1[0, 0])
        assert var[0, i] == pytest.approx(v1[0, 0])
        assert ll[0, i] == pytest.approx(stats.norm.logpdf(y[i], m1[0, 0], np.sqrt(v1[0, 0])))
    # iid: the conditional is the marginal
    mean, var, _ = conditional_loo(y, mu, covariance("iid", r, dn, p))
    np.testing.assert_allclose(mean[0], mu[0])
    np.testing.assert_allclose(var[0], 0.25)


def test_the_pymc_density_matches_the_numpy_twin(species: xr.Dataset) -> None:
    ds = _canonical(species)
    for kind in ("iid", "brownian", "lambda", "ou"):
        s = PhyloRegressionSettings(kind=kind, standardise=False)
        model_ = PhyloRegressionModel()
        d, _ = model_.design(ds, s)
        model = model_.build_model(d, s)
        point = model.initial_point()
        point = {k: v + 0.1 for k, v in point.items()}
        logp = model.compile_logp(vars=[model["trait"]])(point)
        vals = {}
        for v in model.free_RVs:
            tr = model.rvs_to_transforms.get(v)
            raw = point[model.rvs_to_values[v].name]
            vals[v.name] = np.atleast_1d(
                tr.backward(raw, *v.owner.inputs).eval() if tr is not None else raw
            )
        if kind == "ou":
            vals["eta2"] = vals["rate"] / vals["rho"]
        o = d.observed
        mu = vals["intercept"][0] + d.x[o] @ vals["slope"]
        k = covariance(kind, d.r[np.ix_(o, o)], d.dn[np.ix_(o, o)], vals)[0]
        want = stats.multivariate_normal(mu, k).logpdf(d.y[o])
        assert float(logp) == pytest.approx(want, rel=1e-6), kind


# ------------------------------------------------------------------------------- typed errors


def test_typed_refusals(species: xr.Dataset) -> None:
    model = PhyloRegressionModel()
    s = PhyloRegressionSettings()
    ds = _canonical(species)
    stranger = ds.assign_coords(species=["nobody", *ds["species"].values[1:]])
    with pytest.raises(MapiError) as err:
        model.design(stranger, s)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and "nobody" in str(err.value)
    neg = ds.assign(body=ds["body"].where(ds["species"] != ds["species"].values[0], -1.0))
    with pytest.raises(MapiError) as err:
        model.design(neg, s)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and "log_predictors" in str(err.value)
    gap = ds.assign(body=ds["body"].where(ds["species"] != ds["species"].values[0]))
    with pytest.raises(MapiError) as err:
        model.design(gap, s)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and "missing" in str(err.value)
    forest = ds.assign(parent=ds["parent"].where(ds["parent"] >= 0, 0))
    with pytest.raises(MapiError) as err:
        model.design(forest, s)
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    few = ds.assign(trait=ds["trait"].where(np.arange(ds.sizes["species"]) < 2))
    with pytest.raises(MapiError) as err:
        model.design(few, s)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and "at least 3" in str(err.value)


# ------------------------------------------------------------------------------- fit / score


def test_fit_recovers_the_truth_and_stores_pointwise_loo(
    bundle: Bundle, species: xr.Dataset
) -> None:
    groups = bundle.model.groups
    post = groups["posterior"]
    slope = post["slope"].stack(s=("chain", "draw"))
    for name, value in PHYLO_TRUTH["slope"].items():
        lo, hi = np.quantile(slope.sel(predictor=name).values, [0.01, 0.99])
        assert lo < value < hi, (name, lo, hi)
    lo, hi = np.quantile(post["intercept"].values, [0.01, 0.99])
    assert lo < PHYLO_TRUTH["intercept"] < hi, (lo, hi)
    lo, hi = np.quantile(post["sigma"].values, [0.005, 0.995])
    assert lo < PHYLO_TRUTH["sigma"] < hi, (lo, hi)
    # the pointwise LOO density covers exactly the fitted species with a brain value
    ll = groups["log_likelihood"]["trait"]
    n_obs = int(np.isfinite(groups["observed_data"]["trait"]).sum())
    assert ll.dims == ("chain", "draw", "species") and ll.sizes["species"] == n_obs
    assert bool(np.isfinite(ll).all())
    assert set(groups["constant_data"].data_vars) >= {"parent", "tip_label", "predictors"}
    assert post["trait_hat"].sizes["species"] == species.sizes["species"]
    m = bundle.metrics
    assert m.values["elpd_loo"] is not None and m.values["coverage_90"] >= 0.5
    assert bundle.model.learned["kind"] == "lambda"


def test_predict_fills_missing_species_from_relatives(
    bundle: Bundle, species: xr.Dataset, registry: Registry
) -> None:
    out = score_bundle(
        bundle, _req("mode: predict\n"), species.drop_vars("brain"), registry=registry,
        return_full=True,
    )  # fmt: skip
    pred = out["predictions"].to_dataset()["brain"]
    assert pred.dims == ("chain", "draw", "species")
    assert pred.attrs["kind"] == "expected"
    missing = species["brain"].isnull().values
    med = pred.median(("chain", "draw")).values
    assert np.isfinite(med).all() and (med > 0).all()
    # observed species are predicted from their relatives alone (leave-one-out), and close
    obs = np.log(species["brain"].values)
    r = np.corrcoef(np.log(med[~missing]), obs[~missing])[0, 1]
    assert r > 0.8, r
    # predictive draws are noisier than the expected ones and deterministic given the seed
    a = score_bundle(bundle, _req("mode: predict\noutput: {kind: predictive}\nseed: 4\n"),
                     species.drop_vars("brain"), registry=registry, return_full=True)  # fmt: skip
    b = score_bundle(bundle, _req("mode: predict\noutput: {kind: predictive}\nseed: 4\n"),
                     species.drop_vars("brain"), registry=registry, return_full=True)  # fmt: skip
    pa = a["predictions"].to_dataset()["brain"]
    np.testing.assert_array_equal(pa.values, b["predictions"].to_dataset()["brain"].values)
    assert float(np.log(pa).std(("chain", "draw")).mean()) > float(
        np.log(pred).std(("chain", "draw")).mean()
    )


def test_simulate_draws_new_traits_for_a_subset(
    bundle: Bundle, species: xr.Dataset, registry: Registry
) -> None:
    some = species.drop_vars("brain").isel(species=slice(0, 10))
    out = score_bundle(bundle, _req("mode: simulate\nseed: 2\n"), some, registry=registry,
                       return_full=True)  # fmt: skip
    sim = out["predictions"].to_dataset()["brain"]
    assert sim.sizes["species"] == 10 and sim.attrs["kind"] == "predictive"
    assert bool(np.isfinite(sim).all()) and float(sim.min()) > 0
    np.testing.assert_array_equal(sim["species"].values, some["species"].values)


def test_counterfactual_and_forecast_are_refused(
    bundle: Bundle, species: xr.Dataset, registry: Registry
) -> None:
    with pytest.raises(MapiError) as err:
        score_bundle(bundle, _req("mode: counterfactual\ninterventions: [{var: body, "
                                  "multiply: 1.1}]\n"), species.drop_vars("brain"),
                     registry=registry)  # fmt: skip
    assert err.value.code is ErrorCode.MODE_NOT_SUPPORTED


@pytest.mark.parametrize("kind", ["iid", "brownian", "ou"])
def test_every_kind_fits_and_scores(kind: str, registry: Registry) -> None:
    ds = phylo_dataset(n_species=30, missing=3, seed=5)
    spec = PHYLO_SPEC_YAML.replace("kind: lambda", f"kind: {kind}").replace(
        "tune: 150, draws: 100", "tune: 100, draws: 60"
    )
    b = fit_spec(spec, ds, registry=registry, raw_schema=PHYLO_SCHEMA)
    post = b.model.groups["posterior"]
    assert {"intercept", "slope"} <= set(post.data_vars)
    if kind == "ou":
        assert {"rate", "rho", "eta2"} <= set(post.data_vars)
        np.testing.assert_allclose(post["eta2"] * post["rho"], post["rate"], rtol=1e-8)
    else:
        assert "sigma" in post and "lam" not in post
    assert b.metrics.values["elpd_loo"] is not None
    out = score_bundle(b, _req("mode: predict\n"), ds.drop_vars("brain"), registry=registry,
                       return_full=True)  # fmt: skip
    assert bool(np.isfinite(out["predictions"].to_dataset()["brain"]).all())


@pytest.mark.mapi_contract
def test_phylo_regression_passes_the_contract_kit(registry: Registry) -> None:
    data = phylo_dataset(n_species=30, missing=3, seed=2)
    fixture = ModelFixture(spec=PHYLO_SPEC_YAML, data=data, raw_schema=PHYLO_SCHEMA,
                           registry=registry)  # fmt: skip
    report = check_model(PhyloRegressionModel, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "fit",
        "bundle_roundtrip_no_pickle",
        "output_space_and_units",
        "outputs_match_manifest",
        "seed_determinism",
        "plots_sanitize",
    } <= ran, report.summary()
    notes = {r.name: r.detail for r in report.results}
    assert "sci.phylogeny" in notes["plots_sanitize"] and "not applicable" not in (
        notes["plots_sanitize"].split("sci.phylogeny")[0][-40:]
    ), notes["plots_sanitize"]
    print(report.summary())


# ------------------------------------------------------------------------------- readers


def _xdr(*records: bytes) -> bytes:
    return gzip.compress(b"RDX3\nX\n" + struct.pack(">iii", 3, 0, 0) + struct.pack(">i", 5)
                         + b"UTF-8" + b"".join(records))  # fmt: skip


def _i(v: int) -> bytes:
    return struct.pack(">i", v)


def test_read_r_object_reads_a_small_named_list(tmp_path: Path) -> None:
    def string(s: str) -> bytes:
        return _i(9) + _i(len(s)) + s.encode()

    def strvec(*items: str) -> bytes:
        return _i(16) + _i(len(items)) + b"".join(string(s) for s in items)

    names = _i(2 | (1 << 10)) + _i(1) + string("names") + strvec("edge", "len") + _i(254)
    body = (
        _i(19 | (1 << 9)) + _i(2)
        + _i(13) + _i(4) + struct.pack(">4i", 3, 3, 1, 2)
        + _i(14) + _i(2) + struct.pack(">2d", 1.5, 2.5)
        + names
    )  # fmt: skip
    path = tmp_path / "x.rds"
    path.write_bytes(_xdr(body))
    obj = read_r_object(path)
    assert list(obj) == ["edge", "len"]
    np.testing.assert_array_equal(obj["edge"], [3, 3, 1, 2])
    np.testing.assert_allclose(obj["len"], [1.5, 2.5])
    bad = tmp_path / "bad.rds"
    bad.write_bytes(gzip.compress(b"RDX3\nX\n" + _i(3)))
    with pytest.raises(ValueError):
        read_r_object(bad)


def test_read_tree_reads_newick_files(tmp_path: Path) -> None:
    path = tmp_path / "t.nwk"
    path.write_text("((a:1,b:1):2,c:3);")
    tree = read_tree(path)
    c = tree.shared_length(tree.tip_positions(["a", "b", "c"]))
    np.testing.assert_allclose(c, [[3, 2, 0], [2, 3, 0], [0, 0, 3]])


# ------------------------------------------------------------------------------- real data


@pytest.mark.skipif(CSV is None or TREE is None, reason="pymc-challenges data not present")
def test_from_challenges_reads_the_primates_and_their_tree(tmp_path: Path) -> None:
    assert CSV is not None and TREE is not None
    full = read_tree(TREE)
    assert full.tips.size == 301 and full.n == 595
    assert full.tip_depth() == pytest.approx(73.0, abs=0.01)
    six = ["Homo_sapiens", "Pan_troglodytes_verus", "Gorilla_gorilla_gorilla", "Pongo_abelii",
           "Macaca_mulatta", "Lemur_catta"]  # fmt: skip
    c = full.shared_length(full.tip_positions(six))
    np.testing.assert_allclose(c[0, 1:], [66.8, 64.4, 57.9, 43.0, 0.0], atol=0.06)
    ds = from_challenges(CSV)
    assert validate_dataset(PHYLO_SCHEMA, ds) == []
    assert ds.sizes["species"] == 186
    assert int(ds["brain"].notnull().sum()) == 151
    tree = PhyloTree.from_dataset(ds)
    assert tree.tips.size == 186
    # pruning keeps the shared history of the kept species
    kept = [s for s in six if s in set(ds["species"].values.tolist())]
    np.testing.assert_allclose(
        tree.shared_length(tree.tip_positions(kept)), full.shared_length(full.tip_positions(kept))
    )
    bad = tmp_path / "x.csv"
    bad.write_text("name;brain\nA;1\n")
    with pytest.raises(ValueError, match="missing columns"):
        from_challenges(bad, TREE)


@pytest.mark.slow
@pytest.mark.skipif(CSV is None or TREE is None, reason="pymc-challenges data not present")
def test_real_primates_lambda_fit_matches_the_notebook(registry: Registry) -> None:
    assert CSV is not None
    ds = from_challenges(CSV)
    spec = PRIMATES_SPEC_YAML.replace(
        "tune: 1000, draws: 1000, chains: 4, engine: nutpie", "tune: 500, draws: 500, chains: 2, "
        "engine: pymc"
    )
    b = fit_spec(spec, ds, registry=registry, raw_schema=PHYLO_SCHEMA)
    ref = challenges_reference()["lambda"]
    post = b.model.groups["posterior"]
    slope = post["slope"].mean(("chain", "draw"))
    assert float(slope.sel(predictor="body")) == pytest.approx(ref["body"], abs=0.03)
    assert float(slope.sel(predictor="group_size")) == pytest.approx(ref["group_size"], abs=0.03)
    assert float(post["lam"].mean()) == pytest.approx(ref["lam"], abs=0.03)
    assert float(post["sigma"].mean()) == pytest.approx(ref["sigma"], abs=0.03)
    assert b.metrics.values["elpd_loo"] == pytest.approx(ref["elpd"], abs=10.0)
