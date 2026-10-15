"""Precomputed UI views of a fitted bundle (plan §8.6 ``ui/``) and plot sidecars.

* ``ui/thin.zarr.zip`` - the draw groups restricted to the first ``outputs.ui_draws`` pairs
  of the bundle thinning index (a ``sample`` dim), so the browser never needs the full trace;
* ``ui/summaries.json`` - per-parameter summaries (mean, sd, 90% HDI, R-hat, bulk ESS);
* ``ui/tree_index.json`` - the bundle tree's structure (groups, dims, variables, attrs);
* ``ui/grids/<name>.arrow`` - fit-cached derived quantities (response-curve grids per draw)
  as tidy Arrow tables for instant what-if sliders;
* ``ui/plotdata/<plot>.<dataset>.arrow`` - large inline plot datasets moved out of
  ``plots.json`` and referenced by path.
"""

from __future__ import annotations

import base64
import math
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_plots.recipes._stats import ess_bulk, rhat
from mapi_sdk.bundle import Bundle
from mapi_sdk.errors import PayloadLimitError
from mapi_sdk.summarise import hdi, select_draws
from mapi_sdk.wire import to_arrow_table, write_arrow_ipc, write_datatree_zip
from mapi_spec.storage import crc32c, crc32c_to_base64
from mapi_spec.views import (
    MAX_TREE_COORD_VALUES,
    TreeCoord,
    TreeGroup,
    TreeIndexView,
    TreeVar,
    ViewScalar,
)

from mapi_runner.worker.context import WorkerContext

__all__ = ["externalize_plot_data", "tree_index", "write_ui_views"]

INLINE_LIMIT: Final = 64 * 1024
"""Inline plot datasets above this many base64 characters move to ``ui/plotdata``."""
MAX_SUMMARY_CELLS: Final = 20_000
_DRAW_DIMS: Final = {"chain", "draw"}


def _draw_groups(bundle: Bundle) -> dict[str, xr.Dataset]:
    return {
        name: ds for name, ds in bundle.model.groups.items() if set(map(str, ds.dims)) >= _DRAW_DIMS
    }


def write_ui_views(ctx: WorkerContext, bundle: Bundle) -> list[str]:
    """Write every ``ui/`` view; returns the names written."""
    written: list[str] = []
    ui_draws = int(bundle.spec.outputs.ui_draws)
    groups = _draw_groups(bundle)
    if groups and bundle.thin is not None and ui_draws > 0:
        nodes: dict[str, xr.Dataset] = {
            "/": xr.Dataset(attrs={"bundle_id": bundle.bundle_id, "ui_draws": ui_draws})
        }
        for name, ds in groups.items():
            nodes[f"/{name}"] = select_draws(ds, bundle.thin, ui_draws)
        raw = write_datatree_zip(xr.DataTree.from_dict(nodes))
        assert raw is not None
        ctx.write_output("ui/thin.zarr.zip", raw)
        written.append("ui/thin.zarr.zip")
    ctx.write_json("ui/summaries.json", summaries(bundle))
    written.append("ui/summaries.json")
    ctx.write_json("ui/tree_index.json", tree_index(bundle.to_tree()))
    written.append("ui/tree_index.json")
    for name, derived in sorted(bundle.model.derived.items()):
        try:
            data = write_arrow_ipc(to_arrow_table(derived))
        except PayloadLimitError as exc:
            ctx.warning("VIEW_TOO_LARGE", f"grid {name!r} was not written: {exc.detail}")
            continue
        out = f"ui/grids/{_safe(name)}.arrow"
        ctx.write_output(out, data)
        written.append(out)
    return written


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "_.-" else "_" for c in name)[:100] or "x"


def _finite(x: float) -> float | None:
    return x if math.isfinite(x) else None


def summaries(bundle: Bundle) -> dict[str, Any]:
    """Per-cell posterior summaries (capped at :data:`MAX_SUMMARY_CELLS` rows)."""
    rows: list[dict[str, Any]] = []
    post = bundle.model.groups.get("posterior")
    truncated = False
    if post is not None and set(map(str, post.dims)) >= _DRAW_DIMS:
        for var in sorted(map(str, post.data_vars)):
            da = post[var].transpose("chain", "draw", ...)
            extra = [str(d) for d in da.dims[2:]]
            values = np.asarray(da.values, dtype=float)
            cells = int(np.prod(values.shape[2:])) if extra else 1
            flat = values.reshape(values.shape[0], values.shape[1], cells)
            for i in range(cells):
                if len(rows) >= MAX_SUMMARY_CELLS:
                    truncated = True
                    break
                x = flat[:, :, i]
                lo, hi = hdi(x.reshape(-1), 0.9)
                coords: dict[str, Any] = {}
                if extra:
                    idx = np.unravel_index(i, values.shape[2:])
                    for d, j in zip(extra, idx, strict=True):
                        label = da[d].values[j] if d in da.coords else int(j)
                        coords[d] = label.item() if hasattr(label, "item") else label
                rows.append(
                    {
                        "var": var,
                        "coords": {
                            k: str(v) if isinstance(v, np.datetime64) else v
                            for k, v in coords.items()
                        },
                        "mean": _finite(float(np.nanmean(x))),
                        "sd": _finite(float(np.nanstd(x))),
                        "hdi_90_lower": _finite(float(lo)),
                        "hdi_90_upper": _finite(float(hi)),
                        "r_hat": _finite(rhat(x)) if x.shape[0] > 1 else None,
                        "ess_bulk": _finite(ess_bulk(x)),
                    }
                )
    return {
        "bundle_id": bundle.bundle_id,
        "representation": bundle.model.manifest.capabilities.representation.value,
        "rows": rows,
        "truncated": truncated,
        "metrics": bundle.metrics.values,
    }


def _scalar(v: Any) -> ViewScalar:
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, np.datetime64):
        return str(np.datetime_as_string(v))
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, bool | int | str) or v is None:
        return v[:1000] if isinstance(v, str) else v
    return str(v)[:1000]


def _coord_values(values: np.ndarray) -> list[ViewScalar] | None:
    if values.ndim != 1 or values.size > MAX_TREE_COORD_VALUES:
        return None
    if np.issubdtype(values.dtype, np.datetime64):
        return [str(x) for x in np.datetime_as_string(values)]
    return [_scalar(x) for x in values.tolist()]


def tree_index(tree: xr.DataTree) -> dict[str, Any]:
    """``ui/tree_index.json`` (:class:`~mapi_spec.views.TreeIndexView`): every node's dims,
    coordinates (small ones with values), variables and scalar attributes; no arrays."""
    groups: dict[str, TreeGroup] = {}
    for node in tree.subtree:
        ds = node.to_dataset(inherit=False)
        groups[node.path] = TreeGroup(
            dims={str(k): int(v) for k, v in ds.sizes.items()},
            coords={
                str(name): TreeCoord(
                    dims=[str(d) for d in c.dims],
                    dtype=str(c.dtype),
                    size=int(c.size),
                    values=_coord_values(np.asarray(c.values)),
                )
                for name, c in ds.coords.items()
            },
            vars={
                str(name): TreeVar(
                    dims=[str(d) for d in var.dims],
                    shape=[int(n) for n in var.shape],
                    dtype=str(var.dtype),
                    units=var.attrs["units"] if isinstance(var.attrs.get("units"), str) else None,
                    description=var.attrs["description"][:2000]
                    if isinstance(var.attrs.get("description"), str)
                    else None,
                )
                for name, var in ds.data_vars.items()
            },
            attrs={
                str(k): _scalar(v)
                for k, v in node.attrs.items()
                if isinstance(v, str | int | float | bool | np.generic)
            },
        )
    return TreeIndexView(groups=groups).model_dump(mode="json")


def externalize_plot_data(
    ctx: WorkerContext, plots: list[dict[str, Any]], *, prefix: str = ""
) -> list[dict[str, Any]]:
    """Move large inline Arrow datasets of ``plots`` (``plots.json`` bodies) to files."""
    out: list[dict[str, Any]] = []
    for plot in plots:
        datasets = dict(plot.get("datasets") or {})
        for name, ref in list(datasets.items()):
            if not isinstance(ref, dict) or ref.get("kind") != "inline_arrow":
                continue
            b64 = str(ref.get("data", ""))
            if len(b64) <= INLINE_LIMIT:
                continue
            raw = base64.b64decode(b64)
            path = f"ui/plotdata/{_safe(str(plot.get('id', 'plot')))}.{_safe(name)}.arrow"
            ctx.write_output(prefix + path, raw)
            datasets[name] = {
                "kind": "data_ref",
                "path": path,
                "format": "arrow",
                "size": len(raw),
                "crc32c": crc32c_to_base64(crc32c(raw)),
            }
        out.append({**plot, "datasets": datasets})
    return out
