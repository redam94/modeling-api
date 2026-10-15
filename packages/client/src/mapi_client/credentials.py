"""Stored interactive sign-ins (``mapi login``).

An OIDC sign-in yields an ID token (sent to the API as ``Authorization: Bearer``) and,
usually, a refresh token. Both are kept in ``credentials.json`` next to the config file
(``$MAPI_CREDENTIALS`` overrides), written atomically with mode ``0600``, one entry per
profile::

    {"version": 1, "profiles": {"prod": {"api_url": "...", "issuer": "...", ...}}}

Each entry records the API URL it was obtained for: the client never sends a stored ID token
to any other server. API tokens (``mapi_…``) are not kept here; ``mapi login --with-token``
stores them in the profile's ``token`` key of the config file.
"""

from __future__ import annotations

import json
import os
import stat
import warnings
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from mapi_client.config import ConfigError, atomic_write_private

__all__ = ["CREDENTIALS_VERSION", "CredentialStore", "OidcSession"]

CREDENTIALS_VERSION: Final = 1
_REFRESH_SKEW: Final = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class OidcSession:
    """One stored OIDC sign-in. Tokens are hidden from ``repr``."""

    api_url: str
    issuer: str
    client_id: str
    id_token: str = field(repr=False)
    expires_at: datetime
    token_endpoint: str
    refresh_token: str | None = field(default=None, repr=False)
    revocation_endpoint: str | None = None
    subject: str | None = None
    email: str | None = None
    obtained_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        for name in ("expires_at", "obtained_at"):
            value: datetime = getattr(self, name)
            if value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")

    def expired(self, now: datetime | None = None, *, skew: timedelta = _REFRESH_SKEW) -> bool:
        """True if the ID token expires within ``skew`` (refresh it before use)."""
        return (now or datetime.now(UTC)) >= self.expires_at - skew

    @property
    def can_refresh(self) -> bool:
        return bool(self.refresh_token)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["expires_at"] = self.expires_at.isoformat()
        data["obtained_at"] = self.obtained_at.isoformat()
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> OidcSession:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        for name in ("expires_at", "obtained_at"):
            if name in known:
                known[name] = datetime.fromisoformat(str(known[name]))
        return cls(**known)


class CredentialStore:
    """The ``credentials.json`` file (absent file: no sign-ins)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __repr__(self) -> str:
        return f"CredentialStore({str(self.path)!r})"

    def _read(self) -> dict[str, Any]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {"version": CREDENTIALS_VERSION, "profiles": {}}
        except OSError as exc:
            raise ConfigError(f"cannot read {self.path}: {exc.strerror}") from None
        if os.name == "posix" and self.path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            warnings.warn(
                f"{self.path} holds sign-in tokens and is accessible to other users; chmod 600 it",
                stacklevel=4,
            )
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ConfigError(
                f"{self.path} is not valid JSON; delete it and run `mapi login` again"
            ) from None
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
            raise ConfigError(f"{self.path} has an unexpected layout; delete it and log in")
        if data.get("version") != CREDENTIALS_VERSION:
            raise ConfigError(
                f"{self.path} has version {data.get('version')!r}; this client reads "
                f"version {CREDENTIALS_VERSION} (run `mapi login` again)"
            )
        return data

    def _write(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        atomic_write_private(self.path, payload)

    def load(self, profile: str) -> OidcSession | None:
        """The sign-in stored for ``profile``, if any."""
        entry = self._read()["profiles"].get(profile)
        if entry is None:
            return None
        try:
            return OidcSession.from_json(entry)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"{self.path}: the sign-in of profile {profile!r} is invalid ({exc}); "
                "run `mapi logout` and `mapi login`"
            ) from None

    def save(self, profile: str, session: OidcSession) -> None:
        data = self._read()
        data["profiles"][profile] = session.to_json()
        self._write(data)

    def delete(self, profile: str) -> bool:
        """Forget the sign-in of ``profile``; ``False`` if there was none."""
        data = self._read()
        if data["profiles"].pop(profile, None) is None:
            return False
        if data["profiles"]:
            self._write(data)
        else:
            self.path.unlink(missing_ok=True)
        return True

    def profiles(self) -> list[str]:
        return sorted(self._read()["profiles"])
