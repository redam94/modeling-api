"""Automatic binding of a workflow's data onto a model's canonical names (pure, JSON only).

Gallery workflows name models (``ts.sarimax@^1``) but not how a user's columns map onto the
model's canonical variables, because they must work on anyone's data. A model node
therefore completes the spec's ``data.bind`` from the dataset's roles: the model's target
variable is bound to the dataset's (single) target, and the model's time dim to the
dataset's time dim, when the spec does not bind them and the names differ. Explicit
bindings always win. The control plane (static, from the stored schema and the catalog)
and the runtime compute the same binding.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mapi_spec.schema import DataSchema, DataSchemaTemplate, Role

__all__ = ["auto_bind", "model_input_schema"]


def model_input_schema(
    template: DataSchemaTemplate | Mapping[str, Any], settings: Mapping[str, Any]
) -> DataSchema | None:
    """The model's input schema for ``settings`` (``None`` when it cannot be resolved)."""
    tmpl = (
        template if isinstance(template, DataSchemaTemplate)
        else DataSchemaTemplate.model_validate(dict(template))
    )  # fmt: skip
    schema, _ = tmpl.try_resolve(settings)
    return schema


def auto_bind(
    model: DataSchema, data: DataSchema, given: Mapping[str, Any] | None = None
) -> dict[str, dict[str, str]]:
    """``{"dims": ..., "vars": ...}``: ``given`` completed with the target and time
    bindings the data's roles imply."""
    dims = dict((given or {}).get("dims") or {})
    vars_ = dict((given or {}).get("vars") or {})
    used = set(dims.values()) | set(vars_.values())
    data_targets = [v for v, s in data.vars.items() if s.role is Role.TARGET]
    for canon, spec in model.vars.items():
        if spec.role is not Role.TARGET or canon in vars_ or canon in data.vars:
            continue
        free = [t for t in data_targets if t not in used and t not in model.vars]
        if len(free) == 1:
            vars_[canon] = free[0]
            used.add(free[0])
    data_times = [d for d, s in data.dims.items() if s.kind == "time"]
    for canon, dspec in model.dims.items():
        if dspec.kind != "time" or canon in dims or canon in data.dims:
            continue
        free = [d for d in data_times if d not in used and d not in model.dims]
        if len(free) == 1:
            dims[canon] = free[0]
            used.add(free[0])
    out: dict[str, dict[str, str]] = {}
    if dims:
        out["dims"] = {str(k): str(v) for k, v in dims.items()}
    if vars_:
        out["vars"] = {str(k): str(v) for k, v in vars_.items()}
    return out
