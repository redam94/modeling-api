"""Auth hook and transfer helpers."""

from __future__ import annotations

import httpx
import pytest
from mapi_client import BearerAuth
from mapi_client.auth import OidcDeviceFlow
from mapi_client.transfer import (
    default_mapping,
    detect_format,
    require_xarray,
    upload_source,
    xarray_available,
)


def _auth_header(auth: BearerAuth) -> str:
    request = httpx.Request("GET", "http://x")
    flow = auth.auth_flow(request)
    return next(flow).headers["Authorization"]


def test_bearer_auth_with_a_provider_is_called_per_request() -> None:
    tokens = iter(["t1", "t2"])
    auth = BearerAuth(lambda: next(tokens))
    assert _auth_header(auth) == "Bearer t1"
    assert _auth_header(auth) == "Bearer t2"
    assert "t1" not in repr(auth)


def test_bearer_auth_rejects_bad_tokens() -> None:
    with pytest.raises(ValueError):
        BearerAuth("  ")
    with pytest.raises(ValueError, match="single-line"):
        _auth_header(BearerAuth(lambda: "a\r\nX-Evil: 1"))
    with pytest.raises(ValueError, match="empty"):
        _auth_header(BearerAuth(lambda: ""))


def test_oidc_is_m2() -> None:
    with pytest.raises(NotImplementedError, match="M2"):
        OidcDeviceFlow("https://issuer", "client")


@pytest.mark.parametrize(
    ("name", "fmt"),
    [
        ("a.CSV", "csv"),
        ("a.parquet", "parquet"),
        ("data.zarr.zip", "zarr_zip"),
        ("a.zip", "zarr_zip"),
        ("a.nc", "netcdf"),
    ],
)
def test_detect_format(name: str, fmt: str) -> None:
    assert detect_format(name)[0] == fmt


def test_default_mapping_only_for_self_describing_formats() -> None:
    assert default_mapping("zarr_zip").format == "zarr_zip"
    with pytest.raises(ValueError):
        default_mapping("parquet")


def test_upload_sources() -> None:
    src = upload_source(b"abc", filename="x.csv")
    assert (src.size, src.format, b"".join(src.chunks(2))) == (3, "csv", b"abc")
    with pytest.raises(ValueError):
        upload_source(b"abc")
    with pytest.raises(TypeError):
        upload_source(42)  # type: ignore[arg-type]


def test_xarray_extra_is_detected() -> None:
    assert xarray_available()
    require_xarray()
