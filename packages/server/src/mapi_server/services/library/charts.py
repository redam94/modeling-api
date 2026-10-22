"""Chart templates on real plots and on catalog examples (v0.18 D5).

Applying a chart template restyles a stored plot of a model version: the version's ``plots``
row made by the template's source recipe gives the datasets (``data_ref`` paths into the
version's bundle, passed through untouched: the web reads them with the version's artifact
links), the template gives the Vega-Lite spec, and :func:`mapi_plots.chart_templates
.apply_chart_template` swaps the spec, fills role bindings against the model stage's schema
and runs the plot sanitizer. The result carries ``provenance.author = lib_<id>-r<n>``, which
:func:`mapi_spec.sharing.embeddable` refuses: authored charts never reach share links,
client portals or dashboard pictures.

A preview does the same on the source recipe's inline catalog example (no bindings: there is
no model). Both are JSON only: no data is decoded and nothing runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from mapi_plots.chart_templates import apply_chart_template
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity
from mapi_spec.library import ChartPreview, ChartTemplateDoc, parse_library_doc
from mapi_spec.plots import PlotSpec
from mapi_spec.refs import Version
from mapi_spec.schema import DataSchema
from mapi_spec.validate import ScoreMetadata
from pydantic import JsonValue, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.library.compile import per_minute, refusal, source_example
from mapi_server.services.runs import Scope

__all__ = ["PREVIEW_AUTHOR", "apply_chart", "model_stage_schema", "preview_chart", "source_plot"]

PREVIEW_AUTHOR: Final = "library-preview"
"""``provenance.author`` of a previewed chart (it is never stored)."""


def model_stage_schema(mv: m.ModelVersion) -> DataSchema | None:
    """The schema of the data the model saw (the model stage of the version's stored score
    metadata); ``None`` when it was not recorded or could not be typed."""
    if not mv.score_meta:
        return None
    try:
        resolved = ScoreMetadata.model_validate(mv.score_meta).resolved
    except ValidationError:
        return None
    return resolved.stages.get(resolved.model_stage)


async def source_plot(
    session: AsyncSession, mv: m.ModelVersion, doc: ChartTemplateDoc
) -> PlotSpec | None:
    """The version's stored plot made by the template's source recipe in its version range
    (the first by plot id), or ``None``."""
    rows = (
        await session.execute(
            select(m.Plot)
            .where(m.Plot.model_version_id == mv.id, m.Plot.recipe.is_not(None))
            .order_by(m.Plot.plot_id)
        )
    ).scalars()
    for row in rows:
        rid, _, version = (row.recipe or "").partition("@")
        if rid != doc.source_recipe.id:
            continue
        try:
            if version and not doc.source_recipe.range.satisfies(Version.parse(version)):
                continue
            return PlotSpec.model_validate(row.spec)
        except (ValueError, ValidationError):
            continue
    return None


async def apply_chart(
    session: AsyncSession,
    scope: Scope,
    doc: ChartTemplateDoc,
    *,
    model_id: str,
    version: int,
    author: str,
) -> tuple[PlotSpec, list[Issue]]:
    """Apply ``doc`` to model version ``model_id`` v``version`` of ``scope``'s project.

    ``404`` for an unknown version; ``LIBRARY_INVALID`` (422) when the version has no plot
    of the source recipe; the template's own refusals (unknown dataset or column, unsafe
    spec: ``PLOT_UNSAFE``) answer ``422`` with the issues."""
    model = await session.get(m.Model, model_id)
    if model is None or (model.tenant_id, model.project_id) != (scope.tenant_id, scope.project_id):
        raise NotFoundError("model", model_id)
    mv = (
        await session.execute(
            select(m.ModelVersion).where(
                m.ModelVersion.model_id == model.id, m.ModelVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if mv is None:
        raise NotFoundError("model version", f"{model_id} v{version}")
    base = await source_plot(session, mv, doc)
    if base is None:
        raise MapiError(
            f"version {version} of model {model.name!r} has no plot made by "
            f"{str(doc.source_recipe)!r}",
            code=ErrorCode.LIBRARY_INVALID,
            pointer="/source_recipe",
        )
    plot, issues = apply_chart_template(doc, base, schema=model_stage_schema(mv), author=author)
    if plot is None:
        raise refusal(issues, "the chart template cannot be applied")
    return plot, issues


async def preview_chart(
    ctx: ServerContext, session: AsyncSession, document: Mapping[str, JsonValue]
) -> ChartPreview:
    """``POST /v1/library/charts:preview``: the template drawn on its source recipe's catalog
    example. Template problems are answered as issues with ``plot: null`` (the editor shows
    them as the author types); a recipe missing from the catalog, or without an example, is
    ``EXTENSION_UNAVAILABLE`` (409)."""
    await per_minute(
        ctx,
        "libchart",
        ctx.settings.library_chart_preview_per_minute,
        "chart template previews",
    )
    doc = parse_library_doc(document)
    if not isinstance(doc, ChartTemplateDoc):
        raise MapiError(
            "only a chart template can be previewed",
            code=ErrorCode.LIBRARY_INVALID,
            pointer="/kind",
        )
    snapshot = await ctx.catalogs.require(session)
    if snapshot.catalog.resolve_plot(doc.source_recipe) is None:
        raise MapiError(
            f"the plot recipe {str(doc.source_recipe)!r} is not in the current catalog",
            code=ErrorCode.EXTENSION_UNAVAILABLE,
            pointer="/source_recipe",
        )
    example = await source_example(ctx, session, snapshot.catalog, doc)
    if example is None:
        raise MapiError(
            f"the plot recipe {str(doc.source_recipe)!r} has no example data to preview on",
            code=ErrorCode.EXTENSION_UNAVAILABLE,
            pointer="/source_recipe",
        )
    plot, issues = apply_chart_template(doc, example, author=PREVIEW_AUTHOR)
    if plot is not None and not any(i.severity is Severity.ERROR for i in issues):
        return ChartPreview(plot=plot, issues=issues)
    return ChartPreview(plot=None, issues=issues)
