"""v0.15: pack indexing, verified reads, manifests and collection documents."""

from __future__ import annotations

import hashlib
import io
import struct
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from mapi_sdk.collection import (
    BlobLocation,
    Collection,
    CollectionError,
    LocalPackSource,
    collection_schema,
    index_part,
    members_bin,
    read_blob,
    read_manifest,
    write_manifest,
)
from mapi_sdk.testing.images import build_parts, local_collection, shape_images
from mapi_spec.collection import MANIFEST_FIXED
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DimPolicy

pytestmark = pytest.mark.unit

PACK = "ab" * 32


def _reader(data: bytes):  # type: ignore[no-untyped-def]
    return lambda offset, n: data[offset : offset + n]


def _index(data: bytes, **kw: int) -> list:  # type: ignore[type-arg]
    return index_part(_reader(data), len(data), pack=PACK, **kw)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _zip(entries: list[tuple[str, bytes]], method: int = zipfile.ZIP_STORED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=method) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def _cd_at(data: bytes, k: int) -> int:
    """Offset of the k-th central directory header."""
    at = struct.unpack("<I", data[-6:-2])[0]
    for _ in range(k):
        n, e, c = struct.unpack("<HHH", data[at + 28 : at + 34])
        at += 46 + n + e + c
    return at


# ================================================================================ index_part


def test_index_a_deterministic_part() -> None:
    images, _ = shape_images(2)
    parts = build_parts(images)
    assert parts == build_parts(list(reversed(images)))  # byte-identical when rebuilt
    (part,) = parts
    entries = _index(part)
    assert [e.sha256 for e in entries] == sorted(_sha(i) for i in images)
    for e in entries:
        loc = e.location
        assert e.ext == "png" and loc.pack == PACK and loc.method == 0
        raw = part[loc.offset : loc.offset + loc.stored]
        assert _sha(raw) == e.sha256


def test_index_a_deflated_zip_and_read_its_blobs(tmp_path: Path) -> None:
    images, _ = shape_images(1)
    data = _zip([(f"{_sha(i)}.png", i) for i in images], zipfile.ZIP_DEFLATED)
    path = tmp_path / "p.zip"
    path.write_bytes(data)
    source = LocalPackSource({PACK: path})
    entries = _index(data)
    assert {e.location.method for e in entries} == {8}
    for e in entries:
        assert _sha(read_blob(source, e.location, sha256=e.sha256)) == e.sha256


def test_part_splitting_respects_the_byte_budget() -> None:
    images, _ = shape_images(4)
    parts = build_parts(images, part_bytes=2048)
    assert len(parts) > 1
    seen = [e.sha256 for p in parts for e in _index(p)]
    assert seen == sorted(_sha(i) for i in images)


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([("cat.png", b"x")], "not <sha256>"),
        ([("a" * 64 + ".gif", b"x")], "not <sha256>"),
        ([("dir/", b"")], "directory"),
        ([("a" * 64 + ".png", b"x"), ("a" * 64 + ".png", b"y")], "two times"),
    ],
)
def test_bad_entries_are_refused(entries: list[tuple[str, bytes]], message: str) -> None:
    with pytest.warns(UserWarning) if len(entries) == 2 else _nothing():
        data = _zip(entries)
    with pytest.raises(CollectionError, match=message) as err:
        _index(data)
    assert err.value.code is ErrorCode.SCHEMA_INVALID


class _nothing:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


def test_zip64_is_refused() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf, zf.open("a" * 64 + ".png", "w", force_zip64=True) as fh:
        fh.write(b"x")
    with pytest.raises(CollectionError, match="ZIP64"):
        _index(buf.getvalue())


def test_encrypted_and_unknown_methods_are_refused() -> None:
    data = bytearray(_zip([("a" * 64 + ".png", b"xyz")]))
    at = _cd_at(bytes(data), 0)
    enc = bytearray(data)
    enc[at + 8] |= 0x1
    with pytest.raises(CollectionError, match="encrypted"):
        _index(bytes(enc))
    bz = bytearray(data)
    bz[at + 10 : at + 12] = struct.pack("<H", 12)
    with pytest.raises(CollectionError, match="method 12"):
        _index(bytes(bz))


def test_oversized_and_overlapping_entries_are_refused() -> None:
    names = ["a" * 64 + ".png", "b" * 64 + ".png"]
    data = _zip([(names[0], b"x" * 10), (names[1], b"y" * 10)], zipfile.ZIP_DEFLATED)
    at = _cd_at(data, 0)
    big = bytearray(data)
    big[at + 24 : at + 28] = struct.pack("<I", 2**30)
    with pytest.raises(CollectionError, match="more than"):
        _index(bytes(big))
    over = bytearray(data)
    over[at + 20 : at + 24] = struct.pack("<I", 200)
    with pytest.raises(CollectionError, match="overlap"):
        _index(bytes(over))


def test_garbage_and_too_many_entries_are_refused() -> None:
    with pytest.raises(CollectionError, match="end-of-central-directory"):
        _index(b"\0" * 100)
    images, _ = shape_images(2)
    (part,) = build_parts(images)
    with pytest.raises(CollectionError, match="entries"):
        _index(part, max_entries=3)


# ================================================================================= read_blob


def test_read_blob_checks_crc_and_sha(tmp_path: Path) -> None:
    images, _ = shape_images(1)
    (part,) = build_parts(images)
    path = tmp_path / "p.zip"
    path.write_bytes(part)
    source = LocalPackSource({PACK: path})
    e = _index(part)[0]
    with pytest.raises(MapiError, match="does not match its content") as err:
        read_blob(source, e.location, sha256="0" * 64)
    assert err.value.code is ErrorCode.INTEGRITY_FAILURE
    loc = e.location
    bad = BlobLocation(loc.pack, loc.offset, loc.stored, loc.method, loc.crc32 ^ 1, loc.nbytes)
    with pytest.raises(MapiError, match="CRC-32"):
        read_blob(source, bad)
    with pytest.raises(MapiError, match="does not have"):
        read_blob(source, BlobLocation("cd" * 32, 0, 1, 0, 0, 1))


# ================================================================================= manifests


def _df(n: int = 3) -> pd.DataFrame:
    rows = []
    for i in range(n):
        rows.append(
            {
                "image_id": f"img{i}",
                "sha256": f"{i:064x}",
                "pack": PACK,
                "offset": 100 * i,
                "stored": 50,
                "method": 0,
                "crc32": 7,
                "nbytes": 50,
                "format": "png",
                "width": 4,
                "height": 4,
                "mode": "L",
                "label": ["a", "b", ""][i % 3],
                "weight": float(i),
            }
        )
    return pd.DataFrame(rows)


def test_manifest_round_trip_is_deterministic() -> None:
    df = _df()
    raw = write_manifest(df)
    assert raw == write_manifest(df.copy())
    back = read_manifest(raw)
    assert list(back.columns) == [*(c for c, _ in MANIFEST_FIXED), "label", "weight"]
    assert back["method"].dtype == np.int8 and back["width"].dtype == np.int32
    pd.testing.assert_frame_equal(back[["image_id", "label"]], df[["image_id", "label"]])
    with pytest.raises(CollectionError, match="rows"):
        read_manifest(raw, max_rows=2)
    with pytest.raises(CollectionError, match="Parquet"):
        read_manifest(b"PAR1 nope")
    with pytest.raises(CollectionError, match="no column"):
        write_manifest(df.drop(columns="crc32"))


def test_members_bin_is_sorted_and_unique() -> None:
    shas = ["ff" * 32, "00" * 32, "ff" * 32, "0a" * 32]
    raw = members_bin(shas)
    assert len(raw) == 96
    assert [raw[i : i + 32].hex() for i in range(0, 96, 32)] == ["00" * 32, "0a" * 32, "ff" * 32]
    with pytest.raises(CollectionError):
        members_bin(["XYZ"])


# ================================================================================ collection


@pytest.fixture(scope="module")
def col(tmp_path_factory: pytest.TempPathFactory) -> Collection:
    return local_collection(tmp_path_factory.mktemp("c"), n_per_class=4, part_bytes=2048)


def test_local_collection_is_consistent(col: Collection) -> None:
    info = col.info
    assert info.n_images == 12 and len(info.packs) > 1
    assert {c.label: c.count for c in info.classes} == {"circle": 4, "square": 4, "cross": 4}
    assert info.splits == {"test": 3, "train": 9}
    for row in col.manifest.itertuples():
        loc = BlobLocation(row.pack, row.offset, row.stored, row.method, row.crc32, row.nbytes)
        read_blob(col.source, loc, sha256=row.sha256)


def test_manifest_dataset_layout(col: Collection) -> None:
    ds = col.manifest_dataset()
    assert set(ds.dims) == {"image"} and ds.sizes["image"] == 12
    assert list(ds.data_vars) == ["label"]
    assert {"split", "path", "sha256", "width", "height"} <= set(ds.coords)
    assert ds["image"].values[0] == col.manifest["image_id"].iloc[0]


def test_collection_schema(col: Collection) -> None:
    schema = collection_schema(col.info, col.manifest)
    assert schema.dims["image"].policy is DimPolicy.NEW_LEVELS
    assert schema.vars["pixels"].dims == ("image", "y", "x", "channel")
    assert "virtual" in (schema.vars["pixels"].description or "")
    assert schema.targets == ["label"]


@pytest.mark.parametrize("bad", ["dup", "sha", "pack", "rows", "method"])
def test_open_refuses_inconsistent_manifests(col: Collection, bad: str) -> None:
    df = col.manifest.copy()
    info = col.info
    if bad == "dup":
        df.loc[1, "image_id"] = df.loc[0, "image_id"]
    elif bad == "sha":
        df.loc[0, "sha256"] = "XYZ"
    elif bad == "pack":
        df.loc[0, "pack"] = "cd" * 32
    elif bad == "rows":
        df = df.iloc[:-1]
    else:
        df.loc[0, "method"] = 12
    with pytest.raises(CollectionError):
        Collection.open(info, write_manifest(df), col.source)


def test_open_parses_info_json(col: Collection) -> None:
    raw = write_manifest(col.manifest)
    again = Collection.open(col.info.model_dump_json().encode(), raw, col.source)
    assert again.info == col.info and again.schema is not None
    with pytest.raises(CollectionError, match=r"collection\.json"):
        Collection.open(b"{}", raw, col.source)
