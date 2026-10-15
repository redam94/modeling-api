"""The streaming multipart reader: chunk boundaries, limits and malformed bodies."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_gcs_shim._errors import ShimError
from mapi_gcs_shim.multipart import MAX_HEADER_BYTES, MultipartReader, parse_boundary

pytestmark = pytest.mark.unit

BOUNDARY = b"----mapi-boundary-0123"


def _body(fields: Sequence[tuple[str, bytes]], file: bytes, *, ctype: str = "text/plain") -> bytes:
    out = b"preamble ignored\r\n"
    for name, value in fields:
        out += b"--" + BOUNDARY + b"\r\n"
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += value + b"\r\n"
    out += b"--" + BOUNDARY + b"\r\n"
    out += b'Content-Disposition: form-data; name="file"; filename="x.bin"\r\n'
    out += f"Content-Type: {ctype}\r\n\r\n".encode()
    out += file + b"\r\n--" + BOUNDARY + b"--\r\nepilogue"
    return out


async def _stream(data: bytes, cuts: Sequence[int]) -> AsyncIterator[bytes]:
    prev = 0
    for cut in sorted(set(c for c in cuts if 0 < c < len(data))):
        yield data[prev:cut]
        prev = cut
    yield data[prev:]
    yield b""


async def _parse(data: bytes, cuts: Sequence[int] = ()) -> tuple[dict[str, bytes], bytes, str]:
    reader = MultipartReader(_stream(data, cuts), BOUNDARY)
    fields: dict[str, bytes] = {}
    file = b""
    ctype = ""
    while (part := await reader.next_part()) is not None:
        if part.name == "file":
            assert part.filename == "x.bin"
            ctype = part.content_type or ""
            async for chunk in reader.iter_data():
                file += chunk
        else:
            assert part.name is not None
            fields[part.name] = await reader.read_all(1024)
    return fields, file, ctype


@settings(max_examples=150, deadline=None)
@given(
    payload=st.binary(max_size=600),
    value=st.binary(max_size=40).filter(lambda b: b"\r\n--" not in b),
    cuts=st.lists(st.integers(min_value=0, max_value=1200), max_size=12),
)
def test_any_chunking_yields_the_same_parts(payload: bytes, value: bytes, cuts: list[int]) -> None:
    # payloads may contain partial boundaries; only the full delimiter terminates a part
    payload = payload.replace(b"\r\n--" + BOUNDARY, b"")
    data = _body([("key", b"t/x"), ("policy", value)], payload)
    fields, file, ctype = asyncio.run(_parse(data, cuts))
    assert fields == {"key": b"t/x", "policy": value}
    assert file == payload
    assert ctype == "text/plain"


def test_one_byte_chunks() -> None:
    data = _body([("a", b"1")], b"--" + BOUNDARY + b" not a delimiter\r\n-")
    fields, file, _ = asyncio.run(_parse(data, range(len(data))))
    assert fields == {"a": b"1"} and file.endswith(b"delimiter\r\n-")


def test_skipping_parts_drains_them() -> None:
    data = _body([("a", b"x" * 5000), ("b", b"2")], b"file")

    async def run() -> list[str | None]:
        reader = MultipartReader(_stream(data, [100, 2000]), BOUNDARY)
        names = []
        while (part := await reader.next_part()) is not None:
            names.append(part.name)  # never read the bodies
        return names

    assert asyncio.run(run()) == ["a", "b", "file"]


def test_field_limit() -> None:
    data = _body([("a", b"x" * 2000)], b"")
    with pytest.raises(ShimError, match="exceeds 1024"):
        asyncio.run(_parse(data))


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (b"no boundary at all", "no boundary"),
        (b"--" + BOUNDARY + b"\r\nContent-Disposition: form-data; name=a\r\n\r\nvalue", "end of"),
        (b"--" + BOUNDARY + b"XX\r\n\r\n", "CRLF"),
        (b"--" + BOUNDARY + b"\r\nbroken header\r\n\r\nv\r\n--" + BOUNDARY + b"--", "header"),
        (b"--" + BOUNDARY + b"\r\nX: " + b"a" * (MAX_HEADER_BYTES + 10), "too large"),
        (b"--" + BOUNDARY + b"\r\nX: \xff\r\n\r\nv\r\n--" + BOUNDARY + b"--", "UTF-8"),
    ],
)
def test_malformed_bodies(body: bytes, match: str) -> None:
    with pytest.raises(ShimError, match=match) as err:
        asyncio.run(_parse(body))
    assert err.value.status == 400


def test_part_without_headers_and_transport_padding() -> None:
    data = b"--" + BOUNDARY + b"  \r\n\r\nbare\r\n--" + BOUNDARY + b"--"

    async def run() -> tuple[dict[str, str], bytes]:
        reader = MultipartReader(_stream(data, []), BOUNDARY)
        part = await reader.next_part()
        assert part is not None
        body = await reader.read_all(100)
        assert await reader.next_part() is None
        assert await reader.next_part() is None
        return part.headers, body

    assert asyncio.run(run()) == ({}, b"bare")


def test_parse_boundary() -> None:
    assert parse_boundary('multipart/form-data; boundary="abc 1"', subtype="form-data") == b"abc 1"
    assert parse_boundary("multipart/related; boundary=xyz", subtype="related") == b"xyz"
    for bad in (
        None,
        "text/plain",
        "multipart/form-data",
        "multipart/form-data; boundary=" + "a" * 71,
    ):
        with pytest.raises(ShimError):
            parse_boundary(bad, subtype="form-data")
    with pytest.raises(ShimError):
        parse_boundary("multipart/related; boundary=x", subtype="form-data")
