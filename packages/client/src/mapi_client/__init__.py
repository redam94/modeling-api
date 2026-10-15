"""Python client for the modeling-api ``/v1`` HTTP API.

* :class:`Client` / :class:`AsyncClient` - typed with the ``mapi_spec.api`` DTOs; resources
  ``projects``, ``catalog``, ``datasets``, ``specs``, ``runs``, ``models`` and ``plots``;
* errors are :class:`mapi_spec.errors.MapiError` subclasses rebuilt from RFC 7807 problems
  (see :mod:`mapi_client.errors`);
* list endpoints return lazy :class:`Paginator` / :class:`AsyncPaginator` iterators;
* ``runs.stream_events`` follows the SSE stream and resumes with ``Last-Event-ID``;
* ``datasets.upload`` and ``models.score`` handle signed URLs, waiting and CRC32C checks;
  ``xr.Dataset`` uploads and ``xr.DataTree`` results need the ``xarray`` extra.

>>> from mapi_client import Client               # doctest: +SKIP
>>> with Client("http://127.0.0.1:8000") as api:
...     version = api.datasets.upload("eu_media.zarr.zip", dataset="eu_media_daily")
"""

from mapi_client._async import AsyncClient
from mapi_client._common import ScoreResult
from mapi_client._http import Call, RetryPolicy
from mapi_client._sync import Client
from mapi_client._version import __version__
from mapi_client.auth import BearerAuth, TokenProvider
from mapi_client.config import ClientConfig, ConfigError, load_config
from mapi_client.errors import TransportError, UnexpectedResponse, http_status_of, problem_of
from mapi_client.pagination import AsyncPaginator, Paginator
from mapi_client.sse import RunEnd, RunEventFrame, RunSnapshot, RunStreamItem

__all__ = [
    "AsyncClient",
    "AsyncPaginator",
    "BearerAuth",
    "Call",
    "Client",
    "ClientConfig",
    "ConfigError",
    "Paginator",
    "RetryPolicy",
    "RunEnd",
    "RunEventFrame",
    "RunSnapshot",
    "RunStreamItem",
    "ScoreResult",
    "TokenProvider",
    "TransportError",
    "UnexpectedResponse",
    "__version__",
    "http_status_of",
    "load_config",
    "problem_of",
]
