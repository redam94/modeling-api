"""``render``: static PNG/SVG images of sanitized plots (plan §10.3, point 5).

Vega-Lite plots render with ``vl-convert-python`` and Plotly plots with ``kaleido``; both are
optional dependencies of the runner image. Without them the run fails with
``NotImplementedError`` (reported as ``CAPABILITY_UNSUPPORTED``). Plots are re-sanitized
before rendering, and only inline datasets are supported (``data_ref`` datasets would need
their own inputs).
"""

from __future__ import annotations

import base64
import copy
import importlib
import json
from typing import Any

import pyarrow as pa
from mapi_plots.sanitize import SanitizeError, sanitize_plot
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.plots import PlotSpec
from mapi_spec.run import RunnerPhase
from pydantic import ValidationError

from mapi_runner.jobs import INPUT_PLOTS, RenderJob
from mapi_runner.worker.context import WorkerContext

__all__ = ["run_render"]

_MAX_PLOTS_JSON = 64 * 1024 * 1024


def _rows(ref: dict[str, Any], name: str) -> list[dict[str, Any]]:
    if ref.get("kind") != "inline_arrow":
        raise NotImplementedError(
            f"dataset {name!r} is a data_ref; static rendering supports inline datasets only"
        )
    raw = base64.b64decode(str(ref["data"]))
    try:
        table = pa.ipc.open_stream(pa.BufferReader(raw)).read_all()
    except pa.ArrowInvalid:
        table = pa.ipc.open_file(pa.BufferReader(raw)).read_all()
    rows: list[dict[str, Any]] = table.to_pylist()
    return rows


def _vega_lite(plot: PlotSpec, fmt: str, scale: float) -> bytes:
    try:
        vlc = importlib.import_module("vl_convert")
    except ImportError:
        raise NotImplementedError(
            "rendering Vega-Lite plots needs vl-convert-python in the runner image"
        ) from None
    spec: dict[str, Any] = copy.deepcopy(dict(plot.spec))
    given = spec.get("datasets")
    inline: dict[str, Any] = dict(given) if isinstance(given, dict) else {}
    for name, ref in plot.datasets.items():
        inline[name] = _rows(ref.model_dump(mode="json"), name)
    spec["datasets"] = inline
    if fmt == "svg":
        return str(vlc.vegalite_to_svg(spec)).encode("utf-8")
    if fmt == "pdf":  # M3 contract: RenderJob.format may be pdf
        return bytes(vlc.vegalite_to_pdf(spec, scale=scale))
    return bytes(vlc.vegalite_to_png(spec, scale=scale))


def _resolve_refs(node: Any, tables: dict[str, list[dict[str, Any]]]) -> Any:
    if isinstance(node, dict):
        if set(node) == {"$ref"} and isinstance(node["$ref"], str):
            ds, _, col = node["$ref"].partition(".")
            rows = tables.get(ds)
            if rows is None:
                raise MapiError(
                    f"plot references unknown dataset {ds!r}", code=ErrorCode.PLOT_UNSAFE
                )
            return [r.get(col) for r in rows]
        return {k: _resolve_refs(v, tables) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_refs(v, tables) for v in node]
    return node


def _plotly(plot: PlotSpec, fmt: str, scale: float) -> bytes:
    try:
        importlib.import_module("kaleido")
        pio = importlib.import_module("plotly.io")
    except ImportError:
        raise NotImplementedError(
            "rendering Plotly plots needs kaleido in the runner image"
        ) from None
    tables = {name: _rows(ref.model_dump(mode="json"), name) for name, ref in plot.datasets.items()}
    fig = _resolve_refs(plot.spec, tables)
    return bytes(pio.to_image(fig, format=fmt, scale=scale))


def run_render(ctx: WorkerContext, job: RenderJob) -> list[str]:
    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading plots")
    path = ctx.download(INPUT_PLOTS)
    assert path is not None
    raw = path.read_bytes()
    if len(raw) > _MAX_PLOTS_JSON:
        raise MapiError("plots.json is too large", code=ErrorCode.PAYLOAD_TOO_LARGE)
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MapiError("plots.json is not JSON", code=ErrorCode.SCHEMA_INVALID) from exc
    if not isinstance(items, list):
        raise MapiError("plots.json must hold a list of plots", code=ErrorCode.SCHEMA_INVALID)
    plots: dict[str, PlotSpec] = {}
    for i, item in enumerate(items):
        try:
            plot = sanitize_plot(PlotSpec.model_validate(item)).plot
        except (ValidationError, SanitizeError) as exc:
            raise MapiError(
                f"plot {i} is invalid or unsafe",
                code=ErrorCode.PLOT_UNSAFE,
                pointer=f"/plots/{i}",
                untrusted_text=str(exc)[:4000],
            ) from None
        plots[plot.id] = plot
    wanted = job.plots if job.plots is not None else list(plots)
    missing = [p for p in wanted if p not in plots]
    if missing:
        raise MapiError(f"no plot {missing[0]!r} in plots.json", code=ErrorCode.NOT_FOUND)
    ctx.state(RunnerPhase.PLOTS, f"rendering {len(wanted)} plot(s) as {job.format}")
    written: list[str] = []
    for i, pid in enumerate(wanted):
        ctx.cancel.check()
        plot = plots[pid]
        render = _vega_lite if plot.kind == "vega-lite" else _plotly
        image = render(plot, job.format, job.scale)
        name = f"renders/{pid}.{job.format}"
        ctx.write_output(name, image)
        written.append(name)
        ctx.progress((i + 1) / len(wanted), f"rendered {pid}")
    return written
