"""How the stack mode and the end-to-end suites call a running stack (dev profile or not)."""

from __future__ import annotations

from pathlib import Path

import respx
from mapi_conformance.stack import StackAuth, stack_auth

API = "http://127.0.0.1:18000"


def test_a_single_tenant_stack_needs_no_credentials(tmp_path: Path) -> None:
    with respx.mock() as router:
        router.get(f"{API}/v1/me").respond(200, json={})
        auth = stack_auth(API, data_dir=tmp_path, environ={})
    assert auth == StackAuth()
    assert auth.headers() == {} and auth.internal_headers() == {}


def test_a_dev_stack_signs_in_and_finds_its_internal_token(tmp_path: Path) -> None:
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / "internal-token").write_text("s3cret\n")
    with respx.mock() as router:
        router.get(f"{API}/v1/me").respond(401, json={})
        auth = stack_auth(
            API, data_dir=tmp_path, environ={"MAPI_TOKEN": "mapi_tok", "MAPI_PROJECT": "a/b"}
        )
    assert auth.headers() == {"Authorization": "Bearer mapi_tok", "X-Mapi-Project": "a/b"}
    assert auth.internal_headers() == {"Authorization": "Bearer s3cret"}


def test_an_unreachable_stack_is_left_to_the_caller(tmp_path: Path) -> None:
    import httpx

    with respx.mock() as router:
        router.get(f"{API}/v1/me").mock(side_effect=httpx.ConnectError("refused"))
        auth = stack_auth(API, data_dir=tmp_path, environ={"MAPI_INTERNAL_TOKEN": "x"})
    assert auth == StackAuth(internal_token="x")
