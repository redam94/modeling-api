"""Dataset ↔ canonical names (``geo: market``), applied to inputs and reversed on outputs."""

from __future__ import annotations

import json
from collections.abc import Hashable, Mapping
from typing import Any, Literal, TypeVar

import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema
from mapi_spec.spec import Binding

__all__ = ["BindingMap"]

V = TypeVar("V")


class BindingMap:
    """Runtime view of a :class:`~mapi_spec.spec.Binding`.

    ``apply`` renames dataset names to canonical ones (what steps, models and priors use);
    ``reverse`` renames canonical names back on outputs. Unbound names pass through.
    """

    __slots__ = ("_to_canon", "_to_dataset", "binding")

    def __init__(self, binding: Binding | Mapping[str, Any] | None = None) -> None:
        if binding is None:
            binding = Binding()
        elif not isinstance(binding, Binding):
            binding = Binding.model_validate(binding)
        self.binding = binding
        self._to_canon = binding.canonical_of()
        self._to_dataset = binding.dataset_of()

    def __repr__(self) -> str:
        return f"BindingMap({self._to_dataset!r})"

    @property
    def is_identity(self) -> bool:
        return all(k == v for k, v in self._to_canon.items())

    def to_canonical(self, name: str) -> str:
        return self._to_canon.get(name, name)

    def to_dataset(self, name: str) -> str:
        return self._to_dataset.get(name, name)

    def canonical_keys(self, mapping: Mapping[str, V]) -> dict[str, V]:
        """Re-key a ``{dataset name: value}`` mapping by canonical names."""
        return {self.to_canonical(k): v for k, v in mapping.items()}

    # ------------------------------------------------------------------------ datasets
    def apply(self, ds: xr.Dataset, *, missing: Literal["error", "ignore"] = "error") -> xr.Dataset:
        """Rename dataset names to canonical names.

        ``missing="error"`` raises ``BINDING_INVALID`` when a bound dataset name is absent
        (fit time); ``"ignore"`` skips absent names (score payloads may omit the target).
        A canonical name that collides with an unbound dataset name is always an error.
        """
        present = set(map(str, ds.variables)) | set(map(str, ds.dims))
        absent = sorted(n for n in self._to_canon if n not in present)
        if absent and missing == "error":
            raise MapiError(
                f"the data has no dim or variable named {absent[0]!r} (bound in data.bind)",
                code=ErrorCode.BINDING_INVALID,
                untrusted_text=", ".join(absent)[:2000],
            )
        renames = {k: v for k, v in self._to_canon.items() if k in present and k != v}
        bound_sources = set(self._to_canon)
        for canon in renames.values():
            if canon in present and canon not in bound_sources:
                raise MapiError(
                    f"canonical name {canon!r} collides with an unbound name in the data",
                    code=ErrorCode.BINDING_INVALID,
                )
        return _rename(ds, renames)

    def reverse(self, ds: xr.Dataset) -> xr.Dataset:
        """Rename canonical names (dims, coords, variables) back to dataset names."""
        present = set(map(str, ds.variables)) | set(map(str, ds.dims))
        renames = {k: v for k, v in self._to_dataset.items() if k in present and k != v}
        clash = [v for v in renames.values() if v in present and v not in renames]
        if clash:
            raise MapiError(
                f"cannot restore dataset name {clash[0]!r}: the output already uses it",
                code=ErrorCode.BINDING_INVALID,
            )
        return _rename(ds, renames)

    def reverse_tree(self, tree: xr.DataTree) -> xr.DataTree:
        """:meth:`reverse` applied to every node of a ``DataTree``."""
        nodes = {node.path: self.reverse(node.to_dataset(inherit=False)) for node in tree.subtree}
        return xr.DataTree.from_dict(nodes)

    def apply_tree(self, tree: xr.DataTree) -> xr.DataTree:
        nodes = {
            node.path: self.apply(node.to_dataset(inherit=False), missing="ignore")
            for node in tree.subtree
        }
        return xr.DataTree.from_dict(nodes)

    # ------------------------------------------------------------------------- schemas
    def apply_schema(self, schema: DataSchema) -> DataSchema:
        return schema.renamed(
            {k: v for k, v in self._to_canon.items() if schema.has_name(k) and k != v}
        )

    def reverse_schema(self, schema: DataSchema) -> DataSchema:
        return schema.renamed(
            {k: v for k, v in self._to_dataset.items() if schema.has_name(k) and k != v}
        )


#: the keys of a ``grid`` attr (an output's ``GridDecl``) that name dims
_GRID_DIM_KEYS = ("y", "x", "frame", "component")


def _rename(ds: xr.Dataset, renames: Mapping[str, str]) -> xr.Dataset:
    if not renames:
        return ds
    names: dict[Hashable, Hashable] = {k: v for k, v in renames.items()}
    out = ds.rename(names)
    for name in out.data_vars:
        var = out.variables[name]
        if "grid" in var.attrs:
            # a fresh dict: the renamed dataset may share its attrs dicts with the input
            var.attrs = {**var.attrs, "grid": _rename_grid(var.attrs["grid"], renames)}
    return out


def _rename_grid(raw: Any, renames: Mapping[str, str]) -> Any:
    """A ``grid`` attr (dict or JSON text) with its dim names renamed, in the same form.

    A grid attr names the variable's dims, so it must follow the dims when they are renamed;
    otherwise a recipe reading it looks for a dim the variable no longer has.
    """
    text = isinstance(raw, str)
    try:
        grid = json.loads(raw) if text else raw
    except ValueError:
        return raw
    if not isinstance(grid, Mapping):
        return raw
    new = dict(grid)
    for key in _GRID_DIM_KEYS:
        dim = new.get(key)
        if isinstance(dim, str) and dim in renames:
            new[key] = renames[dim]
    return json.dumps(new) if text else new
