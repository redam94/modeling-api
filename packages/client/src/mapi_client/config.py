"""Client configuration: base URL, token and transport settings.

Resolution order (first wins), shared by :class:`~mapi_client.Client` and the ``mapi`` CLI:

1. explicit arguments (``Client(base_url=..., token=...)``, ``mapi --url/--token``);
2. environment: ``MAPI_URL`` (falls back to ``MAPI_API_URL``, the local-dev name),
   ``MAPI_TOKEN``, ``MAPI_PROFILE``;
3. the selected profile of the config file (``$MAPI_CONFIG``, else
   ``$XDG_CONFIG_HOME/mapi/config.toml``, else ``~/.config/mapi/config.toml``);
4. defaults: the local API at ``http://127.0.0.1:8000``, no token.

Config file format (TOML)::

    profile = "local"              # default profile (optional)

    [profiles.local]
    url = "http://127.0.0.1:8000"

    [profiles.staging]
    url = "https://api.example.com"
    token = "mapi_..."             # prefer MAPI_TOKEN over storing tokens on disk
    timeout = 60
    max_retries = 3

Top-level ``url``/``token``/``timeout``/``max_retries`` keys act as an implicit profile used
when no profile is selected.
"""

from __future__ import annotations

import os
import stat
import tomllib
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from mapi_spec.localdev import API_PORT, ENV_API_URL, LOCALHOST

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT",
    "DEFAULT_URL",
    "ENV_CONFIG",
    "ENV_PROFILE",
    "ENV_TOKEN",
    "ENV_URL",
    "ClientConfig",
    "ConfigError",
    "config_path",
    "is_loopback",
    "load_config",
    "read_config_file",
]

DEFAULT_URL: Final = f"http://{LOCALHOST}:{API_PORT}"
DEFAULT_TIMEOUT: Final = 30.0
DEFAULT_MAX_RETRIES: Final = 2

ENV_URL: Final = "MAPI_URL"
ENV_TOKEN: Final = "MAPI_TOKEN"
ENV_PROFILE: Final = "MAPI_PROFILE"
ENV_CONFIG: Final = "MAPI_CONFIG"

_PROFILE_KEYS: Final = frozenset({"url", "token", "timeout", "max_retries", "verify"})


class ConfigError(ValueError):
    """The configuration file or a configuration value is invalid."""


def is_loopback(host: str | None) -> bool:
    """True for ``localhost`` and loopback IP literals."""
    if not host:
        return False
    h = host.strip("[]").lower()
    return h in ("localhost", "::1") or h.startswith("127.")


def _check_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError(f"API URL must be an absolute http(s) URL, got {url!r}")
    if parts.query or parts.fragment:
        raise ConfigError("API URL must not carry a query string or fragment")
    if parts.username or parts.password:
        raise ConfigError("API URL must not embed credentials; use a token instead")
    return url.rstrip("/")


@dataclass(frozen=True, slots=True)
class ClientConfig:
    """Resolved connection settings. ``token`` is hidden from ``repr``."""

    base_url: str = DEFAULT_URL
    token: str | None = field(default=None, repr=False)
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    verify: bool = True
    profile: str | None = None
    allow_insecure_token: bool = False
    sources: Mapping[str, str] = field(default_factory=dict, compare=False)
    """Where each setting came from (``arg``, ``env:MAPI_URL``, ``file:<profile>``, ``default``)."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _check_url(self.base_url))
        if self.timeout <= 0:
            raise ConfigError("timeout must be positive")
        if self.max_retries < 0:
            raise ConfigError("max_retries must be >= 0")
        if self.token is not None and not self.token.strip():
            object.__setattr__(self, "token", None)
        if self.token is not None and not self.allow_insecure_token:
            parts = urlsplit(self.base_url)
            if parts.scheme == "http" and not is_loopback(parts.hostname):
                raise ConfigError(
                    "refusing to send a bearer token over plain http to a non-loopback host; "
                    "use https or set allow_insecure_token=True"
                )

    def with_overrides(self, **changes: Any) -> ClientConfig:
        """A copy with some fields replaced (validation runs again)."""
        return replace(self, **changes)


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Location of the config file (it need not exist)."""
    env = os.environ if environ is None else environ
    explicit = env.get(ENV_CONFIG, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = env.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "mapi" / "config.toml"


def read_config_file(path: Path) -> dict[str, Any]:
    """Parse the TOML config file (an absent file is an empty config)."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc.strerror}") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"invalid config file {path}: {exc}") from None
    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict) or not all(isinstance(v, dict) for v in profiles.values()):
        raise ConfigError(f"{path}: [profiles.<name>] entries must be tables")
    checks: list[tuple[str, set[str]]] = [
        ("<top-level>", set(data) - _PROFILE_KEYS - {"profile", "profiles"}),
        *((f"profiles.{n}", set(p) - _PROFILE_KEYS) for n, p in profiles.items()),
    ]
    for name, unknown in checks:
        if unknown:
            raise ConfigError(f"{path}: unknown key(s) in {name}: {', '.join(sorted(unknown))}")
    if _has_token(data) and os.name == "posix":
        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            warnings.warn(
                f"{path} contains a token and is readable by other users; chmod 600 it",
                stacklevel=3,
            )
    return data


def _has_token(data: Mapping[str, Any]) -> bool:
    profiles = data.get("profiles", {})
    return "token" in data or any("token" in p for p in profiles.values())


def _profile_values(data: Mapping[str, Any], name: str | None, path: Path) -> dict[str, Any]:
    profiles: Mapping[str, Any] = data.get("profiles", {})
    if name is None:
        return {k: v for k, v in data.items() if k in _PROFILE_KEYS}
    if name not in profiles:
        known = ", ".join(sorted(profiles)) or "none defined"
        raise ConfigError(f"profile {name!r} not found in {path} ({known})")
    return dict(profiles[name])


def load_config(
    *,
    url: str | None = None,
    token: str | None = None,
    profile: str | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    allow_insecure_token: bool = False,
) -> ClientConfig:
    """Resolve a :class:`ClientConfig` from arguments, environment, config file and defaults."""
    env = os.environ if environ is None else environ
    cfg_path = path or config_path(env)
    data = read_config_file(cfg_path)

    sources: dict[str, str] = {}
    profile_name = profile or env.get(ENV_PROFILE, "").strip() or data.get("profile") or None
    if profile_name is not None and not isinstance(profile_name, str):
        raise ConfigError(f"{cfg_path}: 'profile' must be a string")
    file_vals = _profile_values(data, profile_name, cfg_path)
    file_src = f"file:{profile_name or '<top-level>'}"

    def pick(key: str, arg: Any, env_names: tuple[str, ...], default: Any) -> Any:
        if arg is not None:
            sources[key] = "arg"
            return arg
        for name in env_names:
            value = env.get(name, "").strip()
            if value:
                sources[key] = f"env:{name}"
                return value
        if key in file_vals:
            sources[key] = file_src
            return file_vals[key]
        sources[key] = "default"
        return default

    base_url = pick("url", url, (ENV_URL, ENV_API_URL), DEFAULT_URL)
    tok = pick("token", token, (ENV_TOKEN,), None)
    to = pick("timeout", timeout, (), DEFAULT_TIMEOUT)
    retries = pick("max_retries", max_retries, (), DEFAULT_MAX_RETRIES)
    verify = file_vals.get("verify", True)
    try:
        return ClientConfig(
            base_url=str(base_url),
            token=None if tok is None else str(tok),
            timeout=float(to),
            max_retries=int(retries),
            verify=bool(verify),
            profile=profile_name,
            allow_insecure_token=allow_insecure_token,
            sources=sources,
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"invalid configuration value: {exc}") from None
