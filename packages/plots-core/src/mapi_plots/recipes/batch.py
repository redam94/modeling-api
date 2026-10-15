"""Render a list of plot refs for a run (``outputs.plots`` / a manifest's ``plots``).

The runner calls :func:`render_plots` after a fit or score: every ref is resolved (a
registry if given, else the bundled recipes), rendered and sanitized. A plot that cannot be
drawn never fails the run; it becomes an :class:`~mapi_spec.errors.Issue` instead.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity
from mapi_spec.plots import PlotSpec
from mapi_spec.refs import UseRef

from mapi_plots.recipes._base import Recipe, as_tree
from mapi_plots.sanitize import SanitizeError, sanitize_plot

__all__ = ["RenderReport", "render_plots"]


@dataclass(slots=True)
class RenderReport:
    """Rendered plots plus one issue per plot that could not be produced."""

    plots: list[PlotSpec] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def to_json(self) -> list[dict[str, Any]]:
        """The ``plots.json`` sidecar body."""
        return [p.model_dump(mode="json", exclude_none=True) for p in self.plots]


def _resolve(ref: str, registry: Any | None) -> Any:
    if registry is not None:
        return registry.plot(ref)
    from mapi_plots.recipes import all_recipes

    parsed = UseRef.parse(ref) if "@" in ref else None
    rid = parsed.id if parsed else ref
    for r in all_recipes():
        if r.id == rid and (parsed is None or parsed.resolve([r.version]) is not None):
            return r
    raise MapiError(f"no plot recipe satisfies {ref!r}", code=ErrorCode.REF_UNRESOLVED)


def render_plots(
    data: Any,
    refs: Iterable[str],
    *,
    registry: Any | None = None,
    options: Mapping[str, Mapping[str, Any]] | None = None,
) -> RenderReport:
    """Render every ref against ``data``; problems become warnings (plugins: errors).

    ``options`` maps a recipe id to its settings. ``registry`` is a ``mapi_sdk`` Registry
    (plugin-provided recipes); without one only the bundled recipes resolve.
    """
    tree = as_tree(data)
    report = RenderReport()
    opts = options or {}
    for i, ref in enumerate(refs):
        pointer = f"/outputs/plots/{i}"
        try:
            recipe = _resolve(ref, registry)
            rid = recipe.id if isinstance(recipe, Recipe) else ref.split("@")[0]
            plot = recipe.render(tree, **dict(opts.get(rid, {})))
            report.plots.append(sanitize_plot(plot).plot)
        except SanitizeError as exc:  # a recipe produced something unsafe: always an error
            report.issues.append(
                Issue(
                    code=ErrorCode.PLOT_UNSAFE,
                    severity=Severity.ERROR,
                    pointer=pointer,
                    message=f"plot {ref!r} was rejected by the sanitizer",
                    untrusted_text=exc.detail[:2000],
                )
            )
        except MapiError as exc:
            report.issues.append(
                Issue(
                    code=exc.code,
                    severity=Severity.WARNING,
                    pointer=pointer,
                    message=f"plot {ref!r} was skipped: {exc.detail}"[:2000],
                    untrusted_text=exc.untrusted_text,
                )
            )
        except Exception as exc:  # plugin recipe code failed; report, keep the run going
            report.issues.append(
                Issue(
                    code=ErrorCode.USER_CODE_ERROR,
                    severity=Severity.WARNING,
                    pointer=pointer,
                    message=f"plot {ref!r} failed while rendering",
                    untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
                )
            )
    return report
