"""The CLI side of sign-in against a real ``MAPI_PROFILE=dev`` server over HTTP.

``mapi_client.oidc.dev_idp_login`` (used by ``scripts/seed.py`` and the end-to-end suite)
drives the server's development IdP without a browser; the resulting ID token is then the
Python client's bearer. This checks the two packages agree on the protocol (discovery URL,
public client, PKCE, nonce, audience) and on the issuer constant.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator, Iterator

import pytest
import uvicorn
from mapi_client import Client
from mapi_client.errors import MapiError as ClientError
from mapi_client.oidc import DEV_ISSUER, OidcError, dev_idp_login
from mapi_server import devidp
from mapi_server.app import create_app
from mapi_server.context import ServerContext
from mapi_server.settings import Profile, Settings

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


PORT = _free_port()
API = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.DEV, "api_url": API})


class _Quiet(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@pytest.fixture
async def live(ctx: ServerContext) -> AsyncIterator[str]:
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    server = _Quiet(
        uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    try:
        yield API
    finally:
        server.should_exit = True
        await task


def test_the_client_and_server_agree_on_the_dev_issuer() -> None:
    assert DEV_ISSUER == devidp.DEV_ISSUER


async def test_scripts_sign_in_to_a_dev_server_without_a_browser(live: str) -> None:
    def work() -> tuple[str, str, int]:
        token = dev_idp_login(live, "admin@dev.local")
        with Client(live, token=token, project="default/default") as api:
            me = api.me()
            created = api.datasets.create("from-dev-login")
            return me.principal.email or "", me.auth, len(created.id)

    email, how, n = await asyncio.to_thread(work)
    assert email == "admin@dev.local" and how == "session" and n > 0


async def test_unknown_dev_accounts_are_refused(live: str) -> None:
    with pytest.raises(OidcError, match="refused"):
        await asyncio.to_thread(dev_idp_login, live, "nobody@dev.local")


async def test_without_a_token_the_dev_server_wants_one(live: str) -> None:
    def work() -> int:
        with Client(live) as api:
            try:
                api.me()
            except ClientError as exc:
                return int(exc.status or 0)
        return 200

    assert await asyncio.to_thread(work) == 401
