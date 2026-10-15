"""Build :class:`~mapi_spec.plots.PlotSpec` envelopes from Altair charts and Plotly figures.

Extra ``recipes`` only (needs altair, plotly, numpy, pandas and pyarrow).

* :func:`from_altair` moves every inline dataset (Altair's consolidated ``datasets`` and any
  ``data.values``) into named Arrow datasets and rewrites the references to
  ``{"name": ...}``; named data (``alt.Data(name=...)``) is filled from ``datasets=``.
* :func:`from_plotly` decodes plotly.py 6 typed arrays (``{dtype, bdata, shape}``) and inline
  data arrays into named Arrow datasets, one per trace (and array length), and binds them with
  ``{"$ref": "<dataset>.<column>"}``. String columns get the inline-text rules here (HTML
  stripped to the Plotly allow-list, script/CSS URLs rejected) because the control-plane
  sanitizer never decodes Arrow; the plot iframe must still re-strip dataset strings.

Both run the result through the sanitizer unless ``sanitize=False``. Datasets are base64 Arrow
IPC streams (:class:`~mapi_spec.plots.InlineArrow`); datetimes are stored as UTC
millisecond timestamps, which JavaScript reads as numbers.
"""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.ipc as ipc
from mapi_spec.plots import (
    MAX_INLINE_ARROW_B64,
    InlineArrow,
    PlotBindings,
    PlotProvenance,
    PlotSpec,
)

from mapi_plots.sanitize import sanitize_plot
from mapi_plots.sanitize._common import SanitizeReport, scan_string
from mapi_plots.sanitize.html import ALLOWED_TAGS, HOVERTEMPLATE_TAGS, strip_html
from mapi_plots.sanitize.plotly import PLOTLY_JS_VERSION, TYPED_ARRAY_DTYPES, plotly_structure
from mapi_plots.sanitize.vegalite import VEGA_LITE_VERSION

__all__ = [
    "arrow_to_frame",
    "decode_typed_array",
    "frame_to_arrow",
    "from_altair",
    "from_plotly",
    "make_provenance",
]

Persona = Literal["guided", "expert", "both"]
type TableLike = pd.DataFrame | pa.Table

_NUMPY_DTYPES: dict[str, str] = {
    "i1": "<i1",
    "u1": "<u1",
    "u1c": "<u1",
    "i2": "<i2",
    "u2": "<u2",
    "i4": "<i4",
    "u4": "<u4",
    "f4": "<f4",
    "f8": "<f8",
}
assert set(_NUMPY_DTYPES) == set(TYPED_ARRAY_DTYPES)


# ------------------------------------------------------------------------------------ Arrow


def _compact(col: pa.ChunkedArray) -> pa.ChunkedArray:
    """float64 -> float32, small int64 -> int32, repetitive strings -> dictionary."""
    t = col.type
    if pa.types.is_float64(t):
        return col.cast(pa.float32())
    if pa.types.is_int64(t) and len(col):
        lo, hi = pc.min(col).as_py(), pc.max(col).as_py()
        if lo is not None and lo >= -(2**31) and hi < 2**31:
            return col.cast(pa.int32())
        return col
    is_text = pa.types.is_string(t) or pa.types.is_large_string(t)
    if is_text and len(col) > 16 and pc.count_distinct(col).as_py() * 4 <= len(col):
        return col.dictionary_encode()
    return col


def _to_table(data: TableLike, compact: bool = False) -> pa.Table:
    if isinstance(data, pa.Table):
        table = data
    else:
        frame = data.reset_index(drop=True)
        for col in frame.columns:
            if isinstance(frame[col].dtype, pd.CategoricalDtype):
                frame[col] = frame[col].astype(str)
        table = pa.Table.from_pandas(frame, preserve_index=False)
    fields = []
    arrays = []
    for f, col in zip(table.schema, table.columns, strict=True):
        if pa.types.is_timestamp(f.type):
            if f.type.tz is not None:
                col = col.cast(pa.timestamp(f.type.unit, tz="UTC"))
            col = col.cast(pa.timestamp("ms", tz=col.type.tz), safe=False)
            f = pa.field(f.name, col.type)
        elif pa.types.is_dictionary(f.type):
            col = col.cast(f.type.value_type)
            f = pa.field(f.name, col.type)
        if compact:
            col = _compact(col)
            f = pa.field(f.name, col.type)
        fields.append(pa.field(str(f.name), f.type))
        arrays.append(col)
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


def frame_to_arrow(data: TableLike, *, compact: bool = False) -> InlineArrow:
    """Encode a DataFrame or Arrow table as a base64 Arrow IPC stream (InlineArrow).

    ``compact`` stores float64 as float32, small int64 as int32 and repetitive strings as
    dictionaries - plenty for display and roughly half the bytes.
    """
    table = _to_table(data, compact)
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    b64 = base64.b64encode(sink.getvalue()).decode("ascii")
    if len(b64) > MAX_INLINE_ARROW_B64:
        raise ValueError(
            f"dataset is {len(b64)} bytes as base64 Arrow; the inline limit is "
            f"{MAX_INLINE_ARROW_B64}. Downsample (fewer draws/points) before plotting."
        )
    return InlineArrow(kind="inline_arrow", data=b64, rows=table.num_rows)


def arrow_to_frame(data: InlineArrow | str) -> pd.DataFrame:
    """Decode an InlineArrow (or its base64 text) back into a DataFrame."""
    text = data.data if isinstance(data, InlineArrow) else data
    reader = ipc.open_stream(base64.b64decode(text))
    frame: pd.DataFrame = reader.read_all().to_pandas()
    return frame


def decode_typed_array(obj: Mapping[str, Any]) -> np.ndarray:
    """Decode a plotly.py 6 typed array ``{dtype, bdata, shape?}`` into a numpy array."""
    dtype = obj.get("dtype")
    if dtype not in _NUMPY_DTYPES or not isinstance(obj.get("bdata"), str):
        raise ValueError(f"not a typed array: dtype={dtype!r}")
    arr = np.frombuffer(base64.b64decode(obj["bdata"], validate=True), dtype=_NUMPY_DTYPES[dtype])
    shape = obj.get("shape")
    if shape is not None:
        dims = (
            [int(p) for p in shape.split(",")]
            if isinstance(shape, str)
            else [int(p) for p in shape]
        )
        arr = arr.reshape(dims)
    return arr.astype(arr.dtype.newbyteorder("="))


# ------------------------------------------------------------------------------- envelope


def make_provenance(
    *,
    recipe: str | None = None,
    plugin: str | None = None,
    author: str | None = None,
    bundle_id: str | None = None,
    generated_at: datetime | None = None,
) -> PlotProvenance:
    """Provenance with exactly one source; defaults to ``author="notebook"``."""
    if recipe is None and plugin is None and author is None:
        author = "notebook"
    return PlotProvenance(
        bundle_id=bundle_id,
        recipe=recipe,
        plugin=plugin,
        author=author,
        generated_at=generated_at or datetime.now(UTC),
    )


def _finish(
    *,
    id: str,
    title: str,
    kind: Literal["vega-lite", "plotly"],
    spec: dict[str, Any],
    datasets: dict[str, TableLike],
    schema_version: str,
    persona: Persona,
    caption_plain: str | None,
    bindings: PlotBindings | None,
    provenance: PlotProvenance,
    sanitize: bool,
    compact: bool = False,
) -> PlotSpec:
    arrows = {name: frame_to_arrow(df, compact=compact) for name, df in datasets.items()}
    if not sanitize:
        return PlotSpec(
            id=id,
            title=title,
            kind=kind,
            spec=spec,
            datasets=arrows,
            bindings=bindings,
            schema_version=schema_version,
            persona=persona,
            caption_plain=caption_plain,
            provenance=provenance,
        )
    # validate as a plain document so every problem surfaces as a typed SanitizeError
    doc: dict[str, Any] = {
        "id": id,
        "title": title,
        "kind": kind,
        "spec": spec,
        "datasets": {k: v.model_dump(exclude_none=True) for k, v in arrows.items()},
        "schema_version": schema_version,
        "persona": persona,
        "caption_plain": caption_plain,
        "provenance": provenance.model_dump(mode="json", exclude_none=True),
    }
    if bindings is not None:
        doc["bindings"] = bindings.model_dump(exclude_none=True)
    return sanitize_plot(doc).plot


# --------------------------------------------------------------------------------- altair


def _rename_data_refs(node: Any, mapping: Mapping[str, str]) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "data" and isinstance(v, dict) and v.get("name") in mapping:
                v = {**v, "name": mapping[v["name"]]}
            out[k] = _rename_data_refs(v, mapping)
        return out
    if isinstance(node, list):
        return [_rename_data_refs(v, mapping) for v in node]
    return node


def _values_frame(values: list[Any]) -> pd.DataFrame:
    if all(isinstance(v, Mapping) for v in values):
        return pd.DataFrame.from_records(values)
    return pd.DataFrame({"data": values})


def _lift_inline_values(node: Any, out: dict[str, pd.DataFrame], counter: list[int]) -> Any:
    if isinstance(node, dict):
        new = {}
        for k, v in node.items():
            if k == "data" and isinstance(v, dict) and isinstance(v.get("values"), list):
                name = f"values_{counter[0]}"
                counter[0] += 1
                out[name] = _values_frame(v["values"])
                rest = {kk: vv for kk, vv in v.items() if kk not in ("values", "format")}
                new[k] = {**rest, "name": name}
            else:
                new[k] = _lift_inline_values(v, out, counter)
        return new
    if isinstance(node, list):
        return [_lift_inline_values(v, out, counter) for v in node]
    return node


def from_altair(
    chart: Any,
    *,
    id: str,
    title: str | None = None,
    datasets: Mapping[str, TableLike] | None = None,
    persona: Persona = "both",
    caption_plain: str | None = None,
    bindings: PlotBindings | None = None,
    recipe: str | None = None,
    plugin: str | None = None,
    author: str | None = None,
    bundle_id: str | None = None,
    sanitize: bool = True,
    compact: bool = False,
) -> PlotSpec:
    """Convert an Altair chart (or a Vega-Lite dict) into a sanitized PlotSpec."""
    spec: dict[str, Any] = chart if isinstance(chart, dict) else chart.to_dict()
    spec = json.loads(json.dumps(spec))  # detach from Altair objects; plain JSON
    tables: dict[str, TableLike] = dict(datasets or {})
    inline = spec.pop("datasets", None) or {}
    mapping: dict[str, str] = {}
    for i, (name, values) in enumerate(inline.items()):
        new = f"inline_{i}"
        while new in tables:
            new += "_"
        mapping[name] = new
        tables[new] = _values_frame(values)
    spec = _rename_data_refs(spec, mapping)
    lifted: dict[str, pd.DataFrame] = {}
    spec = _lift_inline_values(spec, lifted, [0])
    for name, frame in lifted.items():
        if name in tables:
            raise ValueError(f"dataset name {name!r} is used twice")
        tables[name] = frame
    resolved_title = title or _altair_title(spec) or id
    return _finish(
        id=id,
        title=resolved_title,
        kind="vega-lite",
        spec=spec,
        datasets=tables,
        schema_version=f"v{VEGA_LITE_VERSION}",
        persona=persona,
        caption_plain=caption_plain,
        bindings=bindings,
        provenance=make_provenance(
            recipe=recipe, plugin=plugin, author=author, bundle_id=bundle_id
        ),
        sanitize=sanitize,
        compact=compact,
    )


def _altair_title(spec: Mapping[str, Any]) -> str | None:
    t = spec.get("title")
    if isinstance(t, str):
        return t[:300]
    if isinstance(t, Mapping) and isinstance(t.get("text"), str):
        return str(t["text"])[:300]
    return None


# --------------------------------------------------------------------------------- plotly


def _is_typed(v: Any) -> bool:
    return isinstance(v, dict) and "bdata" in v and "dtype" in v


def _list_values(v: list[Any]) -> np.ndarray | list[Any] | None:
    """A column-able representation of an inline list, or None to leave it inline."""
    if not v:
        return None
    if all(isinstance(x, bool) for x in v):
        return np.asarray(v, dtype=bool)
    if all((isinstance(x, int | float) and not isinstance(x, bool)) or x is None for x in v):
        return np.asarray([np.nan if x is None else x for x in v], dtype=float)
    if all(isinstance(x, str) or x is None for x in v):
        return list(v)
    if all(isinstance(x, list) for x in v):
        try:
            arr = np.asarray(v, dtype=float)
        except (TypeError, ValueError):
            return None
        return arr if arr.ndim == 2 else None
    return None


class _PlotlyExtractor:
    def __init__(self, min_length: int) -> None:
        self.s = plotly_structure()
        self.min_length = min_length
        self.datasets: dict[str, dict[str, Any]] = {}

    def column(self, prefix: str, path: str, values: Any) -> dict[str, str]:
        n = len(values)
        names = [n_ for n_ in self.datasets if n_ == prefix or n_.startswith(prefix + "_")]
        target = None
        for name in names:
            cols = self.datasets[name]
            if len(next(iter(cols.values()))) == n and path not in cols:
                target = name
                break
        if target is None:
            target = prefix if prefix not in self.datasets else f"{prefix}_{len(names)}"
            self.datasets[target] = {}
        if isinstance(values, np.ndarray) and values.ndim == 2:
            values = [row for row in values]
        self.datasets[target][path] = values
        return {"$ref": f"{target}.{path}"}

    def trace(self, trace: dict[str, Any], prefix: str) -> dict[str, Any]:
        kind = trace.get("type", "scatter")
        node = self.s.trace(kind) if isinstance(kind, str) else None
        if node is None:
            return trace
        out: dict[str, Any] = self.obj(trace, node, prefix, "")
        return out

    def obj(self, value: dict[str, Any], node: dict[str, Any], prefix: str, path: str) -> Any:
        out: dict[str, Any] = {}
        for key, v in value.items():
            sub = self.s.deref(node["k"].get(key)) if "k" in node else None
            p = f"{path}.{key}" if path else key
            if sub is None:
                out[key] = v
            elif "t" in sub:
                data_ok = sub["t"] == "data_array" or bool(sub.get("a"))
                out[key] = self.leaf(v, prefix, p) if data_ok else v
            elif "arr" in sub or not isinstance(v, dict):
                out[key] = v
            else:
                out[key] = self.obj(v, sub, prefix, p)
        return out

    def leaf(self, v: Any, prefix: str, path: str) -> Any:
        if _is_typed(v):
            arr = decode_typed_array(v)
            if len(arr) < self.min_length:
                return v
            return self.column(prefix, path, arr)
        if isinstance(v, list) and len(v) >= self.min_length:
            vals = _list_values(v)
            if vals is not None:
                return self.column(prefix, path, vals)
        return v


def _sanitize_text_columns(datasets: Mapping[str, dict[str, Any]]) -> None:
    """Apply the inline-text rules to string columns moved into Arrow datasets (in place).

    The control-plane sanitizer never decodes Arrow, yet plotly.js renders the HTML subset
    of every text-like value (``text``, ``hovertext``, category labels, ``%{customdata}``
    in templates). Strings are therefore stripped to ``<b> <i> <br> <sub> <sup>`` (plus
    ``<extra>`` in hover templates) and scanned for script/CSS URLs here, on the sandbox
    side, before encoding. Raises :class:`SanitizeError` for unsafe values.
    """
    report = SanitizeReport()
    for name, cols in datasets.items():
        for path, values in cols.items():
            if not isinstance(values, list) or not any(isinstance(v, str) for v in values):
                continue
            last = path.rsplit(".", 1)[-1]
            allowed = HOVERTEMPLATE_TAGS if last.startswith("hovertemplate") else ALLOWED_TAGS
            ptr = f"/datasets/{name}/{path}"
            clean = [strip_html(v, allowed) if isinstance(v, str) else v for v in values]
            for i, v in enumerate(clean):
                if isinstance(v, str):
                    scan_string(v, f"{ptr}/{i}", report)
            cols[path] = clean
    report.raise_if_errors()


def _frame(cols: Mapping[str, Any]) -> pa.Table:
    arrays: dict[str, pa.Array] = {}
    for name, values in cols.items():
        if isinstance(values, list) and values and isinstance(values[0], np.ndarray):
            arrays[name] = pa.array([row.tolist() for row in values])
        else:
            arrays[name] = pa.array(values, from_pandas=True)
    return pa.table(arrays)


def from_plotly(
    fig: Any,
    *,
    id: str,
    title: str | None = None,
    persona: Persona = "both",
    caption_plain: str | None = None,
    bindings: PlotBindings | None = None,
    recipe: str | None = None,
    plugin: str | None = None,
    author: str | None = None,
    bundle_id: str | None = None,
    keep_template: bool = False,
    min_length: int = 1,
    sanitize: bool = True,
) -> PlotSpec:
    """Convert a Plotly figure (``go.Figure`` or dict) into a sanitized PlotSpec.

    Data arrays (typed or inline) move into Arrow datasets named ``trace<i>`` (``frame<f>_
    trace<i>`` for animation frames); arrays shorter than ``min_length`` stay inline. The
    default plotly.py template is dropped unless ``keep_template`` (the renderer themes).
    """
    import plotly.io as pio

    raw = json.loads(pio.to_json(fig, validate=False, pretty=False))
    spec: dict[str, Any] = {k: raw[k] for k in ("data", "layout", "frames", "config") if k in raw}
    layout = spec.get("layout")
    if isinstance(layout, dict) and not keep_template:
        layout.pop("template", None)
    ex = _PlotlyExtractor(min_length)
    spec["data"] = [
        ex.trace(tr, f"trace{i}") if isinstance(tr, dict) else tr
        for i, tr in enumerate(spec.get("data", []))
    ]
    for f, frame in enumerate(spec.get("frames", []) or []):
        if isinstance(frame, dict) and isinstance(frame.get("data"), list):
            frame["data"] = [
                ex.trace(tr, f"frame{f}_trace{i}") if isinstance(tr, dict) else tr
                for i, tr in enumerate(frame["data"])
            ]
    if sanitize:
        _sanitize_text_columns(ex.datasets)
    resolved_title = title or _plotly_title(spec) or id
    return _finish(
        id=id,
        title=resolved_title,
        kind="plotly",
        spec=spec,
        datasets={name: _frame(cols) for name, cols in ex.datasets.items()},
        schema_version=PLOTLY_JS_VERSION,
        persona=persona,
        caption_plain=caption_plain,
        bindings=bindings,
        provenance=make_provenance(
            recipe=recipe, plugin=plugin, author=author, bundle_id=bundle_id
        ),
        sanitize=sanitize,
    )


def _plotly_title(spec: Mapping[str, Any]) -> str | None:
    layout = spec.get("layout")
    if not isinstance(layout, Mapping):
        return None
    t = layout.get("title")
    if isinstance(t, str):
        return t[:300]
    if isinstance(t, Mapping) and isinstance(t.get("text"), str):
        return str(t["text"])[:300]
    return None
