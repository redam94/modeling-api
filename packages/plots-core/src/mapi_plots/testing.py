"""The plot recipe contract kit: :func:`check_recipe` (v0.18; ``recipes`` extra).

A third-party recipe pack runs it in its own tests (``mapi plugin init --kind recipe``
scaffolds one) and ``mapi plugin validate`` runs it on every exported recipe::

    from mapi_plots.testing import check_recipe

    def test_my_recipe_meets_the_contract() -> None:
        check_recipe(my_recipe)  # raises RecipeContractError with every failed check

Every check has a stable name (``RecipeReport.checks`` keeps them in this order):

=========================  ================================================================
``catalog_entry``          ``catalog_entry()`` builds; id, version, title and tags are valid
``namespace``              the id's namespace is not reserved for the platform or the
                           built-in plugins (unless the recipe ships with the platform)
``about``                  a complete About card (only with ``require_about=True``)
``settings_schema``        the settings publish as a safe JSON Schema of at most 16 KiB
``settings_defaults``      the settings validate with no values (the playground's first form)
``settings_extra_refused`` an unknown settings key is refused (schema and Pydantic agree)
``example_renders``        the catalog example renders (``example=`` or the bundled data)
``example_inline``         every example dataset is inline (no URL, no ``data_ref``)
``example_sanitized``      the example passes the plot sanitizer with no error and no change
``caption``                the example has a plain-language caption
=========================  ================================================================

Colours outside :data:`mapi_plots.palette.PALETTE` are a warning, never a failure: the plot
sandbox swaps palette colours for their dark variants, so other colours do not adapt to the
dark theme.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import cache
from typing import Any, Final

from jsonschema import Draft202012Validator
from mapi_spec.about import about_issues
from mapi_spec.catalog import PlotCatalogEntry
from mapi_spec.errors import Severity
from mapi_spec.namespaces import check_namespace
from mapi_spec.plots import InlineArrow, PlotSpec
from mapi_spec.schema_safety import SETTINGS_SCHEMA_MAX_BYTES, sanitize_schema
from pydantic import ValidationError

from mapi_plots.palette import PALETTE
from mapi_plots.recipes._base import Recipe, recipe_tags
from mapi_plots.sanitize import check_plot

__all__ = [
    "CHECKS",
    "RecipeContractError",
    "RecipeReport",
    "check_recipe",
]

CHECKS: Final[tuple[str, ...]] = (
    "catalog_entry",
    "namespace",
    "about",
    "settings_schema",
    "settings_defaults",
    "settings_extra_refused",
    "example_renders",
    "example_inline",
    "example_sanitized",
    "caption",
)
"""Every check :func:`check_recipe` runs, in report order."""

_UNKNOWN_KEY: Final = "mapi_contract_unknown_setting"
_HEX: Final = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_RGB: Final = re.compile(r"^rgba?\(")
_MAX_LISTED_COLOURS: Final = 5


@dataclass(slots=True)
class RecipeReport:
    """The outcome of :func:`check_recipe`.

    ``checks`` holds ``(name, None)`` for a passed check and ``(name, reason)`` for a failed
    one (names from :data:`CHECKS`); ``warnings`` holds ``(name, message)`` pairs that do not
    fail the recipe."""

    ok: bool
    checks: list[tuple[str, str | None]]
    subject: str = "recipe"
    warnings: list[tuple[str, str]] = field(default_factory=list)

    @property
    def failures(self) -> list[tuple[str, str]]:
        return [(name, reason) for name, reason in self.checks if reason is not None]

    def reason(self, name: str) -> str | None:
        """Why check ``name`` failed (``None`` when it passed); ``KeyError`` if it did not run."""
        for n, reason in self.checks:
            if n == name:
                return reason
        raise KeyError(name)

    def summary(self) -> str:
        lines = [f"recipe contract kit: {self.subject}"]
        for name, reason in self.checks:
            lines.append(
                f"  [{'FAIL' if reason else 'PASS'}] {name}" + (f": {reason}" if reason else "")
            )
        for name, message in self.warnings:
            lines.append(f"  [WARN] {name}: {message}")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "ok": self.ok,
            "checks": [{"name": n, "passed": r is None, "detail": r or ""} for n, r in self.checks],
            "warnings": [{"name": n, "detail": m} for n, m in self.warnings],
        }


class RecipeContractError(AssertionError):
    """A recipe failed :func:`check_recipe` (``raise_on_failure=True``); ``report`` has
    every check. An ``AssertionError``, so pytest shows the summary as the failure."""

    def __init__(self, report: RecipeReport) -> None:
        super().__init__(report.summary())
        self.report = report


class _NotChecked(Exception):
    """A check needs an earlier result that is missing."""


@cache
def _module_distributions() -> dict[str, str]:
    """``{top-level package: distribution}``: from the installed files, then from the
    ``mapi.plots`` entry points (an editable install lists no package files)."""
    out = {k: v[0] for k, v in importlib.metadata.packages_distributions().items() if v}
    for ep in importlib.metadata.entry_points(group="mapi.plots"):
        dist = getattr(getattr(ep, "dist", None), "name", None)
        if dist:
            out.setdefault(ep.module.split(".", 1)[0], dist)
    return out


def _distribution_of(fn: Callable[..., Any]) -> str | None:
    """The installed distribution that ships ``fn``'s top-level package, if any."""
    top = (getattr(fn, "__module__", None) or "").split(".", 1)[0]
    return _module_distributions().get(top) if top else None


def _colours(node: Any) -> Iterator[str]:
    """Every literal colour string (``#rgb``/``#rrggbb``/``rgb()``) in a spec."""
    if isinstance(node, dict):
        for v in node.values():
            yield from _colours(v)
    elif isinstance(node, list):
        for v in node:
            yield from _colours(v)
    elif isinstance(node, str) and (_HEX.match(node) or _RGB.match(node)):
        yield node


def _palette() -> set[str]:
    return {c.lower() for s in PALETTE.values() for c in (s.light, s.dark)}


def check_recipe(
    recipe: Recipe[Any], *, require_about: bool = True, raise_on_failure: bool = True
) -> RecipeReport:
    """Run the plot recipe contract checks (see the module docs for the list).

    The example is rendered once and every example check reads that render. A check that
    needs something an earlier check could not produce fails with ``not checked: ...``.
    ``require_about=False`` passes the ``about`` check for a recipe without a card; a card
    that is there must still be complete. With ``raise_on_failure`` (the
    default) a failed check raises :class:`RecipeContractError`."""
    checks: list[tuple[str, str | None]] = []
    warnings: list[tuple[str, str]] = []
    subject = getattr(recipe, "ref", repr(recipe))
    state: dict[str, Any] = {}

    def run(name: str, fn: Callable[[], str | None]) -> None:
        try:
            reason = fn()
        except _NotChecked as exc:
            reason = str(exc)
        except Exception as exc:  # recipe code: a crash is that check's failure
            reason = f"{type(exc).__name__}: {str(exc)[:500]}"
        checks.append((name, reason))

    def catalog_entry() -> str | None:
        entry = recipe.catalog_entry()
        if not isinstance(entry, PlotCatalogEntry):
            return "catalog_entry() must return a PlotCatalogEntry"
        state["entry"] = entry
        if "." not in recipe.id:
            return f"the id {recipe.id!r} has no namespace: use '<namespace>.<name>'"
        bad = [t for t in recipe.tags if t not in recipe_tags([t])]
        if bad:
            return f"tags must be lowercase slugs ({', '.join(map(repr, bad[:3]))} are not)"
        if len(set(recipe.tags)) > 8:
            return "a recipe has at most 8 tags"
        if recipe.pack_label is not None and not 0 < len(recipe.pack_label) <= 40:
            return "pack_label must have 1 to 40 characters"
        return None

    def namespace() -> str | None:
        return check_namespace(recipe.id, _distribution_of(recipe.fn))

    def about() -> str | None:
        entry: PlotCatalogEntry | None = state.get("entry")
        if entry is None:
            return "not checked: the catalog entry did not build"
        if entry.about is None and not require_about:
            return None
        problems = about_issues(entry.about)
        return "; ".join(problems) if problems else None

    def settings_schema() -> str | None:
        raw = recipe.settings.model_json_schema()
        _, issues = sanitize_schema(raw, mode="refuse", where="/settings_schema")
        if issues:
            first = issues[0]
            return f"{first.message} (at {first.pointer or '/settings_schema'})" + (
                f"; {len(issues) - 1} more" if len(issues) > 1 else ""
            )
        entry: PlotCatalogEntry | None = state.get("entry")
        published = entry.settings_schema if entry is not None else None
        if published is None:
            size = len(json.dumps(raw, separators=(",", ":")).encode())
            return (
                f"the settings schema is {size} bytes; the catalog publishes at most "
                f"{SETTINGS_SCHEMA_MAX_BYTES} (fewer or shorter settings)"
            )
        state["schema"] = published
        return None

    def settings_defaults() -> str | None:
        try:
            recipe.settings.model_validate({})
        except ValidationError as exc:
            missing = [".".join(map(str, e["loc"])) for e in exc.errors()][:3]
            return f"every setting needs a default (no default: {', '.join(missing)})"
        schema = state.get("schema")
        if schema is not None:
            errors = list(Draft202012Validator(schema).iter_errors({}))
            if errors:
                return f"the published schema refuses the defaults: {errors[0].message[:200]}"
        return None

    def settings_extra_refused() -> str | None:
        try:
            recipe.settings.model_validate({_UNKNOWN_KEY: 1})
        except ValidationError:
            pass
        else:
            return "unknown settings are accepted: subclass RecipeSettings (extra='forbid')"
        schema = state.get("schema")
        if schema is not None and Draft202012Validator(schema).is_valid({_UNKNOWN_KEY: 1}):
            return "the published schema accepts unknown settings (additionalProperties)"
        return None

    def example_renders() -> str | None:
        spec = recipe.example_spec()
        if spec is None:
            return (
                "the recipe has no example: pass example=lambda: (data, options) to @recipe "
                "(the catalog card and the playground render it)"
            )
        if spec.id != recipe.id:
            return f"the example plot id is {spec.id!r}, not {recipe.id!r}"
        state["example"] = spec
        return None

    def need_example() -> PlotSpec:
        spec: PlotSpec | None = state.get("example")
        if spec is None:
            raise _NotChecked("not checked: the example did not render")
        return spec

    def example_inline() -> str | None:
        spec = need_example()
        if not spec.datasets:
            return "the example has no datasets"
        refs = [name for name, d in spec.datasets.items() if not isinstance(d, InlineArrow)]
        if refs:
            return f"example datasets must be inline (not: {', '.join(refs[:3])})"
        return None

    def example_sanitized() -> str | None:
        spec = need_example()
        issues = check_plot(spec.model_dump(mode="json"))
        errors = [i for i in issues if i.severity == Severity.ERROR]
        if errors:
            return (
                f"the sanitizer refuses the example: {errors[0].message} (at {errors[0].pointer})"
            )
        if issues:
            return (
                f"the sanitizer changes the example: {issues[0].message} (at {issues[0].pointer})"
            )
        return None

    def caption() -> str | None:
        spec = need_example()
        if not (spec.caption_plain or "").strip():
            return "the example has no caption: return Figure(caption=...) in plain words"
        return None

    run("catalog_entry", catalog_entry)
    run("namespace", namespace)
    run("about", about)
    run("settings_schema", settings_schema)
    run("settings_defaults", settings_defaults)
    run("settings_extra_refused", settings_extra_refused)
    run("example_renders", example_renders)
    run("example_inline", example_inline)
    run("example_sanitized", example_sanitized)
    run("caption", caption)

    spec = state.get("example")
    if spec is not None:
        palette = _palette()
        odd = sorted({c for c in _colours(spec.spec) if c.lower() not in palette})
        if odd:
            more = " ..." if len(odd) > _MAX_LISTED_COLOURS else ""
            listed = ", ".join(odd[:_MAX_LISTED_COLOURS]) + more
            warnings.append(
                (
                    "colours",
                    f"colours outside the platform palette ({listed}) do not follow the dark "
                    "theme: use the swatches in mapi_plots.palette",
                )
            )

    report = RecipeReport(
        ok=all(reason is None for _, reason in checks),
        checks=checks,
        subject=subject,
        warnings=warnings,
    )
    if raise_on_failure and not report.ok:
        raise RecipeContractError(report)
    return report
