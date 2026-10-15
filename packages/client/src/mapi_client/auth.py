"""Authentication hooks.

Programs authenticate with an **API token** (``mapi_…``, ``Authorization: Bearer``; from
``MAPI_TOKEN``, ``--token`` or the profile's ``token``) or, interactively, with an **OIDC
sign-in** made by ``mapi login`` (:mod:`mapi_client.oidc`), whose ID token is renewed with
its refresh token. :func:`default_auth` picks one from a :class:`~mapi_client.ClientConfig`:
an explicit token wins over a stored sign-in.

:class:`BearerAuth` takes a fixed token or a :class:`TokenProvider` (called before every
request). A provider with a ``refresh() -> bool`` method is asked to refresh once when the
API answers ``401``, and the request is resent with the new token.

Tokens are only ever sent to the API base URL: signed storage URLs (uploads, artifact
downloads) are fetched by a separate HTTP client that never carries the Authorization header,
and a stored sign-in is only used for the API URL it was obtained for.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Protocol, runtime_checkable

import httpx
from mapi_spec.tokens import looks_like_token, parse_token

from mapi_client.config import ClientConfig, ConfigError
from mapi_client.credentials import CredentialStore
from mapi_client.oidc import DeviceLogin, OidcTokenProvider

__all__ = [
    "BearerAuth",
    "OidcDeviceFlow",
    "RefreshableTokenProvider",
    "TokenProvider",
    "default_auth",
    "describe_token",
]

OidcDeviceFlow = DeviceLogin
"""The device authorization grant (kept under its v0.2 placeholder name)."""


@runtime_checkable
class TokenProvider(Protocol):
    """Returns a current access token (called before each request)."""

    def __call__(self) -> str: ...


@runtime_checkable
class RefreshableTokenProvider(TokenProvider, Protocol):
    """A provider that can renew its token after the API rejected it."""

    def refresh(self) -> bool: ...


class BearerAuth(httpx.Auth):
    """``Authorization: Bearer <token>`` from a fixed token or a provider callable."""

    def __init__(self, token: str | TokenProvider | Callable[[], str]) -> None:
        self._refresh: Callable[[], bool] | None = None
        if isinstance(token, str):
            if not token.strip():
                raise ValueError("token must not be empty")
            fixed = token.strip()
            self._provider: Callable[[], str] = lambda: fixed
        elif callable(token):
            self._provider = token
            refresh = getattr(token, "refresh", None)
            if callable(refresh):
                self._refresh = refresh
        else:  # pragma: no cover - guarded by typing
            raise TypeError("token must be a string or a callable returning one")

    def token(self) -> str:
        value = self._provider()
        if not isinstance(value, str) or not value.strip():
            raise ValueError("the token provider returned an empty token")
        if any(c in value for c in "\r\n"):
            raise ValueError("tokens must be single-line")
        return value.strip()

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self.token()}"
        response = yield request
        if response.status_code == 401 and self._refresh is not None and self._refresh():
            request.headers["Authorization"] = f"Bearer {self.token()}"
            yield request

    def __repr__(self) -> str:
        return "BearerAuth(<redacted>)"


def describe_token(token: str) -> str:
    """A printable description of a configured token that never reveals its secret:
    ``mapi_<prefix>_…`` for API tokens, ``<set>`` for anything else."""
    if looks_like_token(token):
        try:
            return parse_token(token).display
        except Exception:  # pragma: no cover - looks_like_token already parsed it
            return "<set>"
    return "<set>"


def default_auth(cfg: ClientConfig) -> httpx.Auth | None:
    """The credentials a client built from ``cfg`` uses.

    1. ``cfg.token`` (argument, ``MAPI_TOKEN`` or the profile);
    2. the sign-in stored by ``mapi login`` for ``cfg``'s profile, *only* if it was made for
       the same API URL;
    3. none.
    """
    if cfg.token is not None:
        return BearerAuth(cfg.token)
    if cfg.credentials_file is None:
        return None
    store = CredentialStore(cfg.credentials_file)
    session = store.load(cfg.profile_key)
    if session is None or session.api_url != cfg.base_url:
        return None
    if cfg.insecure_transport and not cfg.allow_insecure_token:
        raise ConfigError("refusing to send a sign-in token over plain http to a non-loopback host")
    provider = OidcTokenProvider(
        store,
        cfg.profile_key,
        session,
        client_secret=cfg.oidc_client_secret,
        verify=cfg.verify,
        timeout=cfg.timeout,
    )
    return BearerAuth(provider)
