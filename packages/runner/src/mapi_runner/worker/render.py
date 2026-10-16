"""``render``: static PNG, SVG or PDF images of sanitized plots (plan §10.3, point 5).

Vega-Lite plots render with ``vl-convert-python`` (a pinned dependency of the runner image)
and Plotly plots with ``kaleido``, which also needs a Chrome/Chromium on the machine; the
base images ship neither, so Plotly renders fail with ``MODE_NOT_SUPPORTED`` (and the API
refuses them up front unless the platform enables them).

Rendering never fetches anything:

* every plot is re-sanitized, then checked again here for anything that could name a
  resource (``url``/``href`` keys, image marks, Plotly layout images);
* datasets are passed inline. ``inline_arrow`` datasets are decoded from the plot itself; a
  ``data_ref`` dataset whose path is ``plotdata.<n>`` is the run input of that name (the
  control plane pinned the bundle object), anything else is refused;
* vl-convert runs with ``allowed_base_urls=[]``, so even a loader request is an error.

``RenderJob.width``/``height`` (CSS pixels, before ``scale``) override the plot's size. The
raster is capped at :data:`mapi_spec.sharing.MAX_RENDER_PIXELS` *before* anything is drawn
(Vega-Lite: the laid-out SVG's size, so composite views and the plot's own size count;
Plotly: the figure's size), and each image at :data:`MAX_IMAGE_BYTES`.
"""

from __future__ import annotations

import base64
import copy
import importlib
import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
from mapi_plots.sanitize import SanitizeError, sanitize_plot
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.jobs import INPUT_PLOTS, PLOT_DATA_INPUT_PREFIX, RenderJob, render_output
from mapi_spec.plots import PlotSpec
from mapi_spec.run import RunnerPhase
from mapi_spec.sharing import check_render_pixels
from pydantic import ValidationError

from mapi_runner.worker.context import WorkerContext

__all__ = [
    "MAX_IMAGE_BYTES",
    "find_chrome",
    "find_fetching_constructs",
    "render_plot",
    "run_render",
]

_MAX_PLOTS_JSON: Final = 64 * 1024 * 1024
MAX_DATA_BYTES: Final = 256 * 1024 * 1024
MAX_IMAGE_BYTES: Final = 32 * 1024 * 1024
_MAX_ROWS: Final = 2_000_000
_COMPOSITE = ("hconcat", "vconcat", "concat", "facet", "repeat")
_SVG_TAG = re.compile(r"<svg\b[^>]*>", re.S)
_PLOTLY_DEFAULT_SIZE: Final = (700, 500)
_FETCH_KEYS = frozenset({"url", "href", "src", "images", "mapbox", "map"})
_PLOTLY_TILE_TRACES = frozenset(
    {
        "image",
        "scattermapbox",
        "choroplethmapbox",
        "densitymapbox",
        "scattermap",
        "choroplethmap",
        "densitymap",
    }
)
_CHROME_NAMES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)
_CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)

LoadData = Callable[[str], bytes]
"""Bytes of the run input a ``data_ref`` dataset names (``plotdata.<n>``)."""


def _unsafe(detail: str, pointer: str) -> MapiError:
    return MapiError(detail, code=ErrorCode.PLOT_UNSAFE, pointer=pointer)


def find_fetching_constructs(kind: str, spec: Any) -> list[str]:
    """JSON pointers of anything that could make a renderer fetch a resource: ``url`` or
    ``href`` keys, Vega-Lite ``image`` marks, Plotly layout images and tile maps. The plot
    sanitizer refuses all of these already; this is the renderer's own last check."""
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}"
                if key in _FETCH_KEYS:
                    found.append(here)
                    continue
                if key in ("mark", "type") and (
                    value == "image" or (isinstance(value, dict) and value.get("type") == "image")
                ):
                    found.append(here)
                if kind == "plotly" and (
                    key == "source" or (key == "type" and value in _PLOTLY_TILE_TRACES)
                ):
                    found.append(here)
                walk(value, here)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}/{i}")

    walk(spec, "/spec")
    return found


def _table(raw: bytes, fmt: str, name: str) -> list[dict[str, Any]]:
    if fmt == "json":
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MapiError(f"dataset {name!r} is not JSON", code=ErrorCode.SCHEMA_INVALID) from exc
        if not isinstance(parsed, list) or not all(isinstance(r, dict) for r in parsed):
            raise MapiError(
                f"dataset {name!r} must be a JSON array of objects",
                code=ErrorCode.SCHEMA_INVALID,
            )
        return parsed
    try:
        table = pa.ipc.open_stream(pa.BufferReader(raw)).read_all()
    except pa.ArrowInvalid:
        try:
            table = pa.ipc.open_file(pa.BufferReader(raw)).read_all()
        except pa.ArrowInvalid as exc:
            raise MapiError(
                f"dataset {name!r} is not an Arrow IPC stream", code=ErrorCode.SCHEMA_INVALID
            ) from exc
    if table.num_rows > _MAX_ROWS:
        raise MapiError(
            f"dataset {name!r} has {table.num_rows} rows; static rendering takes at most "
            f"{_MAX_ROWS}",
            code=ErrorCode.PAYLOAD_TOO_LARGE,
        )
    rows: list[dict[str, Any]] = table.to_pylist()
    return rows


def _rows(ref: dict[str, Any], name: str, load: LoadData | None) -> list[dict[str, Any]]:
    kind = ref.get("kind")
    if kind == "inline_arrow":
        return _table(base64.b64decode(str(ref["data"])), "arrow", name)
    path = str(ref.get("path", ""))
    if kind == "data_ref" and path.startswith(PLOT_DATA_INPUT_PREFIX) and load is not None:
        return _table(load(path), str(ref.get("format", "arrow")), name)
    raise NotImplementedError(
        f"dataset {name!r} is a data_ref to {path!r}; render runs read only inline datasets "
        "and the plotdata.<n> inputs the control plane pins"
    )


def _jsonable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows as JSON values (datetimes as ISO strings, as the plot sandbox does)."""
    out: list[dict[str, Any]] = json.loads(json.dumps(rows, default=_json_default))
    return out


def _json_default(value: Any) -> Any:
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return iso()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return str(value)


def _sized_vega_lite(spec: dict[str, Any], width: int | None, height: int | None) -> None:
    if width is None and height is None:
        return
    if any(k in spec for k in _COMPOSITE):  # composite views size their cells
        config = spec.setdefault("config", {})
        view = config.setdefault("view", {}) if isinstance(config, dict) else {}
        if isinstance(view, dict):
            if width is not None:
                view["continuousWidth"] = width
            if height is not None:
                view["continuousHeight"] = height
        return
    if width is not None:
        spec["width"] = width
    if height is not None:
        spec["height"] = height
    spec.setdefault("autosize", {"type": "fit", "contains": "padding"})


def _vega_lite(
    plot: PlotSpec,
    fmt: str,
    scale: float,
    width: int | None,
    height: int | None,
    load: LoadData | None,
) -> bytes:
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
        inline[name] = _jsonable(_rows(ref.model_dump(mode="json"), name, load))
    spec["datasets"] = inline
    _sized_vega_lite(spec, width, height)
    no_network: dict[str, Any] = {"allowed_base_urls": []}
    try:
        svg = str(vlc.vegalite_to_svg(spec, **no_network))
        if fmt == "svg":
            return svg.encode("utf-8")
        # the laid-out size (composite cells, the plot's own width/height) before rasterising
        w, h = _svg_size(svg)
        check_render_pixels(w, h, scale)
        if fmt == "pdf":
            return bytes(vlc.vegalite_to_pdf(spec, scale=scale, **no_network))
        return bytes(vlc.vegalite_to_png(spec, scale=scale, **no_network))
    except ValueError as exc:  # vl-convert reports compile and render errors as ValueError
        raise MapiError(
            "the Vega-Lite renderer could not draw this plot",
            code=ErrorCode.PLOT_UNSAFE if "not allowed" in str(exc) else ErrorCode.SCHEMA_INVALID,
            untrusted_text=str(exc)[:4000],
        ) from None


def _svg_size(svg: str) -> tuple[float, float]:
    """``(width, height)`` of an SVG's root element (CSS pixels)."""
    tag = _SVG_TAG.search(svg)
    found: dict[str, float] = {}
    for key in ("width", "height"):
        m = re.search(rf'\s{key}="([0-9.]+)', tag.group(0)) if tag else None
        if m is None:
            raise MapiError(
                "the Vega-Lite renderer did not report the plot's size",
                code=ErrorCode.SCHEMA_INVALID,
            )
        found[key] = float(m.group(1))
    return found["width"], found["height"]


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


def find_chrome() -> str | None:
    """A Chrome/Chromium executable on this machine (``BROWSER_PATH`` first), or ``None``.

    Nothing is ever downloaded: kaleido's own browser download is not used."""
    explicit = os.environ.get("BROWSER_PATH")
    if explicit and Path(explicit).is_file():
        return explicit
    for name in _CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for path in _CHROME_PATHS:
        if Path(path).is_file():
            return path
    return None


def _plotly(
    plot: PlotSpec,
    fmt: str,
    scale: float,
    width: int | None,
    height: int | None,
    load: LoadData | None,
) -> bytes:
    layout = plot.spec.get("layout") if isinstance(plot.spec, dict) else None
    own = layout if isinstance(layout, dict) else {}

    def size(override: int | None, key: str, default: int) -> float:
        if override is not None:
            return float(override)
        v = own.get(key)
        return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else default

    if fmt != "svg":
        check_render_pixels(
            size(width, "width", _PLOTLY_DEFAULT_SIZE[0]),
            size(height, "height", _PLOTLY_DEFAULT_SIZE[1]),
            scale,
        )
    chrome = find_chrome()
    try:
        importlib.import_module("kaleido")
        pio = importlib.import_module("plotly.io")
    except ImportError:
        chrome = None
    if chrome is None:
        raise MapiError(
            "static rendering of Plotly plots is not available in this runner image (it "
            "needs kaleido and a Chrome/Chromium); render a Vega-Lite plot instead",
            code=ErrorCode.MODE_NOT_SUPPORTED,
        )
    os.environ.setdefault("BROWSER_PATH", chrome)
    tables = {
        name: _jsonable(_rows(ref.model_dump(mode="json"), name, load))
        for name, ref in plot.datasets.items()
    }
    fig = _resolve_refs(plot.spec, tables)
    return bytes(pio.to_image(fig, format=fmt, scale=scale, width=width, height=height))


def render_plot(
    plot: PlotSpec,
    fmt: str,
    *,
    scale: float = 2.0,
    width: int | None = None,
    height: int | None = None,
    load: LoadData | None = None,
) -> bytes:
    """One sanitized plot as PNG, SVG or PDF bytes (raises :class:`MapiError` with a stable
    code for anything the platform refuses to draw)."""
    fetching = find_fetching_constructs(plot.kind, plot.spec)
    if fetching:
        raise _unsafe(
            f"the plot names external resources ({', '.join(fetching[:5])}); static "
            "rendering never fetches anything",
            fetching[0],
        )
    render = _vega_lite if plot.kind == "vega-lite" else _plotly
    image = render(plot, fmt, scale, width, height, load)
    if len(image) > MAX_IMAGE_BYTES:
        raise MapiError(
            f"the rendered image is larger than {MAX_IMAGE_BYTES} bytes; lower the scale or "
            "the size",
            code=ErrorCode.PAYLOAD_TOO_LARGE,
        )
    return image


def _loader(ctx: WorkerContext) -> LoadData:
    def load(name: str) -> bytes:
        if not ctx.has_input(name):
            raise MapiError(
                f"the render run has no input {name!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/inputs/{name}",
            )
        path = ctx.download(name)
        assert path is not None
        if path.stat().st_size > MAX_DATA_BYTES:
            raise MapiError(f"input {name!r} is too large", code=ErrorCode.PAYLOAD_TOO_LARGE)
        return path.read_bytes()

    return load


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
    load = _loader(ctx)
    written: list[str] = []
    for i, pid in enumerate(wanted):
        ctx.cancel.check()
        image = render_plot(
            plots[pid],
            job.format,
            scale=job.scale,
            width=job.width,
            height=job.height,
            load=load,
        )
        name = render_output(pid, job.format)
        ctx.write_output(name, image)
        written.append(name)
        ctx.progress((i + 1) / len(wanted), f"rendered {pid}")
    return written
