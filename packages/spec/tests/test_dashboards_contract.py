"""v0.16 contracts: the client role and its operation allow-list, the dashboard endpoint table,
share tokens and sessions, the spec and snapshot caps, the accuracy and zone helpers, the
read-only tool surface and the reference docs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, get_args

import pytest
from mapi_spec import api, dashboard_tools, dashboards, sharing
from mapi_spec.api import (
    CLIENT_OPERATIONS,
    ROUTE_ACCESS,
    V016_ENDPOINTS,
    V017_ENDPOINTS,
    RouteScope,
)
from mapi_spec.dashboards import (
    DASHBOARD_SHARE_TOKEN_RE,
    GLOSSARY,
    MAX_READOUT_BYTES,
    MAX_SERIES_POINTS,
    SHARE_SESSION_HEADER,
    ActualsInputs,
    ChannelDef,
    DashboardShareCreate,
    DashboardShareCreated,
    DashboardSpec,
    GlossaryKey,
    KpiDef,
    ReadoutSnapshot,
    ShareSession,
    ShareSessionCreate,
    breakeven_of,
    generate_share_token,
    metrics_of,
    readout_cache_key,
    share_display,
    zone_of,
)
from mapi_spec.errors import ERROR_INFO, ErrorCode
from mapi_spec.tenancy import (
    PERMISSIONS,
    PROJECT_HEADER,
    Action,
    AuthContext,
    ProjectRole,
    ResourceKind,
    UserPrincipal,
    can,
)
from pydantic import BaseModel, ValidationError

pytestmark = pytest.mark.unit

C = ProjectRole.CLIENT
_RK, _AC = ResourceKind, Action
_CLIENT_PAIRS = {(_RK.DASHBOARD, _AC.READ), (_RK.DASHBOARD, _AC.RUN)}
_SPEC: dict[str, Any] = DashboardSpec.model_json_schema()["examples"][0]


def _resolve(name: str) -> type[BaseModel]:
    m = re.fullmatch(r"Page\[(\w+)\]", name)
    obj = getattr(api, m.group(1) if m else name)
    assert isinstance(obj, type) and issubclass(obj, BaseModel), name
    return obj


# ================================================================================ client role


def test_client_is_lowest_and_denied_everything_but_dashboards() -> None:
    assert next(iter(ProjectRole)) is C and C.rank == 0
    assert all(r > C for r in ProjectRole if r is not C)
    for (res, act), rule in PERMISSIONS.items():
        allowed = can(C, act, res)
        assert allowed is ((res, act) in _CLIENT_PAIRS), (res, act)
        if (res, act) not in _CLIENT_PAIRS and rule.min_role is not None:
            assert rule.min_role > C, (res, act)
    ctx = AuthContext(
        principal=UserPrincipal(
            id="usr_01jaf3a9b8c7d6e5f4g3h2j1k0", issuer="https://idp.example", subject="cleo"
        ),
        tenant_id="acme",
        project_id="growth",
        role=C,
    )
    assert ctx.allows("read", "dashboard") and ctx.allows("run", "dashboard")
    for act, res in (("create", "run"), ("run", "model"), ("create", "token"), ("read", "run"),
                     ("read", "project"), ("update", "dashboard"), ("create", "dashboard_share"),
                     ("read", "dashboard_grant"), ("run", "endpoint")):  # fmt: skip
        assert not ctx.allows(act, res), (act, res)


def test_client_operations_exist_in_route_access() -> None:
    assert set(ROUTE_ACCESS) >= CLIENT_OPERATIONS
    assert set(ROUTE_ACCESS) == {e.operation_id for e in api.ENDPOINTS}


def test_client_operations_are_only_dashboards_me_and_session() -> None:
    v016 = {e.operation_id for e in V016_ENDPOINTS + V017_ENDPOINTS}
    session = {"auth_login", "auth_callback", "auth_logout", "auth_session"}
    for op in CLIENT_OPERATIONS:
        acc = ROUTE_ACCESS[op]
        if op in session:
            assert acc.scope is RouteScope.PUBLIC
        elif op == "get_me":
            assert acc.scope is RouteScope.AUTHENTICATED
        else:
            assert op in v016, op
            assert acc.scope is RouteScope.PROJECT and acc.name_param in (None, "dashboard_id")
            assert (acc.resource, acc.action) in _CLIENT_PAIRS and not acc.also, op
    # every project route a client's role passes is on the list, and nothing else is
    passes = set()
    for op, acc in ROUTE_ACCESS.items():
        if acc.scope is not RouteScope.PROJECT or acc.resource is None or acc.action is None:
            continue
        if all(can(C, a, r) for r, a in [(acc.resource, acc.action), *acc.also]):
            passes.add(op)
    # list_dashboard_revisions is a dashboard read (viewers and up), but a revision holds the
    # full spec and people's ids: only the allow-list keeps it from clients
    assert passes - {"list_dashboard_revisions"} == CLIENT_OPERATIONS - session - {"get_me"}
    assert passes - {"list_dashboard_revisions"} == {
        "list_dashboards", "get_dashboard", "get_dashboard_grid", "evaluate_dashboard_whatif",
        "save_dashboard_scenario", "get_dashboard_actuals_template", "submit_dashboard_actuals",
        "list_dashboard_readouts", "get_dashboard_readout",
        # v0.17 inputs, downloads and pictures
        "submit_dashboard_predictions", "get_dashboard_batch_template", "submit_dashboard_batch",
        "download_dashboard_predictions", "get_dashboard_asset",
    }  # fmt: skip
    # membership-only routes and listings stay closed to clients (D4)
    for op in ("list_projects", "list_tenants", "get_catalog", "list_guided_templates",
               "list_workflow_templates", "list_governance_checks", "list_tenant_projects",
               "list_dashboard_refit_requests", "approve_dashboard_refit_request",
               "reject_dashboard_refit_request", "create_dashboard_share",
               "update_dashboard_draft", "create_share_session"):  # fmt: skip
        assert op not in CLIENT_OPERATIONS, op


def test_refit_requests_are_editor_routes_and_approval_needs_runner() -> None:
    for op in ("list_dashboard_refit_requests", "reject_dashboard_refit_request"):
        acc = ROUTE_ACCESS[op]
        assert (acc.resource, acc.action) == (_RK.DASHBOARD, _AC.UPDATE) and not acc.also
    approve = ROUTE_ACCESS["approve_dashboard_refit_request"]
    needs = [(approve.resource, approve.action), *approve.also]
    assert not all(can(ProjectRole.EDITOR, a, r) for r, a in needs)  # type: ignore[arg-type]
    assert all(can(ProjectRole.RUNNER, a, r) for r, a in needs)  # type: ignore[arg-type]
    assert ActualsInputs(enabled=True, refit_workflow_id="wf_01", allow_refit_requests=True)
    with pytest.raises(ValidationError, match="refit"):
        ActualsInputs(enabled=True, allow_refit_requests=True)


# ============================================================================ endpoint table


@pytest.mark.parametrize("e", V016_ENDPOINTS, ids=lambda e: e.operation_id)
def test_v016_rows_follow_the_conventions(e: api.EndpointDecl) -> None:
    assert e.path.startswith("/v1/"), e.path
    assert e.success in (200, 201, 202, 204), e
    assert (e.success == 204) <= (e.response is None), e
    for param in re.findall(r"\{([^}]+)\}", e.path):
        assert re.fullmatch(r"[a-z_]+", param), e.path
    if e.request is not None:
        assert e.method in ("POST", "PUT", "PATCH"), e
        _resolve(e.request)
    if e.response is not None:
        _resolve(e.response)
    assert e.paginated == (e.response or "").startswith("Page["), e
    if e.paginated:
        assert "limit" in e.query, e
    for code in e.errors:
        assert code in ERROR_INFO
    acc = ROUTE_ACCESS[e.operation_id]
    if e.path.startswith(("/v1/shared/", "/v1/share-sessions")):
        assert acc.scope is RouteScope.PUBLIC, e.operation_id
        assert PROJECT_HEADER not in e.headers
        assert (SHARE_SESSION_HEADER in e.headers) is (e.operation_id != "create_share_session")
    else:
        assert e.path.startswith("/v1/dashboards"), e.path
        assert acc.scope is RouteScope.PROJECT and PROJECT_HEADER in e.headers, e.operation_id
        assert acc.resource in (_RK.DASHBOARD, _RK.DASHBOARD_GRANT, _RK.DASHBOARD_SHARE)
        assert (acc.name_param == "dashboard_id") is ("{dashboard_id}" in e.path)


def test_v016_table_is_last_unique_and_exported() -> None:
    v017 = len(V017_ENDPOINTS)  # the v0.17 rows follow
    assert api.ENDPOINTS[-len(V016_ENDPOINTS) - v017 : -v017] == V016_ENDPOINTS
    keys = [(e.method, e.path) for e in api.ENDPOINTS]
    assert len(keys) == len(set(keys))
    assert len({e.operation_id for e in V016_ENDPOINTS}) == len(V016_ENDPOINTS) == 37
    for name in ("V016_ENDPOINTS", "CLIENT_OPERATIONS", "Dashboard", "Readout", "ShareSession"):
        assert name in api.__all__, name
    for code in ("DASHBOARD_NOT_PUBLISHED", "DASHBOARD_INPUT_DISABLED", "DASHBOARD_LIVE_PAUSED",
                 "DASHBOARD_RUN_LIMIT", "DASHBOARD_SPEC_STALE", "SHARE_PASSCODE_REQUIRED",
                 "SHARE_PASSCODE_INVALID", "SHARE_LOCKED", "INVALID_ROLE_SCOPE"):  # fmt: skip
        assert ErrorCode(code) in ERROR_INFO
    assert ERROR_INFO[ErrorCode.SHARE_LOCKED].status == 429
    assert ERROR_INFO[ErrorCode.DASHBOARD_RUN_LIMIT].status == 429


# ============================================================================ tokens, sessions


def test_share_token_regex_alphabet_and_entropy() -> None:
    assert dashboards._ALPHABET == sharing._ALPHABET and len(set(dashboards._ALPHABET)) == 32
    tokens = {generate_share_token() for _ in range(500)}
    assert len(tokens) == 500  # 40 x 5 bits = 200 random bits each
    for t in tokens:
        assert DASHBOARD_SHARE_TOKEN_RE.fullmatch(t), t
    seen = set("".join(t[4:] for t in tokens))
    assert seen <= set(dashboards._ALPHABET) and len(seen) == 32
    good = "mpd_" + "0123456789abcdefghjkmnpqrstvwxyz"[:40].ljust(40, "z")
    assert DASHBOARD_SHARE_TOKEN_RE.fullmatch(good)
    for bad in (good[:-1], good + "z", "mpe_" + good[4:], good.upper(), good[:-1] + "i",
                good[:-1] + "l", good[:-1] + "o", good[:-1] + "u", " " + good):  # fmt: skip
        assert not DASHBOARD_SHARE_TOKEN_RE.fullmatch(bad), bad
    assert share_display(good) == "mpd_…" + good[-4:]
    with pytest.raises(ValidationError):
        ShareSessionCreate(token="mpd_short")


def test_share_created_repr_redacts() -> None:
    created = DashboardShareCreated.model_validate(
        DashboardShareCreated.model_json_schema()["examples"][0]
    )
    assert created.token in created.model_dump_json()  # the 201 body carries it once
    for text in (repr(created), str(created)):
        assert created.token not in text and "redacted" in text
    session = ShareSession.model_validate(ShareSession.model_json_schema()["examples"][0])
    assert session.session not in repr(session) and session.session not in str(session)
    body = ShareSessionCreate(token=created.token, passcode="plum-orbit-42")  # type: ignore[arg-type]
    assert created.token not in repr(body) and "plum-orbit-42" not in repr(body)
    share = DashboardShareCreate(passcode="plum-orbit-42")  # type: ignore[arg-type]
    assert "plum-orbit-42" not in repr(share) and "plum-orbit-42" not in share.model_dump_json()
    assert share.passcode is not None and share.passcode.get_secret_value() == "plum-orbit-42"
    with pytest.raises(ValidationError):
        DashboardShareCreate(passcode="1234567")  # type: ignore[arg-type]  # 8 characters at least
    with pytest.raises(ValidationError):
        DashboardShareCreate(expires_in_days=366)
    assert DashboardShareCreate().allowed_inputs == set()  # links are read-only by default


# ============================================================================== spec, snapshot


def test_spec_note_is_plain_text_and_capped() -> None:
    spec = DashboardSpec.model_validate(_SPEC)
    assert dashboards.dashboard_spec_hash(spec).startswith("sha256:")
    multiline = DashboardSpec.model_validate({**_SPEC, "note": "Line one.\nLine two, 50 % < 60 %."})
    assert multiline.note is not None
    bad_text = (("note", "<b>bold</b>"), ("note", "x" * 2001), ("summary", "<script>"),
                ("note", "Tom &amp; Jerry"), ("title", "Two\nlines"), ("note", "bell\x07"),
                ("summary", "<img src=x onerror=alert(1)>"))  # fmt: skip
    for field, bad in bad_text:
        with pytest.raises(ValidationError):
            DashboardSpec.model_validate({**_SPEC, field: bad})
    for patch in ({"tabs": ["trends", "this_period"]}, {"tabs": ["this_period", "this_period"]},
                  {"tiles": ["expected"] * 2},
                  {"tiles": ["expected", "vs_expected", "accuracy", "index", "return_on_media"]},
                  {"channels": [_SPEC["channels"][0]] * 2},
                  {"refresh": {"follow_endpoint": True}}):  # fmt: skip
        with pytest.raises(ValidationError):
            DashboardSpec.model_validate({**_SPEC, **patch})
    with pytest.raises(ValidationError, match="contain 1"):
        ChannelDef(name="tv", label="TV", min_mult=1.1, max_mult=2.0)
    with pytest.raises(ValidationError, match="currency"):
        KpiDef(variable="revenue", label="Revenue", unit="currency")


def test_snapshot_over_cap_rejected() -> None:
    base: dict[str, Any] = {"lead": "Media brought 4,210 orders this week."}
    ReadoutSnapshot.model_validate(base)
    point = {"t": "2026-10-18", "mean": 1.0, "low": 0.5, "high": 1.5, "actual": 1.1}
    series = [{"name": f"s{i}", "label": f"S{i}", "points": [point] * 260} for i in range(2)]
    ReadoutSnapshot.model_validate({**base, "series": series})  # 520 points: the cap
    with pytest.raises(ValidationError, match="points"):
        ReadoutSnapshot.model_validate(
            {**base, "series": [*series, {**series[0], "points": [point]}]}
        )
    big = {f"k{i}": "x" * 5000 for i in range(60)}  # 300 kB > MAX_READOUT_BYTES
    with pytest.raises(ValidationError, match="bytes"):
        ReadoutSnapshot.model_validate({**base, "technical": big})
    ok = ReadoutSnapshot.model_validate({**base, "technical": {"k": "x" * 200_000}})
    assert len(ok.model_dump_json()) <= MAX_READOUT_BYTES
    with pytest.raises(ValidationError):
        ReadoutSnapshot.model_validate({**base, "notes_plain": ["x" * 401]})
    with pytest.raises(ValidationError):
        ReadoutSnapshot.model_validate({**base, "notes_plain": ["note"] * 21})
    assert MAX_SERIES_POINTS == 520


def test_cache_key_is_canonical_and_scoped_to_the_revision() -> None:
    a = readout_cache_key("dsr_1", {"tv": 0.8, "search": 1.0})
    assert a == readout_cache_key("dsr_1", {"search": 1, "tv": 0.8})
    assert a != readout_cache_key("dsr_2", {"tv": 0.8, "search": 1.0})
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", a)
    assert readout_cache_key("dsr_1", "fp") != readout_cache_key("dsr_1", "fq")


# ======================================================================== metrics and zones

# Copied from apps/web/src/features/score/compare.test.ts ("measures how far off the
# one-step-ahead predictions were"): the joined points of series n then s.
GOLDEN: dict[str, Any] = json.loads(
    """{
  "predicted": [128, 135, 133, 50, 52, 53],
  "actual": [130, 131, null, 50, null, null],
  "low": [120, 127, 121, 45, 47, 46],
  "high": [136, 143, 145, 55, 57, 60],
  "expected": {"n": 3, "bias": 0.6666666666666666,
               "mape": 1.5306322176551184, "band_hit_rate": 100}
}"""
)


def test_metrics_of_matches_compare_ts_golden() -> None:
    got = metrics_of(GOLDEN["predicted"], GOLDEN["actual"], GOLDEN["low"], GOLDEN["high"])
    want = GOLDEN["expected"]
    assert got.n == want["n"]
    assert got.bias == pytest.approx((-2 + 4 + 0) / 3) == pytest.approx(want["bias"])
    assert got.mape == pytest.approx(((2 / 130 + 4 / 131 + 0) / 3) * 100)
    assert got.mape == pytest.approx(want["mape"])
    assert got.band_hit_rate == want["band_hit_rate"]
    empty = metrics_of([1.0], [None], [None], [None])
    assert (empty.n, empty.mape, empty.bias, empty.band_hit_rate) == (0, None, None, None)
    zero = metrics_of([1.0, 2.0], [0.0, 2.0], [None, None], [None, None])
    assert zero.n == 2 and zero.mape == 0.0 and zero.band_hit_rate is None
    nan = metrics_of([float("nan"), 3.0], [1.0, 2.0], [0.0, 4.0], [2.0, 5.0])
    assert nan.n == 1 and nan.band_hit_rate == 0.0
    with pytest.raises(ValueError, match="same length"):
        metrics_of([1.0], [1.0, 2.0], [], [])


@pytest.mark.parametrize(
    ("roi", "mroi", "zone"),
    [
        (0.8, 0.5, "no_payback"),  # below breakeven, whatever the margin
        (0.8, None, "no_payback"),
        (1.5, 0.9, "over"),
        (1.5, 1.0, "optimal"),  # exactly breakeven at the margin
        (1.5, 1.2, "optimal"),  # the band's upper edge is still optimal
        (1.5, 1.21, "headroom"),
        (3.0, 2.5, "headroom"),
        (1.5, None, None),
        (None, 2.0, None),
    ],
)
def test_zone_of_thresholds(roi: float | None, mroi: float | None, zone: str | None) -> None:
    assert zone_of(roi, mroi, breakeven=1.0) == zone


def test_zone_breakeven_from_the_kpi() -> None:
    assert breakeven_of(KpiDef(variable="r", label="Revenue", unit="currency", currency="EUR")) == 1
    orders = KpiDef(variable="o", label="Orders", unit="count", value_per_unit=40.0)
    assert breakeven_of(orders) == pytest.approx(0.025)
    assert breakeven_of(KpiDef(variable="o", label="Orders", unit="count")) is None
    assert zone_of(0.05, 0.04, breakeven=0.025) == "headroom"


# ================================================================================ tool surface


def test_dashboard_tools_read_only_unique_and_schema_exportable() -> None:
    tools = dashboard_tools.DASHBOARD_TOOLS
    names = [t.name for t in tools]
    assert names[:8] == [
        "get_dashboard", "get_latest_readout", "list_readouts", "get_readout",
        "compare_readouts", "get_channel", "explain_metric", "evaluate_whatif",
    ]  # fmt: skip
    assert tuple(names[:8]) == dashboard_tools.V016_TOOL_NAMES  # v0.17 only appends
    assert tuple(names) == dashboard_tools.DASHBOARD_TOOL_NAMES
    for t in tools:
        assert t.read_only is True
        assert re.fullmatch(r"[a-z][a-z_]{2,63}", t.name)
        assert 10 <= len(t.summary_plain) <= 300 and "!" not in t.summary_plain
        assert dashboard_tools.tool(t.name) is t
    exported = dashboard_tools.tools_json_schema()
    text = json.dumps(exported, sort_keys=True)  # plain JSON
    assert json.loads(text) == exported
    for decl, entry in zip(tools, exported, strict=True):
        assert set(entry) == {"name", "description", "parameters", "result"}
        assert entry["name"] == decl.name
        params = entry["parameters"]
        assert isinstance(params, dict) and params.get("type") == "object"
        assert params.get("additionalProperties") is False  # arguments are strict
    # the HTTP routes return the same DTOs as the tools
    assert dashboard_tools.tool("get_dashboard").result is api.ClientDashboard
    assert dashboard_tools.tool("list_readouts").result is api.ReadoutList
    assert dashboard_tools.tool("evaluate_whatif").args is api.WhatIfRequest
    with pytest.raises(KeyError):
        dashboard_tools.tool("start_run")


def test_glossary_is_plain_and_explain_covers_it() -> None:
    assert set(GLOSSARY) == set(get_args(GlossaryKey))
    for key, text in GLOSSARY.items():
        assert text.endswith(".") and "!" not in text and len(text) <= 600, key
        assert dashboard_tools.explain(key).text == text
    with pytest.raises(ValidationError):
        dashboard_tools.ExplainArgs.model_validate({"metric": "p_value"})


# ====================================================================================== docs


def _docs() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "reference" / "http-api.md").exists():
            return parent / "docs"
    return None


def test_reference_docs_cover_v016() -> None:
    docs = _docs()
    if docs is None:  # pragma: no cover - sdist without the docs tree
        pytest.skip("docs/ not available")
    text = (docs / "reference" / "http-api.md").read_text("utf-8")
    for e in V016_ENDPOINTS:
        assert f"`{e.method} {e.path}`" in text, f"http-api.md is missing {e.method} {e.path}"
        for dto in filter(None, (e.request, e.response)):
            name = dto.removeprefix("Page[").removesuffix("]")
            assert f"`{name}`" in text, dto
    for page in ("whats-new-0.16.md", "concepts/client-dashboards.md",
                 "guides/client-dashboards.md", "guides/sharing-dashboards-securely.md",
                 "guides/for-clients.md"):  # fmt: skip
        assert (docs / page).exists(), page
    tenancy = (docs / "concepts" / "tenancy-and-auth.md").read_text("utf-8")
    assert "`client`" in tenancy and "CLIENT_OPERATIONS" in tenancy
    concepts = (docs / "concepts" / "client-dashboards.md").read_text("utf-8")
    for name in dashboard_tools.V016_TOOL_NAMES:  # the v0.17 tools: docs of v0.17
        assert f"`{name}`" in concepts, name
