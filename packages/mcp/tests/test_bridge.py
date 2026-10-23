"""The bridge: credentials, share sessions (open, refresh before expiry, reopen, ended link),
HTTP failures as JSON-RPC errors, and the config file rules."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from mapi_mcp import (
    Bridge,
    ConfigError,
    DashboardTokenCredential,
    ShareLinkCredential,
    load_config,
    redact,
)
from mapi_mcp.bridge import (
    API_UNREACHABLE,
    CREDENTIAL_REFUSED,
    LINK_ENDED,
    RATE_LIMITED,
    REFRESH_MARGIN_S,
    SERVER_ERROR,
)
from mapi_mcp.credentials import check_api_url
from mapi_spec.tokens import generate_token

from .conftest import API, PASSCODE, SHARE_TOKEN, FakeApi, rpc


def _share_bridge(api: FakeApi, transport: httpx.MockTransport) -> Bridge:
    return Bridge(API, ShareLinkCredential(SHARE_TOKEN, PASSCODE), transport, clock=api.clock)


async def _ping(bridge: Bridge, rid: int = 1) -> dict[str, object]:
    return json.loads(await bridge.forward(rpc("ping", rid=rid)) or "")  # type: ignore[no-any-return]


# ============================================================================ share sessions


async def test_share_session_opened_once_and_reused(
    api: FakeApi, transport: httpx.MockTransport
) -> None:
    async with _share_bridge(api, transport) as bridge:
        assert (await _ping(bridge))["result"] == {}
        assert (await _ping(bridge, 2))["result"] == {}
    assert api.sessions_opened == 1 and api.sessions_refreshed == 0
    opened = api.requests[0]
    assert opened.url.path == "/v1/share-sessions"
    assert json.loads(opened.content) == {"token": SHARE_TOKEN, "passcode": PASSCODE}
    for request in api.requests[1:]:
        assert request.headers["x-mapi-share"].startswith("mps_")
        assert "authorization" not in request.headers
        assert SHARE_TOKEN not in str(request.url) and SHARE_TOKEN.encode() not in request.content


async def test_share_session_refreshed_before_expiry(
    api: FakeApi, transport: httpx.MockTransport
) -> None:
    async with _share_bridge(api, transport) as bridge:
        await _ping(bridge)
        api.now += 1800 - REFRESH_MARGIN_S + 1  # inside the refresh margin, still valid
        assert (await _ping(bridge, 2))["result"] == {}
        api.now += 1000  # the old session would have expired: the refreshed one is used
        assert (await _ping(bridge, 3))["result"] == {}
    assert api.sessions_opened == 1 and api.sessions_refreshed == 1
    paths = [r.url.path for r in api.requests]
    assert paths == [
        "/v1/share-sessions", "/v1/mcp", "/v1/share-sessions:refresh", "/v1/mcp", "/v1/mcp",
    ]  # fmt: skip


async def test_expired_session_is_opened_again(
    api: FakeApi, transport: httpx.MockTransport
) -> None:
    async with _share_bridge(api, transport) as bridge:
        await _ping(bridge)
        api.now += 5000  # past expiry: no refresh possible, open a new session
        assert (await _ping(bridge, 2))["result"] == {}
    assert api.sessions_opened == 2


async def test_session_ended_by_server_is_reopened_once(
    api: FakeApi, transport: httpx.MockTransport
) -> None:
    async with _share_bridge(api, transport) as bridge:
        await _ping(bridge)
        api.valid_sessions.clear()  # e.g. the analyst changed the passcode (session version)
        assert (await _ping(bridge, 2))["result"] == {}
    assert api.sessions_opened == 2


async def test_ended_link_stops_every_call(api: FakeApi, transport: httpx.MockTransport) -> None:
    async with _share_bridge(api, transport) as bridge:
        await _ping(bridge)
        api.link_ended = True
        reply = await _ping(bridge, 2)
    error = reply["error"]
    assert isinstance(error, dict)
    assert error["code"] == LINK_ENDED
    assert error["data"] == {"status": 410, "code": "LINK_EXPIRED"}
    assert "secret detail" not in json.dumps(reply)


async def test_wrong_passcode(api: FakeApi, transport: httpx.MockTransport) -> None:
    bridge = Bridge(API, ShareLinkCredential(SHARE_TOKEN, "wrong"), transport, clock=api.clock)
    async with bridge:
        reply = await _ping(bridge)
    error = reply["error"]
    assert isinstance(error, dict) and error["code"] == CREDENTIAL_REFUSED
    assert reply["id"] == 1


# ====================================================================== tokens and HTTP errors


async def test_revoked_token(api: FakeApi, transport: httpx.MockTransport) -> None:
    bridge = Bridge(API, DashboardTokenCredential(str(generate_token())), transport)
    async with bridge:
        reply = await _ping(bridge)
    error = reply["error"]
    assert isinstance(error, dict)
    assert error["code"] == CREDENTIAL_REFUSED and error["data"] == {
        "status": 401, "code": "UNAUTHENTICATED",
    }  # fmt: skip


async def test_rate_limited(api: FakeApi, transport: httpx.MockTransport) -> None:
    api.status_override = 429
    async with Bridge(API, DashboardTokenCredential(api.token), transport) as bridge:
        reply = await _ping(bridge)
    error = reply["error"]
    assert isinstance(error, dict) and error["code"] == RATE_LIMITED
    assert error["data"] == {"status": 429, "code": "RATE_LIMITED", "retry_after_s": 7}


async def test_redirect_is_not_followed(api: FakeApi, transport: httpx.MockTransport) -> None:
    api.redirect = True
    async with Bridge(API, DashboardTokenCredential(api.token), transport) as bridge:
        reply = await _ping(bridge)
    error = reply["error"]
    assert isinstance(error, dict) and error["code"] == SERVER_ERROR
    assert len(api.requests) == 1


async def test_unreachable_api() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    cred = DashboardTokenCredential(str(generate_token()))
    async with Bridge(API, cred, httpx.MockTransport(fail)) as bridge:
        reply = await _ping(bridge)
        assert (
            await bridge.forward('{"jsonrpc":"2.0","method":"notifications/initialized"}') is None
        )
    error = reply["error"]
    assert isinstance(error, dict) and error["code"] == API_UNREACHABLE


async def test_invalid_answer() -> None:
    def junk(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy</html>")

    cred = DashboardTokenCredential(str(generate_token()))
    async with Bridge(API, cred, httpx.MockTransport(junk)) as bridge:
        reply = await _ping(bridge)
    error = reply["error"]
    assert isinstance(error, dict) and error["code"] == SERVER_ERROR


def test_credential_reprs_hide_secrets() -> None:
    token = str(generate_token())
    assert token not in repr(DashboardTokenCredential(token))
    cred = ShareLinkCredential(SHARE_TOKEN, PASSCODE)
    assert SHARE_TOKEN not in repr(cred) and PASSCODE not in repr(cred)


# ============================================================================ config


def _write(path: Path, data: dict[str, object], mode: int) -> Path:
    path.write_text(json.dumps(data))
    os.chmod(path, mode)
    return path


def test_env_token(tmp_path: Path) -> None:
    token = str(generate_token())
    config = load_config(
        {"MAPI_API_URL": "https://api.example.com/", "MAPI_DASHBOARD_TOKEN": token},
        config_path=tmp_path / "missing.json",
    )
    assert config.api_url == "https://api.example.com"
    assert config.credential == DashboardTokenCredential(token)


def test_env_share_link_from_url(tmp_path: Path) -> None:
    config = load_config(
        {
            "MAPI_API_URL": "https://api.example.com",
            "MAPI_SHARE_LINK": f"https://app.example.com/s#{SHARE_TOKEN}",
            "MAPI_SHARE_PASSCODE": PASSCODE,
        },
        config_path=tmp_path / "missing.json",
    )
    assert config.credential == ShareLinkCredential(SHARE_TOKEN, PASSCODE)


@pytest.mark.parametrize(
    "env",
    [
        {"MAPI_API_URL": "https://a.example"},
        {"MAPI_DASHBOARD_TOKEN": "TOKEN"},
        {"MAPI_API_URL": "https://a.example", "MAPI_DASHBOARD_TOKEN": "mapi_bad"},
        {"MAPI_API_URL": "https://a.example", "MAPI_SHARE_LINK": "https://app/s#nothing"},
        {"MAPI_API_URL": "https://a.example", "MAPI_DASHBOARD_TOKEN": "TOKEN",
         "MAPI_SHARE_LINK": SHARE_TOKEN},
    ],
)  # fmt: skip
def test_bad_env_refused_without_echo(tmp_path: Path, env: dict[str, str]) -> None:
    token = str(generate_token())
    env = {k: (token if v == "TOKEN" else v) for k, v in env.items()}
    with pytest.raises(ConfigError) as info:
        load_config(env, config_path=tmp_path / "missing.json")
    assert token not in str(info.value) and "mapi_bad" not in str(info.value)


@pytest.mark.parametrize(
    "url",
    ["http://api.example.com", "ftp://api.example.com", "https://user:pw@api.example.com",
     "https://api.example.com/?token=x", "https://api.example.com/#x", "not a url", "https://"],
)  # fmt: skip
def test_api_url_rules(url: str) -> None:
    with pytest.raises(ConfigError):
        check_api_url(url)


@pytest.mark.parametrize(
    "url", ["http://localhost:18000", "http://127.0.0.1:18000/", "http://[::1]:18000"]
)
def test_http_allowed_for_loopback(url: str) -> None:
    assert check_api_url(url) == url.rstrip("/")


def test_config_file_0600(tmp_path: Path) -> None:
    token = str(generate_token())
    path = _write(
        tmp_path / "mcp.json",
        {"api_url": "https://api.example.com", "dashboard_token": token},
        0o600,
    )
    config = load_config({}, config_path=path)
    assert config.credential == DashboardTokenCredential(token)
    assert config.api_url == "https://api.example.com"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666])
def test_config_file_not_0600_refused(tmp_path: Path, mode: int) -> None:
    token = str(generate_token())
    path = _write(
        tmp_path / "mcp.json",
        {"api_url": "https://api.example.com", "dashboard_token": token},
        mode,
    )
    with pytest.raises(ConfigError, match="chmod 600") as info:
        load_config({}, config_path=path)
    assert token not in str(info.value)


def test_config_file_named_by_env(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "other.json",
        {
            "api_url": "https://api.example.com",
            "share_link": SHARE_TOKEN,
            "share_passcode": PASSCODE,
        },
        0o600,
    )
    config = load_config({"MAPI_MCP_CONFIG": str(path)})
    assert config.credential == ShareLinkCredential(SHARE_TOKEN, PASSCODE)


def test_config_file_symlink_refused(tmp_path: Path) -> None:
    target = _write(tmp_path / "real.json", {"dashboard_token": str(generate_token())}, 0o600)
    link = tmp_path / "mcp.json"
    link.symlink_to(target)
    with pytest.raises(ConfigError):
        load_config({"MAPI_API_URL": "https://a.example"}, config_path=link)


@pytest.mark.parametrize(
    "data",
    [["x"], {"api_url": 5}, {"dashboard_token": "x", "password": "y"}],
)
def test_config_file_shape(tmp_path: Path, data: object) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)
    with pytest.raises(ConfigError):
        load_config({"MAPI_API_URL": "https://a.example"}, config_path=path)


def test_env_wins_over_file(tmp_path: Path) -> None:
    file_token, env_token = str(generate_token()), str(generate_token())
    path = _write(tmp_path / "mcp.json", {"dashboard_token": file_token}, 0o644)
    # the file is not read when the environment has everything (its mode does not matter)
    config = load_config(
        {"MAPI_API_URL": "https://a.example", "MAPI_DASHBOARD_TOKEN": env_token}, config_path=path
    )
    assert config.credential == DashboardTokenCredential(env_token)


# ============================================================================ redaction


def test_redact() -> None:
    token = str(generate_token())
    session = "mps_eyJzaWQiOiJkc3NfMDEiLCJleHAiOjE3." + "B" * 43
    text = f"t={token} s={SHARE_TOKEN} h=Bearer {token} sess={session} p={PASSCODE}"
    out = redact(text, [PASSCODE])
    for secret in (token, SHARE_TOKEN, PASSCODE, session):
        assert secret not in out
    assert "Bearer <redacted>" in out


# ============================================================================ packaging

_ROOT = Path(__file__).resolve().parents[3]


def _lock_packages(text: str) -> dict[str, dict[str, object]]:
    import tomllib

    return {p["name"]: p for p in tomllib.loads(text)["package"]}


def test_package_metadata() -> None:
    import tomllib

    meta = tomllib.loads((_ROOT / "packages/mcp/pyproject.toml").read_text())["project"]
    assert meta["version"] == "0.16.0"
    assert meta["license"] == "Apache-2.0"
    assert meta["dependencies"] == ["mapi-spec", "httpx>=0.27"]
    assert meta["scripts"] == {"mapi-mcp": "mapi_mcp.__main__:main"}


def test_lock_adds_no_third_party_package() -> None:
    import subprocess

    locked = _lock_packages((_ROOT / "uv.lock").read_text())
    entry = locked["mapi-mcp"]
    assert entry["version"] == "0.16.0"
    assert {d["name"] for d in entry["dependencies"]} == {"httpx", "mapi-spec"}  # type: ignore[union-attr,index]
    # httpx was already locked for the server and the client: nothing new comes in
    assert "httpx" in {d["name"] for d in locked["mapi-server"]["dependencies"]}  # type: ignore[union-attr,index]
    try:
        before = subprocess.run(
            ["git", "show", "HEAD:uv.lock"], cwd=_ROOT, capture_output=True, text=True,
            timeout=30, check=True,
        ).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        pytest.skip("no git history to compare the lock with")
    added = set(locked) - set(_lock_packages(before))
    assert added <= {"mapi-mcp"}, f"the lock gained third-party packages: {sorted(added)}"
