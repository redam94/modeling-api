"""Request builders for the M4 rows of ``mapi_spec.api.M4_ENDPOINTS`` (tree datasets: upload
inspection): one pure function per operation (operation ids in comments).

Like :mod:`mapi_client._calls` they validate arguments, encode paths and return a
:class:`~mapi_client._http.Call`; ``tests/test_endpoint_coverage.py`` checks every one of them
against the contract (method, path, statuses, body, query, headers and project scoping).
"""

from __future__ import annotations

from typing import Final

from mapi_spec.api import API_PREFIX, Run, UploadInspection

from mapi_client._http import Call, idempotency_headers, parse_model, segment

__all__ = ["get_upload_inspection", "inspect_upload"]

V1: Final = API_PREFIX

# a finished inspection (or the inspect run already started for the upload) is returned as-is
_ACCEPTED_OR_REPLAY: Final = frozenset({200, 202})


def _upload(dataset_id: str, upload_id: str) -> str:
    return f"{V1}/datasets/{segment(dataset_id)}/uploads/{segment(upload_id)}"


def inspect_upload(dataset_id: str, upload_id: str, idempotency_key: str | None) -> Call[Run]:
    # operation: inspect_upload
    return Call(
        "POST",
        f"{_upload(dataset_id, upload_id)}/inspect",
        parse_model(Run),
        _ACCEPTED_OR_REPLAY,
        headers=idempotency_headers(idempotency_key),
    )


def get_upload_inspection(dataset_id: str, upload_id: str) -> Call[UploadInspection]:
    # operation: get_upload_inspection (409 NOT_READY while the inspect run is running)
    return Call(
        "GET", f"{_upload(dataset_id, upload_id)}/inspection", parse_model(UploadInspection)
    )
