"""Request builders for ``mapi_spec.api.V015_ENDPOINTS`` (v0.15): upload sessions that can be
looked up and renewed (resumable uploads), and the ``blobs/missing`` query of image
collections.

Like the other builder modules they validate arguments before any request, encode paths and
return a :class:`~mapi_client._http.Call`.
"""

from __future__ import annotations

from typing import Final

from mapi_spec.api import API_PREFIX, BlobMissing, BlobQuery, UploadSession

from mapi_client._http import Call, dump, parse_model, segment

__all__ = ["find_missing_images", "get_upload", "renew_upload"]

V1: Final = API_PREFIX


def _upload(dataset_id: str, upload_id: str) -> str:
    return f"{V1}/datasets/{segment(dataset_id)}/uploads/{segment(upload_id)}"


def get_upload(dataset_id: str, upload_id: str) -> Call[UploadSession]:
    # operation: get_upload (``received`` says whether the file arrived)
    return Call("GET", _upload(dataset_id, upload_id), parse_model(UploadSession))


def renew_upload(dataset_id: str, upload_id: str) -> Call[UploadSession]:
    # operation: renew_upload (a fresh signed PUT; 409 when consumed or already arrived)
    return Call("POST", f"{_upload(dataset_id, upload_id)}/renew", parse_model(UploadSession))


def find_missing_images(dataset_id: str, body: BlobQuery) -> Call[BlobMissing]:
    # operation: find_missing_images
    return Call(
        "POST",
        f"{V1}/datasets/{segment(dataset_id)}/blobs/missing",
        parse_model(BlobMissing),
        json=dump(body),
    )
