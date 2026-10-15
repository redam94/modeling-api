"""Client configuration: base URL, credentials, project selection and transport settings.

Resolution order (first wins), shared by :class:`~mapi_client.Client` and the ``mapi`` CLI:

1. explicit arguments (``Client(base_url=..., token=...)``, ``mapi --url/--token/--project``);
2. environment: ``MAPI_URL`` (falls back to ``MAPI_API_URL``, the local-dev name),
   ``MAPI_TOKEN``, ``MAPI_PROJECT``, ``MAPI_PROFILE``, ``MAPI_OIDC_ISSUER``,
   ``MAPI_OIDC_CLIENT_ID``, ``MAPI_OIDC_CLIENT_SECRET``;
3. the selected profile of the config file (``$MAPI_CONFIG``, else
   ``$XDG_CONFIG_HOME/mapi/config.toml``, else ``~/.config/mapi/config.toml``);
4. defaults: the local API at ``http://127.0.0.1:8000``, no token, no project.

Config file format (TOML)::

    profile = "local"              # default profile (optional)

    [profiles.local]
    url = "http://127.0.0.1:8000"

    [profiles.staging]
    url = "https://api.example.com"
    project = "acme/growth"        # sent as X-Mapi-Project (`mapi use acme/growth`)
    token = "mapi_..."             # `mapi login --with-token`, or MAPI_TOKEN
    timeout = 60
    max_retries = 3

    [profiles.prod]
    url = "https://api.mapi.example"
    oidc_issuer = "https://accounts.google.com"     # `mapi login` (browser sign-in)
    oidc_client_id = "1234-abc.apps.googleusercontent.com"

Top-level keys act as an implicit profile used when no profile is selected.

``mapi login``, ``mapi use`` and ``mapi config set`` rewrite the file atomically with mode
``0600`` (comments are not kept). Interactive sign-ins (OIDC ID and refresh tokens) are
stored apart, in ``credentials.json`` next to the config file, also ``0600`` (see
:mod:`mapi_client.credentials`).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tempfile
import tomllib
import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from mapi_spec.errors import MapiError
from mapi_spec.localdev import API_PORT, ENV_API_URL, LOCALHOST
from mapi_spec.tenancy import parse_project_selector

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT",
    "DEFAULT_URL",
    "ENV_CONFIG",
    "ENV_CREDENTIALS",
    "ENV_OIDC_CLIENT_ID",
    "ENV_OIDC_CLIENT_SECRET",
    "ENV_OIDC_DISCOVERY_URL",
    "ENV_OIDC_ISSUER",
    "ENV_PROFILE",
    "ENV_PROJECT",
    "ENV_TOKEN",
    "ENV_URL",
    "PROFILE_KEYS",
    "SECRET_KEYS",
    "ClientConfig",
    "ConfigError",
    "atomic_write_private",
    "check_issuer",
    "check_project",
    "config_path",
    "credentials_path",
    "is_loopback",
    "load_config",
    "read_config_file",
    "update_profile",
    "write_config_file",
]

DEFAULT_URL: Final = f"http://{LOCALHOST}:{API_PORT}"
DEFAULT_TIMEOUT: Final = 30.0
DEFAULT_MAX_RETRIES: Final = 2

ENV_URL: Final = "MAPI_URL"
ENV_TOKEN: Final = "MAPI_TOKEN"
ENV_PROFILE: Final = "MAPI_PROFILE"
ENV_CONFIG: Final = "MAPI_CONFIG"
ENV_PROJECT: Final = "MAPI_PROJECT"
ENV_OIDC_ISSUER: Final = "MAPI_OIDC_ISSUER"
ENV_OIDC_CLIENT_ID: Final = "MAPI_OIDC_CLIENT_ID"
ENV_OIDC_CLIENT_SECRET: Final = "MAPI_OIDC_CLIENT_SECRET"
ENV_OIDC_DISCOVERY_URL: Final = "MAPI_OIDC_DISCOVERY_URL"
ENV_CREDENTIALS: Final = "MAPI_CREDENTIALS"

PROFILE_KEYS: Final = frozenset(
    {
        "url",
        "token",
        "project",
        "timeout",
        "max_retries",
        "verify",
        "oidc_issuer",
        "oidc_discovery_url",
        "oidc_client_id",
        "oidc_client_secret",
    }
)
"""Keys a profile (or the top level) may set."""
SECRET_KEYS: Final = frozenset({"token", "oidc_client_secret"})
"""Keys whose values are never printed."""
_PROFILE_NAME_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEADER: Final = "# mapi client configuration (rewritten by `mapi`; comments are not kept)"


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


def check_project(value: str) -> str:
    """Validate an ``X-Mapi-Project`` selector (``<tenant>/<project>`` or ``<project>``)."""
    try:
        return str(parse_project_selector(value))
    except MapiError as exc:
        raise ConfigError(f"invalid project {value!r}: {exc.detail}") from None


def check_issuer(url: str) -> str:
    """Validate an OIDC issuer URL: https, or plain http on loopback (a local dev IdP)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment:
        raise ConfigError(f"OIDC issuer must be an absolute http(s) URL, got {url!r}")
    if parts.username or parts.password:
        raise ConfigError("OIDC issuer must not embed credentials")
    if parts.scheme == "http" and not is_loopback(parts.hostname):
        raise ConfigError("an OIDC issuer must use https (plain http only on loopback)")
    return url.rstrip("/")


@dataclass(frozen=True, slots=True)
class ClientConfig:
    """Resolved connection settings. Secrets (``token``, ``oidc_client_secret``) are hidden
    from ``repr``."""

    base_url: str = DEFAULT_URL
    token: str | None = field(default=None, repr=False)
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    verify: bool = True
    profile: str | None = None
    allow_insecure_token: bool = False
    project: str | None = None
    """``X-Mapi-Project`` sent on project-scoped requests (``<tenant>/<project>``)."""
    oidc_issuer: str | None = None
    oidc_discovery_url: str | None = None
    """Where the issuer's discovery document lives when it is not under the issuer (the
    ``dev`` profile's built-in IdP serves it at ``<api>/dev/idp``); its ``issuer`` must still
    equal :attr:`oidc_issuer`."""
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = field(default=None, repr=False)
    credentials_file: Path | None = None
    """Where ``mapi login`` keeps interactive sign-ins. ``None`` (the default for configs
    built in code) never reads stored credentials; :func:`load_config` sets it."""
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
        if self.project is not None:
            value = self.project.strip()
            object.__setattr__(self, "project", check_project(value) if value else None)
        if self.oidc_issuer is not None:
            object.__setattr__(self, "oidc_issuer", check_issuer(self.oidc_issuer))
        if self.oidc_discovery_url is not None:
            object.__setattr__(self, "oidc_discovery_url", check_issuer(self.oidc_discovery_url))
        if self.token is not None and not self.allow_insecure_token and self.insecure_transport:
            raise ConfigError(
                "refusing to send a bearer token over plain http to a non-loopback host; "
                "use https or set allow_insecure_token=True"
            )

    def with_overrides(self, **changes: Any) -> ClientConfig:
        """A copy with some fields replaced (validation runs again)."""
        return replace(self, **changes)

    @property
    def profile_key(self) -> str:
        """The name stored sign-ins are kept under (``default`` without a profile)."""
        return self.profile or "default"

    @property
    def insecure_transport(self) -> bool:
        """Plain http to a non-loopback host: no credential may be sent there."""
        parts = urlsplit(self.base_url)
        return parts.scheme == "http" and not is_loopback(parts.hostname)


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Location of the config file (it need not exist)."""
    env = os.environ if environ is None else environ
    explicit = env.get(ENV_CONFIG, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = env.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "mapi" / "config.toml"


def credentials_path(environ: Mapping[str, str] | None = None) -> Path:
    """Location of the sign-in store: ``$MAPI_CREDENTIALS``, else ``credentials.json`` next
    to the config file."""
    env = os.environ if environ is None else environ
    explicit = env.get(ENV_CREDENTIALS, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return config_path(env).parent / "credentials.json"


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
        ("<top-level>", set(data) - PROFILE_KEYS - {"profile", "profiles"}),
        *((f"profiles.{n}", set(p) - PROFILE_KEYS) for n, p in profiles.items()),
    ]
    for name, unknown in checks:
        if unknown:
            raise ConfigError(f"{path}: unknown key(s) in {name}: {', '.join(sorted(unknown))}")
    if _has_secret(data) and os.name == "posix":
        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            warnings.warn(
                f"{path} contains a token and is readable by other users; chmod 600 it",
                stacklevel=3,
            )
    return data


def _has_secret(data: Mapping[str, Any]) -> bool:
    profiles = data.get("profiles", {})
    tables = [data, *profiles.values()]
    return any(key in table for table in tables for key in SECRET_KEYS)


# ------------------------------------------------------------------------------- writing


def _toml_key(name: str) -> str:
    return name if _PROFILE_NAME_RE.fullmatch(name) else json.dumps(name)


def _toml_value(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        # JSON's escapes (\" \\ \n \uXXXX) are valid TOML basic-string escapes
        return json.dumps(value, ensure_ascii=True)
    raise ConfigError(f"cannot store {key} = {type(value).__name__} in the config file")


def _render(data: Mapping[str, Any]) -> str:
    lines = [_HEADER]
    if data.get("profile") is not None:
        lines.append(f"profile = {_toml_value('profile', data['profile'])}")
    lines += [f"{k} = {_toml_value(k, data[k])}" for k in sorted(set(data) & PROFILE_KEYS)]
    for name, table in sorted(dict(data.get("profiles", {})).items()):
        lines += ["", f"[profiles.{_toml_key(name)}]"]
        lines += [f"{k} = {_toml_value(k, v)}" for k, v in sorted(table.items())]
    return "\n".join(lines) + "\n"


def atomic_write_private(path: Path, payload: bytes) -> None:
    """Replace ``path`` with ``payload``, readable by its owner only (``0600``); a missing
    parent directory is created ``0700``."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_config_file(path: Path, data: Mapping[str, Any]) -> None:
    """Write the config atomically with mode ``0600``. The rendered text is parsed and
    checked like :func:`read_config_file` before it replaces the old file."""
    text = _render(data)
    parsed = tomllib.loads(text)
    unknown = set(parsed) - PROFILE_KEYS - {"profile", "profiles"}
    for table in dict(parsed.get("profiles", {})).values():
        unknown |= set(table) - PROFILE_KEYS
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(sorted(unknown))}")
    atomic_write_private(path, text.encode("utf-8"))


def update_profile(
    path: Path,
    profile: str | None,
    *,
    set_values: Mapping[str, Any] | None = None,
    unset: Iterable[str] = (),
    select: bool = False,
) -> dict[str, Any]:
    """Change keys of one profile (``None``: the top-level keys) and rewrite the file.

    A ``None`` value in ``set_values`` removes the key. ``select`` also makes ``profile``
    the default (``profile = "<name>"``). Returns the new content.
    """
    changes = dict(set_values or {})
    drop = set(unset) | {k for k, v in changes.items() if v is None}
    unknown = (set(changes) | drop) - PROFILE_KEYS
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(sorted(unknown))}")
    if profile is not None and not _PROFILE_NAME_RE.fullmatch(profile):
        raise ConfigError("profile names are 1-64 characters of [A-Za-z0-9_-]")
    data = read_config_file(path)
    top: dict[str, Any] = {k: v for k, v in data.items() if k in PROFILE_KEYS}
    profiles: dict[str, dict[str, Any]] = {
        name: dict(table) for name, table in dict(data.get("profiles", {})).items()
    }
    table = top if profile is None else profiles.setdefault(profile, {})
    for key in drop:
        table.pop(key, None)
    table.update({k: v for k, v in changes.items() if v is not None})
    out: dict[str, Any] = {}
    default = profile if select and profile is not None else data.get("profile")
    if default is not None:
        out["profile"] = default
    out.update(top)
    if profiles:
        out["profiles"] = profiles
    write_config_file(path, out)
    return out


# ------------------------------------------------------------------------------- loading


def _profile_values(data: Mapping[str, Any], name: str | None, path: Path) -> dict[str, Any]:
    profiles: Mapping[str, Any] = data.get("profiles", {})
    if name is None:
        return {k: v for k, v in data.items() if k in PROFILE_KEYS}
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
    project: str | None = None,
    oidc_issuer: str | None = None,
    oidc_client_id: str | None = None,
    oidc_discovery_url: str | None = None,
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

    def opt(value: Any) -> str | None:
        return None if value is None else str(value)

    base_url = pick("url", url, (ENV_URL, ENV_API_URL), DEFAULT_URL)
    tok = pick("token", token, (ENV_TOKEN,), None)
    proj = pick("project", project, (ENV_PROJECT,), None)
    to = pick("timeout", timeout, (), DEFAULT_TIMEOUT)
    retries = pick("max_retries", max_retries, (), DEFAULT_MAX_RETRIES)
    issuer = pick("oidc_issuer", oidc_issuer, (ENV_OIDC_ISSUER,), None)
    discovery = pick("oidc_discovery_url", oidc_discovery_url, (ENV_OIDC_DISCOVERY_URL,), None)
    client_id = pick("oidc_client_id", oidc_client_id, (ENV_OIDC_CLIENT_ID,), None)
    client_secret = pick("oidc_client_secret", None, (ENV_OIDC_CLIENT_SECRET,), None)
    verify = file_vals.get("verify", True)
    try:
        return ClientConfig(
            base_url=str(base_url),
            token=opt(tok),
            timeout=float(to),
            max_retries=int(retries),
            verify=bool(verify),
            profile=profile_name,
            allow_insecure_token=allow_insecure_token,
            project=opt(proj),
            oidc_issuer=opt(issuer),
            oidc_discovery_url=opt(discovery),
            oidc_client_id=opt(client_id),
            oidc_client_secret=opt(client_secret),
            credentials_file=credentials_path(env),
            sources=sources,
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"invalid configuration value: {exc}") from None
