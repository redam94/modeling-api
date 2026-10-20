"""Run tracking (v0.11): metric, param, tag and log-field keys, budgets and the run-search grammar.

v0.14 adds the media budgets of :class:`TrackingLimits` and the caps of run comparison,
experiments and saved views (``MAX_COMPARE_*``, ``MAX_EXPERIMENT_*``, :data:`MAX_RUN_VIEWS`).

Every component that records or reads run tracking data agrees on what lives here:

* key and label regexes with their annotated types (:data:`MetricKey`, :data:`ParamKey`,
  :data:`TagKey`, :data:`MetricLabels`, ...) and the matching ``check_*`` helpers, which raise a
  plain :class:`ValueError` so SDK call sites fail where the bad key was written;
* :func:`labels_key` / :func:`parse_labels_key`, the canonical text form of a label set that
  identifies a metric series in storage;
* :func:`param_text`, the text/number pair a param value is stored and compared as;
* :class:`TrackingLimits`, the per-run budgets the supervisor and the server enforce;
* :class:`RunRef` / :class:`RunFilter` / :class:`RunOrder` and their parsers, the grammar of the
  ``filter`` / ``order_by`` / ``column`` query parameters of ``GET /v1/runs``.

Metric keys keep the historic :data:`~mapi_spec._types.METRIC_RE` (no ``/``, no ``.``):
qualifiers such as the chain, the fold or the data split are labels, not key segments.
Pure Python + Pydantic: no numpy or xarray.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Final, Literal, cast

from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
)

from mapi_spec._types import METRIC_RE, StrictModel

__all__ = [
    "LOG_FIELD_KEY_RE",
    "LOG_LEVELS",
    "MAX_COMPARE_KEYS",
    "MAX_COMPARE_MEDIA_ITEMS",
    "MAX_COMPARE_MEDIA_KEYS",
    "MAX_COMPARE_PARAMS",
    "MAX_COMPARE_RUNS",
    "MAX_COMPARE_SERIES",
    "MAX_EXPERIMENT_DESCRIPTION_CHARS",
    "MAX_EXPERIMENT_NAME_CHARS",
    "MAX_EXPERIMENT_RUNS_WRITE",
    "MAX_LOG_FIELDS",
    "MAX_LOG_FIELD_CHARS",
    "MAX_METRIC_LABELS",
    "MAX_PARAMS_PER_EVENT",
    "MAX_PARAMS_PER_WRITE",
    "MAX_PARAM_VALUE_CHARS",
    "MAX_POINTS_PER_EVENT",
    "MAX_POINTS_PER_WRITE",
    "MAX_RUN_COLUMNS",
    "MAX_RUN_FILTERS",
    "MAX_RUN_NAME_CHARS",
    "MAX_RUN_NOTE_CHARS",
    "MAX_RUN_VIEWS",
    "MAX_SERIES_PER_HISTORY",
    "MAX_TAGS_PER_RUN",
    "MAX_TAG_VALUE_CHARS",
    "METRIC_KEY_RE",
    "METRIC_LABEL_KEY_RE",
    "METRIC_LABEL_VALUE_RE",
    "PARAM_KEY_RE",
    "RESERVED_TAG_PREFIX",
    "RUN_FILTER_OPS",
    "SCALAR_STEP",
    "TAG_KEY_RE",
    "LogFieldKey",
    "LogFieldValue",
    "LogLevel",
    "MetricAgg",
    "MetricKey",
    "MetricLabelKey",
    "MetricLabelValue",
    "MetricLabels",
    "ParamKey",
    "ParamValue",
    "RunFilter",
    "RunFilterOp",
    "RunOrder",
    "RunRef",
    "RunRefKind",
    "TagKey",
    "TrackingLimits",
    "check_labels",
    "check_metric_key",
    "check_param_key",
    "check_tag_key",
    "labels_key",
    "param_text",
    "parse_labels_key",
    "parse_run_filter",
    "parse_run_order",
    "parse_run_ref",
]

# ======================================================================================== keys

METRIC_KEY_RE: Final = METRIC_RE
"""Metric keys: ``^[a-z][a-z0-9_]{0,63}$`` (unchanged since M1; ``train/loss`` is refused)."""
METRIC_LABEL_KEY_RE: Final = r"^[a-z][a-z0-9_]{0,31}$"
METRIC_LABEL_VALUE_RE: Final = r"^[A-Za-z0-9_.\-]{1,63}$"
PARAM_KEY_RE: Final = r"^[A-Za-z0-9_][A-Za-z0-9_.\-/]{0,249}$"
TAG_KEY_RE: Final = r"^[A-Za-z0-9_][A-Za-z0-9_.\-/]{0,127}$"
LOG_FIELD_KEY_RE: Final = METRIC_LABEL_KEY_RE

RESERVED_TAG_PREFIX: Final = "mapi."
"""Tag keys with this prefix are platform-only: API and runner writes are refused."""

MAX_METRIC_LABELS: Final = 8
MAX_PARAM_VALUE_CHARS: Final = 6000
MAX_TAG_VALUE_CHARS: Final = 1000
MAX_LOG_FIELDS: Final = 16
MAX_LOG_FIELD_CHARS: Final = 256
MAX_RUN_NAME_CHARS: Final = 200
MAX_RUN_NOTE_CHARS: Final = 20_000
MAX_POINTS_PER_EVENT: Final = 1000
"""Points one ``metrics`` run event carries."""
MAX_PARAMS_PER_EVENT: Final = 200
MAX_POINTS_PER_WRITE: Final = 1000
"""Points one ``POST /v1/runs/{run_id}/metrics`` carries."""
MAX_PARAMS_PER_WRITE: Final = 500
MAX_TAGS_PER_RUN: Final = 100
MAX_SERIES_PER_HISTORY: Final = 64
"""Series one ``GET /v1/runs/{run_id}/metrics/{key}`` returns."""
SCALAR_STEP: Final = -1
"""Storage sentinel for a point without a step (server only; never on the wire)."""
MAX_RUN_FILTERS: Final = 8
MAX_RUN_COLUMNS: Final = 16

# v0.14: run comparison, experiments and saved views
MAX_COMPARE_RUNS: Final = 10
"""Runs one ``GET /v1/run-compare`` compares (at least 2)."""
MAX_COMPARE_KEYS: Final = 12
"""Metric keys whose histories one comparison returns."""
MAX_COMPARE_MEDIA_KEYS: Final = 4
MAX_COMPARE_MEDIA_ITEMS: Final = 50
"""Items per run and media series in a comparison (evenly thinned; the last is kept)."""
MAX_COMPARE_SERIES: Final = 16
"""Series per run and metric key in a comparison."""
MAX_COMPARE_PARAMS: Final = 2000
MAX_RUN_VIEWS: Final = 200
"""Saved run views per project."""
MAX_EXPERIMENT_RUNS_WRITE: Final = 200
"""Run ids one ``POST /v1/experiments/{id}/runs`` adds (and removes)."""
MAX_EXPERIMENT_NAME_CHARS: Final = 200
MAX_EXPERIMENT_DESCRIPTION_CHARS: Final = 20_000

_MAX_IMAGE_BYTES: Final = 8 * 1024 * 1024
"""= :data:`mapi_spec.media.MAX_IMAGE_BYTES` (media imports this module; a test pins them)."""

_METRIC_KEY = re.compile(METRIC_KEY_RE)
_LABEL_KEY = re.compile(METRIC_LABEL_KEY_RE)
_LABEL_VALUE = re.compile(METRIC_LABEL_VALUE_RE)
_PARAM_KEY = re.compile(PARAM_KEY_RE)
_TAG_KEY = re.compile(TAG_KEY_RE)

LogLevel = Literal["debug", "info", "warning", "error"]
LOG_LEVELS: Final[tuple[LogLevel, ...]] = ("debug", "info", "warning", "error")
"""Log levels in ascending severity."""

MetricKey = Annotated[str, StringConstraints(pattern=METRIC_KEY_RE)]
MetricLabelKey = Annotated[str, StringConstraints(pattern=METRIC_LABEL_KEY_RE)]
MetricLabelValue = Annotated[str, StringConstraints(pattern=METRIC_LABEL_VALUE_RE)]
ParamKey = Annotated[str, StringConstraints(pattern=PARAM_KEY_RE)]
TagKey = Annotated[str, StringConstraints(pattern=TAG_KEY_RE)]
LogFieldKey = Annotated[str, StringConstraints(pattern=LOG_FIELD_KEY_RE)]
MetricLabels = Annotated[
    dict[MetricLabelKey, MetricLabelValue], Field(max_length=MAX_METRIC_LABELS)
]
"""Qualifiers of a metric series (``{"chain": "0"}``, ``{"fold": "2", "stage": "inner"}``)."""
ParamValue = (
    StrictBool
    | StrictInt
    | StrictFloat
    | Annotated[StrictStr, Field(max_length=MAX_PARAM_VALUE_CHARS)]
)
LogFieldValue = (
    StrictBool
    | StrictInt
    | StrictFloat
    | Annotated[StrictStr, Field(max_length=MAX_LOG_FIELD_CHARS)]
    | None
)


def check_metric_key(key: str) -> str:
    """Return ``key`` if it is a valid metric key, else raise :class:`ValueError`."""
    if not isinstance(key, str) or _METRIC_KEY.match(key) is None:
        raise ValueError(
            f"metric key {key!r} must match {METRIC_KEY_RE} (use labels, not '/' or '.', "
            "for qualifiers such as the split or the chain)"
        )
    return key


def check_labels(labels: Mapping[str, str] | None) -> dict[str, str]:
    """Validate a metric label set; return it as a new dict sorted by key."""
    if not labels:
        return {}
    if len(labels) > MAX_METRIC_LABELS:
        raise ValueError(f"a metric has at most {MAX_METRIC_LABELS} labels, got {len(labels)}")
    out: dict[str, str] = {}
    for k in sorted(labels):
        v = labels[k]
        if not isinstance(k, str) or _LABEL_KEY.match(k) is None:
            raise ValueError(f"metric label key {k!r} must match {METRIC_LABEL_KEY_RE}")
        if not isinstance(v, str) or _LABEL_VALUE.match(v) is None:
            raise ValueError(
                f"metric label value {v!r} (label {k!r}) must match {METRIC_LABEL_VALUE_RE}"
            )
        out[k] = v
    return out


def check_param_key(key: str) -> str:
    """Return ``key`` if it is a valid param key, else raise :class:`ValueError`."""
    if not isinstance(key, str) or _PARAM_KEY.match(key) is None:
        raise ValueError(f"param key {key!r} must match {PARAM_KEY_RE}")
    return key


def check_tag_key(key: str, *, reserved: bool = False) -> str:
    """Return ``key`` if it is a valid tag key, else raise :class:`ValueError`.

    Keys under :data:`RESERVED_TAG_PREFIX` are refused unless ``reserved`` (platform writes).
    """
    if not isinstance(key, str) or _TAG_KEY.match(key) is None:
        raise ValueError(f"tag key {key!r} must match {TAG_KEY_RE}")
    if not reserved and key.startswith(RESERVED_TAG_PREFIX):
        raise ValueError(f"tag key {key!r} is reserved (prefix {RESERVED_TAG_PREFIX!r})")
    return key


def labels_key(labels: Mapping[str, str]) -> str:
    """Canonical ``"k1=v1,k2=v2"`` form of a label set, sorted by key; ``""`` for no labels.

    Label keys and values cannot contain ``=`` or ``,``, so the form is unambiguous.
    """
    return ",".join(f"{k}={labels[k]}" for k in sorted(labels))


def parse_labels_key(text: str) -> dict[str, str]:
    """Inverse of :func:`labels_key` (validates the labels)."""
    if not text:
        return {}
    out: dict[str, str] = {}
    for part in text.split(","):
        k, sep, v = part.partition("=")
        if not sep:
            raise ValueError(f"labels {text!r}: {part!r} is not 'key=value'")
        if k in out:
            raise ValueError(f"labels {text!r}: label {k!r} is repeated")
        out[k] = v
    return check_labels(out)


def param_text(value: ParamValue) -> tuple[str, float | None]:
    """The ``(value_text, value_num)`` pair a param value is stored and compared as."""
    if isinstance(value, bool):
        return ("true" if value else "false"), None
    if isinstance(value, int):
        return str(value), float(value)
    if isinstance(value, float):
        return repr(value), (value if math.isfinite(value) else None)
    return value, None


# ===================================================================================== budgets


class TrackingLimits(StrictModel):
    """Per-run tracking budgets; the runner, the supervisor and the server enforce them."""

    metric_points_per_series: int = Field(10_000, ge=1)
    metric_points_total: int = Field(500_000, ge=1)
    metric_series: int = Field(1_000, ge=1, description="Distinct (key, labels) series")
    params: int = Field(5_000, ge=0)
    tags: int = Field(MAX_TAGS_PER_RUN, ge=0, le=MAX_TAGS_PER_RUN)
    log_lines: int = Field(10_000, ge=0, description="User (structured) log lines")
    log_bytes: int = Field(2 * 1024 * 1024, ge=0)
    tracking_bytes: int = Field(
        64 * 1024 * 1024,
        ge=1024,
        description="Hard cap on the bytes of tracking frames; above it the run fails with "
        "SANDBOX_VIOLATION",
    )
    flush_interval_s: float = Field(1.0, ge=0.1, le=30)
    media_items: int = Field(1000, ge=0, description="Distinct media rows per run (v0.14)")
    media_bytes: int = Field(
        256 * 1024 * 1024, ge=0, description="Sum of the stored media bytes per run (v0.14)"
    )
    media_item_bytes: int = Field(
        _MAX_IMAGE_BYTES,
        ge=1024,
        le=_MAX_IMAGE_BYTES,
        description="Bytes of one media item (v0.14)",
    )


# ================================================================================== run search

RunRefKind = Literal["metric", "param", "tag", "name", "created_at"]
MetricAgg = Literal["last", "min", "max", "count"]
RunFilterOp = Literal["<", "<=", ">", ">=", "=", "!=", "~"]

_AGGS: Final[tuple[MetricAgg, ...]] = ("last", "min", "max", "count")
RUN_FILTER_OPS: Final[tuple[RunFilterOp, ...]] = ("<=", ">=", "!=", "<", ">", "=", "~")
"""Filter operators, longest first (the parser tries them in this order)."""
_ORDER_OPS: Final = frozenset({"<", "<=", ">", ">="})
_OPS_BY_KIND: Final[Mapping[str, frozenset[str]]] = {
    "metric": frozenset({"<", "<=", ">", ">=", "=", "!="}),
    "param": frozenset(RUN_FILTER_OPS),
    "tag": frozenset({"=", "!=", "~"}),
    "name": frozenset({"=", "~"}),
    "created_at": frozenset(),
}
_METRIC_REF = re.compile(
    r"^metric\.(?P<key>[^.{}]+)(?:\{(?P<labels>[^{}]*)\})?(?:\.(?P<agg>[a-z]+))?$"
)


@dataclass(frozen=True, slots=True)
class RunRef:
    """A run column: a metric series summary, a param, a tag, the run name or its creation time."""

    kind: RunRefKind
    key: str | None = None
    labels: tuple[tuple[str, str], ...] = ()
    agg: MetricAgg = "last"

    def text(self) -> str:
        """Canonical form (``metric.loss{split=val}.min``); the key of :attr:`Run.columns`.

        Labels are sorted and the default aggregate ``last`` is left out.
        """
        if self.kind in ("name", "created_at"):
            return self.kind
        if self.kind != "metric":
            return f"{self.kind}.{self.key}"
        out = f"metric.{self.key}"
        if self.labels:
            out += "{" + labels_key(dict(self.labels)) + "}"
        if self.agg != "last":
            out += f".{self.agg}"
        return out


@dataclass(frozen=True, slots=True)
class RunFilter:
    """One ``filter`` query value: ``<ref> <op> <value>``."""

    ref: RunRef
    op: RunFilterOp
    value: str | float


@dataclass(frozen=True, slots=True)
class RunOrder:
    """The ``order_by`` query value: a ref, descending with a leading ``-``."""

    ref: RunRef
    descending: bool


def parse_run_ref(text: str) -> RunRef:
    """Parse a ref: ``metric.KEY[{k=v,...}][.AGG]``, ``param.KEY``, ``tag.KEY``, ``name``."""
    s = text.strip()
    if s in ("name", "created_at"):
        return RunRef(kind=cast(RunRefKind, s))
    if s.startswith("metric."):
        m = _METRIC_REF.match(s)
        if m is None:
            raise ValueError(
                f"run ref {text!r}: write a metric as 'metric.KEY', 'metric.KEY{{label=value}}' "
                "or 'metric.KEY.AGG'"
            )
        try:
            key = check_metric_key(m["key"])
            labels = parse_labels_key(m["labels"] or "")
        except ValueError as exc:
            raise ValueError(f"run ref {text!r}: {exc}") from None
        agg = m["agg"] or "last"
        if agg not in _AGGS:
            raise ValueError(
                f"run ref {text!r}: aggregate {agg!r} is not one of {', '.join(_AGGS)}"
            )
        return RunRef(
            kind="metric", key=key, labels=tuple(sorted(labels.items())), agg=cast(MetricAgg, agg)
        )
    for kind, check in (("param", check_param_key), ("tag", check_tag_key)):
        if s.startswith(f"{kind}."):
            try:
                key = check(s[len(kind) + 1 :])
            except ValueError as exc:
                raise ValueError(f"run ref {text!r}: {exc}") from None
            return RunRef(kind=cast(RunRefKind, kind), key=key)
    raise ValueError(
        f"run ref {text!r} must be 'metric.KEY', 'param.KEY', 'tag.KEY', 'name' or 'created_at'"
    )


def _op_position(s: str) -> int:
    depth = 0
    for i, ch in enumerate(s):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth == 0 and ch in "<>=!~":
            return i
    return -1


def parse_run_filter(text: str) -> RunFilter:
    """Parse one ``filter`` value (``metric.rmse < 0.5``, ``param.fit.draws >= 1000``, ...).

    Metric filters compare a number with the summary of one exact series; param filters
    compare the text (``= != ~``) or the number (``< <= > >=``); tag filters use ``= != ~``;
    ``name`` uses ``= ~``. ``~`` is a case-insensitive substring match.
    """
    i = _op_position(text)
    if i < 0:
        raise ValueError(f"filter {text!r} has no operator (one of {' '.join(RUN_FILTER_OPS)})")
    op = next((o for o in RUN_FILTER_OPS if text.startswith(o, i)), None)
    if op is None:
        raise ValueError(f"filter {text!r}: the operator is not one of {' '.join(RUN_FILTER_OPS)}")
    try:
        ref = parse_run_ref(text[:i])
    except ValueError as exc:
        raise ValueError(f"filter {text!r}: {exc}") from None
    raw = text[i + len(op) :].strip()
    if op not in _OPS_BY_KIND[ref.kind]:
        if ref.kind == "created_at":
            raise ValueError(f"filter {text!r}: 'created_at' is valid only in order_by and column")
        allowed = " ".join(o for o in RUN_FILTER_OPS if o in _OPS_BY_KIND[ref.kind])
        raise ValueError(f"filter {text!r}: a {ref.kind} filter takes one of {allowed}")
    numeric = ref.kind == "metric" or (ref.kind == "param" and op in _ORDER_OPS)
    if not numeric:
        return RunFilter(ref=ref, op=op, value=raw)
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"filter {text!r}: {raw!r} is not a number") from None
    if not math.isfinite(value):
        raise ValueError(f"filter {text!r}: the value must be a finite number")
    return RunFilter(ref=ref, op=op, value=value)


def parse_run_order(text: str) -> RunOrder:
    """Parse the ``order_by`` value: a ref, with a leading ``-`` for descending order."""
    s = text.strip()
    descending = s.startswith("-")
    try:
        ref = parse_run_ref(s[1:] if descending else s)
    except ValueError as exc:
        raise ValueError(f"order_by {text!r}: {exc}") from None
    return RunOrder(ref=ref, descending=descending)
