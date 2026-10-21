"""Request builders for ``mapi_spec.api.V017_ENDPOINTS`` (v0.17): dashboards for any model
type - presets, the source catalog, the layout 2 upgrade, scenario rows ("try a case"),
keeping a tried case, batch scoring, the predictions download and pictures - and the same
inputs on the share-link routes (``/v1/shared/...``).

The share-link routes are public: they are sent without the client's credentials and are
authorised by a share session in ``X-Mapi-Share``.

Like the other builder modules they validate arguments before any request, encode paths and
return a :class:`~mapi_client._http.Call`. The predictions download and pictures answer
bytes (CSV text and PNG images), not JSON.
"""

from __future__ import annotations

import re
from typing import Final

import httpx
from mapi_spec.api import (
    BatchSubmit,
    BatchTemplate,
    DashboardPresets,
    DashboardUpgrade,
    Readout,
    ReadoutHeader,
    ScenarioRowsRequest,
    SourceCatalog,
)
from mapi_spec.dashboard_v2 import ASSET_ID_RE

from mapi_client._calls import _ACCEPTED_OR_REPLAY
from mapi_client._calls_v016 import V1, _dash, _share, _wait
from mapi_client._http import Call, dump, idempotency_headers, parse_model, segment

__all__ = [
    "download_dashboard_predictions",
    "download_shared_predictions",
    "get_dashboard_asset",
    "get_dashboard_batch_template",
    "get_dashboard_sources",
    "get_shared_asset",
    "get_shared_batch_template",
    "keep_dashboard_readout",
    "list_dashboard_presets",
    "parse_bytes",
    "submit_dashboard_batch",
    "submit_dashboard_predictions",
    "submit_shared_batch",
    "submit_shared_predictions",
    "upgrade_dashboard",
]

_SHARED: Final = f"{V1}/shared"
_ASSET_ID: Final = re.compile(ASSET_ID_RE)


def parse_bytes(response: httpx.Response) -> bytes:
    """The response body as it came (a CSV download or a PNG picture)."""
    return response.content


def _asset(asset_id: str) -> str:
    if not _ASSET_ID.fullmatch(asset_id):
        raise ValueError("not a dashboard picture id (dsa_...)")
    return segment(asset_id)


def _seq(seq: int) -> str:
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise ValueError("seq must be a readout number (at least 1)")
    return segment(seq)


# ===================================================================== presets and catalog


def list_dashboard_presets(
    *, model_id: str | None = None, version: int | None = None, alias: str | None = None
) -> Call[DashboardPresets]:
    """Presets that fit a model version (``model_id``, optional ``version``) or the version
    an endpoint ``alias`` serves, best first, and the compiled layout 2 spec of the first."""
    if (model_id is None) == (alias is None):
        raise ValueError("give model_id (and optionally version) or alias")
    if version is not None and (alias is not None or version < 1):
        raise ValueError("version goes with model_id and must be at least 1")
    params: dict[str, str | int] = {}
    if model_id is not None:
        params["model_id"] = model_id
    if version is not None:
        params["version"] = version
    if alias is not None:
        params["alias"] = alias
    return Call("GET", f"{V1}/dashboards:presets", parse_model(DashboardPresets), params=params)


def get_dashboard_sources(dashboard_id: str) -> Call[SourceCatalog]:
    return Call("GET", f"{_dash(dashboard_id)}/sources", parse_model(SourceCatalog))


def upgrade_dashboard(dashboard_id: str, etag: str) -> Call[DashboardUpgrade]:
    # If-Match: the draft's ETag (412 when another edit landed first); a lost response is
    # safe to resend only by hand (the resend would answer 412 with the new etag)
    if not etag:
        raise ValueError("etag is required")
    return Call(
        "POST",
        f"{_dash(dashboard_id)}:upgrade",
        parse_model(DashboardUpgrade),
        headers={"If-Match": etag},
    )


# ============================================================== inputs (members and clients)


def submit_dashboard_predictions(
    dashboard_id: str,
    body: ScenarioRowsRequest,
    *,
    wait: int | None = None,
    idempotency_key: str | None = None,
) -> Call[Readout]:
    # 202 with the private prediction readout (200 when a cached copy is ready at once, or
    # when it finished within ``wait`` seconds: Prefer: wait=N)
    headers = idempotency_headers(idempotency_key)
    return Call(
        "POST",
        f"{_dash(dashboard_id)}/predictions",
        parse_model(Readout),
        _ACCEPTED_OR_REPLAY,
        json=dump(body),
        headers=headers,
        server_wait=_wait(headers, wait),
    )


def keep_dashboard_readout(dashboard_id: str, seq: int) -> Call[ReadoutHeader]:
    # idempotent: keeping a kept case again changes nothing
    return Call(
        "POST",
        f"{_dash(dashboard_id)}/readouts/{_seq(seq)}:keep",
        parse_model(ReadoutHeader),
        retry_safe=True,
    )


def get_dashboard_batch_template(dashboard_id: str) -> Call[BatchTemplate]:
    return Call("GET", f"{_dash(dashboard_id)}/batch/template", parse_model(BatchTemplate))


def submit_dashboard_batch(
    dashboard_id: str,
    body: BatchSubmit,
    *,
    wait: int | None = None,
    idempotency_key: str | None = None,
) -> Call[Readout]:
    headers = idempotency_headers(idempotency_key)
    return Call(
        "POST",
        f"{_dash(dashboard_id)}/batch",
        parse_model(Readout),
        _ACCEPTED_OR_REPLAY,
        json=dump(body),
        headers=headers,
        server_wait=_wait(headers, wait),
    )


def download_dashboard_predictions(dashboard_id: str, seq: int) -> Call[bytes]:
    # CSV of a batch readout (its own caller or an editor); 410 once it expired
    return Call("GET", f"{_dash(dashboard_id)}/readouts/{_seq(seq)}/predictions.csv", parse_bytes)


def get_dashboard_asset(dashboard_id: str, asset_id: str) -> Call[bytes]:
    return Call("GET", f"{_dash(dashboard_id)}/assets/{_asset(asset_id)}", parse_bytes)


# ============================================================== share links (public routes)


def submit_shared_predictions(
    session: str,
    body: ScenarioRowsRequest,
    *,
    wait: int | None = None,
    idempotency_key: str | None = None,
) -> Call[Readout]:
    headers = {**_share(session), **idempotency_headers(idempotency_key)}
    return Call(
        "POST",
        f"{_SHARED}/predictions",
        parse_model(Readout),
        _ACCEPTED_OR_REPLAY,
        json=dump(body),
        headers=headers,
        server_wait=_wait(headers, wait),
        scoped=False,
        public=True,
    )


def get_shared_batch_template(session: str) -> Call[BatchTemplate]:
    return Call(
        "GET",
        f"{_SHARED}/batch/template",
        parse_model(BatchTemplate),
        headers=_share(session),
        scoped=False,
        public=True,
    )


def submit_shared_batch(
    session: str,
    body: BatchSubmit,
    *,
    wait: int | None = None,
    idempotency_key: str | None = None,
) -> Call[Readout]:
    headers = {**_share(session), **idempotency_headers(idempotency_key)}
    return Call(
        "POST",
        f"{_SHARED}/batch",
        parse_model(Readout),
        _ACCEPTED_OR_REPLAY,
        json=dump(body),
        headers=headers,
        server_wait=_wait(headers, wait),
        scoped=False,
        public=True,
    )


def download_shared_predictions(session: str, seq: int) -> Call[bytes]:
    return Call(
        "GET",
        f"{_SHARED}/readouts/{_seq(seq)}/predictions.csv",
        parse_bytes,
        headers=_share(session),
        scoped=False,
        public=True,
    )


def get_shared_asset(session: str, asset_id: str) -> Call[bytes]:
    return Call(
        "GET",
        f"{_SHARED}/assets/{_asset(asset_id)}",
        parse_bytes,
        headers=_share(session),
        scoped=False,
        public=True,
    )
