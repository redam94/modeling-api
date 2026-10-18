"""fit_spec / score_bundle with tree datasets and tree-input models (M4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import data_hash, fit_spec, prepare_model_stage, score_bundle
from mapi_sdk.models import FitContext, FitOutput, Fitted, ScoreContext
from mapi_sdk.registry import Registry
from mapi_sdk.testing import (
    ContractViolation,
    TreeModelFixture,
    check_tree_model,
    toy,
    tree_dataset,
)
from mapi_sdk.tree import infer_tree_schema, read_tree_zip, select_nodes, write_tree_zip
from mapi_spec import load_model_spec, load_score_request
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest

REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://growth/toy\n"
NODES = {"sales": {"path": "/sales"}, "media": {"path": "/media"}}


def req(extra: str = "mode: predict\n") -> Any:
    return load_score_request(REQ + extra)


def summary(tree: xr.DataTree) -> xr.Dataset:
    node = tree["summary"]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()


@pytest.fixture
def tree() -> xr.DataTree:
    return tree_dataset()


@pytest.fixture
def flat(tree: xr.DataTree) -> xr.Dataset:
    return select_nodes(tree, NODES)


class _Seen:
    fit: list[xr.DataTree | None]
    score: list[xr.DataTree | None]

    def __init__(self) -> None:
        self.fit, self.score = [], []


def _spy(base: type[Any], seen: _Seen) -> type[Any]:
    class Spy(base):  # type: ignore[valid-type,misc]
        def fit(self, ds: xr.Dataset, s: Any, cfg: FitConfig, ctx: FitContext) -> FitOutput:
            seen.fit.append(ctx.tree)
            out: FitOutput = super().fit(ds, s, cfg, ctx)
            return out

        def score(
            self, fitted: Fitted, ds: xr.Dataset, r: ScoreRequest, ctx: ScoreContext
        ) -> xr.DataTree:
            seen.score.append(ctx.tree)
            out: xr.DataTree = super().score(fitted, ds, r, ctx)
            return out

    return Spy


# ---------------------------------------------------------------------- dataset models


def test_dataset_model_fits_a_tree_like_its_flat_merge(
    registry: Registry, tree: xr.DataTree, flat: xr.Dataset
) -> None:
    seen = _Seen()
    reg = registry.copy()
    reg.register_model(_spy(toy.LinearModel, seen), replace=True)
    from_tree = fit_spec(toy.tree_spec("toy.linear@^1"), tree, registry=reg)
    assert seen.fit and all(t is None for t in seen.fit)  # dataset models never see the tree
    # the same data as a plain dataset (the spec then has no data.nodes)
    spec = load_model_spec(toy.tree_spec("toy.linear@^1"))
    plain_spec = spec.model_copy(update={"data": spec.data.model_copy(update={"nodes": None})})
    from_flat = fit_spec(plain_spec, flat, registry=reg)
    xr.testing.assert_allclose(from_tree.model.group("params"), from_flat.model.group("params"))
    assert from_tree.metrics.values == pytest.approx(from_flat.metrics.values)
    assert from_tree.raw_schema == from_flat.raw_schema
    assert from_tree.raw_schema.vars["revenue"].role.value == "target"
    assert from_tree.manifest.data_hash == data_hash(tree) != data_hash(flat)
    # scoring: a tree payload is flattened with the bundle's data.nodes
    a = summary(score_bundle(from_tree, req(), tree, registry=reg))
    b = summary(score_bundle(from_tree, req(), flat, registry=reg))
    xr.testing.assert_allclose(a, b)
    assert all(t is None for t in seen.score)


def test_spec_errors_for_tree_and_plain_datasets(
    registry: Registry, tree: xr.DataTree, flat: xr.Dataset
) -> None:
    spec = load_model_spec(toy.tree_spec("toy.linear@^1", split=False))
    with pytest.raises(MapiError) as plain:
        fit_spec(spec, flat, registry=registry)  # data.nodes on a plain dataset
    assert plain.value.code is ErrorCode.SCHEMA_INVALID
    no_nodes = spec.model_copy(update={"data": spec.data.model_copy(update={"nodes": None})})
    with pytest.raises(MapiError) as root:
        fit_spec(no_nodes, tree, registry=registry)
    assert root.value.code is ErrorCode.SCHEMA_INVALID and root.value.pointer == "/data"
    assert "pick nodes" in root.value.detail
    clash = spec.model_copy(
        update={
            "data": spec.data.model_copy(
                update={
                    "nodes": load_model_spec(toy.tree_spec("toy.linear@^1")).data.nodes
                    | {"again": spec.data.nodes["sales"]}  # type: ignore[index]
                }
            )
        }
    )
    with pytest.raises(MapiError) as dup:
        fit_spec(clash, tree, registry=registry)
    assert dup.value.pointer == "/data/nodes/again"
    with pytest.raises(MapiError):
        fit_spec(spec, flat, registry=registry, tree_schema=infer_tree_schema(tree))


def test_root_only_tree_reads_the_root(registry: Registry, flat: xr.Dataset) -> None:
    spec = load_model_spec(toy.tree_spec("toy.linear@^1", split=False))
    no_nodes = spec.model_copy(update={"data": spec.data.model_copy(update={"nodes": None})})
    rooted = xr.DataTree.from_dict({"/": flat})
    b = fit_spec(no_nodes, rooted, registry=registry)
    assert set(b.raw_schema.vars) == {"revenue", "spend"}


def test_inner_join_through_the_harness(registry: Registry, tree: xr.DataTree) -> None:
    media = tree["media"].to_dataset(inherit=False).isel(date=slice(2, None))
    sales = tree["sales"].to_dataset(inherit=False)
    shifted = xr.DataTree.from_dict(
        {
            "/": xr.Dataset(coords={"market": tree["market"].values}),
            "/sales": sales.assign_coords(date=tree["date"].values),
            "/media": media.assign_coords(
                date=tree["date"].values[2:], channel=tree["channel"].values
            ),
        }
    )
    spec = load_model_spec(toy.tree_spec("toy.linear@^1", split=False))
    with pytest.raises(MapiError) as exact:
        fit_spec(spec, shifted, registry=registry)
    assert exact.value.pointer == "/data/nodes/media"
    inner = spec.model_copy(update={"data": spec.data.model_copy(update={"join": "inner"})})
    b = fit_spec(inner, shifted, registry=registry)
    assert b.fit_coords["raw"]["time"].size == tree["date"].size - 2


# ------------------------------------------------------------------- tree-input models


def test_tree_model_gets_the_bound_tree_at_fit_and_score(
    registry: Registry, tree: xr.DataTree, flat: xr.Dataset
) -> None:
    seen = _Seen()
    reg = registry.copy()
    reg.register_model(_spy(toy.TreeLinearModel, seen), replace=True)
    b = fit_spec(toy.tree_spec(), tree, registry=reg)
    # evaluation fit + production refit both received the whole, bound tree
    assert len(seen.fit) == 2
    for got in seen.fit:
        assert got is not None
        assert {n.path for n in got.subtree} == {"/", "/sales", "/media", "/adjust"}
        assert "geo" in got.dims and "market" not in got.dims  # binding applied per node
        assert got["adjust"]["offset"].dims == ("geo",)
        assert "y" in got["sales"].data_vars
    # the holdout evaluation scored through the score path with the tree too
    assert seen.score and all(t is not None for t in seen.score)
    assert b.model.learned["tree_nodes"] == ["/", "/adjust", "/media", "/sales"]
    np.testing.assert_allclose(b.model.group("adjust")["offset"].values, [5.0, -5.0, 0.0])

    seen.score.clear()
    with_tree = summary(score_bundle(b, req(), tree, registry=reg))
    assert seen.score[-1] is not None
    flat_plus = summary(score_bundle(b, req(), flat, registry=reg, tree=tree))
    xr.testing.assert_allclose(with_tree, flat_plus)
    # a flat payload alone scores with the offset learned at fit
    seen.score.clear()
    alone = summary(score_bundle(b, req(), flat, registry=reg))
    assert seen.score == [None]
    xr.testing.assert_allclose(alone, with_tree)
    # the payload's own /adjust node wins
    moved = tree.copy()
    moved["adjust"].dataset = xr.Dataset({"offset": (("market",), [105.0, -5.0, 0.0])})
    shifted = summary(score_bundle(b, req(), moved, registry=reg))
    diff = (shifted["y_hat"] - with_tree["y_hat"]).sel(market="NL")
    np.testing.assert_allclose(diff.values, 100.0)
    np.testing.assert_allclose((shifted["y_hat"] - with_tree["y_hat"]).sel(market="DE").values, 0.0)


def test_tree_model_refuses_a_plain_dataset(registry: Registry, flat: xr.Dataset) -> None:
    spec = load_model_spec(toy.tree_spec(split=False))
    plain = spec.model_copy(update={"data": spec.data.model_copy(update={"nodes": None})})
    with pytest.raises(MapiError) as err:
        fit_spec(plain, flat, registry=registry)
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_counterfactual_scenario_tree(registry: Registry, tree: xr.DataTree) -> None:
    b = fit_spec(toy.tree_spec(split=False), tree, registry=registry)
    scenario = tree.copy()
    scenario["adjust"].dataset = xr.Dataset({"offset": (("market",), [15.0, -5.0, 0.0])})
    out = score_bundle(
        b,
        req("mode: counterfactual\nscenario: dataset://growth/scenario@v1\n"),
        tree,
        scenario=scenario,
        registry=registry,
    )
    delta = out["delta/summary"].to_dataset()["y_hat"].sel(market="NL")
    np.testing.assert_allclose(delta.values, 10.0)
    # outside counterfactuals the scenario simply is the payload (and brings its tree)
    plain = summary(score_bundle(b, req(), scenario=scenario, registry=registry))
    base = summary(score_bundle(b, req(), tree, registry=registry))
    np.testing.assert_allclose((plain["y_hat"] - base["y_hat"]).sel(market="NL").values, 10.0)


def test_evaluation_fit_sees_only_the_training_part_of_the_tree(
    registry: Registry, tree: xr.DataTree
) -> None:
    seen = _Seen()
    reg = registry.copy()
    reg.register_model(_spy(toy.TreeLinearModel, seen), replace=True)
    fit_spec(toy.tree_spec(), tree, registry=reg)  # time_holdout: last 4 of 40 weeks
    fits = [t["sales"].to_dataset(inherit=True).sizes["time"] for t in seen.fit if t is not None]
    assert fits == [36, 40]  # the evaluation fit never sees the holdout's targets
    evaluation = seen.score[0]
    assert evaluation is not None
    assert evaluation["sales"].to_dataset(inherit=True).sizes["time"] == 4
    assert "adjust" in evaluation.children  # nodes without the split dim stay whole


def test_interventions_reach_the_tree_of_a_tree_model(
    registry: Registry, tree: xr.DataTree
) -> None:
    seen = _Seen()
    reg = registry.copy()
    reg.register_model(_spy(toy.TreeLinearModel, seen), replace=True)
    b = fit_spec(toy.tree_spec(split=False), tree, registry=reg)
    seen.score.clear()
    score_bundle(
        b,
        req(
            "mode: counterfactual\n"
            "interventions:\n  - {var: spend, sel: {channel: tv}, multiply: 2.0}\n"
        ),
        tree,
        registry=reg,
    )
    main, baseline = seen.score
    assert main is not None and baseline is not None
    got = main["media"].to_dataset(inherit=True)["spend"]
    base = baseline["media"].to_dataset(inherit=True)["spend"]
    np.testing.assert_allclose(got.sel(channel="tv").values, 2 * base.sel(channel="tv").values)
    np.testing.assert_allclose(got.sel(channel="search").values, base.sel(channel="search").values)
    # the payload tree itself is untouched
    original = tree["media"].to_dataset(inherit=True)["spend"]
    np.testing.assert_allclose(
        original.rename({"market": "geo", "date": "time"}).transpose(*base.dims).values,
        base.values,
    )


def test_fit_spec_tree_stands_in_for_flat_data(
    registry: Registry, tree: xr.DataTree, flat: xr.Dataset
) -> None:
    # a refit on data already selected from a tree (governance, explorer backtests) gives
    # a tree-input model the tree it would otherwise lose
    seen = _Seen()
    reg = registry.copy()
    reg.register_model(_spy(toy.TreeLinearModel, seen), replace=True)
    b = fit_spec(toy.tree_spec(split=False), flat, registry=reg, tree=tree)
    assert seen.fit[-1] is not None
    np.testing.assert_allclose(b.model.group("adjust")["offset"].values, [5.0, -5.0, 0.0])


def test_score_payload_tree_errors(registry: Registry, tree: xr.DataTree) -> None:
    b = fit_spec(toy.tree_spec("toy.linear@^1", split=False), tree, registry=registry)
    broken = tree.copy()
    del broken["media"]
    with pytest.raises(MapiError) as err:
        score_bundle(b, req(), broken, registry=registry)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and err.value.pointer == "/data"


def test_prepare_model_stage_from_a_tree(registry: Registry, tree: xr.DataTree) -> None:
    stage = prepare_model_stage(toy.tree_spec(split=False), tree, registry=registry)
    assert stage.fit_context.tree is not None
    assert "geo" in stage.fit_context.tree.dims
    assert set(stage.data.data_vars) >= {"y", "spend"}
    lin = prepare_model_stage(toy.tree_spec("toy.linear@^1", split=False), tree, registry=registry)
    assert lin.fit_context.tree is None


def test_bundle_from_a_tree_round_trips(
    tmp_path: Path, registry: Registry, tree: xr.DataTree
) -> None:
    b = fit_spec(toy.tree_spec(), tree, registry=registry)
    b.write(tmp_path / "b")
    back = Bundle.read(tmp_path / "b", registry)
    assert back.spec.data.nodes == b.spec.data.nodes
    xr.testing.assert_allclose(
        summary(score_bundle(b, req(), tree, registry=registry)),
        summary(score_bundle(back, req(), tree, registry=registry)),
    )


def test_fit_from_a_tree_read_back_from_its_zip(registry: Registry, tree: xr.DataTree) -> None:
    raw = write_tree_zip(tree)
    assert raw is not None
    back = read_tree_zip(raw)
    a = fit_spec(toy.tree_spec(split=False), tree, registry=registry)
    b = fit_spec(toy.tree_spec(split=False), back, registry=registry)
    assert a.manifest.data_hash == b.manifest.data_hash
    xr.testing.assert_allclose(a.model.group("params"), b.model.group("params"))


# ------------------------------------------------------------------------- contract kit


def test_contract_kit_passes_for_the_toy_tree_model(registry: Registry, tree: xr.DataTree) -> None:
    report = check_tree_model(
        toy.TreeLinearModel, TreeModelFixture(spec=toy.tree_spec(), tree=tree, registry=registry)
    )
    assert report.ok, report.summary()
    assert report.get("flat_payload_without_tree").detail == "scored without the tree"
    assert report.get("fit_receives_bound_tree").passed


def test_contract_kit_flags_dataset_models_and_crashes(
    registry: Registry, tree: xr.DataTree
) -> None:
    fixture = TreeModelFixture(spec=toy.tree_spec("toy.linear@^1"), tree=tree, registry=registry)
    report = check_tree_model(toy.LinearModel, fixture, raise_on_failure=False)
    assert not report.get("manifest_tree_input").passed
    assert not report.get("fit_receives_bound_tree").passed
    with pytest.raises(ContractViolation):
        check_tree_model(toy.LinearModel, fixture)

    class Crashy(toy.TreeLinearModel):
        def score(
            self, fitted: Fitted, ds: xr.Dataset, r: ScoreRequest, ctx: ScoreContext
        ) -> xr.DataTree:
            if ctx.tree is None:
                raise RuntimeError("no tree")
            return super().score(fitted, ds, r, ctx)

    class Typed(toy.TreeLinearModel):
        def score(
            self, fitted: Fitted, ds: xr.Dataset, r: ScoreRequest, ctx: ScoreContext
        ) -> xr.DataTree:
            if ctx.tree is None:
                raise MapiError("send the tree", code=ErrorCode.SCHEMA_INVALID)
            return super().score(fitted, ds, r, ctx)

    fixture = TreeModelFixture(spec=toy.tree_spec(split=False), tree=tree, registry=registry)
    crashy = check_tree_model(Crashy, fixture, raise_on_failure=False)
    assert not crashy.get("flat_payload_without_tree").passed
    typed = check_tree_model(Typed, fixture, raise_on_failure=False)
    assert typed.ok, typed.summary()
    assert typed.get("flat_payload_without_tree").detail == "refused with SCHEMA_INVALID"


def test_pytest_plugin_fixtures(mapi_tree: xr.DataTree, mapi_check_tree_model: Any) -> None:
    assert {n.path for n in mapi_tree.subtree} == {"/", "/sales", "/media", "/adjust"}
    assert mapi_check_tree_model is check_tree_model
