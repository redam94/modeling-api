"""Read-then-delete of the bootstrap object (plan §8.1).

The supervisor resolves ``MAPI_BOOTSTRAP_URL`` for its nonce, downloads the
:class:`~mapi_spec.run.RunSpec` and deletes the object *before* validating it or starting the
worker, so by the time any untrusted code runs, the nonce in audit logs points at nothing.
A failed delete is fatal: continuing would leave the run's secrets readable.

Two locations are supported, both through the GCS JSON API:

* ``http(s)://.../storage/v1/b/{bucket}/o/{nonce}`` - the local GCS shim (or any JSON-API
  endpoint), authenticated with the ``MAPI_EXEC_TOKEN`` bearer token when one is set;
* ``gs://{bucket}/{name}`` - Cloud Storage, authenticated with the execution service account
  token from the metadata server (get + delete on the bootstrap bucket only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

import httpx
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.run import RunnerEnv

__all__ = [
    "GCS_JSON_API",
    "METADATA_TOKEN_URL",
    "BootstrapError",
    "BootstrapObject",
    "fetch_and_delete",
    "object_url",
]

GCS_JSON_API: Final = "https://storage.googleapis.com/storage/v1"
METADATA_TOKEN_URL: Final = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
MAX_BOOTSTRAP_BYTES: Final = 16 * 1024 * 1024


class BootstrapError(MapiError):
    """The bootstrap object could not be read or deleted (exit ``CONFIG_ERROR``)."""

    default_code = ErrorCode.INFRA_ERROR


@dataclass(frozen=True, slots=True)
class BootstrapObject:
    data: bytes
    generation: int | None


def object_url(env: RunnerEnv) -> str:
    """The JSON-API object URL of the bootstrap object."""
    url = env.resolved_bootstrap_url()
    if url.startswith("gs://"):
        bucket, _, name = url.removeprefix("gs://").partition("/")
        if not bucket or not name:
            raise BootstrapError("MAPI_BOOTSTRAP_URL must look like gs://bucket/object")
        return f"{GCS_JSON_API}/b/{quote(bucket, safe='')}/o/{quote(name, safe='')}"
    return url


def _token(env: RunnerEnv, client: httpx.Client) -> str | None:
    if env.exec_token:
        return env.exec_token
    if not env.bootstrap_url.startswith("gs://"):
        return None
    try:
        resp = client.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    except httpx.TransportError as exc:
        raise BootstrapError("the metadata server is unreachable") from exc
    if resp.status_code != 200:
        raise BootstrapError(f"the metadata server refused a token (HTTP {resp.status_code})")
    try:
        token = resp.json()["access_token"]
    except (ValueError, KeyError, TypeError) as exc:
        raise BootstrapError("the metadata server returned no access token") from exc
    if not isinstance(token, str) or not token:
        raise BootstrapError("the metadata server returned no access token")
    return token


def _with_query(url: str, query: str) -> str:
    return url + ("&" if "?" in url else "?") + query


def fetch_and_delete(env: RunnerEnv, client: httpx.Client) -> BootstrapObject:
    """Download the bootstrap object, then delete exactly that generation."""
    url = object_url(env)
    token = _token(env, client)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = client.get(_with_query(url, "alt=media"), headers=headers)
    except httpx.TransportError as exc:
        raise BootstrapError("the bootstrap object could not be read") from exc
    if resp.status_code == 404:
        raise BootstrapError("the bootstrap object does not exist (already consumed?)")
    if resp.status_code != 200:
        raise BootstrapError(f"reading the bootstrap object failed (HTTP {resp.status_code})")
    data = resp.content
    if len(data) > MAX_BOOTSTRAP_BYTES:
        raise BootstrapError("the bootstrap object is too large")
    generation: int | None = None
    raw_gen = resp.headers.get("x-goog-generation")
    if raw_gen is not None:
        try:
            generation = int(raw_gen)
        except ValueError:
            generation = None
    target = url if generation is None else _with_query(url, f"generation={generation}")
    try:
        deleted = client.delete(target, headers=headers)
    except httpx.TransportError as exc:
        raise BootstrapError("the bootstrap object could not be deleted") from exc
    if deleted.status_code not in (200, 204):
        raise BootstrapError(
            f"deleting the bootstrap object failed (HTTP {deleted.status_code}); refusing to run"
        )
    return BootstrapObject(data=data, generation=generation)
