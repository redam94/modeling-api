"""Automatic binding of a workflow's data onto a model's canonical names (pure, JSON only).

Gallery workflows name models (``ts.sarimax@^1``) but not how a user's columns map onto the
model's canonical variables, because they must work on anyone's data. A model node
therefore completes the spec's ``data.bind`` from the dataset's roles: the model's target
variable is bound to the dataset's (single) target, another model variable to the one
dataset variable of its role, the model's time dim to the dataset's time dim, and a
categorical model dim (a panel's ``geo``, a multivariate model's ``variable``) to the one
unbound dim of the data variables bound to the model variables that carry it, when the spec
does not bind them and the names differ. Explicit bindings always win. The control plane
(static, from the stored schema and the catalog) and the runtime compute the same binding.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from mapi_spec.schema import DataSchema, DataSchemaTemplate, Role

__all__ = ["auto_bind", "canonical_split", "canonical_steps", "model_input_schema"]


def model_input_schema(
    template: DataSchemaTemplate | Mapping[str, Any], settings: Mapping[str, Any]
) -> DataSchema | None:
    """The model's input schema for ``settings`` (``None`` when it cannot be resolved)."""
    tmpl = (
        template
        if isinstance(template, DataSchemaTemplate)
        else DataSchemaTemplate.model_validate(dict(template))
    )
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
    # other roles: a model variable with one same-role variable in the data (``spend`` ->
    # ``spend_local``)
    for canon, spec in model.vars.items():
        if spec.role is Role.TARGET or canon in vars_ or canon in data.vars:
            continue
        same = [
            v
            for v, s in data.vars.items()
            if s.role is spec.role and v not in used and v not in model.vars
        ]
        if len(same) == 1:
            vars_[canon] = same[0]
            used.add(same[0])
    # categorical dims (a panel's ``geo``, a multivariate model's ``variable``): the one
    # unbound, non-time dim of the data variables bound to the model variables that carry it
    for canon, dspec in model.dims.items():
        if dspec.kind == "time" or canon in dims or canon in data.dims:
            continue
        carriers = [vars_.get(v, v) for v, s in model.vars.items() if canon in s.dims]
        free_dims = {
            str(d)
            for v in carriers
            if v in data.vars
            for d in data.vars[v].dims
            if data.dims.get(d) is not None
            and data.dims[d].kind != "time"
            and d not in used
            and d not in model.dims
        }
        if len(free_dims) == 1:
            (d,) = free_dims
            dims[canon] = d
            used.add(d)
    out: dict[str, dict[str, str]] = {}
    if dims:
        out["dims"] = {str(k): str(v) for k, v in dims.items()}
    if vars_:
        out["vars"] = {str(k): str(v) for k, v in vars_.items()}
    return out


def canonical_split(
    split: Mapping[str, Any] | None, bind: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """``split`` (written with the dataset's dim names, as split nodes write it) in the
    model's canonical names under ``bind`` (a spec's split names canonical dims)."""
    if split is None:
        return None
    inv = {str(v): str(k) for k, v in ((bind or {}).get("dims") or {}).items()}
    if not inv:
        return dict(split)
    out = dict(split)
    if isinstance(out.get("dim"), str):
        out["dim"] = inv.get(out["dim"], out["dim"])
    for key in ("within", "dims"):
        if isinstance(out.get(key), list):
            out[key] = [inv.get(d, d) if isinstance(d, str) else d for d in out[key]]
    if isinstance(out.get("block"), dict):
        out["block"] = {inv.get(str(k), str(k)): v for k, v in out["block"].items()}
    return out


def canonical_steps(
    pipeline: Sequence[Mapping[str, Any]], bind: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """Pipeline steps written with the dataset's names (the steps of upstream transform
    nodes) in the model's canonical names under ``bind``: a setting whose value (or list
    item) is a bound dataset dim or variable becomes the canonical name."""
    inv = {str(v): str(k) for k, v in ((bind or {}).get("dims") or {}).items()}
    inv |= {str(v): str(k) for k, v in ((bind or {}).get("vars") or {}).items()}
    out: list[dict[str, Any]] = []
    for step in pipeline:
        doc = dict(step)
        params = doc.get("with")
        if inv and isinstance(params, Mapping):
            new: dict[str, Any] = {}
            for k, v in params.items():
                if isinstance(v, str):
                    new[k] = inv.get(v, v)
                elif isinstance(v, list):
                    new[k] = [inv.get(x, x) if isinstance(x, str) else x for x in v]
                else:
                    new[k] = v
            doc["with"] = new
        out.append(doc)
    return out
