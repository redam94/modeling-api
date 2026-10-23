"""The stdio entry point: stdout carries JSON-RPC only, stderr is redacted, and a credential
is never taken from the command line. Subprocess tests make no network request (they fail
before the bridge would send anything, or send malformed lines that never leave)."""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from mapi_mcp import Bridge, DashboardTokenCredential, ShareLinkCredential
from mapi_mcp.__main__ import RedactingFilter, main, run_stdio
from mapi_spec.tokens import generate_token

from .conftest import API, PASSCODE, SHARE_TOKEN, FakeApi, rpc

_CLEAN_ENV_KEYS = (
    "MAPI_API_URL", "MAPI_DASHBOARD_TOKEN", "MAPI_SHARE_LINK", "MAPI_SHARE_PASSCODE",
    "MAPI_MCP_CONFIG", "MAPI_MCP_LOG",
)  # fmt: skip


def _run(args: list[str], env: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if k not in _CLEAN_ENV_KEYS}
    return subprocess.run(
        [sys.executable, "-m", "mapi_mcp", *args],
        input=stdin,
        capture_output=True,
        text=True,
        env={**base, **env},
        timeout=60,
        check=False,
    )


def _all_jsonrpc(stdout: str) -> list[dict[str, object]]:
    lines = stdout.splitlines()
    out = []
    for line in lines:
        msg = json.loads(line)
        assert isinstance(msg, dict) and msg["jsonrpc"] == "2.0"
        out.append(msg)
    return out


# ============================================================================ argv


@pytest.mark.parametrize(
    "args",
    [["--token", "TOKEN"], ["TOKEN"], ["--token=TOKEN"], ["--share-link", SHARE_TOKEN],
     ["--passcode", PASSCODE]],
)  # fmt: skip
def test_stdio_token_not_in_argv(args: list[str], tmp_path: Path) -> None:
    token = str(generate_token())
    args = [a.replace("TOKEN", token) for a in args]
    done = _run(args, {"MAPI_MCP_CONFIG": str(tmp_path / "none.json")})
    assert done.returncode == 2
    assert done.stdout == ""
    assert "takes no arguments" in done.stderr
    for secret in (token, SHARE_TOKEN, PASSCODE):
        assert secret not in done.stderr


def test_argv_refused_in_process(capsys: pytest.CaptureFixture[str]) -> None:
    token = str(generate_token())
    assert main(["--token", token]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and token not in captured.err


def test_help_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert main(["--version"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "MAPI_DASHBOARD_TOKEN" in captured.err and "mapi-mcp 0.16.0" in captured.err


# ============================================================================ config refusals


def test_missing_credential(tmp_path: Path) -> None:
    done = _run([], {"MAPI_API_URL": "https://api.example.com",
                     "MAPI_MCP_CONFIG": str(tmp_path / "none.json")})  # fmt: skip
    assert done.returncode == 2 and done.stdout == ""
    assert "no credential" in done.stderr


def test_config_not_0600_refused(tmp_path: Path) -> None:
    token = str(generate_token())
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"api_url": "https://api.example.com", "dashboard_token": token}))
    path.chmod(0o644)
    done = _run([], {"MAPI_MCP_CONFIG": str(path)})
    assert done.returncode == 2 and done.stdout == ""
    assert "chmod 600" in done.stderr and token not in done.stderr


def test_bad_token_not_echoed(tmp_path: Path) -> None:
    token = str(generate_token())
    typo = token[:-1] + ("0" if token[-1] != "0" else "1")
    done = _run([], {"MAPI_API_URL": "https://api.example.com", "MAPI_DASHBOARD_TOKEN": typo,
                     "MAPI_MCP_CONFIG": str(tmp_path / "none.json")})  # fmt: skip
    assert done.returncode == 2 and done.stdout == ""
    assert typo not in done.stderr


# ============================================================================ stdout / stderr


def test_stdio_stdout_only_jsonrpc_subprocess(tmp_path: Path) -> None:
    """Malformed input is answered locally: nothing reaches the network, and stdout has
    nothing but JSON-RPC messages (one per line)."""
    token = str(generate_token())
    stdin = "\n".join(
        ["{oops", f"[{rpc('ping')}]", "", '{"jsonrpc":"1.0","id":3,"method":"ping"}',
         json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ping", "params": {"p": "x" * 70_000}})]
    ) + "\n"  # fmt: skip
    done = _run(
        [],
        {"MAPI_API_URL": "https://api.invalid", "MAPI_DASHBOARD_TOKEN": token,
         "MAPI_MCP_LOG": "DEBUG", "MAPI_MCP_CONFIG": str(tmp_path / "none.json")},
        stdin,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    replies = _all_jsonrpc(done.stdout)
    assert [r["error"]["code"] for r in replies] == [-32700, -32600, -32600, -32600]  # type: ignore[index]
    assert token not in done.stderr and token not in done.stdout


async def test_stdio_stdout_only_jsonrpc(api: FakeApi, transport: httpx.MockTransport) -> None:
    lines = [
        rpc("initialize", {"protocolVersion": "2025-11-25"}),
        '{"jsonrpc":"2.0","method":"notifications/initialized"}',
        rpc("tools/list", rid=2),
        rpc("tools/call", {"name": "get_dashboard"}, rid=3),
        "\xff-not-json",
        rpc("ping", rid=4),
    ]
    stdin = io.BytesIO(("\n".join(lines) + "\n").encode("utf-8") + b"\xff\xfe\n")
    out = io.StringIO()
    async with Bridge(API, DashboardTokenCredential(api.token), transport) as bridge:
        await run_stdio(bridge, stdin.readline, out.write)
    replies = _all_jsonrpc(out.getvalue())
    # one answer per request (the notification gets none), in order
    assert [r.get("id") for r in replies] == [1, 2, 3, None, 4, None]
    assert replies[0]["result"]["protocolVersion"] == "2025-11-25"  # type: ignore[index]
    assert replies[3]["error"]["code"] == -32700  # type: ignore[index]
    assert replies[5]["error"]["code"] == -32700  # invalid UTF-8  # type: ignore[index]


def test_stdio_redacts_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    token = str(generate_token())
    session = "mps_eyJzaWQiOiJkc3NfMDEiLCJleHAiOjE3." + "C" * 43
    logger = logging.getLogger("mapi_mcp.test_redaction")
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactingFilter([PASSCODE]))
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning(
            "token %s link %s session %s passcode %s", token, SHARE_TOKEN, session, PASSCODE
        )
        logger.warning("Authorization: Bearer %s", token)
    finally:
        logger.removeHandler(handler)
    err = capsys.readouterr().err
    for secret in (token, SHARE_TOKEN, session, PASSCODE):
        assert secret not in err
    assert err.count("<redacted>") >= 5


async def test_share_bridge_logs_are_redacted(
    api: FakeApi, transport: httpx.MockTransport, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="mapi_mcp")
    bridge = Bridge(API, ShareLinkCredential(SHARE_TOKEN, PASSCODE), transport, clock=api.clock)
    async with bridge:
        await bridge.forward(rpc("ping"))
        api.link_ended = True
        await bridge.forward(rpc("ping", rid=2))
    text = caplog.text
    assert "share session opened" in text
    for secret in (SHARE_TOKEN, PASSCODE, "mps_"):
        assert secret not in text
