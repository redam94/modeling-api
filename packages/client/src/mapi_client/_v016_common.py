"""Pure helpers of the v0.16 and v0.17 dashboard calls shared by the sync and async clients:
request bodies (specs of either layout, scenario rows, batch files), the two views
``GET /v1/dashboards/{id}`` answers with, readout paging and completion, and share-link
tokens and sessions. No I/O here."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
from mapi_spec.api import (
    ActualsSubmit,
    BatchSubmit,
    ClientDashboard,
    Dashboard,
    DashboardShareCreate,
    ReadoutHeader,
    ReadoutList,
    RefitRequestDecision,
    ScenarioRowsRequest,
    ScenarioSave,
    ShareSession,
    ShareSessionCreate,
    WhatIfRequest,
)
from mapi_spec.dashboard_v2 import DashboardSpecV2, parse_dashboard_spec
from mapi_spec.dashboards import DASHBOARD_SHARE_TOKEN_RE, DashboardSpec
from mapi_spec.errors import ErrorCode, MapiError, error_from_problem
from pydantic import SecretStr

from mapi_client import _calls_v016 as vcalls
from mapi_client._http import Call, parse_model
from mapi_client.errors import UnexpectedResponse
from mapi_client.xrjson import as_inline

__all__ = [
    "SESSION_REFRESH_MARGIN",
    "actuals_body",
    "batch_body",
    "dashboard_spec",
    "dashboard_view",
    "decision_body",
    "next_before_seq",
    "readout_done",
    "readout_error",
    "rows_body",
    "scenario_body",
    "session_body",
    "session_due",
    "share_body",
    "share_token",
    "whatif_body",
]

SESSION_REFRESH_MARGIN: Final = timedelta(seconds=60)
"""A share session that expires sooner than this is refreshed before the next request."""
_DONE: Final = frozenset({"ready", "failed"})

_parse_dashboard = parse_model(Dashboard)
_parse_client = parse_model(ClientDashboard)


def _parse_view(response: httpx.Response) -> Dashboard | ClientDashboard:
    # members get the analyst's view (it has a draft); a client user gets ClientDashboard
    try:
        data = json.loads(response.content)
    except ValueError:
        data = None
    if isinstance(data, dict) and "draft_spec" not in data:
        return _parse_client(response)
    return _parse_dashboard(response)


def dashboard_view(dashboard_id: str) -> Call[Dashboard | ClientDashboard]:
    """``GET /v1/dashboards/{id}`` read as whichever view the caller's role gets."""
    return dataclasses.replace(vcalls.get_dashboard(dashboard_id), parse=_parse_view)  # type: ignore[arg-type]


def dashboard_spec(
    spec: DashboardSpec | DashboardSpecV2 | Mapping[str, Any],
) -> DashboardSpec | DashboardSpecV2:
    """A spec of either layout: ``layout_version: 2`` is layout 2 (any model type), a
    missing ``layout_version`` is layout 1 (the v0.16 MMM layout; the server refuses new
    layout 1 dashboards from v0.17)."""
    if isinstance(spec, DashboardSpec | DashboardSpecV2):
        return spec
    return parse_dashboard_spec(dict(spec))


def rows_body(
    rows: Mapping[str, Any] | Iterable[Mapping[str, Any]], *, label: str | None
) -> ScenarioRowsRequest:
    """Scenario rows ("try a case"): one mapping of input values or a list of them (at most
    20). Inputs you leave out take the analyst's defaults; the server refuses (never clips)
    a value outside the range the analyst set."""
    if isinstance(rows, ScenarioRowsRequest):
        raise TypeError("pass the rows, not a ScenarioRowsRequest")
    items = [rows] if isinstance(rows, Mapping) else list(rows)
    return ScenarioRowsRequest.model_validate(
        {"rows": [dict(row) for row in items], "label": label}
    )


def batch_body(inputs: object, *, label: str | None) -> BatchSubmit:
    """A batch file of new cases: ``inputs`` is an ``xr.Dataset`` (encoded as XrJSON), an
    :class:`~mapi_spec.xrjson.XrJsonDataset` or its mapping, one row per case along the
    template's id column."""
    if isinstance(inputs, BatchSubmit):
        raise TypeError("pass the inputs, not a BatchSubmit")
    return BatchSubmit.model_validate({"inputs": as_inline(inputs), "label": label})


def whatif_body(multipliers: Mapping[str, float]) -> WhatIfRequest:
    """Spend multipliers per channel (``1`` = the current plan; missing channels stay at 1)."""
    return WhatIfRequest(multipliers=dict(multipliers))


def scenario_body(
    multipliers: Mapping[str, float], *, exact: bool, label: str | None
) -> ScenarioSave:
    return ScenarioSave(multipliers=dict(multipliers), exact=exact, label=label)


def actuals_body(inputs: object, *, label: str | None, request_refit: bool) -> ActualsSubmit:
    """An actuals upload: ``inputs`` is an ``xr.Dataset`` (encoded as XrJSON), an
    :class:`~mapi_spec.xrjson.XrJsonDataset` or its mapping (the template's columns)."""
    if isinstance(inputs, ActualsSubmit):
        raise TypeError("pass the inputs, not an ActualsSubmit")
    return ActualsSubmit.model_validate(
        {"inputs": as_inline(inputs), "label": label, "request_refit": request_refit}
    )


def decision_body(note: str | None) -> RefitRequestDecision:
    return RefitRequestDecision(note=note)


def share_body(
    *,
    label: str | None,
    expires_in_days: int,
    passcode: str | SecretStr | None,
    allowed_inputs: Collection[str],
) -> DashboardShareCreate:
    """A share link: read-only unless ``allowed_inputs`` names ``whatif``, ``actuals``,
    ``scenario_rows`` or ``batch``."""
    return DashboardShareCreate.model_validate(
        {
            "label": label,
            "expires_in_days": expires_in_days,
            "passcode": passcode,
            "allowed_inputs": set(allowed_inputs),
        }
    )


def share_token(link: str) -> str:
    """The ``mpd_...`` token of a share link: the token itself, the ``/s#mpd_...`` fragment
    or the whole URL (the token is the part after ``#``)."""
    token = link.strip().rsplit("#", 1)[-1]
    if not DASHBOARD_SHARE_TOKEN_RE.fullmatch(token):
        raise ValueError("not a share link or share token")
    return token


def session_body(link: str, passcode: str | SecretStr | None) -> ShareSessionCreate:
    return ShareSessionCreate.model_validate({"token": share_token(link), "passcode": passcode})


def session_due(session: ShareSession, now: datetime | None = None) -> bool:
    """The session expires within :data:`SESSION_REFRESH_MARGIN`: refresh it first."""
    return session.expires_at <= (now or datetime.now(UTC)) + SESSION_REFRESH_MARGIN


def readout_done(readout: ReadoutHeader) -> bool:
    return readout.status in _DONE


def readout_error(readout: ReadoutHeader) -> MapiError:
    """Why a readout failed: its problem, else a plain error naming it."""
    problem = getattr(readout, "problem", None)
    if problem is not None:
        return error_from_problem(problem)
    return MapiError(f"readout {readout.seq} failed", code=ErrorCode.INTERNAL_ERROR)


def next_before_seq(page: ReadoutList, before_seq: int | None) -> int | None:
    """The ``before_seq`` of the next readout page, or ``None`` after the last one. A server
    that does not move back (or sends an empty page with a cursor) would loop forever."""
    nxt = page.next_before_seq
    if nxt is None:
        return None
    if not page.items or (before_seq is not None and nxt >= before_seq):
        raise UnexpectedResponse(f"the server returned next_before_seq {nxt} twice")
    return nxt
