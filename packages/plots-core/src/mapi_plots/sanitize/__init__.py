"""The JSON-only PlotSpec sanitizer (plan §10.3, step 1).

This is the control-plane half of plot safety. It depends only on ``mapi-spec`` and
``jsonschema`` and never imports numpy, pandas, altair or plotly (enforced by import-linter
and a subprocess test), so ``mapi_server`` can run it on every plot it stores or serves.

>>> from mapi_plots.sanitize import sanitize_plot
>>> result = sanitize_plot(plot_json)          # PlotSpec | dict | str | bytes
>>> result.plot                                 # the sanitized PlotSpec
>>> result.changes                              # what was rewritten (HTML stripped, ...)

Anything unsafe raises :class:`SanitizeError` (a ``MapiError``) whose ``issues`` carry the
JSON pointer, the :class:`SanitizeRule` (in ``context["rule"]``) and any user-supplied text in
``untrusted_text``. :func:`check_plot` returns the same issues without raising (for editors).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mapi_spec.errors import Issue
from mapi_spec.loader import validation_issues
from mapi_spec.plots import PlotSpec
from pydantic import ValidationError

from mapi_plots.sanitize._common import (
    DEFAULT_LIMITS,
    DENY_NAMES,
    SanitizeError,
    SanitizeLimits,
    SanitizeReport,
    SanitizeRule,
    check_structure,
    is_safe_image_uri,
    parse_json_strict,
)
from mapi_plots.sanitize.expr import VEGA_FUNCTIONS, check_expression
from mapi_plots.sanitize.html import ALLOWED_TAGS, strip_html
from mapi_plots.sanitize.plotly import (
    PLOTLY_JS_VERSION,
    PLOTLY_REF_SCHEMA,
    TILE_MAP_TRACES,
    is_plotly_ref,
    parse_plotly_ref,
    sanitize_plotly,
)
from mapi_plots.sanitize.vegalite import (
    VEGA_LITE_SCHEMA_URL,
    VEGA_LITE_VERSION,
    sanitize_vega_lite,
    vega_lite_schema,
)

__all__ = [
    "ALLOWED_TAGS",
    "DEFAULT_LIMITS",
    "DENY_NAMES",
    "PLOTLY_JS_VERSION",
    "PLOTLY_REF_SCHEMA",
    "TILE_MAP_TRACES",
    "VEGA_FUNCTIONS",
    "VEGA_LITE_SCHEMA_URL",
    "VEGA_LITE_VERSION",
    "SanitizeError",
    "SanitizeLimits",
    "SanitizeResult",
    "SanitizeRule",
    "check_expression",
    "check_plot",
    "is_plotly_ref",
    "is_safe_image_uri",
    "parse_plotly_ref",
    "sanitize_plot",
    "sanitize_spec",
    "strip_html",
    "vega_lite_schema",
]

PlotInput = PlotSpec | Mapping[str, Any] | str | bytes


@dataclass(frozen=True, slots=True)
class SanitizeResult:
    """A sanitized plot plus the list of changes the sanitizer made (severity ``info``)."""

    plot: PlotSpec
    changes: list[Issue] = field(default_factory=list)


def _spec_size(spec: Any) -> int:
    return len(json.dumps(spec, separators=(",", ":"), ensure_ascii=False).encode())


def _without_arrow_payloads(doc: dict[str, Any]) -> dict[str, Any]:
    """The document with inline Arrow payloads blanked: they are capped by the envelope
    (``InlineArrow.data`` max length) and the document size, not the per-string limit."""
    datasets = doc.get("datasets")
    if not isinstance(datasets, dict):
        return doc
    blank = {
        k: ({**v, "data": ""} if isinstance(v, dict) and isinstance(v.get("data"), str) else v)
        for k, v in datasets.items()
    }
    return {**doc, "datasets": blank}


def _run(
    plot: PlotInput, limits: SanitizeLimits, validate_schema: bool
) -> tuple[PlotSpec | None, SanitizeReport]:
    report = SanitizeReport(limits)
    if isinstance(plot, str | bytes):
        try:
            doc: Any = parse_json_strict(plot, limits)
        except SanitizeError as exc:
            report.errors.extend(exc.issues)
            return None, report
    elif isinstance(plot, PlotSpec):
        doc = plot.model_dump(mode="json", by_alias=True)
    elif isinstance(plot, Mapping):
        doc = dict(plot)
    else:
        report.error(SanitizeRule.ENVELOPE, "", "a plot must be a PlotSpec, mapping or JSON text")
        return None, report
    if not isinstance(doc, dict):
        report.error(SanitizeRule.ENVELOPE, "", "a plot document must be a JSON object")
        return None, report
    if not check_structure(_without_arrow_payloads(doc), "", report):
        return None, report
    try:
        size = _spec_size(doc)
    except (TypeError, ValueError):
        report.error(SanitizeRule.SCHEMA, "", "the plot document is not JSON-serialisable")
        return None, report
    if size > limits.max_document_bytes:
        report.error(SanitizeRule.TOO_LARGE, "", f"document exceeds {limits.max_document_bytes} B")
        return None, report
    kind, spec = doc.get("kind"), doc.get("spec")
    if kind in ("vega-lite", "plotly") and isinstance(spec, dict):
        if _spec_size(spec) > limits.max_spec_bytes:
            report.error(
                SanitizeRule.TOO_LARGE,
                "/spec",
                f"spec exceeds {limits.max_spec_bytes} B; move data into named datasets",
            )
            return None, report
        fn = sanitize_vega_lite if kind == "vega-lite" else sanitize_plotly
        doc = {**doc, "spec": fn(spec, report, validate_schema=validate_schema)}
    if report.errors:
        return None, report
    try:
        out = PlotSpec.model_validate(doc)
    except ValidationError as exc:
        for issue in validation_issues(exc, doc):
            report.error(SanitizeRule.ENVELOPE, issue.pointer, issue.message)
        return None, report
    return out, report


def sanitize_plot(
    plot: PlotInput,
    *,
    limits: SanitizeLimits = DEFAULT_LIMITS,
    validate_schema: bool = True,
) -> SanitizeResult:
    """Validate and sanitize a plot document; raise :class:`SanitizeError` if it is unsafe."""
    out, report = _run(plot, limits, validate_schema)
    report.raise_if_errors()
    assert out is not None
    return SanitizeResult(plot=out, changes=list(report.changes))


def check_plot(
    plot: PlotInput,
    *,
    limits: SanitizeLimits = DEFAULT_LIMITS,
    validate_schema: bool = True,
) -> list[Issue]:
    """Every error and change the sanitizer would report, without raising."""
    _, report = _run(plot, limits, validate_schema)
    return [*report.errors, *report.changes]


def sanitize_spec(
    kind: str,
    spec: Mapping[str, Any],
    *,
    limits: SanitizeLimits = DEFAULT_LIMITS,
    validate_schema: bool = True,
) -> tuple[dict[str, Any], list[Issue]]:
    """Sanitize a bare Vega-Lite or Plotly spec (no envelope); returns ``(spec, changes)``."""
    report = SanitizeReport(limits)
    doc = dict(spec)
    if kind not in ("vega-lite", "plotly"):
        report.error(SanitizeRule.ENVELOPE, "/kind", "kind must be vega-lite or plotly")
    elif check_structure(doc, "", report):
        if _spec_size(doc) > limits.max_spec_bytes:
            report.error(SanitizeRule.TOO_LARGE, "", f"spec exceeds {limits.max_spec_bytes} B")
        else:
            fn = sanitize_vega_lite if kind == "vega-lite" else sanitize_plotly
            doc = fn(doc, report, base="", validate_schema=validate_schema)
    report.raise_if_errors()
    return doc, list(report.changes)
