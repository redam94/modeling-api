"""V4 RSA signing for real Cloud Storage: signed URLs and POST policies via IAM ``signBlob``.

On Cloud Run the control plane has no service-account private key: it signs through the IAM
Credentials API (``projects/-/serviceAccounts/{email}:signBlob``), which signs with a
Google-managed key of that service account (RSA-SHA256, PKCS #1 v1.5). This module builds
``GOOG4-RSA-SHA256`` URLs itself — the same canonical request as
:mod:`mapi_gcs_shim.signing` (and byte-identical to ``google-cloud-storage``'s own V4
signer) — and hands only the *string to sign* to a :class:`BlobSigner`:

* :class:`IamSignBlobSigner` — ``signBlob`` over HTTPS with a refreshing access-token
  provider (the library's helper takes one static token, which expires after an hour), and
  bounded retries on 429/5xx and transport errors;
* any object with ``signer_email`` and ``sign_bytes(bytes) -> bytes`` (a service-account key's
  ``google.oauth2.service_account.Credentials`` has exactly that shape).

The signer email must be the service account whose *IAM permissions* the URL should carry:
GCS evaluates a signed request against the signer's bindings at the time of use.
"""

from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any, Final, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from mapi_gcs_shim.signing import (
    MAX_EXPIRES_SECONDS,
    P_ALGORITHM,
    P_CREDENTIAL,
    P_DATE,
    P_EXPIRES,
    P_SIGNATURE,
    P_SIGNED_HEADERS,
    _fmt,
    _host,
    _now,
    canonical_query,
    canonical_request,
    credential_scope,
    resource_path,
)

__all__ = [
    "IAM_CREDENTIALS_ENDPOINT",
    "RSA_ALGORITHM",
    "BlobSigner",
    "IamSignBlobError",
    "IamSignBlobSigner",
    "google_token_provider",
    "rsa_signed_url",
    "rsa_string_to_sign",
]

RSA_ALGORITHM: Final = "GOOG4-RSA-SHA256"
IAM_CREDENTIALS_ENDPOINT: Final = "https://iamcredentials.googleapis.com"
_RETRY_STATUSES: Final = frozenset({429, 500, 502, 503, 504})


@runtime_checkable
class BlobSigner(Protocol):
    """Signs bytes as a service account (RSA-SHA256). Called from worker threads."""

    @property
    def signer_email(self) -> str: ...

    def sign_bytes(self, message: bytes) -> bytes: ...


class IamSignBlobError(RuntimeError):
    """``signBlob`` failed (``status`` is the last HTTP status, if any)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class IamSignBlobSigner:
    """:class:`BlobSigner` over the IAM Credentials API ``signBlob`` method.

    Args:
        service_account_email: the account to sign as (the caller needs
            ``iam.serviceAccounts.signBlob`` on it, e.g. Service Account Token Creator).
        token: an OAuth access token, or a callable returning a *current* one (preferred —
            see :func:`google_token_provider`).
        client: a synchronous ``httpx.Client`` (tests pass a mock transport).
        max_attempts / backoff_s: retries on 429, 5xx and transport errors.
    """

    def __init__(
        self,
        service_account_email: str,
        token: str | Callable[[], str],
        *,
        client: httpx.Client | None = None,
        endpoint: str = IAM_CREDENTIALS_ENDPOINT,
        max_attempts: int = 4,
        backoff_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        timeout_s: float = 10.0,
    ) -> None:
        if "@" not in service_account_email:
            raise ValueError("service_account_email must be an e-mail address")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._email = service_account_email
        self._token: Callable[[], str] = token if callable(token) else (lambda: token)
        self._client = client or httpx.Client(timeout=timeout_s)
        self._url = (
            f"{endpoint.rstrip('/')}/v1/projects/-/serviceAccounts/"
            f"{quote(service_account_email, safe='@.')}:signBlob"
        )
        self._attempts = max_attempts
        self._backoff = backoff_s
        self._sleep = sleep
        self.last_key_id: str | None = None
        self.calls = 0

    @property
    def signer_email(self) -> str:
        return self._email

    def sign_bytes(self, message: bytes) -> bytes:
        body = {"payload": base64.b64encode(message).decode()}
        last: int | None = None
        for attempt in range(1, self._attempts + 1):
            self.calls += 1
            try:
                response = self._client.post(
                    self._url,
                    json=body,
                    headers={"Authorization": f"Bearer {self._token()}"},
                )
            except httpx.TransportError:
                last = None
                if attempt == self._attempts:
                    raise IamSignBlobError("signBlob: transport error") from None
            else:
                last = response.status_code
                if response.status_code == 200:
                    data: Mapping[str, Any] = response.json()
                    self.last_key_id = data.get("keyId")
                    try:
                        return base64.b64decode(str(data["signedBlob"]), validate=True)
                    except (KeyError, ValueError) as exc:
                        raise IamSignBlobError("signBlob returned no signature") from exc
                if response.status_code not in _RETRY_STATUSES or attempt == self._attempts:
                    raise IamSignBlobError(
                        f"signBlob as {self._email} failed with HTTP {response.status_code}",
                        status=response.status_code,
                    )
            self._sleep(self._backoff * 2 ** (attempt - 1))
        raise IamSignBlobError("signBlob failed", status=last)  # pragma: no cover


def google_token_provider(credentials: Any | None = None) -> Callable[[], str]:
    """A callable returning a fresh access token from ``google.auth`` credentials.

    Uses Application Default Credentials (the Cloud Run metadata server) when
    ``credentials`` is ``None``; refreshes whenever the cached token is no longer valid.
    """
    try:
        import google.auth  # type: ignore[import-not-found,unused-ignore]
        from google.auth.transport.requests import (  # type: ignore[import-not-found,unused-ignore]
            Request,
        )
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError("google_token_provider needs google-auth: 'mapi-gcs-shim[gcp]'") from exc
    if credentials is None:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    request = Request()

    def token() -> str:
        if not credentials.valid:
            credentials.refresh(request)
        return str(credentials.token)

    return token


def rsa_string_to_sign(timestamp: str, scope: str, creq: str) -> str:
    return "\n".join([RSA_ALGORITHM, timestamp, scope, hashlib.sha256(creq.encode()).hexdigest()])


def rsa_signed_url(
    signer: BlobSigner,
    *,
    method: str,
    bucket: str,
    name: str,
    expires_in: timedelta,
    endpoint: str = "https://storage.googleapis.com",
    now: datetime | None = None,
    headers: Mapping[str, str] | None = None,
    query: Mapping[str, str] | None = None,
) -> str:
    """A ``GOOG4-RSA-SHA256`` query-signed URL for ``{endpoint}/{bucket}/{name}``.

    ``headers`` become signed headers the client must send verbatim (``host`` is always
    signed); ``query`` parameters (e.g. ``generation``) are signed too.
    """
    seconds = int(expires_in.total_seconds())
    if seconds < 1 or seconds > MAX_EXPIRES_SECONDS:
        raise ValueError("expires_in must be between 1 second and 7 days")
    ts = _now(now)
    timestamp = _fmt(ts)
    scope = credential_scope(timestamp[:8])
    all_headers = {k.lower(): v for k, v in (headers or {}).items()}
    all_headers["host"] = _host(endpoint)
    signed = sorted(all_headers)
    params: dict[str, str] = dict(query or {})
    params.update(
        {
            P_ALGORITHM: RSA_ALGORITHM,
            P_CREDENTIAL: f"{signer.signer_email}/{scope}",
            P_DATE: timestamp,
            P_EXPIRES: str(seconds),
            P_SIGNED_HEADERS: ";".join(signed),
        }
    )
    path = resource_path(bucket, name)
    creq = canonical_request(method, path, params.items(), all_headers, signed)
    signature = signer.sign_bytes(rsa_string_to_sign(timestamp, scope, creq).encode()).hex()
    return (
        f"{endpoint.rstrip('/')}{path}?{canonical_query(params.items())}&{P_SIGNATURE}={signature}"
    )
