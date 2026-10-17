"""Python client for the modeling-api ``/v1`` HTTP API.

* :class:`Client` / :class:`AsyncClient` - typed with the ``mapi_spec.api`` DTOs; resources
  ``projects``, ``catalog``, ``datasets``, ``specs``, ``runs``, ``models`` and ``plots``,
  and from v0.3 ``tenants``, ``tokens``, ``usage`` and ``admin`` (plus :meth:`Client.me`);
* credentials: an API token (``MAPI_TOKEN``, config profile) or the OIDC sign-in stored by
  ``mapi login`` (:mod:`mapi_client.oidc`), renewed automatically;
* project selection: ``Client(project="acme/growth")`` / ``MAPI_PROJECT`` / the profile's
  ``project`` is sent as ``X-Mapi-Project`` on project-scoped requests;
* ``429`` handling: ``RATE_LIMITED`` is waited out (``Retry-After``) and resent;
  ``QUOTA_EXCEEDED`` raises :class:`~mapi_spec.errors.QuotaExceeded` with
  :func:`retry_after_of` (see :class:`RetryPolicy`);
* errors are :class:`mapi_spec.errors.MapiError` subclasses rebuilt from RFC 7807 problems
  (see :mod:`mapi_client.errors`);
* list endpoints return lazy :class:`Paginator` / :class:`AsyncPaginator` iterators;
* ``runs.stream_events`` follows the SSE stream and resumes with ``Last-Event-ID``;
* ``datasets.upload`` and ``models.score`` handle signed URLs, waiting and CRC32C checks;
  ``xr.Dataset`` uploads and ``xr.DataTree`` results need the ``xarray`` extra;
* from v0.4 ``guided``, ``endpoints`` (and ``Client.endpoint(alias)``, a
  :class:`RemoteEndpoint` whose ``score(ds)`` returns an ``xr.Dataset``), ``webhooks``, saved
  plots, renders and embeds on ``plots``, sign-up (:meth:`Client.signup`) and the trust
  lifecycle on ``tenants``/``admin``; :mod:`mapi_client.webhooks` verifies deliveries and
  :mod:`mapi_client.xrjson` encodes inline payloads;
* from v0.5 ``workflows`` (documents, revisions, runs with :meth:`stream_events` - an SSE
  iterator of :class:`WorkflowSnapshot`, :class:`WorkflowEventFrame` and
  :class:`WorkflowEnd` - node outputs, schedules, triggers, the template gallery),
  ``governance`` (check catalog, project and tenant policies), ``validations`` (requests,
  the review queue, approvals, evidence packs), ``explorations`` and the Data/Model
  Explorer, comparison and budget optimisation (``models.optimize``) methods, and About
  cards with *Help me choose* on ``catalog`` (``abouts``, ``chooser``, ``recommend``).

>>> from mapi_client import Client               # doctest: +SKIP
>>> with Client("http://127.0.0.1:8000") as api:
...     version = api.datasets.upload("eu_media.zarr.zip", dataset="eu_media_daily")
"""

from mapi_client._async import AsyncClient
from mapi_client._common import ScoreResult
from mapi_client._http import Call, RetryPolicy, RetryState
from mapi_client._m3 import RemoteEndpoint
from mapi_client._m3_async import AsyncRemoteEndpoint
from mapi_client._m3_common import forecast_call
from mapi_client._sync import Client
from mapi_client._version import __version__
from mapi_client.auth import BearerAuth, TokenProvider, default_auth
from mapi_client.config import ClientConfig, ConfigError, load_config
from mapi_client.credentials import CredentialStore, OidcSession
from mapi_client.errors import (
    RateLimited,
    TransportError,
    UnexpectedResponse,
    http_status_of,
    problem_of,
    retry_after_of,
)
from mapi_client.oidc import OidcError, OidcTokenProvider
from mapi_client.pagination import AsyncPaginator, Paginator
from mapi_client.sse import (
    RunEnd,
    RunEventFrame,
    RunLiveFrame,
    RunSnapshot,
    RunStreamItem,
    WorkflowEnd,
    WorkflowEventFrame,
    WorkflowSnapshot,
    WorkflowStreamItem,
)

__all__ = [
    "AsyncClient",
    "AsyncPaginator",
    "AsyncRemoteEndpoint",
    "BearerAuth",
    "Call",
    "Client",
    "ClientConfig",
    "ConfigError",
    "CredentialStore",
    "OidcError",
    "OidcSession",
    "OidcTokenProvider",
    "Paginator",
    "RateLimited",
    "RemoteEndpoint",
    "RetryPolicy",
    "RetryState",
    "RunEnd",
    "RunEventFrame",
    "RunLiveFrame",
    "RunSnapshot",
    "RunStreamItem",
    "ScoreResult",
    "TokenProvider",
    "TransportError",
    "UnexpectedResponse",
    "WorkflowEnd",
    "WorkflowEventFrame",
    "WorkflowSnapshot",
    "WorkflowStreamItem",
    "__version__",
    "default_auth",
    "forecast_call",
    "http_status_of",
    "load_config",
    "problem_of",
    "retry_after_of",
]
