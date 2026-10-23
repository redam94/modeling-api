"""``mapi-mcp``: the stdio entry point.

An MCP client starts this command and talks JSON-RPC over its stdin and stdout, one message
per line. stdout carries JSON-RPC messages only; logs go to stderr with every secret
redacted. The command takes no arguments, so a credential never appears in a process list
or a shell history: it reads ``MAPI_API_URL`` and ``MAPI_DASHBOARD_TOKEN`` (or
``MAPI_SHARE_LINK`` and ``MAPI_SHARE_PASSCODE``) from the environment, or
``~/.config/mapi/mcp.json`` (mode 0600).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable, Sequence
from typing import Final

from mapi_spec.mcp import PARSE_ERROR, JsonRpcError, error_response

from mapi_mcp import __version__
from mapi_mcp.bridge import Bridge, json_line
from mapi_mcp.credentials import ConfigError, Credential, load_config, redact

__all__ = ["main", "run_stdio"]

USAGE: Final = (
    "usage: mapi-mcp\n"
    "\n"
    "Connects a chat tool to one dashboard over the Model Context Protocol (stdio).\n"
    "Set these in the environment of the chat tool (never on the command line):\n"
    "  MAPI_API_URL          the API address, for example https://api.example.com\n"
    "  MAPI_DASHBOARD_TOKEN  a dashboard read token\n"
    "  or MAPI_SHARE_LINK and MAPI_SHARE_PASSCODE  a share link and its passcode\n"
    "Or put them in ~/.config/mapi/mcp.json (chmod 600).\n"
)
ARGV_REFUSED: Final = (
    "mapi-mcp: this command takes no arguments. Do not put a credential on the command line. "
    "Set MAPI_API_URL and MAPI_DASHBOARD_TOKEN (or MAPI_SHARE_LINK and MAPI_SHARE_PASSCODE) "
    "in the environment.\n"
)


class RedactingFilter(logging.Filter):
    """Replaces tokens, share sessions, bearer values and the known secrets in each record."""

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self._secrets)
        record.args = None
        return True


def _setup_logging(credential: Credential | None) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("mapi-mcp: %(levelname)s %(message)s"))
    handler.addFilter(RedactingFilter(credential.secrets() if credential else ()))
    root = logging.getLogger("mapi_mcp")
    root.handlers[:] = [handler]
    root.propagate = False
    level = os.environ.get("MAPI_MCP_LOG", "WARNING").upper()
    root.setLevel(level if level in ("DEBUG", "INFO", "WARNING", "ERROR") else "WARNING")
    # httpx logs request URLs at INFO; keep it quiet and redacted all the same
    httpx_log = logging.getLogger("httpx")
    httpx_log.handlers[:] = [handler]
    httpx_log.propagate = False
    httpx_log.setLevel(logging.WARNING)


async def run_stdio(
    bridge: Bridge, readline: Callable[[], bytes], write: Callable[[str], None]
) -> None:
    """Read lines until end of input; write one answer line per request, in order."""
    while True:
        raw = await asyncio.to_thread(readline)
        if not raw:
            return
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            write(json_line(error_response(JsonRpcError(PARSE_ERROR, "Parse error"))) + "\n")
            continue
        reply = await bridge.forward(line)
        if reply is not None:
            write(reply + "\n")


async def _serve(bridge: Bridge) -> None:
    stdin = sys.stdin.buffer
    stdout = sys.stdout

    def write(text: str) -> None:
        stdout.write(text)
        stdout.flush()

    async with bridge:
        await run_stdio(bridge, stdin.readline, write)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["--help"], ["-h"]):
        sys.stderr.write(USAGE)
        return 0
    if args == ["--version"]:
        sys.stderr.write(f"mapi-mcp {__version__}\n")
        return 0
    if args:
        # never echo the arguments: they may be a credential
        sys.stderr.write(ARGV_REFUSED)
        return 2
    try:
        config = load_config(os.environ)
    except ConfigError as exc:
        _setup_logging(None)
        sys.stderr.write(f"mapi-mcp: {redact(str(exc))}\n")
        return 2
    _setup_logging(config.credential)
    bridge = Bridge(config.api_url, config.credential)
    try:
        asyncio.run(_serve(bridge))
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
