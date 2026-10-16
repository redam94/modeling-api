"""Saved workflows: documents, immutable revisions and their static validation (JSON only).

A workflow (``workflow://project/name``) is a name plus revisions. Every revision stores the
document, its hash and the result of :func:`mapi_spec.workflow.validate_workflow` against the
cached catalog (model refs) and the project's saved workflows (sub-workflows): revisions
with errors are stored but cannot run (``WORKFLOW_NOT_RUNNABLE``).
"""

from __future__ import annotations

import copy
from typing import Any

from mapi_spec.api import new_id
from mapi_spec.errors import ErrorCode, Issue, MapiError
from mapi_spec.loader import parse_text
from mapi_spec.refs import WorkflowRef
from mapi_spec.workflow import (
    Workflow,
    WorkflowRevision,
    WorkflowSpec,
    WorkflowValidation,
    validate_workflow,
    workflow_hash,
)
from mapi_spec.workflow_nodes import SUBWORKFLOW_NODE
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES
from pydantic import JsonValue, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.catalog import CatalogService
from mapi_server.services.runs import Scope, now

__all__ = [
    "document_of",
    "load_revision",
    "load_workflow",
    "resolve_workflow_ref",
    "revision_dto",
    "revision_row",
    "validate_document",
    "workflow_dto",
    "workflow_ref",
]


def workflow_ref(row: m.Workflow, revision: int | None = None) -> str:
    base = f"workflow://{row.project_id}/{row.name}"
    return base if revision is None else f"{base}@r{revision}"


def workflow_dto(row: m.Workflow) -> Workflow:
    return Workflow(
        id=row.id,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        name=row.name,
        ref=workflow_ref(row),
        description=row.description,
        latest_revision=row.latest_revision,
        archived=row.archived,
        template=row.template,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
    )


def revision_dto(wf: m.Workflow, rev: m.WorkflowRevisionRow) -> WorkflowRevision:
    return WorkflowRevision(
        workflow_id=rev.workflow_id,
        revision=rev.revision,
        ref=workflow_ref(wf, rev.revision),
        document=rev.document,
        spec_hash=rev.spec_hash,
        validation=WorkflowValidation.model_validate(rev.validation),
        message=rev.message,
        created_at=rev.created_at,
        created_by=rev.created_by,
    )


def document_of(
    *, document: dict[str, JsonValue] | None, text: str | None, template: str | None = None
) -> tuple[dict[str, Any], str | None]:
    """The JSON document of a create/revision/validate request (``SPEC_PARSE_ERROR`` for
    unreadable text, ``NOT_FOUND`` for an unknown template)."""
    if template is not None:
        t = WORKFLOW_TEMPLATES.get(template)
        if t is None:
            raise NotFoundError("workflow template", template)
        return copy.deepcopy(dict(t.document)), t.id
    if text is not None:
        data = parse_text(text)
        if not isinstance(data, dict):
            raise MapiError("the workflow text is not a mapping", code=ErrorCode.SPEC_PARSE_ERROR)
        return data, None
    assert document is not None
    return copy.deepcopy(dict(document)), None


async def _subworkflows(
    session: AsyncSession, scope: Scope, doc: dict[str, Any]
) -> dict[str, WorkflowSpec]:
    """The saved revisions the document's ``subworkflow.run`` nodes reference."""
    out: dict[str, WorkflowSpec] = {}
    nodes = doc.get("nodes")
    if not isinstance(nodes, list):
        return out
    for n in nodes:
        if not isinstance(n, dict) or str(n.get("use", "")).split("@")[0] != SUBWORKFLOW_NODE:
            continue
        ref_text = (
            (n.get("with") or {}).get("workflow") if isinstance(n.get("with"), dict) else None
        )
        if not isinstance(ref_text, str):
            continue
        try:
            ref = WorkflowRef.parse(ref_text)
        except ValueError:
            continue
        spec = await resolve_workflow_ref(session, scope, ref)
        if spec is not None:
            out[str(ref)] = spec
    return out


async def resolve_workflow_ref(
    session: AsyncSession, scope: Scope, ref: WorkflowRef
) -> WorkflowSpec | None:
    """The document of a saved revision (``None`` when it does not exist or is invalid)."""
    if ref.project != scope.project_id:
        return None
    wf = (
        await session.execute(
            select(m.Workflow).where(
                m.Workflow.tenant_id == scope.tenant_id,
                m.Workflow.project_id == scope.project_id,
                m.Workflow.name == ref.name,
            )
        )
    ).scalar_one_or_none()
    if wf is None:
        return None
    number = ref.revision_number or wf.latest_revision
    rev = await session.get(m.WorkflowRevisionRow, (wf.id, number))
    if rev is None:
        return None
    try:
        return WorkflowSpec.model_validate(rev.document)
    except ValidationError:
        return None


async def validate_document(
    session: AsyncSession, scope: Scope, catalogs: CatalogService, doc: dict[str, Any]
) -> WorkflowValidation:
    """Static checks of a workflow document in a project (pure apart from the lookups)."""
    snapshot = await catalogs.get(session)
    subs = await _subworkflows(session, scope, doc)
    result = validate_workflow(
        doc,
        catalog=snapshot.catalog if snapshot is not None else None,
        resolve_subworkflow=lambda ref: subs.get(str(ref)),
    )
    issues = list(result.issues)
    meta = doc.get("metadata")
    project = meta.get("project") if isinstance(meta, dict) else None
    if isinstance(project, str) and project != scope.project_id:
        issues.append(
            Issue(
                code=ErrorCode.WORKFLOW_INVALID,
                pointer="/metadata/project",
                message=f"the document names project {project!r} but is saved in "
                f"{scope.project_id!r}",
            )
        )
        return result.model_copy(update={"valid": False, "issues": issues, "order": []})
    return result


def _name(doc: dict[str, Any]) -> str:
    meta = doc.get("metadata")
    name = meta.get("name") if isinstance(meta, dict) else None
    if not isinstance(name, str):
        raise MapiError(
            "a workflow needs metadata.name",
            code=ErrorCode.WORKFLOW_INVALID,
            pointer="/metadata/name",
        )
    return name


def revision_row(
    wf: m.Workflow,
    revision: int,
    doc: dict[str, Any],
    validation: WorkflowValidation,
    *,
    message: str | None,
    created_by: str | None,
) -> m.WorkflowRevisionRow:
    return m.WorkflowRevisionRow(
        workflow_id=wf.id,
        revision=revision,
        tenant_id=wf.tenant_id,
        project_id=wf.project_id,
        document=doc,
        spec_hash=workflow_hash(doc),
        validation=validation.model_dump(mode="json"),
        valid=validation.valid,
        message=message,
        created_by=created_by,
        created_at=now(),
    )


def new_workflow(
    scope: Scope, doc: dict[str, Any], *, template: str | None, created_by: str | None
) -> m.Workflow:
    ts = now()
    meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    description = meta.get("description") if isinstance(meta, dict) else None
    return m.Workflow(
        id=new_id("wf"),
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        name=_name(doc),
        description=description if isinstance(description, str) else None,
        latest_revision=1,
        archived=False,
        template=template,
        created_by=created_by,
        created_at=ts,
        updated_at=ts,
    )


async def load_workflow(
    session: AsyncSession, scope: Scope, workflow_id: str, *, for_update: bool = False
) -> m.Workflow:
    row = await session.get(m.Workflow, workflow_id, with_for_update=for_update)
    if row is None or (row.tenant_id, row.project_id) != (scope.tenant_id, scope.project_id):
        raise NotFoundError("workflow", workflow_id)
    return row


async def load_revision(
    session: AsyncSession, wf: m.Workflow, revision: int | None
) -> m.WorkflowRevisionRow:
    number = revision or wf.latest_revision
    rev = await session.get(m.WorkflowRevisionRow, (wf.id, number))
    if rev is None:
        raise NotFoundError("workflow revision", f"{workflow_ref(wf)}@r{number}")
    return rev
