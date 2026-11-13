"""Chart templates (v0.18, D5): an analyst's Vega-Lite restyle of a platform plot.

A :class:`~mapi_spec.library.ChartTemplateDoc` names a source recipe (``core.fit_vs_actual@^1``)
and holds a Vega-Lite spec that reads only that recipe's named datasets. Applying it swaps
the spec of a stored plot of the recipe and keeps the plot's datasets untouched (opaque
``data_ref`` or ``inline_arrow`` envelopes, never decoded), fills role/dim bindings against
the model's schema when there are any, and runs the full plot sanitizer. The result is a
PlotSpec with ``provenance.author`` (``lib_<id>-r<n>``), which
:func:`mapi_spec.sharing.embeddable` refuses: share links, client portals and dashboard
pictures never show authored charts.

The same function previews a template on the recipe's inline catalog example. Column names
come from :data:`mapi_plots.templates.STARTERS` (the example carries them only inside Arrow).

JSON-only (control-plane safe): imports :mod:`mapi_spec`, :mod:`mapi_plots.templates` and
:mod:`mapi_plots.sanitize`, never :mod:`mapi_plots.recipes`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Final

from mapi_spec.errors import ErrorCode, Issue, Severity, has_errors, json_pointer
from mapi_spec.library import ChartTemplateDoc
from mapi_spec.plots import PlotSpec
from mapi_spec.refs import Version
from mapi_spec.schema import DataSchema
from pydantic import ValidationError

from mapi_plots.sanitize import SanitizeError, sanitize_plot
from mapi_plots.templates import STARTERS, TemplateError, apply_template

__all__ = [
    "CHART_ID_PREFIX",
    "apply_chart_template",
    "chart_id",
    "check_chart_template",
    "known_columns",
]

CHART_ID_PREFIX: Final = "lib."
_REQUIRED_CHANNELS: Final = frozenset({"x", "y"})
"""Bound channels a real model must resolve; every other bound channel is optional (an
unresolved ``facet`` or ``color`` simply disappears)."""
_ALL_CHANNELS: Final = frozenset(
    {
        "x",
        "y",
        "y2",
        "color",
        "facet",
        "row",
        "column",
        "size",
        "shape",
        "band_lower",
        "band_upper",
        "tooltip",
    }
)
_FOLD_DEFAULTS: Final = ("key", "value")


def chart_id(doc: ChartTemplateDoc) -> str:
    """The PlotSpec id of an applied template: ``lib.<name>``."""
    return f"{CHART_ID_PREFIX}{doc.metadata.name}"


def known_columns(recipe_id: str) -> dict[str, list[str]] | None:
    """``{dataset: [column, ...]}`` of a source recipe's plots, or ``None`` when unknown."""
    entry = STARTERS.get(recipe_id)
    if entry is None:
        return None
    return {name: list(cols) for name, cols in entry["datasets"].items()}


def _issue(
    pointer: str,
    message: str,
    *,
    code: ErrorCode = ErrorCode.LIBRARY_INVALID,
    severity: Severity = Severity.ERROR,
    action: str | None = None,
) -> Issue:
    return Issue(
        code=code, severity=severity, pointer=pointer, message=message, suggested_action=action
    )


def _column(field: str) -> str:
    """The top-level column a Vega-Lite ``field`` reads (``\\.`` is a literal dot; an
    unescaped ``.`` or ``[`` is nested access into that column)."""
    out: list[str] = []
    escaped = False
    for ch in field:
        if escaped:
            out.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch in ".[":
            break
        else:
            out.append(ch)
    return "".join(out)


def _walk(node: Any, ptr: str, dataset: str | None) -> Iterator[tuple[str, Any, str, str | None]]:
    """Yield ``(key, value, pointer, dataset in scope)`` for every key of every object."""
    if isinstance(node, Mapping):
        data = node.get("data")
        if isinstance(data, Mapping) and isinstance(data.get("name"), str):
            dataset = data["name"]
        for k, v in node.items():
            child = ptr + json_pointer(str(k))
            yield str(k), v, child, dataset
            yield from _walk(v, child, dataset)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, ptr + json_pointer(i), dataset)


def _derived(plot: Mapping[str, Any]) -> set[str]:
    """Field names that transforms create (``as``, fold/pivot defaults)."""
    out: set[str] = set()
    for key, value, _, _ in _walk(plot, "", None):
        if key == "as":
            for v in value if isinstance(value, list) else [value]:
                if isinstance(v, str):
                    out.add(v)
        elif key == "fold":
            out.update(_FOLD_DEFAULTS)
        elif key == "pivot" and isinstance(value, str):
            out.add(value)
    return out


def _structure_issues(
    doc: ChartTemplateDoc, datasets: Mapping[str, Any], columns: Mapping[str, list[str]] | None
) -> list[Issue]:
    """Data only by name, only the source's datasets, only known columns."""
    issues: list[Issue] = []
    plot = doc.plot
    if "datasets" in plot:
        issues.append(
            _issue(
                "/plot/datasets",
                "A chart template cannot carry its own data. "
                "It reads the datasets of the source plot.",
            )
        )
    used: set[str] = set()
    for key, value, ptr, _ in _walk(plot, "/plot", None):
        if key == "usermeta":
            issues.append(
                _issue(ptr, "A chart template cannot carry usermeta.", action="Remove usermeta.")
            )
            continue
        if key != "data":
            continue
        if not (
            isinstance(value, Mapping) and set(value) == {"name"} and isinstance(value["name"], str)
        ):
            issues.append(
                _issue(
                    ptr, 'A chart template reads data only by dataset name: {"name": "<dataset>"}.'
                )
            )
            continue
        used.add(value["name"])
        if value["name"] not in datasets:
            issues.append(
                _issue(
                    ptr + "/name",
                    f"The source plot has no dataset {value['name']!r}.",
                    action="Use one of: " + ", ".join(sorted(datasets)) + ".",
                )
            )
    if not used:
        issues.append(
            _issue(
                "/plot/data",
                "The chart reads no dataset.",
                action='Add "data": {"name": "<dataset>"}.',
            )
        )
    if columns is None or has_errors(issues):
        return issues
    derived = _derived(plot)
    everything = {c for cols in columns.values() for c in cols}
    for key, value, ptr, dataset in _walk(plot, "/plot", None):
        names: list[tuple[str, str]] = []
        if key == "field" and isinstance(value, str):
            names = [(value, ptr)]
        elif key in ("groupby", "fold") and isinstance(value, list):
            names = [(v, ptr + json_pointer(i)) for i, v in enumerate(value) if isinstance(v, str)]
        for name, where in names:
            col = _column(name)
            known = set(columns.get(dataset, ())) if dataset is not None else everything
            if col and col not in known and col not in derived:
                issues.append(
                    _issue(
                        where,
                        f"The dataset {dataset or 'of the plot'} has no column {col!r}.",
                        action=("Use one of: " + ", ".join(sorted(known)) + ".") if known else None,
                    )
                )
    return issues


def _source_issues(doc: ChartTemplateDoc, base: PlotSpec) -> list[Issue]:
    """The base plot must be a recipe plot of the template's source recipe."""
    ref = base.provenance.recipe
    if ref is None:
        return [_issue("/source_recipe", "The plot is not made by a platform recipe.")]
    rid, _, version = ref.partition("@")
    if rid != doc.source_recipe.id:
        return [
            _issue(
                "/source_recipe", f"The plot is made by {rid!r}, not by {doc.source_recipe.id!r}."
            )
        ]
    if version:
        try:
            ok = doc.source_recipe.range.satisfies(Version.parse(version))
        except ValueError:
            ok = True
        if not ok:
            return [
                _issue(
                    "/source_recipe",
                    f"Version {version} of {rid!r} is not in the "
                    f"range {str(doc.source_recipe.range)!r}.",
                )
            ]
    return []


def _doc_pointer(pointer: str) -> str:
    """Map a PlotSpec pointer back into the template document."""
    if pointer == "/spec" or pointer.startswith("/spec/"):
        return "/plot" + pointer[len("/spec") :]
    if pointer == "/title":
        return "/metadata/title"
    if pointer.startswith(("/caption_plain", "/bindings")):
        return pointer
    return ""


def _remap(issues: list[Issue]) -> list[Issue]:
    return [i.model_copy(update={"pointer": _doc_pointer(i.pointer)}) for i in issues]


def _render(
    doc: ChartTemplateDoc, base: PlotSpec, schema: DataSchema | None, author: str
) -> tuple[PlotSpec | None, list[Issue]]:
    issues: list[Issue] = []
    envelope: dict[str, Any] = {
        "id": chart_id(doc),
        "title": doc.metadata.title,
        "kind": "vega-lite",
        "spec": doc.plot,
        "datasets": {k: v.model_dump(mode="json") for k, v in base.datasets.items()},
        "bindings": None if doc.bindings is None else doc.bindings.model_dump(mode="json"),
        "schema_version": base.schema_version if base.kind == "vega-lite" else "v5",
        "persona": "both",
        "caption_plain": doc.caption_plain or None,
        "provenance": {"author": author, "generated_at": base.provenance.generated_at},
    }
    try:
        plot = PlotSpec.model_validate(envelope)
    except ValidationError as exc:
        for err in exc.errors(include_url=False, include_input=False)[:20]:
            loc = [str(p) for p in err["loc"]]
            issues.append(
                _issue(
                    _doc_pointer(json_pointer(*loc)),
                    str(err["msg"]).removeprefix("Value error, ")[:2000],
                )
            )
        return None, issues
    if plot.bindings is not None:
        if schema is not None:
            optional = _ALL_CHANNELS - _REQUIRED_CHANNELS
            preview = None
        else:
            issues.append(
                _issue(
                    "/bindings",
                    "Role bindings apply on a real model. The preview leaves them empty.",
                    severity=Severity.INFO,
                )
            )
            # an empty schema with every channel optional: placeholders drop out
            optional, preview = _ALL_CHANNELS, plot.bindings.model_copy(update={"tooltip": []})
        try:
            plot = apply_template(
                plot if preview is None else plot.model_copy(update={"bindings": preview}),
                schema if schema is not None else DataSchema(),
                optional=optional,
            )
        except TemplateError as exc:
            issues.append(exc.to_issue().model_copy(update={"code": ErrorCode.LIBRARY_INVALID}))
            return None, _remap(issues)
        plot = plot.model_copy(update={"bindings": None})
    try:
        result = sanitize_plot(plot)
    except SanitizeError as exc:
        return None, [*issues, *_remap(exc.issues)]
    # the datasets pass through unchanged: they are the stored plot's, never decoded
    out = result.plot.model_copy(update={"datasets": dict(base.datasets)})
    return out, [*issues, *_remap(result.changes)]


def check_chart_template(doc: ChartTemplateDoc, example: PlotSpec) -> list[Issue]:
    """Check a template against its source recipe's catalog example: data read by name from
    the example's datasets only, known columns only, and a clean sanitizer pass. Issues
    point into the template document (``/plot/layer/0/encoding/x/field``)."""
    issues = _source_issues(doc, example)
    issues += _structure_issues(doc, example.datasets, known_columns(doc.source_recipe.id))
    if known_columns(doc.source_recipe.id) is None:
        issues.append(
            _issue(
                "/source_recipe",
                "The columns of this recipe are not known. Only the dataset names are checked.",
                severity=Severity.WARNING,
            )
        )
    if has_errors(issues):
        return issues
    _, rendered = _render(doc, example, None, author="check")
    return [*issues, *rendered]


def apply_chart_template(
    doc: ChartTemplateDoc,
    base: PlotSpec,
    *,
    schema: DataSchema | None = None,
    author: str,
) -> tuple[PlotSpec | None, list[Issue]]:
    """Restyle ``base`` (a stored plot, or the catalog example for a preview) with ``doc``.

    The spec is swapped, ``base.datasets`` are kept byte-identical, bindings are filled
    against ``schema`` (without one they are left empty, with an info issue), the result is
    sanitized and carries ``provenance.author = author`` (``lib_<id>-r<n>``) and the id
    ``lib.<name>``. Returns ``(None, issues)`` when the template is refused.
    """
    issues = _source_issues(doc, base)
    issues += _structure_issues(doc, base.datasets, known_columns(doc.source_recipe.id))
    if has_errors(issues):
        return None, issues
    plot, rendered = _render(doc, base, schema, author)
    issues += rendered
    if has_errors(issues):
        return None, issues
    return plot, issues
