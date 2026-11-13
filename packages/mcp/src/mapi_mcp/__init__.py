"""``mapi-mcp``: connect a chat tool to one dashboard over the Model Context Protocol.

The package is a thin stdio bridge. An MCP client (a desktop chat app, an IDE) starts
``mapi-mcp`` as a subprocess and writes JSON-RPC messages to its stdin, one per line. The
bridge sends each message to the control plane's ``POST /v1/mcp`` with the credential from
the environment, and writes the answer to stdout. The protocol itself runs on the server
(:mod:`mapi_spec.mcp`); the tools are the read-only dashboard tools.

Credentials come from the environment or ``~/.config/mapi/mcp.json`` (mode 0600), never from
the command line: a dashboard read token (``MAPI_DASHBOARD_TOKEN``) or a share link and its
passcode (``MAPI_SHARE_LINK``, ``MAPI_SHARE_PASSCODE``).
"""

from __future__ import annotations

from mapi_mcp.bridge import Bridge, BridgeError
from mapi_mcp.credentials import (
    BridgeConfig,
    ConfigError,
    Credential,
    DashboardTokenCredential,
    ShareLinkCredential,
    load_config,
    redact,
)

__version__ = "0.16.0"

__all__ = [
    "Bridge",
    "BridgeConfig",
    "BridgeError",
    "ConfigError",
    "Credential",
    "DashboardTokenCredential",
    "ShareLinkCredential",
    "__version__",
    "load_config",
    "redact",
]
