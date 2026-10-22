"""The source catalog of a model version, and the layout 2 checks against it (v0.17, I-6).

A layout 2 panel binds a typed **source** (a model output, a fit metric, a scorecard check,
a view, an accuracy metric) and an input binds a variable or a dim. :func:`source_catalog`
says what one model version offers to them, from its real artefacts:

* the plugin's :class:`~mapi_spec.capabilities.ModelManifest` from the catalog of the image
  (the entry of exactly the plugin version the bundle was fitted with), expanded by
  :func:`mapi_spec.dashboard_presets.catalog_from_manifest` over the version's raw input
  schema; the dims of each output take the **dataset** names (the score summary names its
  dim columns as the dataset does), the inputs keep the canonical names (inline payloads
  use them);
* the fit run's metric series (``run_metric_summary``), the scorecard check ids, the
  embeddable plots of the version (platform recipe provenance only, never a version that
  ran uploaded code);
* the input contract (:func:`mapi_server.services.endpoints.snapshot_for`): score modes,
  conditioning, the fitted levels and date ranges of each dim, and the fit data's range of
  each variable (``profile.json`` of the fit dataset, size-capped, JSON only).

A model version never changes, so the catalog is cached in the process per version (and
catalog digest). :func:`check_version_v2` is the layout 2 half of ``:validate`` and
``:publish``: the version must be ready, fitted with platform plugins, and able to give what
the spec asks (:func:`mapi_spec.dashboard_presets.check_spec_v2`; for the media planner also
the response grid of v0.16).

No numpy: everything is JSON in the control plane.
"""

from __future__ import annotations

import contextlib
import logging
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from mapi_spec.capabilities import ModelManifest
from mapi_spec.dashboard_presets import catalog_from_manifest
from mapi_spec.dashboard_presets import check_spec_v2 as _check_spec_v2
from mapi_spec.dashboard_v2 import (
    CatalogInput,
    CatalogMetric,
    CatalogOutput,
    CatalogPicture,
    DashboardSpecV2,
    SourceCatalog,
)
from mapi_spec.endpoints import EndpointSchemaSnapshot
from mapi_spec.errors import ErrorCode, Issue, MapiError
from mapi_spec.scorecard import Scorecard
from mapi_spec.sharing import embeddable
from mapi_spec.validate import ScoreMetadata
from mapi_spec.views import ProfileView
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.jsonlimits import loads_limited
from mapi_server.services.endpoints import snapshot_for
from mapi_server.services.sharing import ran_uploaded_code

__all__ = [
    "MAX_PROFILE_BYTES",
    "VersionSources",
    "build_catalog",
    "check_version_v2",
    "clear_catalog_cache",
    "source_catalog",
    "version_sources",
]

log = logging.getLogger(__name__)

MAX_PROFILE_BYTES: Final = 2 * 1024 * 1024
"""The largest ``profile.json`` of a fit dataset the catalog reads (larger: no profile ranges)."""
_CACHE_SIZE: Final = 256
_MAX_LEVELS: Final = 200
_MAX_METRICS: Final = 500


@dataclass(frozen=True, slots=True)
class VersionSources:
    """What :func:`version_sources` found: the manifest (``None`` when the image catalog
    does not list the plugin version) and the catalog built from it."""

    manifest: ModelManifest | None
    catalog: SourceCatalog


_CACHE: OrderedDict[tuple[str, str], VersionSources] = OrderedDict()


def clear_catalog_cache() -> None:
    """Forget every cached catalog (tests)."""
    _CACHE.clear()


# ==================================================================================== builder


def _dataset_name(binding: Mapping[str, str], name: str | None) -> str | None:
    return binding.get(name, name) if name is not None else None


def _scalar_text(v: object) -> str:
    return str(v)[:64]


def _as_date(v: object) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v is not None else None
    except ValueError:
        return None


def _input(
    ci: CatalogInput,
    snapshot: EndpointSchemaSnapshot | None,
    profile: ProfileView | None,
    var_names: Mapping[str, str],
) -> CatalogInput:
    """``ci`` with the fitted levels/date range of its dim and the fit data's range of its
    variable (the profile names variables as the dataset does)."""
    update: dict[str, Any] = {}
    if ci.dim is not None and snapshot is not None:
        coord = snapshot.fit_coords.get(ci.dim)
        if coord is not None:
            if coord.kind == "time":
                update["date_min"] = _as_date(coord.start)
                update["date_max"] = _as_date(coord.end)
            if coord.values is not None and coord.kind != "time":
                update["levels"] = [_scalar_text(v) for v in coord.values[:_MAX_LEVELS]]
    if ci.var is not None and profile is not None:
        prof = profile.vars.get(var_names.get(ci.var, ci.var)) or profile.vars.get(ci.var)
        if prof is not None and ci.kind in ("number", "integer"):
            update["profile_min"] = prof.min
            update["profile_max"] = prof.max
    return ci.model_copy(update=update) if update else ci


def _output(o: CatalogOutput, dims: Mapping[str, str]) -> CatalogOutput:
    renamed = [dims.get(d, d) for d in o.dims]
    if renamed == o.dims:
        return o
    return o.model_copy(update={"dims": renamed, "time_dim": _dataset_name(dims, o.time_dim)})


def build_catalog(
    manifest: ModelManifest | None,
    *,
    snapshot: EndpointSchemaSnapshot | None = None,
    binding_dims: Mapping[str, str] | None = None,
    binding_vars: Mapping[str, str] | None = None,
    metrics: Sequence[CatalogMetric] = (),
    scorecard: Sequence[str] = (),
    pictures: Sequence[CatalogPicture] = (),
    profile: ProfileView | None = None,
) -> SourceCatalog:
    """The catalog of a version from what the server read (pure: tests feed it fixtures).

    Without a manifest the catalog has no outputs and no inputs (the checks then skip the
    rules on them) but keeps the input contract's score modes."""
    dims = dict(binding_dims or {})
    var_names = dict(binding_vars or {})
    schema = snapshot.input_schema if snapshot is not None else None
    # without a manifest: no outputs and no inputs (the checks then skip their rules), the
    # score modes of the input contract only
    base = (
        catalog_from_manifest(manifest, schema=schema)
        if manifest is not None
        else SourceCatalog(row_dim=None)
    )
    modes = list(base.score_modes)
    if snapshot is not None:
        modes = sorted(set(modes) | set(snapshot.score_modes))
    views: list[Any] = []
    if "forecast" in modes:
        views.append("forecast")
    if base.has_grid:
        views.append("fields")
    if "counterfactual" in modes:
        views.append("counterfactual")
    return base.model_copy(
        update={
            "outputs": [_output(o, dims) for o in base.outputs],
            "inputs": [_input(ci, snapshot, profile, var_names) for ci in base.inputs],
            "metrics": list(metrics)[:_MAX_METRICS],
            "scorecard": list(dict.fromkeys(scorecard))[:64],
            "pictures": list(pictures)[:64],
            "score_modes": modes,
            "conditions_on_actuals": (
                snapshot.conditions_on_actuals
                if snapshot is not None
                else base.conditions_on_actuals
            ),
            "views": views,
        }
    )


# ==================================================================================== readers


async def _manifest(
    ctx: ServerContext, session: AsyncSession, meta: ScoreMetadata | None
) -> tuple[ModelManifest | None, str]:
    """The plugin manifest of exactly the version the bundle was fitted with, and the
    digest of the image catalog it came from (part of the cache key)."""
    if meta is None or ctx.catalogs is None:
        return None, ""
    snap = await ctx.catalogs.get(session)
    if snap is None:
        return None, ""
    ref = meta.resolved.model.resolved
    entry = next((e for e in snap.catalog.models if e.ref == ref), None)
    return (entry.manifest if entry is not None else None), snap.digest


async def _metrics(session: AsyncSession, mv: m.ModelVersion) -> list[CatalogMetric]:
    rows = (
        await session.execute(
            select(m.RunMetricSummary)
            .where(m.RunMetricSummary.run_id == mv.run_id)
            .order_by(m.RunMetricSummary.key, m.RunMetricSummary.labels_key)
            .limit(_MAX_METRICS * 2)
        )
    ).scalars()
    out: list[CatalogMetric] = []
    for row in rows:
        labels = {str(k): str(v) for k, v in (row.labels or {}).items()}
        try:
            out.append(CatalogMetric(key=row.key, labels=labels, last=row.last))
        except ValidationError:  # a key or labels a panel cannot bind (too many, too long)
            continue
        if len(out) >= _MAX_METRICS:
            break
    return out


def _scorecard(mv: m.ModelVersion) -> list[str]:
    if not mv.scorecard:
        return []
    try:
        card = Scorecard.model_validate(mv.scorecard)
    except ValidationError:
        return []
    return [i.check for i in card.items if i.check]


async def _pictures(session: AsyncSession, mv: m.ModelVersion) -> list[CatalogPicture]:
    """Plots of the version that may become pictures: platform recipes only, and none at
    all for a version that ran uploaded code (what its plot documents claim is not
    trusted)."""
    if ran_uploaded_code(mv):
        return []
    rows = (
        await session.execute(
            select(m.Plot).where(m.Plot.model_version_id == mv.id).order_by(m.Plot.plot_id)
        )
    ).scalars()
    out: list[CatalogPicture] = []
    for row in rows:
        prov = (row.spec or {}).get("provenance")
        if not isinstance(prov, dict):
            continue
        # PlotProvenance: exactly one of recipe, plugin or author (the stored, sanitized doc)
        source = "recipe" if prov.get("recipe") else "plugin" if prov.get("plugin") else "author"
        recipe = str(prov.get("recipe") or "") or None
        if recipe is None or row.recipe != recipe or not embeddable("model_plot", source, recipe):
            continue
        name = recipe.split("@", 1)[0]  # panels name recipes without a version
        out.append(CatalogPicture(plot_id=row.plot_id, recipe=name, title=row.title[:200]))
        if len(out) >= 64:
            break
    return out


async def _profile(
    ctx: ServerContext, session: AsyncSession, mv: m.ModelVersion
) -> ProfileView | None:
    """The fit dataset's ``profile.json`` (``None`` when absent, too large or invalid)."""
    fit = await session.get(m.Run, mv.run_id)
    if fit is None or fit.dataset_id is None or fit.dataset_version is None:
        return None
    dv = await session.get(m.DatasetVersion, (fit.dataset_id, fit.dataset_version))
    obj = (dv.objects or {}).get("profile.json") if dv is not None else None
    if not isinstance(obj, dict):
        return None
    try:
        raw = await ctx.store.get(
            str(obj["bucket"]),
            str(obj["name"]),
            generation=int(obj["generation"]),
            max_bytes=MAX_PROFILE_BYTES,
        )
        return ProfileView.model_validate(loads_limited(raw))
    except (KeyError, ValueError, ValidationError, MapiError) as exc:
        log.info("model version %s: no profile ranges (%s)", mv.id, type(exc).__name__)
        return None
    except Exception:  # storage errors: the catalog still works without profile ranges
        log.warning("model version %s: the fit profile could not be read", mv.id, exc_info=True)
        return None


async def version_sources(
    ctx: ServerContext, session: AsyncSession, mv: m.ModelVersion
) -> VersionSources:
    """The manifest and the source catalog of ``mv`` (cached per version and catalog)."""
    meta: ScoreMetadata | None = None
    snapshot: EndpointSchemaSnapshot | None = None
    if mv.score_meta is not None:
        try:
            meta = ScoreMetadata.model_validate(mv.score_meta)
            snapshot = snapshot_for(mv)
        except (MapiError, ValidationError):
            meta, snapshot = None, None
    manifest, digest = await _manifest(ctx, session, meta)
    key = (mv.id, digest)
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
        return hit
    binding = meta.resolved.binding if meta is not None else None
    catalog = build_catalog(
        manifest,
        snapshot=snapshot,
        binding_dims=binding.dims if binding is not None else None,
        binding_vars=binding.vars if binding is not None else None,
        metrics=await _metrics(session, mv),
        scorecard=_scorecard(mv),
        pictures=await _pictures(session, mv),
        profile=await _profile(ctx, session, mv) if snapshot is not None else None,
    )
    found = VersionSources(manifest, catalog)
    _CACHE[key] = found
    while len(_CACHE) > _CACHE_SIZE:
        _CACHE.popitem(last=False)
    return found


async def source_catalog(
    ctx: ServerContext, session: AsyncSession, mv: m.ModelVersion
) -> SourceCatalog:
    """``GET …/sources``: what ``mv`` offers to panels and inputs (I-6)."""
    return (await version_sources(ctx, session, mv)).catalog


# ===================================================================================== checks


def _issue(code: ErrorCode, pointer: str, message: str) -> Issue:
    return Issue(code=code, pointer=pointer, message=message)


async def _check_planner(
    ctx: ServerContext,
    session: AsyncSession,
    spec: DashboardSpecV2,
    mv: m.ModelVersion,
    issues: list[Issue],
) -> None:
    """The v0.16 checks of the media planner: the response grid with every channel in it,
    and the KPI among the model's variables."""
    from mapi_server.services.dashboards.crud import read_grid

    planner = spec.mmm
    if planner is None:  # the spec validator requires it; nothing to check
        return
    try:
        grid = await read_grid(ctx, mv)
    except MapiError as exc:
        issues.append(_issue(exc.code, "/source", f"the response grid cannot be read: {exc}"))
        grid = None
    if grid is None and planner.whatif.enabled:
        issues.append(
            _issue(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "/mmm/whatif/enabled",
                "the model version has no response grid: turn what-ifs off",
            )
        )
    if grid is not None:
        known = set(grid.channels)
        for i, ch in enumerate(planner.channels):
            if ch.name not in known:
                issues.append(
                    _issue(
                        ErrorCode.BINDING_INVALID,
                        f"/mmm/channels/{i}/name",
                        f"channel {ch.name!r} is not in the model's response grid",
                    )
                )
    variables: set[str] = {grid.kpi} if grid is not None else set()
    with contextlib.suppress(MapiError):
        variables |= set(snapshot_for(mv).input_schema.vars)
    if variables and spec.subject.variable not in variables:
        issues.append(
            _issue(
                ErrorCode.BINDING_INVALID,
                "/subject/variable",
                f"{spec.subject.variable!r} is not a variable of the model",
            )
        )


async def check_version_v2(
    ctx: ServerContext,
    session: AsyncSession,
    spec: DashboardSpecV2,
    mv: m.ModelVersion,
    *,
    catalog: SourceCatalog | None = None,
) -> list[Issue]:
    """What stops ``spec`` on ``mv`` (errors) or asks the analyst to check (warnings,
    infos): a version that is not ready or ran uploaded code, a catalog that cannot be read,
    and every rule of :func:`mapi_spec.dashboard_presets.check_spec_v2` (against
    ``catalog`` when the caller already read it, else :func:`source_catalog`)."""
    issues: list[Issue] = []
    if mv.status != "ready":
        issues.append(_issue(ErrorCode.NOT_READY, "/source", f"model version {mv.id} is not ready"))
    if ran_uploaded_code(mv):
        issues.append(
            _issue(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "/source",
                "the model version ran uploaded plugin code; dashboards show only versions "
                "fitted with platform plugins",
            )
        )
        return issues
    if catalog is None:
        try:
            catalog = await source_catalog(ctx, session, mv)
        except MapiError as exc:
            why = f"the model's sources cannot be read: {exc}"
            issues.append(_issue(exc.code, "/source", why))
            return issues
    if spec.preset == "mmm_planner":
        await _check_planner(ctx, session, spec, mv, issues)
    if spec.actuals.enabled and mv.score_meta is None:
        issues.append(
            _issue(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "/actuals/enabled",
                "the model version has no input contract for actuals: turn uploads off",
            )
        )
    issues += _check_spec_v2(spec, catalog)
    return issues[:200]
