"""Run tracking from model code: metrics, params, tags, structured log lines and chains.

Every context a plugin or workflow node sees (:class:`~mapi_sdk.models.FitContext`,
:class:`~mapi_sdk.models.ScoreContext`, ``mapi_workflows`` ``NodeContext``) mixes in
:class:`TrackingMixin`, so model code logs the same way everywhere::

    for step in range(n):
        ...
        ctx.log_metric("loss", loss, step=step)
    ctx.log_metrics({"rmse": r, "mae": a}, labels={"split": "val"})
    ctx.log_param("solver", "lbfgs")
    ctx.set_tag("family", "gaussian")
    ctx.log_line("refit done", level="info", fields={"iterations": n})
    ctx.log_image("samples", pixels, step=step)          # v0.14 media
    if ctx.tracking_media:
        ctx.log_figure("residuals", make_figure())

The mixin validates every call (keys and labels with the :mod:`mapi_spec.tracking` helpers)
whether or not a :class:`Tracker` is attached, so a bad key fails in a notebook the same way
it fails in a run. Non-finite values are dropped silently. The tracker attached by the
platform decides where the values go: the runner streams them as run events, the harness
also records them into the bundle's ``metrics_history`` group (:class:`MetricRecorder`).

Media (images, figures, tables) go only to a tracker that is also a :class:`MediaTracker`;
without one the call validates its arguments and returns without encoding anything.

Metric keys keep :data:`mapi_spec.tracking.METRIC_KEY_RE` (``loss``, ``step_size``);
qualifiers such as the chain, the fold or the split are labels (``{"chain": "0"}``).
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
import threading
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Final, Protocol, cast, runtime_checkable

import numpy as np
import xarray as xr
from mapi_spec.run import ChainProgress
from mapi_spec.tracking import (
    LOG_FIELD_KEY_RE,
    LOG_LEVELS,
    MAX_LOG_FIELD_CHARS,
    MAX_LOG_FIELDS,
    MAX_METRIC_LABELS,
    MAX_PARAM_VALUE_CHARS,
    MAX_TAG_VALUE_CHARS,
    PARAM_KEY_RE,
    LogFieldValue,
    LogLevel,
    ParamValue,
    TrackingLimits,
    check_labels,
    check_metric_key,
    check_param_key,
    check_tag_key,
    labels_key,
    param_text,
)

from mapi_sdk.media import MediaItem, check_media_args, figure_item, image_item, table_item

if TYPE_CHECKING:
    from mapi_spec.capabilities import ModelManifest
    from mapi_spec.spec import ModelSpec

__all__ = [
    "MAX_HISTORY_POINTS",
    "METRICS_HISTORY",
    "MediaTracker",
    "MetricRecorder",
    "TeeTracker",
    "Tracker",
    "TrackingMixin",
    "accepts_media",
    "spec_params",
    "with_labels",
]

METRICS_HISTORY: Final = "metrics_history"
"""Bundle model group holding the metrics a fit logged (read by ``core.loss_curve``)."""
MAX_HISTORY_POINTS: Final = 5000
"""Points kept per series in :data:`METRICS_HISTORY` (thinned evenly, the last step kept)."""

MAX_LINE_CHARS: Final = 4096
_MAX_HISTORY_KEYS: Final = 64
_MAX_HISTORY_SERIES: Final = 64  # label combinations per key
_MAX_HISTORY_STEPS: Final = 20_000  # union of steps over every key
_MAX_HISTORY_CELLS: Final = 4096  # label cells per step of one key (product of label dims)
_MAX_SETTINGS_PARAMS: Final = 200
_MAX_SETTING_JSON: Final = 500
_PARAM_KEY = re.compile(PARAM_KEY_RE)
_FIELD_KEY = re.compile(LOG_FIELD_KEY_RE)

Labels = tuple[tuple[str, str], ...]


@runtime_checkable
class Tracker(Protocol):
    """Where tracked values go. Implementations MUST be thread-safe.

    Inputs are already validated by :class:`TrackingMixin` (keys, labels, finite values).
    """

    def log_metrics(
        self,
        values: Mapping[str, float],
        *,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None: ...

    def log_params(self, params: Mapping[str, ParamValue]) -> None: ...

    def set_tags(self, tags: Mapping[str, str]) -> None: ...

    def log_line(
        self,
        message: str,
        *,
        level: LogLevel = "info",
        fields: Mapping[str, LogFieldValue] | None = None,
    ) -> None: ...

    def chains(self, chains: Sequence[ChainProgress]) -> None: ...

    def flush(self) -> None: ...


@runtime_checkable
class MediaTracker(Protocol):
    """Optional capability of a :class:`Tracker`: it accepts media (v0.14). Thread-safe.

    Kept apart from :class:`Tracker` so trackers written before v0.14 stay valid.
    """

    def log_media(self, item: MediaItem) -> None: ...


def accepts_media(tracker: object) -> bool:
    """``True`` when ``tracker`` (or, for a tee or a labelled wrapper, an inner tracker)
    is a :class:`MediaTracker`. A :class:`MetricRecorder` without a media budget
    (``media_items=0``) takes none."""
    if isinstance(tracker, MetricRecorder):
        return tracker.limits.media_items > 0
    if isinstance(tracker, TeeTracker):
        return any(accepts_media(t) for t in tracker.trackers)
    if isinstance(tracker, _Labelled):
        return accepts_media(tracker.inner)
    return isinstance(tracker, MediaTracker)


# --------------------------------------------------------------------------- validation


@lru_cache(maxsize=4096)
def _metric_key(key: str) -> str:
    return check_metric_key(key)


@lru_cache(maxsize=4096)
def _labels(items: Labels) -> Labels:
    return tuple(check_labels(dict(items)).items())


def _check_labels(labels: Mapping[str, str] | None) -> dict[str, str]:
    if not labels:
        return {}
    try:
        items = tuple(sorted(labels.items()))
        hash(items)
    except TypeError:  # unsortable or unhashable keys/values: let the checker word the error
        return check_labels(labels)
    return dict(_labels(items))


def _check_step(step: int | None) -> int | None:
    if step is None:
        return None
    if isinstance(step, bool) or not isinstance(step, int | np.integer):
        raise ValueError(f"metric step must be an int >= 0 or None, got {step!r}")
    if step < 0:
        raise ValueError(f"metric step must be >= 0, got {step}")
    return int(step)


def _number(key: str, value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"metric {key!r}: value {value!r} is not a number") from None


def _param_value(key: str, value: Any) -> ParamValue:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        if len(value) > MAX_PARAM_VALUE_CHARS:
            raise ValueError(
                f"param {key!r}: the value has {len(value)} characters "
                f"(at most {MAX_PARAM_VALUE_CHARS})"
            )
        return value
    raise ValueError(
        f"param {key!r}: value {value!r} must be a bool, int, float or str "
        "(write a list or a dict as JSON text)"
    )


def _field_value(key: str, value: Any) -> LogFieldValue:
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:MAX_LOG_FIELD_CHARS]
    raise ValueError(f"log field {key!r}: value {value!r} must be a bool, int, float, str or None")


def _check_fields(fields: Mapping[str, Any] | None) -> dict[str, LogFieldValue] | None:
    if not fields:
        return None
    if len(fields) > MAX_LOG_FIELDS:
        raise ValueError(f"a log line has at most {MAX_LOG_FIELDS} fields, got {len(fields)}")
    out: dict[str, LogFieldValue] = {}
    for k, v in fields.items():
        if not isinstance(k, str) or _FIELD_KEY.match(k) is None:
            raise ValueError(f"log field key {k!r} must match {LOG_FIELD_KEY_RE}")
        out[k] = _field_value(k, v)
    return out


class TrackingMixin:
    """Logging methods shared by the fit, score and workflow-node contexts.

    The host class declares ``tracker: Tracker | None`` (``None``: validate only) and
    ``_logged_params: dict[str, ParamValue]`` (the params this context logged, for the
    write-once rule). Every method validates its input even without a tracker and raises
    :class:`ValueError` at the call site; non-finite metric values are dropped.
    """

    __slots__ = ()

    tracker: Tracker | None
    _logged_params: dict[str, ParamValue]

    @property
    def tracking(self) -> bool:
        """``True`` when a tracker is attached (skip computing expensive metrics otherwise)."""
        return self.tracker is not None

    def log_metric(
        self,
        key: str,
        value: float,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Log one metric point; ``step=None`` makes it a scalar (the newest value wins)."""
        self.log_metrics({key: value}, step=step, labels=labels)

    def log_metrics(
        self,
        values: Mapping[str, float],
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Log several metrics that share one step and one label set."""
        if not values:
            return
        st = _check_step(step)
        lab = _check_labels(labels)
        clean: dict[str, float] = {}
        for k, v in values.items():
            key = _metric_key(k)
            x = _number(key, v)
            if math.isfinite(x):
                clean[key] = x
        tracker = self.tracker
        if clean and tracker is not None:
            tracker.log_metrics(clean, step=st, labels=lab or None)

    def log_param(self, key: str, value: ParamValue) -> None:
        """Log one run parameter (write-once: a different second value is an error)."""
        self.log_params({key: value})

    def log_params(self, params: Mapping[str, ParamValue]) -> None:
        """Log run parameters (write-once per key; the same value again is a no-op)."""
        if not params:
            return
        clean: dict[str, ParamValue] = {}
        for k, v in params.items():
            key = check_param_key(k)
            clean[key] = _param_value(key, v)
        seen = self._logged_params
        for key, value in clean.items():
            if key in seen and param_text(seen[key]) != param_text(value):
                raise ValueError(
                    f"param {key!r} is already {seen[key]!r}; params are write-once "
                    f"(cannot change it to {value!r})"
                )
        fresh = {k: v for k, v in clean.items() if k not in seen}
        if not fresh:
            return
        seen.update(fresh)
        if self.tracker is not None:
            self.tracker.log_params(fresh)

    def set_tag(self, key: str, value: str) -> None:
        """Set one run tag (the last value wins)."""
        self.set_tags({key: value})

    def set_tags(self, tags: Mapping[str, str]) -> None:
        """Set run tags (the last value wins; keys under ``mapi.`` are reserved)."""
        if not tags:
            return
        clean: dict[str, str] = {}
        for k, v in tags.items():
            key = check_tag_key(k)
            if not isinstance(v, str):
                raise ValueError(f"tag {key!r}: value {v!r} must be a str")
            if len(v) > MAX_TAG_VALUE_CHARS:
                raise ValueError(
                    f"tag {key!r}: the value has {len(v)} characters "
                    f"(at most {MAX_TAG_VALUE_CHARS})"
                )
            clean[key] = v
        if self.tracker is not None:
            self.tracker.set_tags(clean)

    def log_line(
        self,
        message: str,
        level: LogLevel = "info",
        fields: Mapping[str, LogFieldValue] | None = None,
    ) -> None:
        """Write a structured log line (stream ``user``) with a level and optional fields.

        Without a tracker the line goes to the context's plain ``log`` callable, if any.
        """
        if level not in LOG_LEVELS:
            raise ValueError(f"log level {level!r} must be one of {', '.join(LOG_LEVELS)}")
        text = str(message)[:MAX_LINE_CHARS]
        clean = _check_fields(fields)
        if self.tracker is not None:
            self.tracker.log_line(text, level=level, fields=clean)
            return
        plain = getattr(self, "log", None)
        if callable(plain):
            plain(f"[{level}] {text}")

    def chains(self, chains: Sequence[ChainProgress]) -> None:
        """Report per-chain sampler progress (shown live on the run page)."""
        items = list(chains)
        for c in items:
            if not isinstance(c, ChainProgress):
                raise ValueError(f"chains() takes mapi_spec.run.ChainProgress items, got {c!r}")
        if self.tracker is not None:
            self.tracker.chains(items)

    # ------------------------------------------------------------------- media (v0.14)
    @property
    def tracking_media(self) -> bool:
        """``True`` when the tracker takes media (skip rendering expensive figures otherwise)."""
        return accepts_media(self.tracker)

    def _media_tracker(
        self,
        key: str,
        step: int | None,
        labels: Mapping[str, str] | None,
        caption: str | None,
    ) -> MediaTracker | None:
        check_media_args(key, step, labels, caption)
        tracker = self.tracker
        return cast(MediaTracker, tracker) if accepts_media(tracker) else None

    def log_image(
        self,
        key: str,
        image: Any,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
        caption: str | None = None,
    ) -> None:
        """Log an image: a numpy array (``(H, W)``, ``(H, W, 3)``, ``(H, W, 4)``; uint8, or
        floats in ``[0, 1]``), PNG/JPEG bytes, a Pillow image or a ``(y, x[, channel])``
        DataArray. ``step`` makes a series (a slider on the run page)."""
        tracker = self._media_tracker(key, step, labels, caption)
        if tracker is not None:
            tracker.log_media(image_item(key, image, step=step, labels=labels, caption=caption))

    def log_figure(
        self,
        key: str,
        figure: Any,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
        caption: str | None = None,
    ) -> None:
        """Log a matplotlib figure as a PNG image (rendered only when a tracker takes media)."""
        tracker = self._media_tracker(key, step, labels, caption)
        if tracker is not None:
            tracker.log_media(figure_item(key, figure, step=step, labels=labels, caption=caption))

    def log_table(
        self,
        key: str,
        table: Any,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
        caption: str | None = None,
    ) -> None:
        """Log a small table: a pandas DataFrame, a mapping of columns or a list of records."""
        tracker = self._media_tracker(key, step, labels, caption)
        if tracker is not None:
            tracker.log_media(table_item(key, table, step=step, labels=labels, caption=caption))


# --------------------------------------------------------------------------- trackers


class MetricRecorder:
    """In-memory :class:`Tracker` (and :class:`MediaTracker`) bounded by
    :class:`~mapi_spec.tracking.TrackingLimits`.

    The fit harness records every fit with one and stores :meth:`history` in the bundle.
    Stepped points: the first value of a ``(series, step)`` wins; scalar points (no step):
    the newest wins. Over-budget points, params, tags, lines and media are dropped and
    counted in :attr:`dropped` by reason. Media stay in memory (:attr:`media`); nothing of
    them goes into the bundle.
    """

    def __init__(self, limits: TrackingLimits | None = None) -> None:
        self.limits = limits or TrackingLimits()
        self._lock = threading.Lock()
        # (key, labels_key) -> (labels, {step: value}); insertion order = first logged
        self._stepped: dict[tuple[str, str], tuple[dict[str, str], dict[int, float]]] = {}
        self._scalars: dict[tuple[str, str], tuple[dict[str, str], float]] = {}
        self._points = 0
        self._log_bytes = 0
        self.params: dict[str, ParamValue] = {}
        self.tags: dict[str, str] = {}
        self.lines: list[tuple[LogLevel, str, dict[str, LogFieldValue]]] = []
        self.dropped: dict[str, int] = {}
        self.last_chains: list[ChainProgress] = []
        self.media: list[MediaItem] = []
        self._media_shas: set[str] = set()
        self._media_bytes = 0

    def _drop(self, reason: str, n: int = 1) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + n

    def log_metrics(
        self,
        values: Mapping[str, float],
        *,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        lab = dict(labels or {})
        lk = labels_key(lab)
        lim = self.limits
        with self._lock:
            for key, raw in values.items():
                value = float(raw)
                if not math.isfinite(value):
                    continue
                sk = (key, lk)
                known = sk in self._stepped or sk in self._scalars
                if not known and len(self._stepped) + len(self._scalars) >= lim.metric_series:
                    self._drop("series_limit")
                    continue
                if step is None:
                    if sk not in self._scalars:
                        if self._points >= lim.metric_points_total:
                            self._drop("points_limit")
                            continue
                        self._points += 1
                    self._scalars[sk] = (lab, value)
                    continue
                entry = self._stepped.get(sk)
                if entry is not None and step in entry[1]:
                    continue  # first value wins
                if entry is not None and len(entry[1]) >= lim.metric_points_per_series:
                    self._drop("series_points_limit")
                    continue
                if self._points >= lim.metric_points_total:
                    self._drop("points_limit")
                    continue
                if entry is None:
                    entry = self._stepped[sk] = (lab, {})
                entry[1][step] = value
                self._points += 1

    def log_params(self, params: Mapping[str, ParamValue]) -> None:
        with self._lock:
            for key, value in params.items():
                if key in self.params:
                    continue  # first value wins
                if len(self.params) >= self.limits.params:
                    self._drop("params_limit")
                    continue
                self.params[key] = value

    def set_tags(self, tags: Mapping[str, str]) -> None:
        with self._lock:
            for key, value in tags.items():
                if key not in self.tags and len(self.tags) >= self.limits.tags:
                    self._drop("tags_limit")
                    continue
                self.tags[key] = value

    def log_line(
        self,
        message: str,
        *,
        level: LogLevel = "info",
        fields: Mapping[str, LogFieldValue] | None = None,
    ) -> None:
        size = len(message.encode("utf-8"))
        with self._lock:
            if len(self.lines) >= self.limits.log_lines:
                self._drop("log_lines_limit")
                return
            if self._log_bytes + size > self.limits.log_bytes:
                self._drop("log_bytes_limit")
                return
            self._log_bytes += size
            self.lines.append((level, message, dict(fields or {})))

    def chains(self, chains: Sequence[ChainProgress]) -> None:
        with self._lock:
            self.last_chains = list(chains)

    def log_media(self, item: MediaItem) -> None:
        lim = self.limits
        with self._lock:
            if len(self.media) >= lim.media_items:
                self._drop("media_limit")
                return
            new = item.sha256 not in self._media_shas
            if new and self._media_bytes + item.size > lim.media_bytes:
                self._drop("media_limit")
                return
            if new:  # the same bytes are stored once
                self._media_shas.add(item.sha256)
                self._media_bytes += item.size
            self.media.append(item)

    def flush(self) -> None:
        return None

    # ---- reading

    @property
    def points(self) -> int:
        """Metric points kept (stepped points plus one per scalar series)."""
        with self._lock:
            return self._points

    def series(self) -> dict[tuple[str, str], list[tuple[int | None, float]]]:
        """Every kept series as ``(key, labels_key) -> [(step, value), ...]`` (step order).

        A scalar series has one point with step ``None``.
        """
        with self._lock:
            out: dict[tuple[str, str], list[tuple[int | None, float]]] = {
                sk: sorted(pts.items()) for sk, (_, pts) in self._stepped.items()
            }
            for sk, (_, value) in self._scalars.items():
                out.setdefault(sk, []).append((None, value))
            return out

    def history(self) -> xr.Dataset | None:
        """The :data:`METRICS_HISTORY` dataset, or ``None`` when no metric or param was logged.

        One data var per stepped key over ``step`` (the union of every kept step) plus one
        dim per label key (coords: the label values, sorted; ``""`` where a series lacks
        that label); NaN where a cell has no point. Each series is thinned to
        :data:`MAX_HISTORY_POINTS` (var attr ``thinned_from``). A stepped key named ``step``
        is the var ``metric_step`` (var attr ``mapi_key``). Scalars, params and tags are
        JSON string attrs ``mapi_scalars`` / ``mapi_params`` / ``mapi_tags``.
        """
        with self._lock:
            stepped = [
                (key, lab, np.fromiter(pts.keys(), np.int64), np.fromiter(pts.values(), float))
                for (key, _), (lab, pts) in self._stepped.items()
                if pts
            ]
            scalars = {sk: v for sk, (_, v) in self._scalars.items()}
            params = dict(self.params)
            tags = dict(self.tags)
        if not stepped and not scalars and not params:
            return None
        return _history_dataset(stepped, scalars, params, tags)


def _thin(n: int, keep: int) -> np.ndarray:
    """Evenly spaced indices into ``range(n)``, at most ``keep``, first and last kept."""
    if n <= keep:
        return np.arange(n)
    return np.unique(np.linspace(0, n - 1, keep).round().astype(np.int64))


def _json_param(value: ParamValue) -> ParamValue:
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    return value


def _history_dataset(
    stepped: list[tuple[str, dict[str, str], np.ndarray, np.ndarray]],
    scalars: Mapping[tuple[str, str], float],
    params: Mapping[str, ParamValue],
    tags: Mapping[str, str],
) -> xr.Dataset:
    by_key: dict[str, list[tuple[dict[str, str], np.ndarray, np.ndarray, int]]] = {}
    for key, lab, steps, values in stepped:
        order = np.argsort(steps, kind="stable")
        steps, values = steps[order], values[order]
        n = steps.size
        idx = _thin(n, MAX_HISTORY_POINTS)
        by_key.setdefault(key, []).append((lab, steps[idx], values[idx], n))
    keys = list(by_key)
    dropped_keys = max(0, len(keys) - _MAX_HISTORY_KEYS)
    keys = keys[:_MAX_HISTORY_KEYS]
    dropped_series = 0
    for key in keys:
        if len(by_key[key]) > _MAX_HISTORY_SERIES:
            dropped_series += len(by_key[key]) - _MAX_HISTORY_SERIES
            by_key[key] = by_key[key][:_MAX_HISTORY_SERIES]

    union = (
        np.unique(np.concatenate([s for k in keys for _, s, _, _ in by_key[k]]))
        if keys
        else np.zeros(0, np.int64)
    )
    if union.size > _MAX_HISTORY_STEPS:
        union = union[_thin(union.size, _MAX_HISTORY_STEPS)]

    # a metric logged as "step" cannot share its name with the step dim: its var gets a
    # "metric_" prefix (attr ``mapi_key`` keeps the logged key)
    var_of = {k: k for k in keys}
    if "step" in var_of:
        name = "metric_step"
        while name in var_of:
            name += "_"
        var_of["step"] = name

    # label dims: a label named like a metric var or "step" gets a "label_" prefix
    taken = {*var_of.values(), "step"}

    def dim_of(label: str) -> str:
        return f"label_{label}" if label in taken else label

    label_dims: dict[str, set[str]] = {}
    var_labels: dict[str, list[str]] = {}
    for key in keys:
        names = sorted({k for lab, *_ in by_key[key] for k in lab})
        var_labels[key] = names
        for name in names:
            label_dims.setdefault(name, set()).update(lab.get(name, "") for lab, *_ in by_key[key])
    coords: dict[str, Any] = {"step": union}
    for name, vals in label_dims.items():
        coords[dim_of(name)] = np.array(sorted(vals), dtype=str)

    data_vars: dict[str, xr.Variable] = {}
    for key in keys:
        names = var_labels[key]
        dims = ["step", *(dim_of(n) for n in names)]
        shape = [union.size, *(len(coords[dim_of(n)]) for n in names)]
        if math.prod(shape[1:]) > _MAX_HISTORY_CELLS:
            dropped_keys += 1
            continue
        arr = np.full(shape, np.nan)
        thinned_from = 0
        for lab, steps, values, n in by_key[key]:
            pos = np.searchsorted(union, steps)
            ok = (pos < union.size) & (union[np.minimum(pos, union.size - 1)] == steps)
            cell = tuple(int(np.searchsorted(coords[dim_of(n_)], lab.get(n_, ""))) for n_ in names)
            arr[(pos[ok], *cell)] = values[ok]
            if n > steps.size:
                thinned_from = max(thinned_from, n)
        attrs: dict[str, Any] = {"thinned_from": thinned_from} if thinned_from else {}
        if var_of[key] != key:
            attrs["mapi_key"] = key
        data_vars[var_of[key]] = xr.Variable(dims, arr, attrs=attrs)
    used = {d for v in data_vars.values() for d in v.dims}
    ds = xr.Dataset(data_vars, coords={k: v for k, v in coords.items() if k in used})

    scalar_doc: dict[str, dict[str, float]] = {}
    for (key, lk), value in scalars.items():
        scalar_doc.setdefault(key, {})[lk] = value
    ds.attrs["mapi_scalars"] = json.dumps(scalar_doc, sort_keys=True, allow_nan=False)
    ds.attrs["mapi_params"] = json.dumps(
        {k: _json_param(v) for k, v in params.items()}, sort_keys=True, allow_nan=False
    )
    ds.attrs["mapi_tags"] = json.dumps(dict(tags), sort_keys=True)
    if dropped_keys:
        ds.attrs["mapi_dropped_keys"] = dropped_keys
    if dropped_series:
        ds.attrs["mapi_dropped_series"] = dropped_series
    return ds


class TeeTracker:
    """A :class:`Tracker` forwarding every call to each non-``None`` tracker, in order."""

    __slots__ = ("trackers",)

    def __init__(self, *trackers: Tracker | None) -> None:
        self.trackers: tuple[Tracker, ...] = tuple(t for t in trackers if t is not None)

    def log_metrics(
        self,
        values: Mapping[str, float],
        *,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        for t in self.trackers:
            t.log_metrics(values, step=step, labels=labels)

    def log_params(self, params: Mapping[str, ParamValue]) -> None:
        for t in self.trackers:
            t.log_params(params)

    def set_tags(self, tags: Mapping[str, str]) -> None:
        for t in self.trackers:
            t.set_tags(tags)

    def log_line(
        self,
        message: str,
        *,
        level: LogLevel = "info",
        fields: Mapping[str, LogFieldValue] | None = None,
    ) -> None:
        for t in self.trackers:
            t.log_line(message, level=level, fields=fields)

    def chains(self, chains: Sequence[ChainProgress]) -> None:
        for t in self.trackers:
            t.chains(chains)

    def log_media(self, item: MediaItem) -> None:
        for t in self.trackers:
            if accepts_media(t):
                cast(MediaTracker, t).log_media(item)

    def flush(self) -> None:
        for t in self.trackers:
            t.flush()


class _Labelled:
    """Adds fixed labels to every ``log_metrics`` call and an optional prefix to every
    param key; other calls pass through."""

    __slots__ = ("inner", "labels", "param_prefix")

    def __init__(self, inner: Tracker, labels: Mapping[str, str], param_prefix: str = "") -> None:
        self.inner = inner
        self.labels = check_labels(labels)
        self.param_prefix = param_prefix

    def log_metrics(
        self,
        values: Mapping[str, float],
        *,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.inner.log_metrics(values, step=step, labels=_merge_labels(self.labels, labels))

    def log_params(self, params: Mapping[str, ParamValue]) -> None:
        if self.param_prefix:
            # a prefixed key that is too long for a param key is dropped
            params = {
                f"{self.param_prefix}{k}": v
                for k, v in params.items()
                if _PARAM_KEY.match(f"{self.param_prefix}{k}")
            }
        if params:
            self.inner.log_params(params)

    def set_tags(self, tags: Mapping[str, str]) -> None:
        self.inner.set_tags(tags)

    def log_line(
        self,
        message: str,
        *,
        level: LogLevel = "info",
        fields: Mapping[str, LogFieldValue] | None = None,
    ) -> None:
        self.inner.log_line(message, level=level, fields=fields)

    def chains(self, chains: Sequence[ChainProgress]) -> None:
        self.inner.chains(chains)

    def log_media(self, item: MediaItem) -> None:
        if not accepts_media(self.inner):
            return
        merged = _merge_labels(self.labels, item.labels)
        if merged != item.labels:
            item = dataclasses.replace(item, labels=merged)
        cast(MediaTracker, self.inner).log_media(item)

    def flush(self) -> None:
        self.inner.flush()


def _merge_labels(fixed: Mapping[str, str], labels: Mapping[str, str] | None) -> dict[str, str]:
    """Fixed labels plus call labels, sorted; call labels win, and fixed labels that do not
    fit :data:`MAX_METRIC_LABELS` are dropped."""
    merged = {**fixed, **(labels or {})}
    if len(merged) > MAX_METRIC_LABELS:
        own = dict(labels or {})
        room = MAX_METRIC_LABELS - len(own)
        extra = [(k, v) for k, v in fixed.items() if k not in own][: max(room, 0)]
        merged = {**dict(extra), **own}
    return dict(sorted(merged.items()))


def with_labels(
    tracker: Tracker | None, labels: Mapping[str, str], *, param_prefix: str = ""
) -> Tracker | None:
    """``tracker`` adding ``labels`` to every metric (call labels win) and ``param_prefix``
    to every param key; ``None`` stays None.

    The evaluation fit of ``evaluate_then_refit`` uses ``{"fit": "evaluation"}`` and
    ``"evaluation."``: its params (often data-dependent, such as the number of rows) then
    do not take the keys of the refit, whose values describe the model version.
    """
    if tracker is None:
        return None
    if not labels and not param_prefix:
        return tracker
    return _Labelled(tracker, labels, param_prefix)


# --------------------------------------------------------------------------- spec params


def _flatten(prefix: str, value: Any, out: list[tuple[str, Any]]) -> None:
    if isinstance(value, Mapping) and value:
        for k, v in value.items():
            _flatten(f"{prefix}.{k}", v, out)
    else:
        out.append((prefix, value))


def _setting_value(value: Any) -> ParamValue | None:
    if value is None:
        return None
    if isinstance(value, bool | int | str):
        return value[:MAX_PARAM_VALUE_CHARS] if isinstance(value, str) else value
    if isinstance(value, float):
        return value
    try:
        text = json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:_MAX_SETTING_JSON]


def spec_params(
    spec: ModelSpec, settings: Mapping[str, Any], manifest: ModelManifest
) -> dict[str, ParamValue]:
    """The params every fit logs: model id, set fit fields, split kind and plugin settings.

    Keys: ``model`` (``"<id>@<version>"``), ``fit.<field>`` for the set fields among engine,
    draws, tune, chains, target_accept, seed and mode, ``data.split`` (the split kind) and
    ``settings.<dotted path>`` per settings leaf (lists and empty dicts as compact JSON, at
    most 500 characters; keys that are not valid param keys are skipped; at most 200).
    """
    out: dict[str, ParamValue] = {"model": f"{manifest.id}@{manifest.version}"}
    fit = spec.model.fit
    for name in ("engine", "draws", "tune", "chains", "target_accept", "seed", "mode"):
        v = getattr(fit, name)
        if v is None:
            continue
        out[f"fit.{name}"] = v.value if hasattr(v, "value") else v
    if spec.data.split is not None:
        out["data.split"] = spec.data.split.kind
    leaves: list[tuple[str, Any]] = []
    for k, v in settings.items():
        _flatten(f"settings.{k}", v, leaves)
    n = 0
    for key, raw in leaves:
        if n >= _MAX_SETTINGS_PARAMS:
            break
        value = _setting_value(raw)
        if value is None or _PARAM_KEY.match(key) is None:
            continue
        out[key] = value
        n += 1
    return out
