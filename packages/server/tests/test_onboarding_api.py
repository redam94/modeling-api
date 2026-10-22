"""``/v1/tenants/{tenant_id}/me/onboarding``: the tour and checklist progress of the signed-in
member (v0.18 D19). Per user and per tenant, sessions only, merges that never un-complete a
step, ``If-Match`` against the version, a 2 KiB body cap."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import (
    grant_role,
    make_tenant,
    make_token,
    make_user,
    session_caller,
    token_caller,
)
from mapi_spec.onboarding import MAX_ONBOARDING_PATCH_BYTES, OnboardingState
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

URL = "/v1/tenants/acme/me/onboarding"
OPS = "ops@platform.test"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT, "operator_emails": [OPS]})


async def _people(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    await make_tenant(ctx, "acme", projects=("growth",))
    await make_tenant(ctx, "other", projects=("main",))
    out: dict[str, Any] = {}
    for role in ("viewer", "editor"):
        u = await make_user(ctx, f"{role}@acme.test")
        await grant_role(ctx, u, "acme", "growth", role)
        out[role] = await session_caller(ctx, client, u, tenant_id="acme", project_id="growth")
        out[f"{role}_user"] = u
    admin = await make_user(ctx, "admin@acme.test")
    await grant_role(ctx, admin, "acme", None, "admin")
    await grant_role(ctx, admin, "other", None, "admin")
    out["admin"] = await session_caller(ctx, client, admin)
    out["admin_user"] = admin
    cl = await make_user(ctx, "client@acme.test")
    await grant_role(ctx, cl, "acme", "growth", "client")
    out["client"] = await session_caller(ctx, client, cl)
    outsider = await make_user(ctx, "someone@other.test")
    await grant_role(ctx, outsider, "other", "main", "editor")
    out["outsider"] = await session_caller(ctx, client, outsider)
    return out


def _state(r: httpx.Response) -> OnboardingState:
    assert r.status_code == 200, r.text
    state = OnboardingState.model_validate(r.json())
    assert r.headers["ETag"] == f'"{state.version}"'
    return state


async def test_a_new_member_reads_an_empty_state_at_version_zero(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    state = _state(await who["viewer"].get(URL))
    assert state == OnboardingState(version=0)
    # reading writes nothing
    async with ctx.db.session() as s:
        assert (await s.execute(select(m.OnboardingProgress))).first() is None


async def test_patch_merges_and_never_uncompletes_a_step(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    viewer = who["viewer"]
    first = _state(await viewer.patch(URL, json={"complete": ["open_sample"], "welcomed": True}))
    assert first.version == 1
    assert set(first.checklist) == {"open_sample"} and first.welcomed_at is not None
    done_at = first.checklist["open_sample"]

    tour = {"status": "completed", "step": 4, "updated_at": "2020-01-01T00:00:00Z"}
    second = _state(
        await viewer.patch(
            URL, json={"complete": ["open_sample", "fit_model"], "tours": {"welcome": tour}}
        )
    )
    assert second.version == 2
    # a step keeps its first time; a completed tour also completes took_tour
    assert second.checklist["open_sample"] == done_at
    assert set(second.checklist) == {"took_tour", "open_sample", "fit_model"}
    assert second.tours["welcome"].status == "completed"
    assert second.tours["welcome"].updated_at.year > 2020  # set by the server

    # nothing in a PATCH removes a step; dismissing and tips are plain flags
    third = _state(
        await viewer.patch(
            URL,
            json={"complete": [], "checklist_dismissed": True, "tips_off": True,
                  "tours": {"welcome": {**tour, "status": "dismissed"}}},
        )
    )  # fmt: skip
    assert set(third.checklist) == {"took_tour", "open_sample", "fit_model"}
    assert third.checklist_dismissed and third.tips_off

    # a PATCH that changes nothing keeps the version
    same = _state(await viewer.patch(URL, json={"complete": ["fit_model"]}))
    assert same.version == third.version
    assert _state(await viewer.get(URL)) == same


async def test_unknown_steps_and_fields_are_refused(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    for body in ({"complete": ["took_a_nap"]}, {"bogus": True}, {"tours": {"nope": {}}}):
        r = await who["viewer"].patch(URL, json=body)
        assert r.status_code == 422, (body, r.text)
    assert _state(await who["viewer"].get(URL)).version == 0


async def test_if_match_must_name_the_current_version(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    viewer = who["viewer"]
    etag = (await viewer.get(URL)).headers["ETag"]
    ok = await viewer.patch(URL, json={"complete": ["own_data"]}, headers={"If-Match": etag})
    assert _state(ok).version == 1
    # the same (now stale) ETag is refused, and nothing changes
    stale = await viewer.patch(URL, json={"complete": ["fit_model"]}, headers={"If-Match": etag})
    assert stale.status_code == 412, stale.text
    assert stale.json()["code"] == "PRECONDITION_FAILED"
    assert set(_state(await viewer.get(URL)).checklist) == {"own_data"}
    # weak tags and * are accepted
    weak = await viewer.patch(URL, json={"complete": ["fit_model"]}, headers={"If-Match": 'W/"1"'})
    assert _state(weak).version == 2
    star = await viewer.patch(URL, json={"tips_off": True}, headers={"If-Match": "*"})
    assert _state(star).version == 3


async def test_a_body_over_two_kib_is_413(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    who = await _people(ctx, client)
    head = '{"complete": [], "hide_samples": ["x"], "unhide_samples": ['
    tail = "]}"
    filler = MAX_ONBOARDING_PATCH_BYTES - len(head) - len(tail)
    exact = head + " " * filler + tail
    assert len(exact.encode()) == MAX_ONBOARDING_PATCH_BYTES
    headers = {"Content-Type": "application/json"}
    ok = await who["viewer"].patch(URL, content=exact, headers=headers)
    assert ok.status_code == 200, ok.text
    over = head + " " * (filler + 1) + tail
    r = await who["viewer"].patch(URL, content=over, headers=headers)
    assert r.status_code == 413, r.text
    assert r.json()["code"] == "PAYLOAD_TOO_LARGE"


async def test_api_tokens_and_clients_are_refused(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    secret = await make_token(ctx, who["admin_user"], "acme", "growth", ["admin"])
    as_token = token_caller(client, secret)
    for r in (
        await as_token.get(URL),
        await as_token.patch(URL, json={"complete": ["took_tour"]}),
        await as_token.post(URL + ":reset"),
    ):
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "FORBIDDEN"
    # a client reaches no route outside the dashboard portal (404, as for anything hidden)
    for r in (
        await who["client"].get(URL),
        await who["client"].patch(URL, json={"complete": ["took_tour"]}),
    ):
        assert r.status_code == 404, r.text
    async with ctx.db.session() as s:
        assert (await s.execute(select(m.OnboardingProgress))).first() is None


async def test_progress_is_per_user_and_per_tenant(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    _state(await who["viewer"].patch(URL, json={"complete": ["took_tour"]}))
    # another member of the same tenant has their own progress
    assert _state(await who["editor"].get(URL)).version == 0
    # the admin of two tenants: progress in one is not progress in the other
    admin = who["admin"]
    _state(await admin.patch(URL, json={"complete": ["make_dashboard"]}))
    other = _state(await admin.get("/v1/tenants/other/me/onboarding"))
    assert other.version == 0 and not other.checklist
    # a member of another tenant cannot reach this tenant's route at all
    for r in (
        await who["outsider"].get(URL),
        await who["outsider"].patch(URL, json={"complete": ["took_tour"]}),
    ):
        assert r.status_code == 404, r.text
    async with ctx.db.session() as s:
        rows = list((await s.execute(select(m.OnboardingProgress))).scalars())
    assert sorted((r.tenant_id, r.principal_id) for r in rows) == sorted(
        [("acme", who["viewer_user"].id), ("acme", who["admin_user"].id)]
    )
    by_user = {r.principal_id: r for r in rows}
    assert by_user[who["viewer_user"].id].state["checklist"].keys() == {"took_tour"}


async def test_reset_starts_again_but_keeps_hidden_samples_and_the_welcome(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    viewer = who["viewer"]
    before = _state(
        await viewer.patch(
            URL,
            json={"complete": ["took_tour", "fit_model"], "welcomed": True, "tips_off": True,
                  "hide_samples": ["geo_mmm"], "checklist_dismissed": True},
        )
    )  # fmt: skip
    after = _state(await viewer.post(URL + ":reset"))
    assert after.version == before.version + 1
    assert not after.checklist and not after.tours
    assert not after.tips_off and not after.checklist_dismissed
    assert after.hidden_samples == ["geo_mmm"] and after.welcomed_at == before.welcomed_at
    # a reset on a member without a row creates it
    fresh = _state(await who["editor"].post(URL + ":reset"))
    assert fresh.version == 1
