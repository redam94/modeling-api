"""Authentication hooks.

M1 runs without authentication (local, single tenant on 127.0.0.1). The client still sends
a bearer token when one is configured, so the same code works against M2, which adds scoped
API tokens and OIDC. :class:`BearerAuth` takes a fixed token or a :class:`TokenProvider`
(called before every request, so a provider can refresh short-lived OIDC access tokens).

Tokens are only ever sent to the API base URL: signed storage URLs (uploads, artifact
downloads) are fetched by a separate HTTP client that never carries the Authorization header.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Protocol, runtime_checkable

import httpx

__all__ = ["BearerAuth", "OidcDeviceFlow", "TokenProvider"]


@runtime_checkable
class TokenProvider(Protocol):
    """Returns a current access token (called before each request)."""

    def __call__(self) -> str: ...


class BearerAuth(httpx.Auth):
    """``Authorization: Bearer <token>`` from a fixed token or a provider callable."""

    def __init__(self, token: str | TokenProvider | Callable[[], str]) -> None:
        if isinstance(token, str):
            if not token.strip():
                raise ValueError("token must not be empty")
            fixed = token.strip()
            self._provider: Callable[[], str] = lambda: fixed
        elif callable(token):
            self._provider = token
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
        yield request

    def __repr__(self) -> str:
        return "BearerAuth(<redacted>)"


class OidcDeviceFlow:
    """Placeholder for interactive OIDC login (device authorization grant).

    M2 adds OIDC (Identity Platform) and scoped API tokens; M1 has no authentication.
    """

    def __init__(self, issuer: str, client_id: str) -> None:
        raise NotImplementedError(
            "OIDC login arrives with multi-tenancy in M2; M1 has no authentication. "
            "Use a static token (MAPI_TOKEN) or a TokenProvider callable instead."
        )
