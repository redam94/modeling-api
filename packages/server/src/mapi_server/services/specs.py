"""Static spec validation and reference resolution (JSON only, plan §3.3 layer 2).

Documents are parsed with the strict loader (no anchors/tags, size/depth/node caps),
validated as typed documents, then checked against the cached catalog and the *stored*
``DataSchema`` of the referenced dataset version — never against data. Everything is
reported at once as :class:`~mapi_spec.errors.Issue` objects with JSON pointers.

The per-class data caps (plan §8.5) are checked here too: the stored sizes of the dataset
and side-table versions a run would read must fit the resource class's dataset cap.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from mapi_spec.capabilities import Capabilities
from mapi_spec.coords import DataSummary
from mapi_spec.errors import (
    ErrorCode,
    Issue,
    MapiError,
    Severity,
    ValidationFailed,
    has_errors,
)
from mapi_spec.jobs import INPUT_DATASET, INPUT_SCENARIO, SCORE_INPUTS, side_table_input
from mapi_spec.loader import check_limits, parse_document, parse_text, spec_hash
from mapi_spec.quotas import max_score_draws
from mapi_spec.refs import DatasetRef, EndpointRef, ModelVersionRef
from mapi_spec.resources import CPU_CLASSES, RESOURCE_CLASSES, ResourceClass
from mapi_spec.schema import DataSchema
from mapi_spec.spec import ModelSpec, ScoreRequest
from mapi_spec.tree import ROOT, DataLayout, TreeSchema
from mapi_spec.validate import ResolvedSpec, ScoreMetadata, validate_score_request, validate_spec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m
from mapi_server.services.calibration import check_spec_calibration
from mapi_server.services.catalog import CatalogService, CatalogSnapshot
from mapi_server.services.runs import Scope

__all__ = [
    "COLLECTION_REFUSED",
    "DOCUMENT_KINDS",
    "Validation",
    "check_score_request",
    "class_for_size",
    "data_cap_issue",
    "first_error",
    "layout_of",
    "live_capabilities",
    "pin_document",
    "refuse_retired_kind",
    "resolve_dataset",
    "resolve_model_version",
    "score_draw_cap",
    "side_table_node_issue",
    "tree_schema_of",
    "validate_document",
]

DOCUMENT_KINDS = ("ModelSpec", "ScoreRequest", "Sweep", "Backtest", "SBC", "Campaign")
COLLECTION_REFUSED = "side tables and workflow inputs must be datasets, not image collections"
"""v0.15: why a collection version cannot be a side table or a workflow input."""
RETIRED_KINDS: dict[str, str] = {"Sweep": "Use a workflow with a search.grid node"}
"""v0.14: kinds stored documents may still have, which new specs and revisions may not."""


def refuse_retired_kind(v: Validation) -> None:
    """A new spec or revision of a retired kind (``Sweep``) is refused: 422 at ``/kind``.
    Stored documents of that kind still read and list."""
    action = RETIRED_KINDS.get(str(v.document.get("kind")))
    if action is not None:
        raise MapiError(
            f"{v.document['kind']} documents are retired. {action}.",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/kind",
        )


@dataclass
class Validation:
    """Outcome of :func:`validate_document`."""

    document: dict[str, Any]
    kind: str | None
    hash: str | None
    issues: list[Issue] = field(default_factory=list)
    stages: list[tuple[str, DataSchema | None, bool]] = field(default_factory=list)
    resolved: ResolvedSpec | None = None
    catalog: CatalogSnapshot | None = None
    dataset: m.DatasetVersion | None = None
    side_tables: dict[str, m.DatasetVersion] = field(default_factory=dict)
    model_version: m.ModelVersion | None = None
    typed: ModelSpec | ScoreRequest | None = None

    @property
    def valid(self) -> bool:
        return not has_errors(self.issues)


def _issue(code: ErrorCode, pointer: str, message: str) -> Issue:
    return Issue(code=code, pointer=pointer, message=message[:2000])


def tree_schema_of(dv: m.DatasetVersion) -> TreeSchema | None:
    """The stored :class:`TreeSchema` of a tree version (M4); ``None`` for a plain dataset."""
    if dv.layout != "tree" or not dv.tree_schema:
        return None
    return TreeSchema.model_validate(dv.tree_schema)


def layout_of(dv: m.DatasetVersion) -> DataLayout:
    """The stored layout of a version (``dataset`` for anything unknown)."""
    if dv.layout == "tree":
        return "tree"
    return "collection" if dv.layout == "collection" else "dataset"


def side_table_node_issue(key: str, node: str | None, tree: TreeSchema | None) -> Issue | None:
    """M4: a tree side table must name ONE node of its version (``SideTable.node``); a plain
    one names none (or the root). The same rules the runner applies, checked statically."""
    pointer = f"/data/side_tables/{key}/node"
    if tree is None:
        if node not in (None, ROOT):
            return _issue(
                ErrorCode.SCHEMA_INVALID,
                pointer,
                f"side table {key!r} is a plain dataset: it has no node {node!r}",
            )
        return None
    if node is None:
        return _issue(
            ErrorCode.SCHEMA_INVALID,
            pointer,
            f"side table {key!r} is a tree dataset: name the node to read in "
            f"data.side_tables.{key}.node",
        )
    if node not in tree.nodes:
        return _issue(
            ErrorCode.SCHEMA_INVALID,
            pointer,
            f"side table {key!r} has no node {node!r} (nodes: {', '.join(list(tree.nodes)[:20])})",
        )
    return None


def class_for_size(size_bytes: int) -> ResourceClass | None:
    """The smallest resource class whose dataset cap holds ``size_bytes`` (plan §8.5).
    Never ``GPU`` (v0.15): an accelerator is asked for, not picked by data size."""
    for rc in CPU_CLASSES:
        if size_bytes <= RESOURCE_CLASSES[rc].max_dataset_bytes:
            return rc
    return None


def _gb(n: int) -> str:
    return f"{n / 1e9:.2f} GB"


def data_cap_issue(
    resource_class: ResourceClass,
    versions: list[m.DatasetVersion],
    pointer: str = "/execution/resource_class",
) -> Issue | None:
    """``PAYLOAD_TOO_LARGE`` when the input data exceeds the class's dataset cap. Collection
    versions (v0.15) do not count: their images are streamed from packs, and the sample a
    model that reads datasets gets (``data.collection``) is capped at run time."""
    total = sum(v.size_bytes or 0 for v in versions if v.layout != "collection")
    cap = RESOURCE_CLASSES[resource_class].max_dataset_bytes
    if total <= cap:
        return None
    message = (
        f"the input data ({_gb(total)}) is more than the {_gb(cap)} dataset cap of resource "
        f"class {resource_class.value}"
    )
    need = class_for_size(total)
    if need is not None:
        message += f". Use resource class {need.value} or larger"
    return _issue(ErrorCode.PAYLOAD_TOO_LARGE, pointer, message)


async def resolve_dataset(
    session: AsyncSession, scope: Scope, ref: DatasetRef
) -> tuple[m.DatasetVersion | None, str | None]:
    """The **ready** dataset version a ref points at, or ``(None, reason)``."""
    if ref.project != scope.project_id:
        return None, f"project {ref.project!r} does not exist (M1 has only {scope.project_id!r})"
    ds = (
        await session.execute(
            select(m.Dataset).where(
                m.Dataset.tenant_id == scope.tenant_id,
                m.Dataset.project_id == scope.project_id,
                m.Dataset.name == ref.name,
            )
        )
    ).scalar_one_or_none()
    if ds is None:
        return None, f"dataset {ref.name!r} does not exist"
    stmt = select(m.DatasetVersion).where(
        m.DatasetVersion.dataset_id == ds.id, m.DatasetVersion.status == "ready"
    )
    if ref.is_pinned:
        stmt = stmt.where(m.DatasetVersion.sha256 == ref.version)
    elif ref.version_number is not None:
        stmt = stmt.where(m.DatasetVersion.version == ref.version_number)
    stmt = stmt.order_by(m.DatasetVersion.version.desc()).limit(1)
    dv = (await session.execute(stmt)).scalar_one_or_none()
    if dv is None:
        return None, f"no ready version of dataset {ref.name!r} matches {ref.version or 'latest'!r}"
    return dv, None


async def resolve_model_version(
    session: AsyncSession, scope: Scope, ref: ModelVersionRef | EndpointRef
) -> tuple[m.ModelVersion | None, str | None]:
    """The model version a ref points at (an endpoint: the version it serves now), or
    ``(None, reason)``."""
    if ref.project != scope.project_id:
        return None, f"project {ref.project!r} does not exist"
    if isinstance(ref, EndpointRef):
        endpoint = (
            await session.execute(
                select(m.Endpoint).where(
                    m.Endpoint.tenant_id == scope.tenant_id,
                    m.Endpoint.project_id == scope.project_id,
                    m.Endpoint.alias == ref.alias,
                )
            )
        ).scalar_one_or_none()
        if endpoint is None:
            return None, f"endpoint {ref.alias!r} does not exist"
        served = await session.get(m.ModelVersion, endpoint.model_version_id)
        if served is None:
            return None, f"endpoint {ref.alias!r} serves no model version"
        return served, None
        return None, f"project {ref.project!r} does not exist"
    model = (
        await session.execute(
            select(m.Model).where(
                m.Model.tenant_id == scope.tenant_id,
                m.Model.project_id == scope.project_id,
                m.Model.name == ref.name,
            )
        )
    ).scalar_one_or_none()
    if model is None:
        return None, f"model {ref.name!r} does not exist"
    stmt = select(m.ModelVersion).where(m.ModelVersion.model_id == model.id)
    if ref.version == "latest":
        stmt = stmt.order_by(m.ModelVersion.version.desc()).limit(1)
    else:
        stmt = stmt.where(m.ModelVersion.version == int(ref.version.removeprefix("v")))
    mv = (await session.execute(stmt)).scalar_one_or_none()
    if mv is None:
        return None, f"model {ref.name!r} has no version {ref.version!r}"
    return mv, None


async def validate_document(
    session: AsyncSession,
    scope: Scope,
    catalogs: CatalogService,
    *,
    text: str | None = None,
    document: dict[str, Any] | None = None,
    dataset_override: str | None = None,
    resource_class: ResourceClass | None = None,
) -> Validation:
    """Parse and statically validate one document. Parse/size errors raise; everything
    else is reported as issues (so editors get every problem at once).

    ``resource_class`` overrides the spec's ``execution.resource_class`` for the data-cap
    check (a run submitted with an explicit class).
    """
    data: Any = parse_text(text) if text is not None else document
    if text is None:
        check_limits(data)
    if not isinstance(data, dict):
        raise ValidationFailed(
            [_issue(ErrorCode.SCHEMA_INVALID, "", "a document must be a mapping")]
        )
    kind = data.get("kind") if data.get("kind") in DOCUMENT_KINDS else None
    out = Validation(document=data, kind=kind, hash=spec_hash(data))
    try:
        typed = parse_document(data)
    except ValidationFailed as exc:
        out.issues.extend(exc.issues)
        return out
    except MapiError as exc:  # UnsupportedApiVersion and friends
        out.issues.append(exc.to_issue())
        return out
    if isinstance(typed, ModelSpec):
        out.typed = typed
        await _validate_model_spec(
            session, scope, catalogs, typed, out, dataset_override, resource_class
        )
    elif isinstance(typed, ScoreRequest):
        out.typed = typed
        await _validate_score(session, scope, typed, out, catalogs)
    else:
        out.issues.append(
            _issue(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "/kind",
                f"{typed.kind} documents (fan-out parents) arrive in v0.5 (M4)",
            )
        )
    return out


async def _validate_model_spec(
    session: AsyncSession,
    scope: Scope,
    catalogs: CatalogService,
    spec: ModelSpec,
    out: Validation,
    dataset_override: str | None,
    resource_class: ResourceClass | None = None,
) -> None:
    if spec.metadata.project != scope.project_id:
        out.issues.append(
            _issue(
                ErrorCode.REF_UNRESOLVED,
                "/metadata/project",
                f"project {spec.metadata.project!r} does not exist "
                f"(M1 has only {scope.project_id!r})",
            )
        )
    snapshot = await catalogs.require(session)
    out.catalog = snapshot
    ref = spec.data.dataset
    pointer = "/data/dataset"
    if dataset_override is not None:
        try:
            ref = DatasetRef.parse(dataset_override)
        except ValueError as exc:
            out.issues.append(_issue(ErrorCode.SCHEMA_INVALID, "/dataset", str(exc)))
            return
        pointer = "/dataset"
    dv, why = await resolve_dataset(session, scope, ref)
    for name, table in spec.data.side_tables.items():
        tv, twhy = await resolve_dataset(session, scope, table.ref)
        if tv is None:
            out.issues.append(
                _issue(ErrorCode.REF_UNRESOLVED, f"/data/side_tables/{name}/ref", twhy or "")
            )
        elif tv.layout == "collection":  # v0.15
            out.issues.append(
                _issue(
                    ErrorCode.SCHEMA_INVALID, f"/data/side_tables/{name}/ref", COLLECTION_REFUSED
                )
            )
        else:
            out.side_tables[name] = tv
            node_issue = side_table_node_issue(name, table.node, tree_schema_of(tv))
            if node_issue is not None:
                out.issues.append(node_issue)
    if dv is None:
        out.issues.append(_issue(ErrorCode.REF_UNRESOLVED, pointer, why or "dataset not found"))
        return
    out.dataset = dv
    cap = data_cap_issue(
        resource_class or spec.execution.resource_class,
        [dv, *out.side_tables.values()],
        "/resource_class" if resource_class is not None else "/execution/resource_class",
    )
    if cap is not None:
        out.issues.append(cap)
    if dv.data_schema is None:
        out.issues.append(
            _issue(ErrorCode.SCHEMA_INVALID, pointer, "the dataset version has no stored schema")
        )
        return
    tree = tree_schema_of(dv)
    result = validate_spec(
        spec,
        snapshot.catalog,
        DataSchema.model_validate(dv.data_schema),
        tree_schema=tree,
        dataset_layout=layout_of(dv),
    )
    out.issues.extend(result.issues)
    if spec.calibration is not None and dv.summary:
        out.issues.extend(check_spec_calibration(spec, DataSummary.model_validate(dv.summary)))
    split = result.resolved.split_stage if result.resolved else None
    out.stages = [(st.name, st.schema, st.name == split) for st in result.stages]
    out.resolved = result.resolved


async def _validate_score(
    session: AsyncSession,
    scope: Scope,
    req: ScoreRequest,
    out: Validation,
    catalogs: CatalogService | None = None,
) -> None:
    mv, why = await resolve_model_version(session, scope, req.model)
    if mv is None:
        out.issues.append(_issue(ErrorCode.REF_UNRESOLVED, "/model", why or "model not found"))
        return
    out.model_version = mv
    await check_score_request(session, scope, req, mv, out.issues, catalogs=catalogs)


async def live_capabilities(
    session: AsyncSession, catalogs: CatalogService | None, meta: ScoreMetadata
) -> Capabilities | None:
    """The capabilities of the plugin build that scores a fitted model now: the catalog
    entry of exactly the plugin version the bundle was fitted with (``id@version``), which
    may have gained score-time features since the fit (``conditions_on_actuals``). ``None``
    when no catalog is cached or it does not list that version: the fitted capabilities
    apply."""
    if catalogs is None:
        return None
    snap = await catalogs.get(session)
    if snap is None:
        return None
    ref = meta.resolved.model.resolved
    entry = next((e for e in snap.catalog.models if e.ref == ref), None)
    return entry.manifest.capabilities if entry is not None else None


async def score_draw_cap(session: AsyncSession, scope: Scope) -> int:
    """How many draws a score request of this tenant may return (plan Q2, per tier)."""
    tier = (
        await session.execute(select(m.Tenant.trust_tier).where(m.Tenant.id == scope.tenant_id))
    ).scalar_one_or_none()
    return max_score_draws(tier)


async def check_score_request(
    session: AsyncSession,
    scope: Scope,
    req: ScoreRequest,
    mv: m.ModelVersion,
    issues: list[Issue],
    *,
    inline: DataSummary | None = None,
    catalogs: CatalogService | None = None,
) -> dict[str, m.DatasetVersion]:
    """CoordPlan checks from metadata + resolution of the request's dataset refs.

    ``inline`` summarises a payload sent inline (endpoint score calls with XrJSON data):
    it is used when the request names no dataset for ``data``/``scenario``. With
    ``catalogs`` the checks use the capabilities of the plugin build that will score
    (:func:`live_capabilities`), e.g. whether a forecast may carry actuals."""
    datasets: dict[str, m.DatasetVersion] = {}
    for name, ref in (("data", req.data), ("scenario", req.scenario)):
        if ref is None:
            continue
        dv, why = await resolve_dataset(session, scope, ref)
        if dv is None:
            issues.append(_issue(ErrorCode.REF_UNRESOLVED, f"/{name}", why or ""))
        else:
            datasets[SCORE_INPUTS[name]] = dv
    for name, ref in req.side_tables.items():
        dv, why = await resolve_dataset(session, scope, ref)
        if dv is None:
            issues.append(_issue(ErrorCode.REF_UNRESOLVED, f"/side_tables/{name}", why or ""))
        elif dv.layout == "collection":  # v0.15
            issues.append(
                _issue(ErrorCode.SCHEMA_INVALID, f"/side_tables/{name}", COLLECTION_REFUSED)
            )
        else:
            datasets[side_table_input(name)] = dv
    if mv.score_meta is None:
        issues.append(
            _issue(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "/model",
                "this model version has no stored score metadata",
            )
        )
        return datasets
    meta = ScoreMetadata.model_validate(mv.score_meta)
    payload = datasets.get(INPUT_SCENARIO) or datasets.get(INPUT_DATASET)
    summary = inline
    if payload is not None and payload.summary is not None:
        summary = DataSummary.model_validate(payload.summary)
    cap = await score_draw_cap(session, scope)
    live = await live_capabilities(session, catalogs, meta)
    issues.extend(validate_score_request(req, meta, summary, max_draws=cap, live_capabilities=live))
    if req.data is None and req.scenario is None and req.horizon is None and inline is None:
        # the sandbox would fail the run with the same message; say it before a run starts
        issues.append(
            _issue(
                ErrorCode.SCHEMA_INVALID,
                "/data",
                "the score request has no data. Give data, a scenario dataset or a "
                "forecast horizon",
            )
        )
    return datasets


def pin_document(document: dict[str, Any], v: Validation) -> dict[str, Any]:
    """The resolved document handed to the runner: ``use:`` ranges pinned to the exact
    versions they resolved to and dataset refs pinned to their content hash."""
    doc = copy.deepcopy(document)
    rs = v.resolved
    if rs is not None:
        by_id = {s.id: s.resolved for s in rs.steps}
        for step in doc.get("pipeline", []):
            if isinstance(step, dict) and step.get("id") in by_id:
                step["use"] = by_id[step["id"]]
        if isinstance(doc.get("model"), dict):
            doc["model"]["use"] = rs.model.resolved
    data = doc.get("data")
    if isinstance(data, dict) and v.dataset is not None and v.dataset.sha256:
        ref = DatasetRef.parse(str(data["dataset"]))
        data["dataset"] = str(ref.pinned(v.dataset.sha256.removeprefix("sha256:")))
        for name, tv in v.side_tables.items():
            table = data.get("side_tables", {}).get(name)
            if isinstance(table, dict) and tv.sha256:
                tref = DatasetRef.parse(str(table["ref"]))
                table["ref"] = str(tref.pinned(tv.sha256.removeprefix("sha256:")))
    return doc


def first_error(issues: list[Issue]) -> Issue | None:
    return next((i for i in issues if i.severity is Severity.ERROR), None)
