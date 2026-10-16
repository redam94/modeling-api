"""Guided mode, data path (plan §11 tasks 1-2; M3): templates, role detection, drafts and
the BayesInsight import.

Everything here is JSON in, JSON out: the control plane never sees an upload's bytes.
Role detection reads column *profiles* - sent by the browser (which profiles the file
locally) or stored with a dataset version by its sandboxed ingest run (``columns.json``) -
and drafts are validated statically against the catalog with the schema their ingest
mapping promises (:func:`mapi_spec.guided.draft_schema`). Drafts are not stored: the client
uploads the data with the draft's mapping and saves the YAML with ``POST /v1/specs``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path
from mapi_spec.api import Page
from mapi_spec.bayesinsight import import_bayesinsight
from mapi_spec.coords import DataSummary
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity
from mapi_spec.guided import (
    GUIDED_TEMPLATES,
    BayesInsightImport,
    ColumnProfile,
    DetectRolesRequest,
    GuidedAnswers,
    GuidedDraft,
    GuidedTemplate,
    RoleSuggestions,
    build_draft,
    draft_schema,
    get_template,
    profiles_from_dataset,
    suggest_roles,
)
from mapi_spec.schema import DataSchema
from mapi_spec.spec import ModelSpec
from mapi_spec.validate import validate_spec
from pydantic import ValidationError

from mapi_server.api.common import DEFAULT_LIMIT, Ctx, Limit, Session, decode_cursor, encode_cursor
from mapi_server.api.problems import AUTH_PROBLEMS, problem_responses
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.catalog import CatalogUnavailable
from mapi_server.services.specs import resolve_dataset

__all__ = ["dataset_profiles", "router", "validate_draft"]

router = APIRouter(prefix="/v1/guided", tags=["guided"])

TemplateId = Annotated[str, Path(pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")]
_E = ErrorCode


def _template(template_id: str) -> GuidedTemplate:
    try:
        return get_template(template_id)
    except MapiError:
        raise NotFoundError("guided template", template_id) from None


@router.get(
    "/templates",
    response_model=Page[GuidedTemplate],
    operation_id="list_guided_templates",
    summary="Guided templates (geo marketing mix, weekly forecast, demand by store, choice)",
    responses=problem_responses(_E.SCHEMA_INVALID, *AUTH_PROBLEMS),
)
async def list_guided_templates(
    limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
) -> Page[GuidedTemplate]:
    """The built-in templates in id order: questions, the spec template, scorecard checks
    and ``x-ui-level`` hints. Templates are data and the same for every tenant."""
    after = decode_cursor(cursor)
    ids = sorted(GUIDED_TEMPLATES)
    if after is not None:
        ids = [i for i in ids if i > str(after)]
    page = ids[:limit]
    more = len(ids) > limit
    return Page[GuidedTemplate](
        items=[GUIDED_TEMPLATES[i] for i in page],
        next_cursor=encode_cursor(page[-1]) if more and page else None,
    )


@router.get(
    "/templates/{template_id}",
    response_model=GuidedTemplate,
    operation_id="get_guided_template",
    summary="One template: questions, spec template, scorecard checks, UI levels",
    responses=problem_responses(_E.NOT_FOUND, *AUTH_PROBLEMS),
)
async def get_guided_template(template_id: TemplateId) -> GuidedTemplate:
    return _template(template_id)


# ============================================================================ detection


async def dataset_profiles(
    session: Session, ctx: Ctx, ref: Any
) -> tuple[m.DatasetVersion, list[ColumnProfile]]:
    """The column profiles of a stored (ready) dataset version of the request's project:
    its ``columns.json`` when the ingest wrote one, else profiles derived from its schema
    and summary (JSON only; the data itself is never read)."""
    dv, why = await resolve_dataset(session, ctx.scope, ref)
    if dv is None:
        raise MapiError(why or "dataset not found", code=_E.NOT_FOUND, pointer="/dataset")
    if dv.columns:
        try:
            return dv, [ColumnProfile.model_validate(c) for c in dv.columns]
        except ValidationError:  # pragma: no cover - validated when the version was finalized
            pass
    if dv.data_schema is None:
        raise MapiError(
            "the dataset version has no stored schema", code=_E.SCHEMA_INVALID, pointer="/dataset"
        )
    schema = DataSchema.model_validate(dv.data_schema)
    summary = DataSummary.model_validate(dv.summary) if dv.summary else None
    return dv, profiles_from_dataset(schema, summary)


@router.post(
    "/detect-roles",
    response_model=RoleSuggestions,
    operation_id="detect_roles",
    summary="Suggest a column for every question from column profiles",
    responses=problem_responses(_E.SCHEMA_INVALID, _E.NOT_FOUND, *AUTH_PROBLEMS),
)
async def detect_roles(ctx: Ctx, session: Session, body: DetectRolesRequest) -> RoleSuggestions:
    """Rank the columns for each question of a template and pre-fill the answers.

    Send ``columns`` (profiled in the browser) or name a stored ``dataset`` version; the
    profiles of a stored version come from its ingest run (``columns.json``: the uploaded
    columns, for MFF uploads the pivoted variables) or, for older versions, from its dims
    and variables."""
    template = _template(body.template_id)
    if body.columns is not None:
        return suggest_roles(template, body.columns)
    assert body.dataset is not None
    _, columns = await dataset_profiles(session, ctx, body.dataset)
    return suggest_roles(template, columns)


# =============================================================================== drafts


async def validate_draft(
    session: Session,
    ctx: Ctx,
    draft: GuidedDraft,
    columns: list[ColumnProfile] | None,
) -> GuidedDraft:
    """The draft with the static validator's findings added (against the catalog and the
    schema its mapping promises). A missing catalog degrades to an info issue."""
    try:
        snapshot = await ctx.catalogs.require(session)
    except CatalogUnavailable:
        return _with(draft, Issue(
            code=_E.INFRA_ERROR, severity=Severity.INFO, pointer="",
            message="the model catalog is not available yet, so the draft was not validated; "
            "validate the spec with POST /v1/specs:validate once the data is uploaded",
        ))  # fmt: skip
    try:
        schema = draft_schema(draft.mapping, columns)
    except ValueError:
        return _with(draft, Issue(
            code=_E.SCHEMA_INVALID, severity=Severity.INFO, pointer="/mapping",
            message="the mapping lets the preset decide the dims, so the spec is validated "
            "once the data is ingested (POST /v1/specs:validate)",
        ))  # fmt: skip
    spec = ModelSpec.model_validate(draft.spec)
    result = validate_spec(spec, snapshot.catalog, schema)
    return draft.model_copy(update={"issues": [*draft.issues, *result.issues]})


def _with(draft: GuidedDraft, issue: Issue) -> GuidedDraft:
    return draft.model_copy(update={"issues": [*draft.issues, issue]})


@router.post(
    "/drafts",
    response_model=GuidedDraft,
    operation_id="create_guided_draft",
    summary="Answers -> ingest mapping + ModelSpec draft (+ YAML); stores nothing",
    responses=problem_responses(
        _E.SCHEMA_INVALID, _E.NOT_FOUND, _E.GUIDED_ANSWERS_INVALID, *AUTH_PROBLEMS
    ),
)
async def create_guided_draft(ctx: Ctx, session: Session, body: GuidedAnswers) -> GuidedDraft:
    """Turn answers into the ingest mapping for the upload, the ``ModelSpec`` (and its YAML)
    for the request's project, and the static validator's findings. ``422
    GUIDED_ANSWERS_INVALID`` points at ``/answers/<question>`` for a missing or impossible
    answer; problems the validator finds come back as ``issues`` (200)."""
    template = _template(body.template_id)
    draft = build_draft(template, body, project=ctx.scope.project_id)
    return await validate_draft(session, ctx, draft, body.columns)


@router.post(
    "/import/bayesinsight",
    response_model=GuidedDraft,
    operation_id="import_bayesinsight",
    summary="BayesInsight model_def.json + metadata.json -> a draft for the geo MMM",
    responses=problem_responses(_E.SCHEMA_INVALID, _E.IMPORT_UNSUPPORTED, *AUTH_PROBLEMS),
)
async def import_bayesinsight_model(
    ctx: Ctx, session: Session, body: BayesInsightImport
) -> GuidedDraft:
    """Import a BayesInsight project (Q8). With the default ``target:
    mmm.hierarchical_geo`` the draft maps media transforms, priors, controls, seasonality
    and pooling to the built-in geo MMM and lists every feature without an equivalent as
    an ``IMPORT_UNSUPPORTED`` warning; ``target: mmm.bayesinsight`` keeps ``model_def``
    verbatim for the planned dedicated plugin. ``422 IMPORT_UNSUPPORTED`` when the model
    cannot run at all (no single KPI, no media, split series)."""
    draft = import_bayesinsight(body, project=ctx.scope.project_id)
    if body.target == "mmm.bayesinsight":
        return draft
    return await validate_draft(session, ctx, draft, None)
