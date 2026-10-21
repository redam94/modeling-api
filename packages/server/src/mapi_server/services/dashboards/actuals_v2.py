"""Actuals for layout 2 dashboards: accuracy tracking for every model family (v0.17,
plan §3.3).

* ``match="time"`` (forecasts, causal impact): the v0.16 path - the upload is checked as
  layout 1 checks it (caps, the KPI column, contiguous periods) and scored as the publisher,
  ``conditioned`` when the model can, else ``compared``. A ``causal_impact`` model that
  forecasts scores the post-period **without** the outcome (``forecast`` over the uploaded
  periods, the outcome stripped): the materialised effect is the sum of the actuals minus
  the forecast without the change, with the range ``[Σa − high, Σa − low]``. Causal models
  that do not forecast (synthetic control, DiD) compare only; the refit request (when the
  dashboard allows it) updates the estimate.
* ``match="row"`` (regression, classifier): the upload is one row per case - the id column,
  the driver columns and the outcome (``label_column`` or the target) - at most
  :data:`~mapi_spec.dashboard_v2.MAX_ROW_ACTUALS` rows and 64 columns, ids unique, at most 50
  distinct class labels (each one of the model's classes, matched as
  :func:`~mapi_spec.dashboard_metrics.normalise_label` does). The outcome is **stripped**
  from the scored payload; the predictions are joined to the upload by id at materialise.

The whole upload is stored (object storage, content-addressed; never Postgres) as the
readout's ``input_ref``; the run reads only the scored payload. Refusals are
``ACTUALS_INVALID`` with pointers into ``/inputs``.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Final

from mapi_spec.audit import AuditAction
from mapi_spec.dashboard_metrics import match_label
from mapi_spec.dashboard_v2 import MAX_ROW_ACTUALS, DashboardSpecV2
from mapi_spec.dashboards import (
    MAX_ACTUALS_ROWS,
    ActualsSubmit,
    ActualsTemplate,
    readout_cache_key,
)
from mapi_spec.endpoints import (
    INPUT_INLINE_DATA,
    EndpointSchemaSnapshot,
    EndpointScoreCall,
    InputTable,
    InputTableColumn,
    _future_labels,
    input_table,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity
from mapi_spec.loader import canonical_json
from mapi_spec.schema import Role
from mapi_spec.spec import FutureCovariates, Horizon
from mapi_spec.xrjson import XrJsonDataset, check_xrjson_size
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards.access import DashboardContext
from mapi_server.services.dashboards.cases import (
    V2Parts,
    _check_cell,
    _column,
    _representation,
    batch_columns,
    binding_dims,
    score_output,
    table_payload,
    table_rows,
    v2_parts,
)
from mapi_server.services.dashboards.inputs import (
    _actuals_call,
    _audit,
    _cache_lookup,
    _check_actuals,
    _copy_for,
    _disabled,
    _invalid,
    _limits_check,
    _refuse_unsafe,
    _submit,
    _time_dim,
)
from mapi_server.services.dashboards.readouts import Actor, _lock_dashboard, new_readout
from mapi_server.services.dashboards.runas import owner_context, paused_error
from mapi_server.services.endpoints import (
    endpoint_schema_issues,
    live_capabilities_snapshot,
    store_inline_inputs,
)
from mapi_server.services.runs import now

__all__ = [
    "MAX_ROW_COLUMNS",
    "MAX_ROW_LABELS",
    "actuals_template_v2",
    "outcome_column",
    "submit_actuals_v2",
]

MAX_ROW_COLUMNS: Final = 64
MAX_ROW_LABELS: Final = 50


def outcome_column(spec: DashboardSpecV2, snapshot: EndpointSchemaSnapshot) -> str:
    """The upload column that holds the observed outcome: ``label_column`` (row matching),
    else the subject when it is a target of the input contract, else its first target."""
    if spec.actuals.match == "row" and spec.actuals.label_column is not None:
        return spec.actuals.label_column
    targets = [n for n, v in snapshot.input_schema.vars.items() if v.role is Role.TARGET]
    if spec.subject.variable in targets or not targets:
        return spec.subject.variable
    return targets[0]


async def _snapshot(
    ctx: ServerContext, session: AsyncSession, mv: m.ModelVersion
) -> EndpointSchemaSnapshot:
    try:
        return await live_capabilities_snapshot(session, ctx.catalogs, mv)
    except MapiError:
        raise _disabled("uploading numbers (the model has no input contract)") from None


def _err(pointer: str, message: str) -> Issue:
    return Issue(code=ErrorCode.ACTUALS_INVALID, pointer=pointer, message=message)


def _as_actuals(exc: MapiError) -> MapiError:
    """A table refusal (``DASHBOARD_INPUT_INVALID``) as an upload refusal."""
    issues = [i.model_copy(update={"code": ErrorCode.ACTUALS_INVALID}) for i in (exc.issues or [])]
    pointer = issues[0].pointer if issues else (exc.pointer or "/inputs")
    return _invalid(str(exc.args[0]) if exc.args else "the numbers cannot be used", pointer,
                    issues or None)  # fmt: skip


async def actuals_template_v2(
    ctx: ServerContext, session: AsyncSession, dctx: DashboardContext
) -> ActualsTemplate:
    """``GET …/actuals/template`` of a layout 2 dashboard: the input table and the outcome
    column (``match=time``), or the id, driver and outcome columns of one row per case
    (``match=row``)."""
    if not dctx.inputs.actuals:
        raise _disabled("uploading numbers")
    p = await v2_parts(session, dctx, "uploading numbers")
    _refuse_unsafe(p.mv)
    snapshot = await _snapshot(ctx, session, p.mv)
    table = input_table(snapshot, periods=8)
    if table is None:
        raise _disabled("uploading numbers (the model has no input table)")
    act = p.spec.actuals
    kpi = outcome_column(p.spec, snapshot)
    mode = dctx.inputs.actuals_mode or "compared"
    if act.match == "time":
        head = table.header
        lines = [",".join(head)] + [
            ",".join("" if v is None else str(v) for v in row) for row in table.rows
        ]
        return ActualsTemplate(
            mode=mode,
            input_table=table,
            kpi_column=kpi,
            example_csv=("\n".join(lines) + "\n")[:65_536],
            max_rows=MAX_ACTUALS_ROWS,
        )
    assert act.id_column is not None  # the spec validator makes sure
    shape = batch_columns(p.spec, snapshot, binding_dims(p.mv), act.id_column)
    names = [shape.id_column, *shape.dim_columns, *shape.variables, kpi]
    names = list(dict.fromkeys(names))[:MAX_ROW_COLUMNS]
    coords = {shape.id_column, *shape.dim_columns}
    row_table = InputTable(
        columns=[
            InputTableColumn(
                name=n,
                role="coord" if n in coords else "target" if n == kpi else "input",
                dims=[] if n in coords else [shape.id_column],
                description=(
                    "One row per case: its id" if n == shape.id_column
                    else "The actual result of the case" if n == kpi
                    else f"The {n} of the case" if n in coords
                    else "The value of the case"
                ),
            )
            for n in names
        ],
    )  # fmt: skip
    return ActualsTemplate(
        mode="compared",
        input_table=row_table,
        kpi_column=kpi,
        example_csv=(",".join(names) + "\n")[:65_536],
        max_rows=MAX_ROW_ACTUALS,
        match="row",
        id_column=shape.id_column,
        label_column=act.label_column,
    )


# ======================================================================================= time


def _effect_call(
    payload: XrJsonDataset,
    snapshot: EndpointSchemaSnapshot,
    kpi: str,
    hdim: str,
    spec: DashboardSpecV2,
    representation: str | None,
) -> EndpointScoreCall:
    """The forecast without the change over the uploaded periods (the outcome stripped).
    The periods must start right after the fit (a forecast starts there)."""
    coord = payload.coords.get(hdim)
    labels = list(coord.data) if coord is not None and isinstance(coord.data, list) else []
    if not labels:
        raise _invalid(f"the upload has no column {hdim!r} (the periods)", f"/inputs/coords/{hdim}")
    fit = snapshot.fit_coords.get(hdim)
    future = _future_labels(fit, len(labels)) if fit is not None else None
    if future is not None and str(labels[0])[:10] != str(future[0])[:10]:
        raise _invalid(
            f"the periods must start right after the model's fit ({future[0]})",
            f"/inputs/coords/{hdim}/data/0",
        )
    rest = {k: v for k, v in payload.data_vars.items() if k != kpi}
    data = (
        XrJsonDataset.model_validate(
            {"coords": payload.model_dump(mode="json")["coords"],
             "data_vars": {k: v.model_dump(mode="json") for k, v in rest.items()}}
        )
        if rest
        else None
    )  # fmt: skip
    return EndpointScoreCall(
        mode="forecast",
        horizon=Horizon(dim=hdim, periods=len(labels)),
        data=data,
        future_covariates=FutureCovariates.CARRY_LAST,
        output=score_output(spec, representation),
    )


def _time_call(
    p: V2Parts,
    dctx: DashboardContext,
    payload: XrJsonDataset,
    snapshot: EndpointSchemaSnapshot,
    kpi: str,
) -> tuple[EndpointScoreCall, dict[str, Any], str]:
    hdim = _time_dim(snapshot)
    _check_actuals(payload, snapshot, kpi, hdim)
    rep = _representation(snapshot, p.mv)
    if p.spec.preset == "causal_impact" and hdim is not None and "forecast" in snapshot.score_modes:
        call = _effect_call(payload, snapshot, kpi, hdim, p.spec, rep)
        mode = "effect"
    else:
        mode = dctx.inputs.actuals_mode or "compared"
        call = _actuals_call(payload, snapshot, mode, kpi, hdim)
        call = call.model_copy(update={"output": score_output(p.spec, rep)})
    issues, summary = endpoint_schema_issues(call, snapshot)
    if hdim is not None:
        coords = summary.coords if summary is not None else {}
        cs = coords.get(hdim)
        if mode == "effect":  # the scored payload may hold no periods: check the upload's
            _, upload_summary = endpoint_schema_issues(
                EndpointScoreCall(mode="predict", data=payload), snapshot
            )
            cs = upload_summary.coords.get(hdim) if upload_summary is not None else None
        if cs is not None and not cs.contiguous:
            issues.append(
                _err(f"/data/coords/{hdim}", "the periods must follow each other without gaps")
            )
    _raise_errors(issues)
    return call, {"kpi_column": kpi, "time_dim": hdim, "match": "time", "summary": summary}, mode


def _raise_errors(issues: list[Issue]) -> None:
    errors = [i for i in issues if i.severity is Severity.ERROR]
    if errors:
        moved = [
            i.model_copy(update={"pointer": "/inputs" + i.pointer.removeprefix("/data")})
            for i in errors
        ]
        raise _invalid(f"the numbers cannot be used: {moved[0].message}", moved[0].pointer, moved)


# ======================================================================================== row


def _subject_output(p: V2Parts, catalog: Any) -> Any:
    return next((o for o in getattr(catalog, "outputs", []) if o.name == p.spec.subject.variable),
                None)  # fmt: skip


def _classes(p: V2Parts, catalog: Any) -> list[str]:
    out = _subject_output(p, catalog)
    return list(out.classes) if out is not None else []


def _multiclass(p: V2Parts, catalog: Any) -> bool:
    """Whether the subject gives a chance per class along a class dim (its labels are the
    coordinates of the score, so the catalog may not list them)."""
    out = _subject_output(p, catalog)
    return out is not None and "top_class" in out.reduces


def _check_outcomes(
    values: list[Any], column: str, *, classifier: bool, classes: list[str],
    positive: str | None, multiclass: bool = False,
) -> None:  # fmt: skip
    """The outcome column: finite numbers (regression) or at most 50 class labels that are
    classes of the model."""
    issues: list[Issue] = []
    pointer = f"/inputs/data_vars/{column}/data"
    if classifier:
        labels: dict[str, int] = {}
        for i, v in enumerate(values):
            if v is None:
                continue
            if isinstance(v, float) and not math.isfinite(v):
                issues.append(_err(f"{pointer}/{i}", "an outcome must be a label or a number"))
                continue
            text = (
                ("true" if v else "false")
                if isinstance(v, bool)
                else (f"{v:g}" if isinstance(v, float) else str(v))
            )
            labels.setdefault(text, i)
        if len(labels) > MAX_ROW_LABELS:
            issues.append(_err(pointer, f"the outcome has more than {MAX_ROW_LABELS} labels"))
        elif classes:
            for text, i in labels.items():
                if match_label(text, classes) < 0:
                    issues.append(_err(f"{pointer}/{i}", f"{text!r} is not a class of the model"))
        elif len(labels) > 2 and positive is None and not multiclass:
            issues.append(_err(pointer, "a yes/no model takes two outcome labels"))
    else:
        for i, v in enumerate(values):
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
                issues.append(_err(f"{pointer}/{i}", "an outcome must be a finite number"))
            if len(issues) >= 50:
                break
    if issues:
        raise _invalid(f"the numbers cannot be used: {issues[0].message}", issues[0].pointer,
                       issues[:50])  # fmt: skip


async def _row_call(
    ctx: ServerContext,
    session: AsyncSession,
    p: V2Parts,
    payload: XrJsonDataset,
    snapshot: EndpointSchemaSnapshot,
    kpi: str,
) -> tuple[EndpointScoreCall, dict[str, Any]]:
    act = p.spec.actuals
    assert act.id_column is not None
    dims = binding_dims(p.mv)
    if len(payload.data_vars) > MAX_ROW_COLUMNS:
        raise _invalid(
            f"the upload has {len(payload.data_vars)} columns (max {MAX_ROW_COLUMNS})",
            "/inputs/data_vars",
        )
    try:
        shape = batch_columns(p.spec, snapshot, dims, act.id_column)
        rows = table_rows(payload, shape.id_column)
    except MapiError as exc:
        if exc.code is ErrorCode.DASHBOARD_INPUT_DISABLED:
            raise
        raise _as_actuals(exc) from None
    if len(rows) > MAX_ROW_ACTUALS:
        raise _invalid(
            f"the upload has {len(rows)} rows (max {MAX_ROW_ACTUALS})",
            f"/inputs/coords/{shape.id_column}",
        )
    if kpi not in payload.data_vars:
        raise _invalid(
            f"the upload has no column {kpi!r} (the result to compare)",
            f"/inputs/data_vars/{kpi}",
        )
    want = set(shape.variables) | set(shape.dim_columns)
    have = set(payload.data_vars) - {kpi}
    issues = [
        _err(
            f"/inputs/data_vars/{c}",
            f"the upload does not take the column {c!r} (its columns are "
            f"{', '.join(repr(x) for x in [shape.id_column, *sorted(want), kpi])})"[:300],
        )
        for c in sorted(have - want)
    ] + [
        _err(f"/inputs/data_vars/{c}", f"the upload needs the column {c!r}")
        for c in sorted(want - have)
    ]
    if issues:
        raise _invalid(f"the numbers cannot be used: {issues[0].message}", issues[0].pointer,
                       issues)  # fmt: skip
    from mapi_server.services.dashboards.sources import source_catalog

    catalog = await source_catalog(ctx, session, p.mv)
    classifier = p.spec.preset == "classifier"
    _check_outcomes(
        [r.get(kpi) for r in rows], kpi, classifier=classifier, classes=_classes(p, catalog),
        positive=p.spec.subject.positive_label, multiclass=_multiclass(p, catalog),
    )  # fmt: skip
    columns = {c: _column(p.spec, snapshot, c, dim=True) for c in shape.dim_columns}
    columns |= {v: _column(p.spec, snapshot, v, dim=False) for v in shape.variables}
    for i, row in enumerate(rows):
        for c, col in columns.items():
            why = _check_cell(row.get(c), col.model_copy(update={"min": None, "max": None}), None)
            if why is not None:
                issues.append(_err(f"/inputs/data_vars/{c}/data/{i}", why))
        if len(issues) >= 50:
            break
    if issues:
        raise _invalid(f"the numbers cannot be used: {issues[0].message}", issues[0].pointer,
                       issues[:50])  # fmt: skip
    scored = [{k: v for k, v in r.items() if k != kpi} for r in rows]
    try:
        data = table_payload(scored, shape, snapshot)
    except ValueError as exc:
        raise _invalid(f"the numbers cannot be used: {str(exc)[:300]}", "/inputs") from None
    call = EndpointScoreCall(
        mode="predict", data=data, output=score_output(p.spec, _representation(snapshot, p.mv))
    )
    issues, summary = endpoint_schema_issues(call, snapshot)
    _raise_errors(issues)
    return call, {
        "kpi_column": kpi, "label_column": kpi, "match": "row", "id_column": shape.id_column,
        "summary": summary,
    }  # fmt: skip


# ===================================================================================== submit


def _upload_call(payload: XrJsonDataset) -> EndpointScoreCall:
    """A call that only carries the upload, so it is stored as it was sent."""
    return EndpointScoreCall(mode="predict", data=payload)


async def submit_actuals_v2(
    ctx: ServerContext, session: AsyncSession, dctx: DashboardContext, body: ActualsSubmit
) -> m.DashboardReadout:
    """``POST …/actuals`` of a layout 2 dashboard: validate, store and score the upload as
    the publisher (``published`` when uploads publish automatically, else
    ``pending_review``); with ``request_refit`` (when allowed) an open refit request too."""
    if not dctx.inputs.actuals:
        raise _disabled("uploading numbers")
    if body.request_refit and not dctx.inputs.refit_requests:
        raise _disabled("asking for a refit")
    p = await v2_parts(session, dctx, "uploading numbers")
    _refuse_unsafe(p.mv)
    if p.dash.live_inputs == "paused":
        raise paused_error()
    check_xrjson_size(body.inputs.model_dump(mode="json"))
    snapshot = await _snapshot(ctx, session, p.mv)
    kpi = outcome_column(p.spec, snapshot)
    if p.spec.actuals.match == "row":
        call, meta = await _row_call(ctx, session, p, body.inputs, snapshot, kpi)
        mode = "row"
    else:
        call, meta, mode = _time_call(p, dctx, body.inputs, snapshot, kpi)
    summary = meta.pop("summary")
    fingerprint = (
        "sha256:"
        + hashlib.sha256(canonical_json(body.inputs.model_dump(mode="json")).encode()).hexdigest()
    )
    key = readout_cache_key(p.rev.id, fingerprint)
    actor = Actor.of(dctx)
    visibility = "published" if p.spec.actuals.publish == "auto" else "pending_review"
    title = body.label or f"Numbers sent on {now():%d/%m/%Y}"
    hit = await _cache_lookup(ctx, session, p.dash, key, actor)
    if hit is not None and hit.kind == "actuals":
        row = (
            hit
            if hit.actor_kind == actor.kind and hit.actor_id == actor.id
            else await _copy_for(ctx, session, p.dash, p.rev, hit, actor, visibility, title=title)
        )
    else:
        octx = await owner_context(ctx, session, p.dash)  # before the lock (it may pause)
        await _lock_dashboard(session, p.dash.id)  # count-and-insert under the row lock
        await _limits_check(ctx, session, p.dash, p.spec, actor)
        upload = await store_inline_inputs(ctx, octx.scope, _upload_call(body.inputs))
        stored = await store_inline_inputs(ctx, octx.scope, call) if call.inline_inputs() else {}
        row = await new_readout(
            session,
            p.dash,
            p.rev,
            kind="actuals",
            trigger="actuals",
            visibility=visibility,  # type: ignore[arg-type]
            actor=actor,
            title=title,
            cache_key=key,
            input_ref={**dict(upload[INPUT_INLINE_DATA]), **meta},
            input_fingerprint=fingerprint,
        )
        request = call.to_score_request(p.rev.model_ref)
        await _submit(
            session, octx, row, request, extra_inputs=stored or None, inline_summary=summary
        )
    await _audit(
        ctx,
        session,
        dctx,
        AuditAction.DASHBOARD_ACTUALS_SUBMIT,
        {"seq": row.seq, "mode": mode, "fingerprint": fingerprint},
    )
    if body.request_refit:
        from mapi_server.services.dashboards.refits import request_refit

        await request_refit(ctx, session, dctx, p.dash, row)
    return row
