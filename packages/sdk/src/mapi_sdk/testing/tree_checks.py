"""Contract-kit checks for tree-input models (M4, ``capabilities.input="tree"``).

:func:`check_tree_model` fits a model from a tree dataset through the harness and checks
what a tree-input model may rely on: the manifest declares the tree input, the fixture
tree is a valid tree dataset that survives ``data.tree.zip``, ``fit`` and ``score`` receive
the whole tree with the binding applied per node, a tree payload scores like its flat
merge plus the tree, a bundle round trip keeps the scores, and a flat payload without a
tree either scores or is refused with a typed error (never a crash).
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.loader import load_model_spec
from mapi_spec.spec import ModelSpec, ScoreRequest
from mapi_spec.tree import TreeSchema

from mapi_sdk.binding import BindingMap
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import data_hash, fit_spec, score_bundle
from mapi_sdk.models import FitContext, FitOutput, Fitted, ScoreContext
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.testing.checks import (
    CheckResult,
    ContractReport,
    _request,
    _run,
    _summary,
)
from mapi_sdk.tree import (
    check_tree,
    infer_tree_schema,
    read_tree_zip,
    select_nodes,
    write_tree_zip,
)

__all__ = ["TreeModelFixture", "check_tree_model"]


@dataclass(slots=True)
class TreeModelFixture:
    """A tiny end-to-end fixture for :func:`check_tree_model`.

    ``spec`` must reference the plugin under test and select nodes of ``tree`` in
    ``data.nodes`` (or read its root); keep fit settings tiny.
    """

    spec: ModelSpec | str
    tree: xr.DataTree
    tree_schema: TreeSchema | None = None
    side_tables: Mapping[str, xr.Dataset] = field(default_factory=dict)
    registry: Registry | None = None

    def model_spec(self) -> ModelSpec:
        return load_model_spec(self.spec) if isinstance(self.spec, str) else self.spec


def _names(tree: xr.DataTree) -> set[str]:
    out: set[str] = set()
    for node in tree.subtree:
        ds = node.to_dataset(inherit=False)
        out.update(str(n) for n in ds.variables)
        out.update(str(d) for d in ds.dims)
    return out


def _recording(plugin: type[Any], seen: dict[str, list[xr.DataTree | None]]) -> type[Any]:
    """``plugin`` with ``fit``/``score`` recording the tree of their context."""

    def fit(self: Any, ds: xr.Dataset, s: Any, cfg: Any, ctx: FitContext) -> FitOutput:
        seen["fit"].append(ctx.tree)
        out: FitOutput = plugin.fit(self, ds, s, cfg, ctx)
        return out

    def score(
        self: Any, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        seen["score"].append(ctx.tree)
        out: xr.DataTree = plugin.score(self, fitted, ds, req, ctx)
        return out

    return type(plugin.__name__, (plugin,), {"fit": fit, "score": score})


def check_tree_model(
    plugin: type[Any],
    fixture: TreeModelFixture,
    *,
    raise_on_failure: bool = True,
    checks: Sequence[str] | None = None,
) -> ContractReport:
    """Run the tree-input model checks (see the module docstring) and return the report."""
    seen: dict[str, list[xr.DataTree | None]] = {"fit": [], "score": []}
    reg = (fixture.registry or default_registry()).copy()
    reg.register_model(_recording(plugin, seen), replace=True)
    spec = fixture.model_spec()
    tree = fixture.tree
    tables = dict(fixture.side_tables)
    report = ContractReport(f"tree model {plugin.id}@{plugin.version}")
    holder: dict[str, Any] = {}

    def fit_once() -> Bundle:
        if "bundle" not in holder:
            holder["bundle"] = fit_spec(spec, tree, tables, reg, tree_schema=fixture.tree_schema)
        bundle: Bundle = holder["bundle"]
        return bundle

    def score(ds: xr.Dataset | xr.DataTree, bundle: Bundle | None = None, **kw: Any) -> Any:
        return score_bundle(
            bundle or fit_once(), _request(), ds, side_tables=tables, registry=reg, **kw
        )

    def flat() -> xr.Dataset:
        return select_nodes(tree, spec.data.nodes, spec.data.join, schema=fixture.tree_schema)

    def c_manifest() -> str:
        caps = plugin.manifest().capabilities
        if caps.input != "tree":
            raise AssertionError(
                f"capabilities.input is {caps.input!r}; a tree-input model declares 'tree'"
            )
        return "capabilities.input == 'tree'"

    def c_tree_schema() -> str:
        schema = fixture.tree_schema or infer_tree_schema(tree)
        check_tree(tree, schema)
        return f"{len(schema.nodes)} node(s)"

    def c_zip() -> str:
        raw = write_tree_zip(tree)
        assert raw is not None
        back = read_tree_zip(raw)
        if data_hash(back) != data_hash(tree):
            raise AssertionError("the tree changes through data.tree.zip")
        return f"{len(raw)} bytes"

    def c_fit() -> str:
        b = fit_once()
        return f"bundle {b.bundle_id[:12]}"

    def c_fit_tree() -> str:
        fit_once()
        if not seen["fit"] or any(t is None for t in seen["fit"]):
            raise AssertionError("fit() did not receive the tree (ctx.tree is None)")
        got = seen["fit"][-1]
        assert got is not None
        want = {n.path for n in tree.subtree}
        if {n.path for n in got.subtree} != want:
            raise AssertionError("ctx.tree does not hold every node of the dataset")
        binding = BindingMap(spec.data.bind)
        stale = sorted(n for n in _names(got) if binding.to_canonical(n) != n and n in _names(tree))
        if stale:
            raise AssertionError(f"ctx.tree still uses dataset names {stale[:5]} (unbound)")
        return f"{len(want)} node(s), bound"

    def c_score_tree() -> str:
        before = len(seen["score"])
        a = _summary(score(tree))
        got = seen["score"][before:]
        if not got or got[-1] is None:
            raise AssertionError("score() did not receive the payload's tree")
        b = _summary(score(flat(), tree=tree))
        xr.testing.assert_allclose(a, b)
        return "tree payload == flat payload + tree"

    def c_roundtrip() -> str:
        b = fit_once()
        with tempfile.TemporaryDirectory() as tmp:
            b.write(tmp)
            back = Bundle.read(tmp, reg)
        xr.testing.assert_allclose(_summary(score(tree, b)), _summary(score(tree, back)))
        return "scores match after write/read"

    def c_flat_without_tree() -> str:
        try:
            score(flat())
        except MapiError as exc:
            if exc.code is ErrorCode.USER_CODE_ERROR:
                raise AssertionError(
                    "a flat payload without its tree crashed the model; refuse it with a "
                    f"typed error instead: {exc.detail}"
                ) from exc
            return f"refused with {exc.code.value}"
        return "scored without the tree"

    all_checks: list[tuple[str, Callable[[], str | None]]] = [
        ("manifest_tree_input", c_manifest),
        ("tree_schema_valid", c_tree_schema),
        ("tree_zip_roundtrip", c_zip),
        ("fit", c_fit),
        ("fit_receives_bound_tree", c_fit_tree),
        ("score_tree_payload", c_score_tree),
        ("bundle_roundtrip", c_roundtrip),
        ("flat_payload_without_tree", c_flat_without_tree),
    ]
    for name, fn in all_checks:
        if checks is not None and name not in checks:
            continue
        fit_failed = any(r.name == "fit" and not r.passed for r in report.results)
        independent = ("manifest_tree_input", "tree_schema_valid", "tree_zip_roundtrip", "fit")
        if fit_failed and name not in independent:
            report.results.append(CheckResult(name, True, "fit failed", skipped=True))
            continue
        _run(report, name, fn)
    if raise_on_failure:
        report.raise_for_failures()
    return report
