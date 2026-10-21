"""v0.16 from the client: client dashboards (list/get in both views, drafts, what-if,
scenarios, actuals and waiting for their readout, readout paging, refit requests, share
links) and the share-link view opened from a token - sync and async."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from mapi_client import (
    AsyncClient,
    AsyncSharedDashboard,
    Client,
    ClientConfig,
    SharedDashboard,
    UnexpectedResponse,
    share_token,
)
from mapi_client._v016_common import actuals_body, session_due
from mapi_spec.api import (
    ActualsSubmit,
    ActualsTemplate,
    ClientDashboard,
    Dashboard,
    DashboardGrid,
    DashboardRefitRequest,
    DashboardShareCreated,
    DashboardSummary,
    DashboardValidation,
    Readout,
    ReadoutHeader,
    ShareSession,
    WhatIfResult,
)
from mapi_spec.errors import MapiError

from .conftest import _example as ex

BASE = "http://api.test"
V1 = f"{BASE}/v1"
PROJECT = "acme/growth"
TOKEN = "mapi_test_token"
DSH = "dsh_01jaf3r1s2t3v4w5x6y7z8a9b0"
DRR = "drr_01jaf3w1x2y3z4a5b6c7d8e9f0"
SHARE = "mpd_7k2m9q4x0123456789abcdefghjkmnpqrstvwxyz"
SESSION = ex(ShareSession)["session"]
INPUTS = ex(ActualsSubmit)["inputs"]


def body_of(request: httpx.Request) -> Any:
    return json.loads(request.content)


def _page(*items: dict[str, Any], next_cursor: str | None = None) -> dict[str, Any]:
    return {"items": list(items), "next_cursor": next_cursor}


def _header(seq: int, status: str = "ready", **changes: Any) -> dict[str, Any]:
    return ex(ReadoutHeader, 0, seq=seq, status=status, **changes)


def _readout(seq: int, status: str = "ready", **changes: Any) -> dict[str, Any]:
    data = ex(Readout, 0, seq=seq, status=status, **changes)
    if status != "ready":
        data["snapshot"] = None
    return data


def _session(expires_in: timedelta = timedelta(minutes=30)) -> dict[str, Any]:
    expires = (datetime.now(UTC) + expires_in).isoformat().replace("+00:00", "Z")
    return ex(ShareSession, 0, expires_at=expires)


def _no_credentials(request: httpx.Request) -> bool:
    return "authorization" not in request.headers and "x-mapi-project" not in request.headers


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as r:
        yield r


@pytest.fixture
def client(router: respx.MockRouter) -> Iterator[Client]:
    config = ClientConfig(
        base_url=BASE, token=TOKEN, project=PROJECT, max_retries=0, allow_insecure_token=True
    )
    with Client(config=config, sleep=lambda _s: None) as c:
        yield c


@pytest.fixture
async def aclient(router: respx.MockRouter) -> AsyncIterator[AsyncClient]:
    async def nosleep(_s: float) -> None:
        return None

    config = ClientConfig(
        base_url=BASE, token=TOKEN, project=PROJECT, max_retries=0, allow_insecure_token=True
    )
    async with AsyncClient(config=config, sleep=nosleep) as c:
        yield c


# =============================================================================== dashboards


def test_list_and_get_in_both_views(client: Client, router: respx.MockRouter) -> None:
    listing = router.get(f"{V1}/dashboards").mock(
        side_effect=[
            httpx.Response(200, json=_page(ex(DashboardSummary), next_cursor="c2")),
            httpx.Response(200, json=_page(ex(DashboardSummary, name="acme-monthly"))),
        ]
    )
    rows = list(client.dashboards.list(status="published", limit=1))
    assert [r.name for r in rows] == ["acme-weekly", "acme-monthly"]
    first, second = (call.request for call in listing.calls)
    assert first.url.params["status"] == "published" and second.url.params["cursor"] == "c2"
    assert first.headers["x-mapi-project"] == PROJECT
    assert first.headers["authorization"] == f"Bearer {TOKEN}"
    with pytest.raises(ValueError, match="status"):
        client.dashboards.list(status="deleted").first_page()

    router.get(f"{V1}/dashboards/{DSH}").mock(
        side_effect=[
            httpx.Response(200, json=ex(Dashboard)),
            httpx.Response(200, json=ex(ClientDashboard)),
        ]
    )
    analyst = client.dashboards.get(DSH)
    assert isinstance(analyst, Dashboard) and analyst.draft_etag == '"3"'
    viewer = client.dashboards.get(DSH)
    assert isinstance(viewer, ClientDashboard) and viewer.title == "Weekly media results"


def test_draft_validate_publish(client: Client, router: respx.MockRouter) -> None:
    dash = ex(Dashboard)
    created = router.post(f"{V1}/dashboards").mock(return_value=httpx.Response(201, json=dash))
    draft = router.put(f"{V1}/dashboards/{DSH}/draft").mock(
        return_value=httpx.Response(200, json=dash)
    )
    router.post(f"{V1}/dashboards/{DSH}:validate").mock(
        return_value=httpx.Response(200, json=ex(DashboardValidation, 1))
    )
    router.post(f"{V1}/dashboards/{DSH}:publish").mock(return_value=httpx.Response(200, json=dash))

    out = client.dashboards.create("acme-weekly", dash["draft_spec"])
    assert out.id == DSH and body_of(created.calls.last.request)["name"] == "acme-weekly"
    client.dashboards.update_draft(DSH, dash["draft_spec"], etag='"3"')
    assert draft.calls.last.request.headers["if-match"] == '"3"'
    check = client.dashboards.validate(DSH)
    assert not check.ok and check.issues[0].code == "BINDING_INVALID"
    assert client.dashboards.publish(DSH).status == "published"


def test_whatif_and_scenario(client: Client, router: respx.MockRouter) -> None:
    whatif = router.post(f"{V1}/dashboards/{DSH}/whatif:evaluate").mock(
        return_value=httpx.Response(200, json=ex(WhatIfResult))
    )
    scen = router.post(f"{V1}/dashboards/{DSH}/scenarios").mock(
        return_value=httpx.Response(200, json=_readout(13, kind="scenario"))
    )
    result = client.dashboards.whatif(DSH, {"tv": 0.8, "search": 1.3})
    assert result.delta_kpi == 85.0
    assert body_of(whatif.calls.last.request) == {"multipliers": {"tv": 0.8, "search": 1.3}}
    with pytest.raises(ValueError):
        client.dashboards.whatif(DSH, {"tv": -1.0})

    saved = client.dashboards.save_scenario(DSH, {"tv": 0.8}, label="Less TV", wait=20)
    assert saved.seq == 13
    req = scen.calls.last.request
    assert req.headers["prefer"] == "wait=20" and "idempotency-key" in req.headers
    assert body_of(req) == {"multipliers": {"tv": 0.8}, "exact": False, "label": "Less TV"}


def test_actuals_submit_and_wait(client: Client, router: respx.MockRouter) -> None:
    router.get(f"{V1}/dashboards/{DSH}/actuals/template").mock(
        return_value=httpx.Response(200, json=ex(ActualsTemplate))
    )
    submit = router.post(f"{V1}/dashboards/{DSH}/actuals").mock(
        return_value=httpx.Response(
            202, json=_header(14, "queued", kind="actuals", refit_status="open")
        )
    )
    polls = router.get(f"{V1}/dashboards/{DSH}/readouts/14").mock(
        side_effect=[
            httpx.Response(200, json=_readout(14, "running")),
            httpx.Response(200, json=_readout(14, "ready")),
        ]
    )
    assert client.dashboards.actuals_template(DSH).mode == "compared"
    header = client.dashboards.submit_actuals(
        DSH, INPUTS, label="Week 43", request_refit=True, idempotency_key="k-1"
    )
    assert header.status == "queued" and header.refit_status == "open"
    req = submit.calls.last.request
    assert req.headers["idempotency-key"] == "k-1"
    sent = body_of(req)
    assert sent["label"] == "Week 43" and sent["request_refit"] is True
    assert sent["inputs"]["data_vars"]["orders"]["data"] == [4302.0]

    done = client.dashboards.wait_readout(DSH, header.seq, poll_interval=0.01)
    assert done.status == "ready" and polls.call_count == 2


def test_wait_readout_raises_the_problem_or_times_out(
    client: Client, router: respx.MockRouter
) -> None:
    problem = {"title": "Score failed", "status": 500, "code": "INTERNAL_ERROR"}
    router.get(f"{V1}/dashboards/{DSH}/readouts/15").mock(
        return_value=httpx.Response(200, json=_readout(15, "failed", problem=problem))
    )
    with pytest.raises(MapiError) as info:
        client.dashboards.wait_readout(DSH, 15)
    assert info.value.code == "INTERNAL_ERROR"
    failed = client.dashboards.wait_readout(DSH, 15, raise_on_failure=False)
    assert failed.status == "failed" and failed.problem is not None

    router.get(f"{V1}/dashboards/{DSH}/readouts/16").mock(
        return_value=httpx.Response(200, json=_readout(16, "running"))
    )
    with pytest.raises(TimeoutError, match="still running"):
        client.dashboards.wait_readout(DSH, 16, timeout=0.0, poll_interval=1.0)


def test_actuals_body_needs_the_inputs() -> None:
    with pytest.raises(TypeError):
        actuals_body(
            ActualsSubmit.model_validate({"inputs": INPUTS}), label=None, request_refit=False
        )
    with pytest.raises(ValueError):
        actuals_body({"nope": 1}, label=None, request_refit=False)


def test_actuals_from_an_xarray_dataset(client: Client, router: respx.MockRouter) -> None:
    xr = pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")
    ds = xr.Dataset(
        {"orders": ("week", np.array([4302.0])), "tv": ("week", np.array([51000.0]))},
        coords={"week": np.array(["2026-10-25"], dtype="datetime64[ns]")},
    )
    submit = router.post(f"{V1}/dashboards/{DSH}/actuals").mock(
        return_value=httpx.Response(202, json=_header(14, "queued", kind="actuals"))
    )
    client.dashboards.submit_actuals(DSH, ds)
    sent = body_of(submit.calls.last.request)["inputs"]
    assert sent["data_vars"]["orders"]["data"] == [4302.0]
    assert sent["coords"]["week"]["data"][0].startswith("2026-10-25")


# ================================================================================= readouts


def test_readouts_page_back_by_seq(client: Client, router: respx.MockRouter) -> None:
    route = router.get(f"{V1}/dashboards/{DSH}/readouts").mock(
        side_effect=[
            httpx.Response(200, json={"items": [_header(12), _header(11)], "next_before_seq": 11}),
            httpx.Response(200, json={"items": [_header(10)], "next_before_seq": None}),
        ]
    )
    seqs = [r.seq for r in client.dashboards.readouts(DSH, kind="refresh", limit=2)]
    assert seqs == [12, 11, 10]
    first, second = (call.request for call in route.calls)
    assert dict(first.url.params) == {"kind": "refresh", "limit": "2"}
    assert second.url.params["before_seq"] == "11"
    route.side_effect = None
    route.return_value = httpx.Response(200, json={"items": [], "next_before_seq": None})
    page = client.dashboards.readout_page(DSH, visibility="private", before_seq=5)
    assert page.items == []
    assert dict(route.calls.last.request.url.params) == {"before_seq": "5", "visibility": "private"}


def test_readouts_stop_a_server_that_does_not_move_back(
    client: Client, router: respx.MockRouter
) -> None:
    router.get(f"{V1}/dashboards/{DSH}/readouts").mock(
        return_value=httpx.Response(200, json={"items": [_header(12)], "next_before_seq": 12})
    )
    with pytest.raises(UnexpectedResponse):
        list(client.dashboards.readouts(DSH))


def test_publish_and_hide_readouts(client: Client, router: respx.MockRouter) -> None:
    router.post(f"{V1}/dashboards/{DSH}/readouts/14:publish").mock(
        return_value=httpx.Response(200, json=_header(14, visibility="published"))
    )
    router.post(f"{V1}/dashboards/{DSH}/readouts/14:hide").mock(
        return_value=httpx.Response(200, json=_header(14, visibility="hidden"))
    )
    assert client.dashboards.publish_readout(DSH, 14).visibility == "published"
    assert client.dashboards.hide_readout(DSH, 14).visibility == "hidden"


# =========================================================================== refit requests


def test_refit_requests_list_approve_reject(client: Client, router: respx.MockRouter) -> None:
    listing = router.get(f"{V1}/dashboards/{DSH}/refit-requests").mock(
        return_value=httpx.Response(200, json=_page(ex(DashboardRefitRequest, 0, id=DRR)))
    )
    approve = router.post(f"{V1}/dashboards/{DSH}/refit-requests/{DRR}:approve").mock(
        return_value=httpx.Response(200, json=ex(DashboardRefitRequest, 1, id=DRR))
    )
    reject = router.post(f"{V1}/dashboards/{DSH}/refit-requests/{DRR}:reject").mock(
        return_value=httpx.Response(
            200, json=ex(DashboardRefitRequest, 0, id=DRR, status="rejected")
        )
    )
    open_ = list(client.dashboards.refit_requests(DSH, status="open"))
    assert [r.id for r in open_] == [DRR]
    assert listing.calls.last.request.url.params["status"] == "open"

    done = client.dashboards.approve_refit(DSH, DRR, note="Refit with October.")
    assert done.status == "approved" and done.workflow_run_id is not None
    req = approve.calls.last.request
    assert body_of(req) == {"note": "Refit with October."} and "idempotency-key" in req.headers
    assert req.headers["x-mapi-project"] == PROJECT

    assert client.dashboards.reject_refit(DSH, DRR).status == "rejected"
    assert body_of(reject.calls.last.request) == {}


# ============================================================================= share links


def test_create_share_sends_the_passcode_in_clear(client: Client, router: respx.MockRouter) -> None:
    created = router.post(f"{V1}/dashboards/{DSH}/shares").mock(
        return_value=httpx.Response(201, json=ex(DashboardShareCreated))
    )
    share = client.dashboards.create_share(
        DSH, label="Board pack", passcode="correct horse", allowed_inputs=["whatif"]
    )
    assert share.token.startswith("mpd_") and "token" not in repr(share).replace("token=<", "")
    req = created.calls.last.request
    assert body_of(req) == {
        "label": "Board pack",
        "expires_in_days": 30,
        "passcode": "correct horse",
        "allowed_inputs": ["whatif"],
    }
    assert "idempotency-key" not in req.headers  # never resent: it would mint a second link
    with pytest.raises(ValueError):
        client.dashboards.create_share(DSH, passcode="short")


def test_share_token_reads_links() -> None:
    assert share_token(SHARE) == SHARE
    assert share_token(f"/s#{SHARE}") == SHARE
    assert share_token(f"https://app.example/s#{SHARE}\n") == SHARE
    for bad in ("", "/s#", "mpd_short", f"https://app.example/s?t={SHARE}"):
        with pytest.raises(ValueError, match="share"):
            share_token(bad)


def test_open_share_sends_no_credentials(client: Client, router: respx.MockRouter) -> None:
    opened = router.post(f"{V1}/share-sessions").mock(
        return_value=httpx.Response(200, json=_session())
    )
    readouts = router.get(f"{V1}/shared/readouts").mock(
        return_value=httpx.Response(200, json={"items": [_header(12)], "next_before_seq": None})
    )
    router.get(f"{V1}/shared/readouts/12").mock(return_value=httpx.Response(200, json=_readout(12)))
    whatif = router.post(f"{V1}/shared/whatif:evaluate").mock(
        return_value=httpx.Response(200, json=ex(WhatIfResult))
    )
    actuals = router.post(f"{V1}/shared/actuals").mock(
        return_value=httpx.Response(202, json=_header(14, "queued", kind="actuals"))
    )
    router.get(f"{V1}/shared/dashboard").mock(
        return_value=httpx.Response(200, json=ex(ClientDashboard))
    )

    view = client.dashboards.open_share(f"https://app.example/s#{SHARE}", passcode="hunter22")
    assert isinstance(view, SharedDashboard) and view.dashboard.id == DSH
    assert SESSION not in repr(view)
    req = opened.calls.last.request
    assert body_of(req) == {"token": SHARE, "passcode": "hunter22"} and _no_credentials(req)

    assert [r.seq for r in view.readouts()] == [12]
    assert view.readout(12).snapshot is not None
    assert view.whatif({"tv": 0.5}).lead
    assert view.submit_actuals(INPUTS).status == "queued"
    assert view.get().title == "Weekly media results"
    for route in (readouts, whatif, actuals):
        sent = route.calls.last.request
        assert sent.headers["x-mapi-share"] == SESSION and _no_credentials(sent)


def test_shared_session_is_refreshed_before_it_expires(
    client: Client, router: respx.MockRouter
) -> None:
    router.post(f"{V1}/share-sessions").mock(
        return_value=httpx.Response(200, json=_session(timedelta(seconds=5)))
    )
    refresh = router.post(f"{V1}/share-sessions:refresh").mock(
        return_value=httpx.Response(200, json=_session())
    )
    router.get(f"{V1}/shared/grid").mock(return_value=httpx.Response(200, json=ex(DashboardGrid)))
    view = client.dashboards.open_share(SHARE)
    assert session_due(ShareSession.model_validate(_session(timedelta(seconds=5))))
    view.grid()
    assert refresh.call_count == 1
    assert refresh.calls.last.request.headers["x-mapi-share"] == SESSION
    view.grid()
    assert refresh.call_count == 1  # the fresh session is good for 30 minutes


# ================================================================================== async


async def test_async_parity(aclient: AsyncClient, router: respx.MockRouter) -> None:
    router.get(f"{V1}/dashboards").mock(
        return_value=httpx.Response(200, json=_page(ex(DashboardSummary)))
    )
    router.get(f"{V1}/dashboards/{DSH}").mock(
        return_value=httpx.Response(200, json=ex(ClientDashboard))
    )
    router.post(f"{V1}/dashboards/{DSH}/whatif:evaluate").mock(
        return_value=httpx.Response(200, json=ex(WhatIfResult))
    )
    router.post(f"{V1}/dashboards/{DSH}/actuals").mock(
        return_value=httpx.Response(202, json=_header(14, "queued", kind="actuals"))
    )
    router.get(f"{V1}/dashboards/{DSH}/readouts/14").mock(
        side_effect=[
            httpx.Response(200, json=_readout(14, "materialising")),
            httpx.Response(200, json=_readout(14, "ready")),
        ]
    )
    router.get(f"{V1}/dashboards/{DSH}/readouts").mock(
        side_effect=[
            httpx.Response(200, json={"items": [_header(14)], "next_before_seq": 14}),
            httpx.Response(200, json={"items": [_header(13)], "next_before_seq": None}),
        ]
    )
    router.get(f"{V1}/dashboards/{DSH}/refit-requests").mock(
        return_value=httpx.Response(200, json=_page(ex(DashboardRefitRequest, 0, id=DRR)))
    )
    router.post(f"{V1}/dashboards/{DSH}/refit-requests/{DRR}:approve").mock(
        return_value=httpx.Response(200, json=ex(DashboardRefitRequest, 1, id=DRR))
    )
    router.post(f"{V1}/dashboards/{DSH}/refit-requests/{DRR}:reject").mock(
        return_value=httpx.Response(
            200, json=ex(DashboardRefitRequest, 0, id=DRR, status="rejected")
        )
    )
    dashboards = aclient.dashboards
    assert [d.name async for d in dashboards.list()] == ["acme-weekly"]
    assert isinstance(await dashboards.get(DSH), ClientDashboard)
    assert (await dashboards.whatif(DSH, {"tv": 0.8})).delta_kpi == 85.0
    header = await dashboards.submit_actuals(DSH, INPUTS, request_refit=True)
    assert (await dashboards.wait_readout(DSH, header.seq)).status == "ready"
    assert [r.seq async for r in dashboards.readouts(DSH)] == [14, 13]
    assert [r.id async for r in dashboards.refit_requests(DSH)] == [DRR]
    assert (await dashboards.approve_refit(DSH, DRR, note="ok")).status == "approved"
    assert (await dashboards.reject_refit(DSH, DRR)).status == "rejected"


async def test_async_share_view(aclient: AsyncClient, router: respx.MockRouter) -> None:
    opened = router.post(f"{V1}/share-sessions").mock(
        return_value=httpx.Response(200, json=_session())
    )
    readouts = router.get(f"{V1}/shared/readouts").mock(
        return_value=httpx.Response(200, json={"items": [_header(12)], "next_before_seq": None})
    )
    router.get(f"{V1}/shared/readouts/12").mock(return_value=httpx.Response(200, json=_readout(12)))
    router.get(f"{V1}/shared/actuals/template").mock(
        return_value=httpx.Response(200, json=ex(ActualsTemplate))
    )
    view = await aclient.dashboards.open_share(f"/s#{SHARE}")
    assert isinstance(view, AsyncSharedDashboard) and SESSION not in repr(view)
    assert body_of(opened.calls.last.request) == {"token": SHARE}
    assert _no_credentials(opened.calls.last.request)
    assert [r.seq async for r in view.readouts(kind="refresh")] == [12]
    assert (await view.wait_readout(12)).status == "ready"
    assert (await view.actuals_template()).kpi_column == "orders"
    sent = readouts.calls.last.request
    assert sent.headers["x-mapi-share"] == SESSION and _no_credentials(sent)
