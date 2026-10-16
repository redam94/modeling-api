"""The server's root secret and the keys derived from it.

One random secret (``MAPI_SERVER_SECRET``, or a ``0600`` file created by
``mapi-server migrate`` under ``data_dir/keys``) is expanded with HMAC-SHA256 into
independent, purpose-bound keys:

* the **token pepper**: API tokens are stored as ``HMAC(pepper, token)``
  (:func:`mapi_spec.tokens.hash_token`), so a database dump alone does not allow offline
  guessing; rotating the secret invalidates every token;
* the **audit fingerprint key**: client IPs and user agents are stored as keyed digests
  (:func:`mapi_spec.audit.client_fingerprint`);
* the **webhook sealing key**: webhook signing secrets are stored AES-GCM sealed
  (:mod:`mapi_server.services.webhooks.crypto`), so a database dump alone cannot forge
  deliveries; rotating the server secret makes every webhook secret unusable (rotate them).

Every process that authenticates tokens (API instances) must share the secret.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

from mapi_server.settings import Settings

__all__ = ["ServerSecrets", "load_server_secrets"]

_SECRET_BYTES = 32


def _derive(root: bytes, purpose: str) -> bytes:
    return hmac.new(root, f"mapi/{purpose}/v1".encode(), hashlib.sha256).digest()


@dataclass(frozen=True, slots=True, repr=False)
class ServerSecrets:
    """Purpose-bound keys derived from the root secret (never logged)."""

    token_pepper: bytes
    audit_key: bytes
    webhook_key: bytes

    @classmethod
    def from_root(cls, root: bytes) -> ServerSecrets:
        if len(root) < 16:
            raise ValueError("the server secret must hold at least 16 bytes")
        return cls(
            token_pepper=_derive(root, "token-pepper"),
            audit_key=_derive(root, "audit"),
            webhook_key=_derive(root, "webhook-secrets"),
        )

    @classmethod
    def generate(cls) -> ServerSecrets:
        return cls.from_root(secrets.token_bytes(_SECRET_BYTES))

    def __repr__(self) -> str:
        return "ServerSecrets(<redacted>)"


def _write_secret(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(_SECRET_BYTES)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".server-secret-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value + "\n")
        with contextlib.suppress(FileExistsError):  # never replace another process's secret
            os.link(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return path.read_text().strip().encode()


def load_server_secrets(settings: Settings, *, create: bool | None = None) -> ServerSecrets:
    """``MAPI_SERVER_SECRET``, else the secret file (created when missing and allowed)."""
    if settings.server_secret:
        return ServerSecrets.from_root(settings.server_secret.encode())
    path = settings.secret_path
    if path.is_file():
        return ServerSecrets.from_root(path.read_text().strip().encode())
    allowed = settings.signing_key_create if create is None else create
    if not allowed:
        raise RuntimeError(
            f"no server secret at {path} and MAPI_SIGNING_KEY_CREATE is off: run "
            "`mapi-server migrate` first (it creates the secret) or set MAPI_SERVER_SECRET"
        )
    return ServerSecrets.from_root(_write_secret(path))
