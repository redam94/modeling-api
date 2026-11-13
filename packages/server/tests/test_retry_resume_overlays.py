"""Re-admitted runs keep their overlay pins (v0.19, WP7, D9).

A **retry** (``submit_retry``) copies the failed run's pins and gates them again for the
person retrying: a teammate cannot retry someone's run of a private upload. A **resume**
(``POST /v1/runs/{id}:resume``) re-admits the same row with ``overlays=None``, which never
clears ``runs.overlays``; the pins are gated for the run's original creator, so a version
withdrawn since then stops it.
"""

from __future__ import annotations

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.submit import admit, submit_retry
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.builders import trend_spec
from mapi_server_testkit.identity import grant_role, make_user, session_caller
from mapi_spec.api import RunRetry
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.run import RunState
from test_admission_overlays import STEP_SPEC, fit, the_pin
from test_catalog_view import acting, seed_version, set_state, sha

pytestmark = pytest.mark.integration


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


@pytest.fixture
async def people(ctx: ServerContext) -> dict[str, m.User]:
    """``ana`` and ``ed``, both admins of ``default`` (both may run anything)."""
    out: dict[str, m.User] = {}
    for name in ("ana", "ed"):
        user = await make_user(ctx, f"{name}@default.test")
        await grant_role(ctx, user, "default", None, "admin")
        out[name] = user
    return out


async def _private_run(
    ctx: ServerContext, people: dict[str, m.User], state: RunState
) -> tuple[str, str]:
    """``ed``'s fit with his own private upload, moved to ``state``; ``(version, run)``."""
    vid = await seed_version(ctx, uploaded_by=people["ed"].id, state="kit_passed", sha256=sha(31))
    run = await fit(await acting(ctx, people["ed"].id), await trend_spec(ctx, yaml=STEP_SPEC))
    async with ctx.db.session() as s:
        row = await s.get(m.Run, run.id)
        assert row is not None
        row.state = state.value
    return vid, run.id


async def _retry(ctx: ServerContext, user: m.User, run_id: str) -> m.Run:
    as_user = await acting(ctx, user.id)
    async with as_user.db.session() as s:
        old = await s.get(m.Run, run_id)
        assert old is not None
        run = await submit_retry(s, as_user, old, RunRetry())
        s.expunge(run)
    return run


async def test_a_retry_keeps_the_pins(ctx: ServerContext, people: dict[str, m.User]) -> None:
    vid, run_id = await _private_run(ctx, people, RunState.FAILED)
    again = await _retry(ctx, people["ed"], run_id)
    assert again.retry_of == run_id
    assert again.overlays == [the_pin(vid, sha(31))] and again.untrusted_code


async def test_a_teammate_cannot_retry_a_private_upload_run(
    ctx: ServerContext, people: dict[str, m.User]
) -> None:
    _, run_id = await _private_run(ctx, people, RunState.FAILED)
    with pytest.raises(MapiError) as err:
        await _retry(ctx, people["ana"], run_id)
    assert err.value.code is ErrorCode.PLUGIN_UNAVAILABLE


async def test_a_resume_keeps_the_pins_of_the_run(
    ctx: ServerContext, client: httpx.AsyncClient, people: dict[str, m.User]
) -> None:
    vid, run_id = await _private_run(ctx, people, RunState.PARTIAL)
    # resumed by a teammate: the pins are checked for the run's creator, and kept
    ana = await session_caller(ctx, client, people["ana"], tenant_id="default",
                               project_id="default")  # fmt: skip
    r = await ana.post(f"/v1/runs/{run_id}:resume")
    assert r.status_code == 202, r.text
    async with ctx.db.session() as s:
        row = await s.get(m.Run, run_id)
        assert row is not None
        assert row.overlays == [the_pin(vid, sha(31))] and row.untrusted_code


async def test_a_resume_is_refused_once_the_upload_is_withdrawn(
    ctx: ServerContext, client: httpx.AsyncClient, people: dict[str, m.User]
) -> None:
    vid, run_id = await _private_run(ctx, people, RunState.PARTIAL)
    await set_state(ctx, vid, "yanked")
    ed = await session_caller(ctx, client, people["ed"], tenant_id="default",
                              project_id="default")  # fmt: skip
    r = await ed.post(f"/v1/runs/{run_id}:resume")
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "PLUGIN_UNAVAILABLE"


async def test_admit_without_overlays_never_clears_them(
    ctx: ServerContext, people: dict[str, m.User]
) -> None:
    vid, run_id = await _private_run(ctx, people, RunState.QUEUED)
    async with ctx.db.session() as s:
        row = await s.get(m.Run, run_id)
        assert row is not None
        await admit(s, ctx, row, audit_action=None)  # no request: gated for the creator
        assert row.overlays == [the_pin(vid, sha(31))] and row.untrusted_code
