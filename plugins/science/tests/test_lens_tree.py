"""``sci.strong_lens`` on tree datasets (M4): the PSF from a ``/psf`` node, flat back-compat."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_science.lens import PSF_NODE, StrongLensModel, StrongLensSettings, _tree_psf
from mapi_plugin_science.lens_data import (
    LENS_SCHEMA,
    LENS_SPEC_YAML,
    LENS_TREE_SCHEMA,
    LENS_TREE_SPEC_YAML,
    lens_dataset,
    to_tree,
)
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.testing import TreeModelFixture, check_tree_model
from mapi_sdk.tree import check_tree, infer_tree_schema, select_nodes
from mapi_spec.errors import ErrorCode, MapiError

TINY = ("tune: 150, draws: 100", "tune: 20, draws: 20")


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(StrongLensModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def mock() -> xr.Dataset:
    return lens_dataset(seed=0)


@pytest.fixture(scope="module")
def tree(mock: xr.Dataset) -> xr.DataTree:
    return to_tree(mock)


@pytest.fixture(scope="module")
def flat_bundle(registry: Registry, mock: xr.Dataset) -> Bundle:
    return fit_spec(LENS_SPEC_YAML, mock, registry=registry, raw_schema=LENS_SCHEMA)


@pytest.fixture(scope="module")
def tree_bundle(registry: Registry, tree: xr.DataTree) -> Bundle:
    return fit_spec(LENS_TREE_SPEC_YAML, tree, registry=registry, tree_schema=LENS_TREE_SCHEMA)


# ============================================================================ contracts


def test_manifest_declares_tree_input() -> None:
    m = StrongLensModel.manifest()
    assert m.capabilities.input == "tree"
    assert "tree" in m.description
    # default: the flat input still needs the PSF exactly as before (back-compat)
    plain = m.schema_template.resolve({})
    assert set(plain.vars) == {"image", "weight", "psf"}
    assert set(plain.dims) == {"row", "col", "psf_row", "psf_col"}
    # psf_source: tree - the PSF comes from the tree's /psf node only
    tree_only = m.schema_template.resolve({"psf_source": "tree", "use_mask": True})
    assert set(tree_only.vars) == {"image", "weight", "mask"}
    assert set(tree_only.dims) == {"row", "col"}


def test_to_tree_is_a_valid_tree_dataset(mock: xr.Dataset, tree: xr.DataTree) -> None:
    assert sorted(n.path for n in tree.subtree) == ["/", "/image", "/psf"]
    assert check_tree(tree, LENS_TREE_SCHEMA) == []
    assert set(infer_tree_schema(tree).nodes) == set(LENS_TREE_SCHEMA.nodes)
    xr.testing.assert_identical(tree["psf"].to_dataset()["psf"], mock["psf"])
    flat = select_nodes(tree, {"image": {"path": "/image"}, "psf": {"path": "/psf"}})
    xr.testing.assert_equal(flat[["image", "weight", "psf"]], mock[["image", "weight", "psf"]])
    assert tree.attrs["pixel_scale"] == 0.1 and not tree["image"].attrs


def test_tree_psf_picks_the_psf_node(tree: xr.DataTree, mock: xr.Dataset) -> None:
    auto, only = StrongLensSettings(), StrongLensSettings(psf_source="tree")
    got = _tree_psf(tree, auto)
    assert got is not None
    np.testing.assert_array_equal(got.values, mock["psf"].values)
    assert _tree_psf(None, auto) is None  # a plain dataset: the flat psf
    no_psf = xr.DataTree.from_dict({"/image": tree["image"].to_dataset()})
    assert _tree_psf(no_psf, auto) is None
    for t in (None, no_psf):
        with pytest.raises(MapiError, match="psf_source is 'tree'") as exc:
            _tree_psf(t, only)
        assert exc.value.code is ErrorCode.SCHEMA_INVALID
    bad = xr.DataTree.from_dict({"/psf": xr.Dataset({"psf": (("y", "x"), mock["psf"].values)})})
    with pytest.raises(MapiError, match="must have dims") as exc:
        _tree_psf(bad, auto)
    assert exc.value.code is ErrorCode.SCHEMA_INVALID


# ============================================================================ fit


def test_tree_fit_matches_the_flat_fit(flat_bundle: Bundle, tree_bundle: Bundle) -> None:
    """The image from /image and the PSF from /psf give the flat fit's posterior."""
    flat, tree = flat_bundle.model, tree_bundle.model
    assert tree.learned["psf_node"] == PSF_NODE and "psf_node" not in flat.learned
    assert tree.learned["psf_rank"] == flat.learned["psf_rank"]
    # the staged optimisation is deterministic: the same mode
    for k, v in flat.learned["mode"]["values"].items():
        np.testing.assert_allclose(tree.learned["mode"]["values"][k], v, rtol=1e-8, atol=1e-10)
    # the same seed, data and model with the seed-deterministic PyMC engine: the same draws
    xr.testing.assert_allclose(tree.posterior, flat.posterior, rtol=1e-6, atol=1e-9)
    xr.testing.assert_identical(
        tree.group("constant_data")["psf"], flat.group("constant_data")["psf"]
    )
    assert tree_bundle.metrics.values == pytest.approx(flat_bundle.metrics.values, rel=1e-6)


def test_auto_prefers_the_tree_psf(registry: Registry, tree: xr.DataTree) -> None:
    """psf_source auto: both nodes selected (flat psf too), the /psf node is used."""
    spec = (
        LENS_TREE_SPEC_YAML.replace("    psf_source: tree\n", "")
        .replace(
            "nodes: {image: {path: /image}}", "nodes: {image: {path: /image}, k: {path: /psf}}"
        )
        .replace(*TINY)
    )
    b = fit_spec(spec, tree, registry=registry, tree_schema=LENS_TREE_SCHEMA)
    assert b.model.learned["psf_node"] == PSF_NODE
    assert abs(b.model.posterior["theta_E"].mean().item() - 0.7) < 0.05


def test_check_tree_model(registry: Registry, tree: xr.DataTree) -> None:
    report = check_tree_model(
        StrongLensModel,
        TreeModelFixture(
            LENS_TREE_SPEC_YAML.replace(*TINY),
            tree,
            tree_schema=LENS_TREE_SCHEMA,
            registry=registry,
        ),
    )
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "manifest_tree_input",
        "fit_receives_bound_tree",
        "score_tree_payload",
        "flat_payload_without_tree",
    } <= ran
