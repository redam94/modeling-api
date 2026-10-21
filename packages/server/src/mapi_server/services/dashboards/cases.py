"""Scenario rows, batch scoring and the baselines of layout 2 dashboards (v0.17, plan §3.2,
§3.4, D11, D13, D17).

* **Scenario rows** ("Try a case", :func:`submit_scenario_rows`): a client edits the values
  the analyst exposed; every value is checked against the **published revision's** defs
  (:func:`mapi_spec.dashboard_inputs.check_rows`: refused with ``/rows/<i>/<name>`` pointers,
  never clipped), merged with the defaults and the ``fixed`` values and scored as the
  publisher: a ``predict`` on the inline rows, or a ``forecast`` over the horizon a forecast
  row asks for. The readout is ``private`` (``kind=prediction``, ``trigger=scenario_rows``).
* **Batch scoring** (:func:`batch_template`, :func:`submit_batch`): a file of new cases (one
  id column plus the driver columns, parsed in the browser into a one-dim XrJSON table) is
  checked (columns, ids, the row cap, with ``enforce_ranges`` every cell like a scenario
  row, a cell estimate of the score summary), turned into the model's layout and scored as
  the publisher (``kind=batch``, ``trigger=batch``, private). No image batch for clients or
  share links (plan §10.3); a model that reads image collections is never batch scored here.
* **Baselines** (:func:`submit_baseline_v2`, D17): per preset - a forecast run over
  ``refresh.horizon`` periods, one predict on the scenario defaults (the reference rows), a
  predict over the fitted data, or no run at all (fit metrics and pictures only).

Every spending input sets ``cache_key``, so the caps of v0.16 count it (D11); batch scores
also count against ``limits.batch_runs_per_day``, ``share_batch_per_day``,
``client_batch_per_day`` and ``batch_rows_per_day``. A repeated request answers with the
earlier result (copied into the caller's own private row when it was somebody else's).
Outcome ranges are predictive (D9, plan §10.2) for joint draws; class probabilities and
point models stay expected.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from mapi_spec.capabilities import OutputSelector, Representation
from mapi_spec.dashboard_inputs import (
    HORIZON_KEY,
    MAX_TEXT_CHARS,
    RowContext,
    check_rows,
    check_value,
    coerce_value,
    resolve_row,
    rows_payload,
)
from mapi_spec.dashboard_v2 import (
    MAX_BATCH_ROWS,
    BatchColumn,
    BatchSubmit,
    BatchTemplate,
    DashboardSpecV2,
    DimBind,
    ScenarioInputDef,
    ScenarioRowsDef,
    ScenarioRowsRequest,
    VarBind,
)
from mapi_spec.dashboards import _plain, readout_cache_key
from mapi_spec.endpoints import (
    INPUT_INLINE_DATA,
    EndpointSchemaSnapshot,
    EndpointScoreCall,
    _future_labels,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity, json_pointer
from mapi_spec.loader import canonical_json
from mapi_spec.schema import DimPolicy, Role
from mapi_spec.spec import API_VERSION, FutureCovariates, Horizon, ScoreRequest
from mapi_spec.xrjson import XrJsonDataset, check_xrjson_size, xrjson_from_rows
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError, ProblemWithHeaders
from mapi_server.services.dashboards.access import DashboardContext, any_revision_spec
from mapi_server.services.dashboards.inputs import (
    _cache_lookup,
    _copy_for,
    _count,
    _disabled,
    _limit,
    _limits_check,
    _private_expiry,
    _refuse_unsafe,
    _submit,
    baseline_request,
    readout_output,
)
from mapi_server.services.dashboards.readouts import Actor, _lock_dashboard, new_readout
from mapi_server.services.dashboards.runas import owner_context, paused_error
from mapi_server.services.endpoints import (
    endpoint_schema_issues,
    live_capabilities_snapshot,
    store_inline_inputs,
)
from mapi_server.services.runs import now
from mapi_server.services.submit import fit_dataset_ref

__all__ = [
    "BATCH_CELL_BUDGET",
    "BATCH_LIMIT_MESSAGE",
    "DEFAULT_HORIZON",
    "V2Parts",
    "batch_cells",
    "batch_columns",
    "batch_template",
    "binding_dims",
    "row_context",
    "score_output",
    "submit_baseline_v2",
    "submit_batch",
    "submit_scenario_rows",
    "table_payload",
    "table_rows",
    "v2_parts",
]

BATCH_CELL_BUDGET: Final = 60_000
"""The largest cell estimate of a batch score summary (headroom under the summary's
100 000 cells, so a summary is never cut)."""
BATCH_LIMIT_MESSAGE: Final = (
    "You reached today's limit for scoring files. Try again tomorrow or ask your analyst."
)
DEFAULT_HORIZON: Final = 6
"""Periods a forecast baseline reaches when ``refresh.horizon`` is not set (D17)."""
_MAX_BATCH_COLUMNS: Final = 64
_MAX_ISSUES: Final = 50
_SKIP_ROLES: Final = (Role.TARGET, Role.INDEX, Role.CONSTANT)


# ==================================================================================== loading


@dataclass(frozen=True, slots=True)
class V2Parts:
    """The published layout 2 dashboard a caller acts on."""

    dash: m.Dashboard
    rev: m.DashboardRevision
    spec: DashboardSpecV2
    mv: m.ModelVersion


async def v2_parts(session: AsyncSession, dctx: DashboardContext, what: str) -> V2Parts:
    """The dashboard, its published revision, the layout 2 spec and the pinned version
    (``DASHBOARD_INPUT_DISABLED`` naming ``what`` on a layout 1 revision)."""
    dash = await session.get(m.Dashboard, dctx.dashboard_id)
    rev = await session.get(m.DashboardRevision, dctx.revision_id)
    if dash is None or rev is None or dash.status != "published":
        raise NotFoundError("dashboard", dctx.dashboard_id)
    spec = any_revision_spec(rev)
    if not isinstance(spec, DashboardSpecV2):
        raise _disabled(what)
    mv = await session.get(m.ModelVersion, rev.model_version_id)
    if mv is None:  # pragma: no cover - versions are never deleted under a revision
        raise NotFoundError("model version", rev.model_version_id)
    return V2Parts(dash, rev, spec, mv)


async def _snapshot(
    ctx: ServerContext, session: AsyncSession, mv: m.ModelVersion, what: str
) -> EndpointSchemaSnapshot:
    try:
        return await live_capabilities_snapshot(session, ctx.catalogs, mv)
    except MapiError:
        raise _disabled(f"{what} (the model has no input contract)") from None


def binding_dims(mv: m.ModelVersion | None) -> dict[str, str]:
    """Canonical dim name → dataset dim name (the summary tables use the dataset's)."""
    if mv is None or mv.score_meta is None:
        return {}
    from mapi_spec.validate import ScoreMetadata
    from pydantic import ValidationError

    try:
        meta = ScoreMetadata.model_validate(mv.score_meta)
    except ValidationError:
        return {}
    binding = meta.resolved.binding
    return dict(binding.dims) if binding is not None else {}


def _canonical_dim(name: str, snapshot: EndpointSchemaSnapshot, dims: Mapping[str, str]) -> str:
    """``name`` as the input contract names it (a spec may hold the dataset's name)."""
    if name in snapshot.input_schema.dims:
        return name
    back = {v: k for k, v in dims.items()}
    return back.get(name, name)


def _representation(snapshot: EndpointSchemaSnapshot, mv: m.ModelVersion) -> str | None:
    if snapshot.representation is not None:
        return snapshot.representation.value
    rep = (mv.capabilities or {}).get("representation")
    return rep if isinstance(rep, str) else None


def score_output(
    spec: DashboardSpecV2, representation: Representation | str | None
) -> OutputSelector:
    """The summaries a layout 2 score asks for (:func:`~.inputs.readout_output`), with the
    predictive range for joint draws (D9, plan §10.2: one new result, also for an upgraded
    media planner). Class probabilities, point models and models whose range already is
    one of single results (quantiles, intervals) keep ``expected``."""
    out = readout_output(representation)
    rep = str(representation) if representation is not None else Representation.DRAWS.value
    if rep == Representation.DRAWS.value and spec.preset not in ("classifier", "image"):
        out = out.model_copy(update={"kind": "predictive"})
    return out


def _input_invalid(issues: Sequence[Issue], what: str = "the values cannot be used") -> MapiError:
    first = issues[0]
    return MapiError(
        f"{what}: {first.message}",
        code=ErrorCode.DASHBOARD_INPUT_INVALID,
        pointer=first.pointer,
        issues=list(issues)[:_MAX_ISSUES],
    )


def _issue(pointer: str, message: str) -> Issue:
    return Issue(code=ErrorCode.DASHBOARD_INPUT_INVALID, pointer=pointer, message=message)


def _schema_errors(
    call: EndpointScoreCall, snapshot: EndpointSchemaSnapshot, prefix: str
) -> tuple[list[Issue], Any]:
    """The input contract's refusals of ``call``, with pointers moved under ``prefix``."""
    issues, summary = endpoint_schema_issues(call, snapshot)
    errors = [
        Issue(
            code=ErrorCode.DASHBOARD_INPUT_INVALID,
            pointer=prefix + i.pointer.removeprefix("/data"),
            message=i.message,
        )
        for i in issues
        if i.severity is Severity.ERROR
    ]
    return errors, summary


def _fingerprint(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


# =============================================================================== row context


def _row_dim(dims: Sequence[str], snapshot: EndpointSchemaSnapshot, taken: set[str]) -> str | None:
    """The dim each new row adds a level on: a ``new_levels`` dim no input picks, else the
    dim a forecast extends."""
    schema = snapshot.input_schema
    free = [d for d in dims if d not in taken]
    for d in free:
        if schema.dims[d].policy == DimPolicy.NEW_LEVELS:
            return d
    for d in free:
        if schema.dims[d].policy == DimPolicy.EXTEND:
            return d
    return None


def row_context(defs: ScenarioRowsDef, snapshot: EndpointSchemaSnapshot) -> RowContext:
    """How the revision's scenario rows become cells of the scored dataset: the dims of the
    bound variables (input contract names), their policies, the fitted levels and the
    labels after the fit end of the row dim. Raises ``ValueError`` when no input binds a
    variable or a dim of the model."""
    schema = snapshot.input_schema
    var_names = [i.bind.var for i in defs.inputs if isinstance(i.bind, VarBind)]
    var_names += [k for k in defs.fixed if k in schema.vars]
    bound_dims = {i.bind.dim for i in defs.inputs if isinstance(i.bind, DimBind)}
    used: set[str] = set(bound_dims)
    for v in var_names:
        spec = schema.vars.get(v)
        if spec is None:
            raise ValueError(f"{v!r} is not an input of the model")
        used |= set(spec.dims)
    dims = [d for d in schema.dims if d in used]
    if not dims:
        raise ValueError("the scenario inputs set no variable or dim of the model")
    taken = bound_dims | {d for d in defs.fixed if d in schema.dims}
    row_dim = _row_dim(dims, snapshot, taken)
    levels: dict[str, list[str | int]] = {}
    for d in dims:
        cs = snapshot.fit_coords.get(d)
        if cs is not None and cs.values is not None and cs.kind != "time":
            levels[d] = [v if isinstance(v, int) and not isinstance(v, bool) else str(v)
                         for v in cs.values[:200]]  # fmt: skip
    next_labels: list[str | int] = []
    if row_dim is not None and schema.dims[row_dim].policy == DimPolicy.EXTEND:
        fit = snapshot.fit_coords.get(row_dim)
        found = _future_labels(fit, 20) if fit is not None else None
        next_labels = [x if isinstance(x, int) else str(x) for x in found or []]
    return RowContext(
        dims=dims,
        var_dims={v: list(schema.vars[v].dims) for v in var_names},
        row_dim=row_dim,
        policies={d: schema.dims[d].policy for d in dims},
        levels=levels,
        next_labels=next_labels,
    )


# ============================================================================= scenario rows


def _forecasting(defs: ScenarioRowsDef, snapshot: EndpointSchemaSnapshot) -> bool:
    return (
        defs.horizon is not None
        and "forecast" in snapshot.score_modes
        and snapshot.horizon_dim is not None
    )


def _forecast_call(
    resolved: Mapping[str, Any],
    defs: ScenarioRowsDef,
    snapshot: EndpointSchemaSnapshot,
    output: OutputSelector,
) -> EndpointScoreCall:
    """One forecast row: the horizon it asks for, the bound variables (and the ``fixed``
    ones) held at their value over every forecast period."""
    hdim = snapshot.horizon_dim
    assert hdim is not None
    periods = int(resolved.get(HORIZON_KEY) or (defs.horizon.default if defs.horizon else 1))
    schema = snapshot.input_schema
    values: dict[str, Any] = {}
    for idef in defs.inputs:
        if isinstance(idef.bind, VarBind):
            values[idef.bind.var] = resolved[idef.name]
    for k, v in defs.fixed.items():
        if k in schema.vars:
            values[k] = v
    picks = {i.bind.dim: resolved[i.name] for i in defs.inputs if isinstance(i.bind, DimBind)} | {
        k: v for k, v in defs.fixed.items() if k in schema.dims
    }
    data: XrJsonDataset | None = None
    if values:
        fit = snapshot.fit_coords.get(hdim)
        labels = _future_labels(fit, periods) if fit is not None else None
        if not labels:
            raise MapiError(
                "the model cannot name the periods after its fit",
                code=ErrorCode.DASHBOARD_INPUT_DISABLED,
            )
        variables = {v: list(schema.vars[v].dims) for v in values}
        rows: list[dict[str, Any]] = []
        for label in labels:
            cells: dict[str, Any] = {hdim: label, **values}
            for v in values:
                for d in schema.vars[v].dims:
                    if d == hdim or d in cells:
                        continue
                    if d in picks:
                        cells[d] = picks[d]
                        continue
                    cs = snapshot.fit_coords.get(d)
                    if cs is not None and cs.values is not None and len(cs.values) == 1:
                        cells[d] = cs.values[0]
                        continue
                    raise MapiError(
                        f"nothing gives the {d!r} of a forecast row: ask your analyst",
                        code=ErrorCode.DASHBOARD_INPUT_INVALID,
                        pointer="/rows/0",
                    )
            rows.append(cells)
        data = xrjson_from_rows(rows, variables)
    return EndpointScoreCall(
        mode="forecast",
        horizon=Horizon(dim=hdim, periods=periods),
        data=data,
        future_covariates=FutureCovariates.CARRY_LAST,
        output=output,
    )


def _echo(resolved: Mapping[str, Any], defs: ScenarioRowsDef) -> dict[str, Any]:
    """The values a client sees next to a result (their inputs, never the fixed ones)."""
    out = {i.name: resolved.get(i.name) for i in defs.inputs}
    if HORIZON_KEY in resolved and not any(i.name == HORIZON_KEY for i in defs.inputs):
        out[HORIZON_KEY] = resolved[HORIZON_KEY]
    return out


def _case_title(spec: DashboardSpecV2, echo: Sequence[Mapping[str, Any]]) -> str:
    if len(echo) != 1:
        return f"{len(echo)} cases"
    labels = {i.name: i.label for i in spec.scenario_rows.inputs}
    parts = []
    for k, v in echo[0].items():
        text = f"{v:g}" if isinstance(v, float) else str(v)
        parts.append(f"{labels.get(k, 'periods' if k == HORIZON_KEY else k)} {text}")
    return (", ".join(parts)[:200]) or "A case"


async def submit_scenario_rows(
    ctx: ServerContext, session: AsyncSession, dctx: DashboardContext, body: ScenarioRowsRequest
) -> m.DashboardReadout:
    """``POST …/predictions``: check the rows against the published revision's defs, then
    score them as the publisher (a private ``prediction`` readout; a cached result answers
    a repeated request)."""
    if not dctx.inputs.scenario_rows:
        raise _disabled("trying a case")
    p = await v2_parts(session, dctx, "trying a case")
    _refuse_unsafe(p.mv)
    if p.dash.live_inputs == "paused":
        raise paused_error()
    defs = p.spec.scenario_rows
    issues = check_rows(body.rows, defs)
    if issues:
        raise _input_invalid(issues)
    resolved = [resolve_row(r, defs) for r in body.rows]
    snapshot = await _snapshot(ctx, session, p.mv, "trying a case")
    output = score_output(p.spec, _representation(snapshot, p.mv))
    dims = binding_dims(p.mv)
    row_dim: str | None = None
    row_labels: list[str] = []
    if _forecasting(defs, snapshot):
        if len(body.rows) > 1:
            raise _input_invalid([_issue("/rows", "a forecast scores one case at a time")])
        call = _forecast_call(resolved[0], defs, snapshot, output)
    else:
        try:
            rctx = row_context(defs, snapshot)
            payload = rows_payload(body.rows, defs, rctx)
        except ValueError as exc:
            raise _input_invalid([_issue("/rows", str(exc)[:300])]) from None
        if rctx.row_dim is not None and rctx.policy(rctx.row_dim) in (
            DimPolicy.NEW_LEVELS, DimPolicy.EXTEND
        ):  # fmt: skip
            from mapi_spec.dashboard_inputs import row_coordinates

            row_dim = dims.get(rctx.row_dim, rctx.row_dim)
            row_labels = [str(c[rctx.row_dim]) for c in row_coordinates(body.rows, defs, rctx)]
        call = EndpointScoreCall(mode="predict", data=payload, output=output)
    errors, summary = _schema_errors(call, snapshot, "/rows")
    if errors:
        raise _input_invalid(errors)
    echo = [_echo(r, defs) for r in resolved]
    key = readout_cache_key(p.rev.id, {"rows": resolved})
    return await _spend(
        ctx, session, dctx, p,
        kind="prediction", trigger="scenario_rows",
        title=body.label or _case_title(p.spec, echo),
        key=key, call=call, summary=summary,
        input_ref={"rows": echo, "row_dim": row_dim, "row_labels": row_labels},
        fingerprint=_fingerprint({"rows": resolved}),
    )  # fmt: skip


async def _spend(
    ctx: ServerContext,
    session: AsyncSession,
    dctx: DashboardContext,
    p: V2Parts,
    *,
    kind: str,
    trigger: str,
    title: str,
    key: str,
    call: EndpointScoreCall,
    summary: Any,
    input_ref: Mapping[str, Any],
    fingerprint: str,
    batch_rows: int | None = None,
) -> m.DashboardReadout:
    """The cached result, or a new private readout scored as the publisher within the caps
    (counted and inserted under the dashboard row lock)."""
    actor = Actor.of(dctx)
    hit = await _cache_lookup(ctx, session, p.dash, key, actor)
    if hit is not None and hit.kind == kind:
        if hit.actor_kind == actor.kind and hit.actor_id == actor.id:
            return hit
        return await _copy_for(ctx, session, p.dash, p.rev, hit, actor, "private")
    octx = await owner_context(ctx, session, p.dash)  # before the lock (it may pause)
    await _lock_dashboard(session, p.dash.id)
    await _limits_check(ctx, session, p.dash, p.spec, actor)
    if batch_rows is not None:
        await _batch_limits(ctx, session, p, actor, batch_rows)
    stored = await store_inline_inputs(ctx, octx.scope, call) if call.inline_inputs() else {}
    ref = dict(stored[INPUT_INLINE_DATA]) if INPUT_INLINE_DATA in stored else {}
    row = await new_readout(
        session,
        p.dash,
        p.rev,
        kind=kind,  # type: ignore[arg-type]
        trigger=trigger,  # type: ignore[arg-type]
        visibility="private",
        actor=actor,
        title=title,
        cache_key=key,
        input_ref={**ref, **input_ref},
        input_fingerprint=fingerprint,
        expires_at=_private_expiry(ctx),
    )
    request = call.to_score_request(p.rev.model_ref)
    await _submit(session, octx, row, request, extra_inputs=stored or None, inline_summary=summary)
    return row


# ================================================================================ batch caps


async def _batch_limits(
    ctx: ServerContext, session: AsyncSession, p: V2Parts, actor: Actor, n_rows: int
) -> None:
    """``DASHBOARD_RUN_LIMIT`` (429, ``Retry-After``) over a batch cap of D11: batch scores
    per dashboard, share link or client user and day, and rows per dashboard and day."""
    at = now()
    day = timedelta(days=1)
    batch = (m.DashboardReadout.kind == "batch", m.DashboardReadout.created_at > at - day)
    n, oldest = await _count(session, m.DashboardReadout.dashboard_id == p.dash.id, *batch)
    if n >= p.spec.limits.batch_runs_per_day:
        raise _batch_limit(oldest, day)
    if actor.kind == "share":
        n, oldest = await _count(session, m.DashboardReadout.share_id == actor.id, *batch)
        if n >= ctx.settings.share_batch_per_day:
            raise _batch_limit(oldest, day)
    elif actor.kind == "client":
        n, oldest = await _count(
            session,
            m.DashboardReadout.dashboard_id == p.dash.id,
            m.DashboardReadout.actor_kind == "client",
            m.DashboardReadout.actor_id == actor.id,
            *batch,
        )
        if n >= ctx.settings.client_batch_per_day:
            raise _batch_limit(oldest, day)
    refs = (
        await session.execute(
            select(m.DashboardReadout.input_ref, m.DashboardReadout.created_at)
            .where(
                m.DashboardReadout.dashboard_id == p.dash.id,
                m.DashboardReadout.cache_key.is_not(None),
                *batch,
            )
            .order_by(m.DashboardReadout.created_at)
            .limit(1000)
        )
    ).all()
    used = sum(int(r[0].get("n_rows") or 0) for r in refs if isinstance(r[0], dict))
    if used + n_rows > ctx.settings.batch_rows_per_day:
        raise _batch_limit(refs[0][1] if refs else None, day)


def _batch_limit(oldest: Any, window: timedelta) -> MapiError:
    err = _limit(oldest, window)
    return ProblemWithHeaders(BATCH_LIMIT_MESSAGE, code=err.code, headers=err.headers)


# ===================================================================================== batch


@dataclass(frozen=True, slots=True)
class _BatchShape:
    """The columns of a batch file: the id (a dim of the model) and the drivers (variables
    of the input contract, then the other dims a row must give)."""

    id_dim: str  # input contract name
    id_column: str  # the column name the file uses (the spec's)
    variables: dict[str, list[str]]  # driver variable -> its dims
    dim_columns: list[str]  # dims (other than the id) each row gives
    fixed: dict[str, Any]


def _single_level(snapshot: EndpointSchemaSnapshot, dim: str) -> Any:
    cs = snapshot.fit_coords.get(dim)
    if cs is not None and cs.values is not None and len(cs.values) == 1:
        return cs.values[0]
    return None


def batch_columns(
    spec: DashboardSpecV2,
    snapshot: EndpointSchemaSnapshot,
    dims: Mapping[str, str],
    id_column: str | None = None,
) -> _BatchShape:
    """The columns a file of cases needs on ``snapshot``'s input contract: a batch file
    (``id_column`` ``None``: the batch's) or actuals matched by row
    (``DASHBOARD_INPUT_DISABLED`` when the id column is not a dim of the model)."""
    if id_column is None:
        assert spec.batch is not None
        id_column = spec.batch.id_column
    schema = snapshot.input_schema
    id_dim = _canonical_dim(id_column, snapshot, dims)
    if id_dim not in schema.dims:
        raise _disabled(f"scoring a file (the id column {id_column!r} is not a dim)")
    fixed = dict(spec.scenario_rows.fixed)
    variables = {
        n: list(v.dims)
        for n, v in schema.vars.items()
        if v.role not in _SKIP_ROLES and id_dim in v.dims and n not in fixed
    }
    if not variables:
        raise _disabled("scoring a file (no input of the model runs along the id column)")
    used = [d for d in schema.dims if any(d in ds for ds in variables.values())]
    dim_columns = [
        d for d in used
        if d != id_dim and d not in fixed and _single_level(snapshot, d) is None
    ]  # fmt: skip
    return _BatchShape(id_dim, id_column, variables, dim_columns, fixed)


def _def_for(spec: DashboardSpecV2, column: str, *, dim: bool) -> ScenarioInputDef | None:
    for idef in spec.scenario_rows.inputs:
        if dim and isinstance(idef.bind, DimBind) and idef.bind.dim == column:
            return idef
        if not dim and isinstance(idef.bind, VarBind) and idef.bind.var == column:
            return idef
    return None


def _column(
    spec: DashboardSpecV2, snapshot: EndpointSchemaSnapshot, name: str, *, dim: bool
) -> BatchColumn:
    idef = _def_for(spec, name, dim=dim)
    if idef is not None:
        return BatchColumn(
            name=name, role="driver", type=idef.type, min=idef.min, max=idef.max,
            choices=list(idef.choices),
        )  # fmt: skip
    schema = snapshot.input_schema
    if dim:
        d = schema.dims[name]
        if str(d.kind) == "time":
            return BatchColumn(name=name, role="driver", type="date")
        cs = snapshot.fit_coords.get(name)
        levels = [str(v)[:64] for v in (cs.values or [])[:200]] if cs is not None else []
        if d.policy == DimPolicy.FIXED and levels:
            return BatchColumn(name=name, role="driver", type="choice", choices=levels)
        return BatchColumn(name=name, role="driver", type="text")
    dtype = str(schema.vars[name].dtype)
    kind: Any = (
        "toggle" if dtype.startswith("bool")
        else "integer" if dtype.startswith("int")
        else "number" if dtype.startswith(("float", "num"))
        else "date" if dtype.startswith("datetime")
        else "text"
    )  # fmt: skip
    bounds = schema.vars[name].bounds
    lo, hi = bounds if bounds is not None else (None, None)
    return BatchColumn(name=name, role="driver", type=kind, min=lo, max=hi)


def _example(col: BatchColumn, spec: DashboardSpecV2, k: int) -> str:
    idef = _def_for(spec, col.name, dim=False) or _def_for(spec, col.name, dim=True)
    if idef is not None:
        v = idef.default
        return (
            ("true" if v else "false")
            if isinstance(v, bool)
            else (f"{v:g}" if isinstance(v, float) else str(v))
        )
    if col.choices:
        return col.choices[k % len(col.choices)]
    if col.type == "toggle":
        return "false"
    if col.type in ("number", "integer"):
        if col.min is not None:
            return f"{col.min:g}"
        return "0"
    return ""


async def batch_template(
    ctx: ServerContext, session: AsyncSession, dctx: DashboardContext
) -> BatchTemplate:
    """``GET …/batch/template``: the columns of a batch file (the id, then the drivers), an
    example CSV of two rows from the defaults, and the row cap."""
    if not dctx.inputs.batch:
        raise _disabled("scoring a file")
    p = await v2_parts(session, dctx, "scoring a file")
    _refuse_unsafe(p.mv)
    assert p.spec.batch is not None  # effective inputs say batch is on
    snapshot = await _snapshot(ctx, session, p.mv, "scoring a file")
    shape = batch_columns(p.spec, snapshot, binding_dims(p.mv))
    columns = [BatchColumn(name=shape.id_column, role="id", type="text")]
    columns += [_column(p.spec, snapshot, d, dim=True) for d in shape.dim_columns]
    columns += [_column(p.spec, snapshot, v, dim=False) for v in shape.variables]
    columns = columns[:_MAX_BATCH_COLUMNS]
    lines = [",".join(c.name for c in columns)]
    for k in range(2):
        cells = [f"case{k + 1}"] + [_example(c, p.spec, k) for c in columns[1:]]
        lines.append(",".join(cells))
    return BatchTemplate(
        columns=columns,
        id_column=shape.id_column,
        example_csv="\n".join(lines) + "\n",
        max_rows=p.spec.batch.max_rows,
    )


def table_rows(
    ds: XrJsonDataset, id_column: str, *, pointer: str = "/inputs"
) -> list[dict[str, Any]]:
    """The rows of a one-dim table (every data variable along ``id_column``), the id first.
    Raises ``DASHBOARD_INPUT_INVALID`` for another layout, a missing, empty or repeated id."""
    coord = ds.coords.get(id_column)
    ids = coord.data if coord is not None else None
    if coord is None or not isinstance(ids, list) or list(coord.dims) != [id_column]:
        raise _input_invalid(
            [_issue(f"{pointer}/coords/{id_column}", f"the file needs the column {id_column!r}")]
        )
    issues: list[Issue] = []
    seen: dict[str, int] = {}
    for i, raw in enumerate(ids):
        text = "" if raw is None else str(raw)
        why = None
        if isinstance(raw, bool) or not isinstance(raw, str | int) or not text.strip():
            why = "every row needs an id (text or a whole number)"
        elif len(text) > 64:
            why = "an id holds at most 64 characters"
        else:
            try:
                _plain(text, multiline=False)
            except ValueError:
                why = "an id must be plain text"
        if why is None and text in seen:
            why = f"the id {text!r} is also on row {seen[text] + 1}"
        if why is not None:
            issues.append(_issue(json_pointer(pointer[1:], "coords", id_column, "data", i), why))
        seen.setdefault(text, i)
        if len(issues) >= _MAX_ISSUES:
            break
    for name, var in ds.data_vars.items():
        if list(var.dims) != [id_column] or not isinstance(var.data, list):
            issues.append(
                _issue(
                    f"{pointer}/data_vars/{name}/dims",
                    f"the column {name!r} must have one value per row (dims [{id_column!r}])",
                )
            )
    if issues:
        raise _input_invalid(issues[:_MAX_ISSUES], "the file cannot be used")
    cols = {n: v.data for n, v in ds.data_vars.items() if isinstance(v.data, list)}
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(ids):
        row: dict[str, Any] = {id_column: raw}
        for n, data in cols.items():
            row[n] = data[i] if i < len(data) else None
        out.append(row)
    return out


def _check_cell(value: object, col: BatchColumn, idef: ScenarioInputDef | None) -> str | None:
    """Why a batch cell is not valid (plain text), ``None`` when it is."""
    if value is None:
        return f"{col.name} is empty"
    if idef is not None:
        return check_value(value, idef)
    if col.type in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, int | float):
            return f"{col.name} must be a number"
        if not math.isfinite(float(value)):
            return f"{col.name} must be a finite number"
        if col.type == "integer" and not float(value).is_integer():
            return f"{col.name} must be a whole number"
        return None
    if col.type == "toggle":
        return None if isinstance(value, bool) else f"{col.name} must be true or false"
    if isinstance(value, str):
        if len(value) > MAX_TEXT_CHARS:
            return f"{col.name} holds more than {MAX_TEXT_CHARS} characters"
        try:
            _plain(value, multiline=False)
        except ValueError:
            return f"{col.name} must be plain text"
        if col.choices and value not in col.choices:
            return f"{col.name} must be one of the choices"
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return f"{col.name} must be a value"
    return None if math.isfinite(float(value)) else f"{col.name} must be a finite number"


def table_payload(
    rows: Sequence[Mapping[str, Any]], shape: _BatchShape, snapshot: EndpointSchemaSnapshot
) -> XrJsonDataset:
    """The model's layout of checked table rows: one cell per row at its coordinates (the
    id, the row's dim columns, a fixed value or the single fitted level), ``null``
    elsewhere. Raises ``ValueError`` (two rows on one cell, no column)."""
    long: list[dict[str, Any]] = []
    for row in rows:
        cells: dict[str, Any] = {shape.id_dim: row[shape.id_column]}
        for d in shape.dim_columns:
            cells[d] = row.get(d)
        for v, ds in shape.variables.items():
            cells[v] = row.get(v)
            for d in ds:
                if d in cells:
                    continue
                if d in shape.fixed:
                    cells[d] = shape.fixed[d]
                else:
                    cells[d] = _single_level(snapshot, d)
        for k, v in shape.fixed.items():
            if k in snapshot.input_schema.vars:
                cells[k] = v
        long.append(cells)
    variables = dict(shape.variables)
    for k in shape.fixed:
        var = snapshot.input_schema.vars.get(k)
        if var is not None:
            variables[k] = list(var.dims)
    return xrjson_from_rows(long, variables)


def batch_cells(
    spec: DashboardSpecV2,
    catalog: Any,
    n_rows: int,
    *,
    per_row: set[str],
    sizes: Mapping[str, int],
    dims: Mapping[str, str],
) -> int:
    """The cells the batch score's summary will hold: per output, rows × the sizes of its
    other dims × (its dims + 3: centre, low, high). ``per_row``: dims each row gives one
    value of (input contract names); ``sizes``: the fitted sizes of the other dims."""
    back = {v: k for k, v in dims.items()}
    total = 0
    for out in getattr(catalog, "outputs", None) or []:
        cells = n_rows
        for d in out.dims:
            canon = back.get(d, d)
            if canon in per_row:
                continue
            if out.classes and canon not in sizes:
                cells *= max(1, len(out.classes))
                continue
            cells *= max(1, sizes.get(canon, 1))
        total += cells * (len(out.dims) + 3)
    return total


async def submit_batch(
    ctx: ServerContext, session: AsyncSession, dctx: DashboardContext, body: BatchSubmit
) -> m.DashboardReadout:
    """``POST …/batch``: check the file and score it as the publisher (a private ``batch``
    readout; ``DASHBOARD_BATCH_TOO_LARGE`` when its summary would not fit)."""
    if not dctx.inputs.batch:
        raise _disabled("scoring a file")
    p = await v2_parts(session, dctx, "scoring a file")
    _refuse_unsafe(p.mv)
    bdef = p.spec.batch
    assert bdef is not None
    if p.spec.preset == "image" and dctx.principal_kind != "member":  # plan §10.3
        raise _disabled("scoring a file")
    if p.dash.live_inputs == "paused":
        raise paused_error()
    check_xrjson_size(body.inputs.model_dump(mode="json"))
    snapshot = await _snapshot(ctx, session, p.mv, "scoring a file")
    from mapi_server.services.dashboards.sources import source_catalog

    catalog = await source_catalog(ctx, session, p.mv)
    if catalog.input_kind == "collection":
        raise _disabled("scoring a file (the model reads images: score them from the model page)")
    dims = binding_dims(p.mv)
    shape = batch_columns(p.spec, snapshot, dims)
    rows = table_rows(body.inputs, shape.id_column)
    n = len(rows)
    if n > bdef.max_rows or n > MAX_BATCH_ROWS:
        raise MapiError(
            f"the file has {n} rows (at most {bdef.max_rows})",
            code=ErrorCode.DASHBOARD_BATCH_TOO_LARGE,
            pointer=f"/inputs/coords/{shape.id_column}",
        )
    want = set(shape.variables) | set(shape.dim_columns)
    have = set(body.inputs.data_vars)
    issues = [
        _issue(f"/inputs/data_vars/{c}", f"{c!r} is not a column of this file")
        for c in sorted(have - want)
    ] + [
        _issue(f"/inputs/data_vars/{c}", f"the file needs the column {c!r}")
        for c in sorted(want - have)
    ]
    if issues:
        raise _input_invalid(issues, "the file cannot be used")
    columns = {c: _column(p.spec, snapshot, c, dim=True) for c in shape.dim_columns}
    columns |= {v: _column(p.spec, snapshot, v, dim=False) for v in shape.variables}
    for i, row in enumerate(rows):
        for c, col in columns.items():
            idef = _def_for(p.spec, c, dim=c in shape.dim_columns) if bdef.enforce_ranges else None
            if idef is None and not bdef.enforce_ranges:
                col = col.model_copy(update={"min": None, "max": None})
            why = _check_cell(row.get(c), col, idef)
            if why is None and col.min is not None and col.max is not None and idef is None:
                x = row.get(c)
                if isinstance(x, int | float) and not col.min <= float(x) <= col.max:
                    why = f"{c} must be between {col.min:g} and {col.max:g}"
            if why is not None:
                issues.append(_issue(f"/inputs/data_vars/{c}/data/{i}", why))
            elif idef is not None:
                row[c] = coerce_value(row[c], idef)
            if len(issues) >= _MAX_ISSUES:
                break
        if len(issues) >= _MAX_ISSUES:
            break
    if issues:
        raise _input_invalid(issues, "the file cannot be used")
    per_row = {shape.id_dim, *shape.dim_columns, *(d for d in shape.fixed)}
    sizes = {d: int(cs.size or 1) for d, cs in snapshot.fit_coords.items()}
    for d in snapshot.input_schema.dims:
        if _single_level(snapshot, d) is not None:
            per_row.add(d)
    cells = batch_cells(p.spec, catalog, n, per_row=per_row, sizes=sizes, dims=dims)
    if cells > BATCH_CELL_BUDGET:
        raise MapiError(
            f"the file would give about {cells} results, more than one batch can hold "
            f"({BATCH_CELL_BUDGET}): send fewer rows",
            code=ErrorCode.DASHBOARD_BATCH_TOO_LARGE,
        )
    try:
        payload = table_payload(rows, shape, snapshot)
    except ValueError as exc:
        raise _input_invalid(
            [_issue("/inputs", str(exc)[:300])], "the file cannot be used"
        ) from None
    output = score_output(p.spec, _representation(snapshot, p.mv))
    call = EndpointScoreCall(mode="predict", data=payload, output=output)
    errors, summary = _schema_errors(call, snapshot, "/inputs")
    if errors:
        raise _input_invalid(errors, "the file cannot be used")
    fingerprint = _fingerprint(body.inputs.model_dump(mode="json"))
    key = readout_cache_key(p.rev.id, "batch|" + fingerprint)
    return await _spend(
        ctx, session, dctx, p,
        kind="batch", trigger="batch",
        title=body.label or f"File of {n} cases sent on {now():%d/%m/%Y}",
        key=key, call=call, summary=summary,
        input_ref={
            "n_rows": n, "id_column": shape.id_column,
            "id_dim": dims.get(shape.id_dim, shape.id_dim),
            "columns": [c for c in [*shape.dim_columns, *shape.variables]][:_MAX_BATCH_COLUMNS],
        },
        fingerprint=fingerprint,
        batch_rows=n,
    )  # fmt: skip


# ================================================================================== baselines


async def _reference_rows(
    ctx: ServerContext,
    session: AsyncSession,
    row: m.DashboardReadout,
    octx: ServerContext,
    spec: DashboardSpecV2,
    snapshot: EndpointSchemaSnapshot,
    output: OutputSelector,
    dims: Mapping[str, str],
) -> bool:
    """Score one row of the scenario defaults (the regression and classifier baseline).
    ``False`` when the defs cannot make such a row."""
    defs = spec.scenario_rows
    if not defs.inputs:
        return False
    try:
        rctx = row_context(defs, snapshot)
        payload = rows_payload([{}], defs, rctx)
    except ValueError:
        return False
    call = EndpointScoreCall(mode="predict", data=payload, output=output)
    issues, summary = endpoint_schema_issues(call, snapshot)
    if any(i.severity is Severity.ERROR for i in issues):
        return False
    stored = await store_inline_inputs(ctx, octx.scope, call)
    echo = [_echo(resolve_row({}, defs), defs)]
    row_dim = dims.get(rctx.row_dim, rctx.row_dim) if rctx.row_dim is not None else None
    labels: list[str] = []
    if rctx.row_dim is not None and rctx.policy(rctx.row_dim) in (
        DimPolicy.NEW_LEVELS, DimPolicy.EXTEND
    ):  # fmt: skip
        from mapi_spec.dashboard_inputs import row_coordinates

        labels = [str(c[rctx.row_dim]) for c in row_coordinates([{}], defs, rctx)]
    row.input_ref = {
        **dict(stored[INPUT_INLINE_DATA]), "rows": echo, "row_dim": row_dim, "row_labels": labels,
    }  # fmt: skip
    await _submit(
        session, octx, row, call.to_score_request(row.model_ref), extra_inputs=stored,
        inline_summary=summary,
    )  # fmt: skip
    return True


async def submit_baseline_v2(
    ctx: ServerContext,
    session: AsyncSession,
    row: m.DashboardReadout,
    dash: m.Dashboard,
    spec: DashboardSpecV2,
    mv: m.ModelVersion,
) -> None:
    """Submit a ``queued`` baseline or refresh readout of a layout 2 revision as the owner
    (D17): never a score of a large fit dataset. Presets that read only fit metrics and
    pictures (causal models without a forecast, image models) spend no run: the readout
    goes straight to ``materialising``. Raises ``MapiError`` (the loop fails the row)."""
    octx = await owner_context(ctx, session, dash)
    try:
        snapshot: EndpointSchemaSnapshot | None = await live_capabilities_snapshot(
            session, ctx.catalogs, mv
        )
    except MapiError:
        snapshot = None
    rep = (
        _representation(snapshot, mv)
        if snapshot is not None
        else ((mv.capabilities or {}).get("representation"))
    )
    output = score_output(spec, rep)
    modes = set(snapshot.score_modes) if snapshot is not None else set()
    preset = spec.preset
    no_run = preset == "image" or (preset == "causal_impact" and "forecast" not in modes)
    if preset == "generic" and snapshot is not None and "predict" not in modes:
        no_run = True
    if no_run:
        row.status = "materialising"
        await session.flush()
        return
    async with session.begin_nested():
        if (
            preset == "forecast"
            and snapshot is not None
            and "forecast" in modes
            and snapshot.horizon_dim is not None
        ):
            periods = spec.refresh.horizon or DEFAULT_HORIZON
            request = ScoreRequest.model_validate(
                {
                    "apiVersion": API_VERSION,
                    "kind": "ScoreRequest",
                    "model": row.model_ref,
                    "mode": "forecast",
                    "horizon": {"dim": snapshot.horizon_dim, "periods": periods},
                    "output": output.model_dump(exclude_defaults=True),
                }
            )
            await _submit(session, octx, row, request)
            return
        if (
            preset in ("regression", "classifier")
            and snapshot is not None
            and "predict" in modes
            and await _reference_rows(
                ctx, session, row, octx, spec, snapshot, output, binding_dims(mv)
            )
        ):
            return
        data = await fit_dataset_ref(session, mv)
        request = baseline_request(row.model_ref, data, rep)
        body = request.model_dump(mode="json", by_alias=True, exclude_none=True)
        body["output"] = output.model_dump(exclude_defaults=True)
        await _submit(session, octx, row, ScoreRequest.model_validate(body))
