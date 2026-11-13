"""Where the bridge gets its API URL and credential, and how secrets are kept out of logs.

Sources, in order (the first that names a credential wins; the API URL may come from either):

1. The environment: ``MAPI_API_URL`` and either ``MAPI_DASHBOARD_TOKEN``, or
   ``MAPI_SHARE_LINK`` (the link, or only its ``mpd_…`` token) with ``MAPI_SHARE_PASSCODE``
   when the link has a passcode.
2. The config file ``~/.config/mapi/mcp.json`` (``MAPI_MCP_CONFIG`` names another path):
   ``{"api_url": …, "dashboard_token": …}`` or ``{"api_url": …, "share_link": …,
   "share_passcode": …}``. The file holds a secret, so it must be a regular file that only
   its owner can read (mode 0600); otherwise the bridge refuses it.

The command line never carries a credential (``__main__`` refuses arguments). Error messages
never repeat a presented secret.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from mapi_spec.errors import MapiError
from mapi_spec.tokens import parse_token

__all__ = [
    "CONFIG_ENV",
    "DEFAULT_CONFIG_PATH",
    "BridgeConfig",
    "ConfigError",
    "Credential",
    "DashboardTokenCredential",
    "ShareLinkCredential",
    "load_config",
    "redact",
]

DEFAULT_CONFIG_PATH: Final = Path("~/.config/mapi/mcp.json")
CONFIG_ENV: Final = "MAPI_MCP_CONFIG"
MAX_CONFIG_BYTES: Final = 16_384
MAX_PASSCODE_CHARS: Final = 128

_SHARE_TOKEN_SCAN: Final = re.compile(r"(?<![0-9a-z_])mpd_[0-9a-hjkmnp-tv-z]{40}(?![0-9a-z])")
_REDACTIONS: Final = (
    re.compile(r"mapi_[0-9a-z]{8}_[0-9a-z]{39}"),  # API and read tokens
    re.compile(r"mpd_[0-9a-z]{40}"),  # share tokens
    re.compile(r"mps_[A-Za-z0-9_-]{16,400}\.[A-Za-z0-9_-]{43}"),  # share sessions
    re.compile(r"(?i)(bearer\s+)[^\s\"',;]+"),
)
_LOOPBACK_NAMES: Final = frozenset({"localhost", "localhost."})


class ConfigError(Exception):
    """The bridge cannot start: a missing, unsafe or malformed setting. The message is
    plain text and never contains a secret."""


@dataclass(frozen=True)
class DashboardTokenCredential:
    """A dashboard read token (``mapi_…`` with a ``dashboard:<id>`` scope), sent as
    ``Authorization: Bearer``."""

    token: str = field(repr=False)

    def secrets(self) -> tuple[str, ...]:
        return (self.token,)


@dataclass(frozen=True)
class ShareLinkCredential:
    """A share link token (``mpd_…``) and its passcode. The bridge exchanges them for a
    short-lived share session and refreshes it before it expires."""

    share_token: str = field(repr=False)
    passcode: str | None = field(default=None, repr=False)

    def secrets(self) -> tuple[str, ...]:
        return (self.share_token,) + ((self.passcode,) if self.passcode else ())


Credential = DashboardTokenCredential | ShareLinkCredential


@dataclass(frozen=True)
class BridgeConfig:
    api_url: str
    credential: Credential


def redact(text: str, extra: Iterable[str] = ()) -> str:
    """``text`` with every token, share token, share session and bearer value replaced by
    ``<redacted>``, and every string of ``extra`` (known secrets such as a passcode) too."""
    for secret in extra:
        if secret:
            text = text.replace(secret, "<redacted>")
    for pattern in _REDACTIONS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + "<redacted>", text)
        else:
            text = pattern.sub("<redacted>", text)
    return text


# ============================================================================ API URL


def _is_loopback(host: str) -> bool:
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_api_url(value: str) -> str:
    """The API base URL without a trailing slash. HTTPS is required, except for a loopback
    host (local development), so a credential never crosses a network in clear text."""
    value = value.strip()
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        _ = parts.port
    except ValueError:
        raise ConfigError("MAPI_API_URL is not a valid URL") from None
    if parts.scheme not in ("https", "http") or not host:
        raise ConfigError("MAPI_API_URL must be an https:// URL")
    if parts.scheme == "http" and not _is_loopback(host):
        raise ConfigError("MAPI_API_URL must use https:// (http:// is accepted for localhost only)")
    if parts.username is not None or parts.password is not None:
        raise ConfigError("MAPI_API_URL must not contain a user name or password")
    if parts.query or parts.fragment:
        raise ConfigError("MAPI_API_URL must not contain a query or a fragment")
    return value.rstrip("/")


# ============================================================================ credentials


def _token_credential(value: str, source: str) -> DashboardTokenCredential:
    value = value.strip()
    try:
        parsed = parse_token(value)
    except MapiError:
        raise ConfigError(f"{source} is not a valid dashboard read token") from None
    return DashboardTokenCredential(str(parsed))


def _share_credential(link: str, passcode: str | None, source: str) -> ShareLinkCredential:
    found = _SHARE_TOKEN_SCAN.findall(link)
    if len(found) != 1:
        raise ConfigError(f"{source} does not contain one share link token (mpd_…)")
    if passcode is not None:
        passcode = passcode.strip("\r\n")
        if not passcode:
            passcode = None
        elif len(passcode) > MAX_PASSCODE_CHARS:
            raise ConfigError(f"the share passcode is longer than {MAX_PASSCODE_CHARS} characters")
    return ShareLinkCredential(found[0], passcode)


def _credential_from(
    values: Mapping[str, str | None], keys: tuple[str, str, str], source: str
) -> Credential | None:
    token_key, link_key, passcode_key = keys
    token = values.get(token_key)
    link = values.get(link_key)
    if token and link:
        raise ConfigError(f"{source}: set a dashboard token or a share link, not both")
    if token:
        return _token_credential(token, f"{source} {token_key}")
    if link:
        return _share_credential(link, values.get(passcode_key), f"{source} {link_key}")
    return None


def _read_config_file(path: Path) -> dict[str, str | None]:
    """The config file's string settings, after the owner-only permission check."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {}
    except OSError:
        raise ConfigError(f"cannot open the config file {path}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"the config file {path} is not a regular file")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ConfigError(
                f"the config file {path} contains a credential and other users can read it; "
                "make it private (chmod 600)"
            )
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ConfigError(f"the config file {path} belongs to another user")
        raw = os.read(fd, MAX_CONFIG_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError(f"the config file {path} is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ConfigError(f"the config file {path} is not valid JSON") from None
    if not isinstance(data, dict):
        raise ConfigError(f"the config file {path} must contain a JSON object")
    known = {"api_url", "dashboard_token", "share_link", "share_passcode"}
    unknown = sorted(k for k in data if k not in known)
    if unknown:
        raise ConfigError(f"the config file {path} has unknown keys: {', '.join(unknown[:5])}")
    out: dict[str, str | None] = {}
    for key, value in data.items():
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"the config file {path}: {key} must be a string")
        out[key] = value
    return out


def load_config(env: Mapping[str, str], *, config_path: Path | None = None) -> BridgeConfig:
    """The API URL and credential from ``env`` and the config file (see the module docs)."""
    path = config_path
    if path is None:
        path = Path(env[CONFIG_ENV]) if env.get(CONFIG_ENV) else DEFAULT_CONFIG_PATH
    path = path.expanduser()

    credential = _credential_from(
        env,
        ("MAPI_DASHBOARD_TOKEN", "MAPI_SHARE_LINK", "MAPI_SHARE_PASSCODE"),
        "the environment:",
    )
    file_values: dict[str, str | None] = {}
    if credential is None or not env.get("MAPI_API_URL"):
        file_values = _read_config_file(path)
    if credential is None:
        credential = _credential_from(
            file_values, ("dashboard_token", "share_link", "share_passcode"), f"{path}:"
        )
    if credential is None:
        raise ConfigError(
            "no credential: set MAPI_DASHBOARD_TOKEN, or MAPI_SHARE_LINK and "
            "MAPI_SHARE_PASSCODE, in the environment of the MCP client"
        )
    api_url = env.get("MAPI_API_URL") or file_values.get("api_url")
    if not api_url:
        raise ConfigError("MAPI_API_URL is not set")
    return BridgeConfig(check_api_url(api_url), credential)
