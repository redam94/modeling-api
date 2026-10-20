"""v0.15: the numpy safetensors codec and the weights dataset."""

from __future__ import annotations

import json
import struct

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_sdk.bundle import assert_no_pickle
from mapi_sdk.safetensors import (
    read_safetensors,
    tensor_dtypes,
    tensors_from_dataset,
    weights_dataset,
    write_safetensors,
)
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.ingest import WEIGHTS_ATTR, WEIGHTS_DIM, WEIGHTS_VAR

pytestmark = pytest.mark.unit

ALL = {
    "f64": np.linspace(-1, 1, 6).reshape(2, 3),
    "f32": np.arange(4, dtype=np.float32),
    "f16": np.array([0.5, -2.0], dtype=np.float16),
    "i64": np.array([-(2**40), 7], dtype=np.int64),
    "i32": np.array([[1, -2]], dtype=np.int32),
    "i16": np.array([3], dtype=np.int16),
    "i8": np.array([-8, 8], dtype=np.int8),
    "u8": np.array([0, 255], dtype=np.uint8),
    "bool": np.array([True, False, True]),
    "scalar": np.array(3.5, dtype=np.float32),
    "empty": np.zeros((0, 3), dtype=np.float32),
}


def _raw(header: dict[str, object], data: bytes = b"") -> bytes:
    text = json.dumps(header).encode()
    return struct.pack("<Q", len(text)) + text + data


def test_round_trip_every_dtype() -> None:
    raw = write_safetensors(ALL, {"note": "x"})
    out, meta = read_safetensors(raw)
    assert meta == {"note": "x"}
    assert sorted(out) == sorted(ALL)
    for name, arr in ALL.items():
        assert out[name].dtype == arr.dtype and out[name].shape == arr.shape
        np.testing.assert_array_equal(out[name], arr)
    assert tensor_dtypes(raw)["f16"] == "F16" and tensor_dtypes(raw)["bool"] == "BOOL"


def test_bytes_are_deterministic_and_little_endian() -> None:
    big = {"w": np.arange(3, dtype=">f4")}
    raw = write_safetensors(big)
    assert raw == write_safetensors({"w": np.arange(3, dtype="<f4")})
    two = {"b": np.zeros(2, np.int8), "a": np.ones(1, np.float64)}
    assert write_safetensors(two) == write_safetensors(dict(reversed(list(two.items()))))
    n = struct.unpack("<Q", raw[:8])[0]
    assert n % 8 == 0 and raw[8 + n - 1 : 8 + n] in (b" ", b"}")


def test_bf16_reads_as_float32_and_round_trips() -> None:
    ml_dtypes = pytest.importorskip("ml_dtypes")
    vals = np.array([1.0, -2.5, 3.140625], dtype=ml_dtypes.bfloat16)
    raw = write_safetensors({"b": vals})
    assert tensor_dtypes(raw) == {"b": "BF16"}
    out, _ = read_safetensors(raw)
    assert out["b"].dtype == np.float32
    np.testing.assert_array_equal(out["b"], vals.astype(np.float32))


def test_bf16_bits_are_the_high_half_of_float32() -> None:
    bits = np.array([0x3F80, 0xC020], dtype="<u2")  # 1.0, -2.5
    header = {"b": {"dtype": "BF16", "shape": [2], "data_offsets": [0, 4]}}
    out, _ = read_safetensors(_raw(header, bits.tobytes()))
    np.testing.assert_array_equal(out["b"], np.array([1.0, -2.5], dtype=np.float32))


def test_arrays_are_copies() -> None:
    raw = bytearray(write_safetensors({"a": np.arange(4, dtype=np.float32)}))
    out, _ = read_safetensors(memoryview(raw))
    raw[-4:] = b"\x00\x00\x00\x00"
    assert out["a"][-1] == 3.0
    assert out["a"].flags.writeable


@settings(max_examples=150, deadline=None)
@given(
    names=st.lists(
        st.text(alphabet="abcdefghij._0123456789", min_size=1, max_size=40),
        min_size=1,
        max_size=6,
        unique=True,
    ),
    meta=st.dictionaries(st.text(max_size=30), st.text(max_size=60), max_size=3),
    rows=st.integers(0, 3),
)
def test_the_header_length_never_starts_with_0x80(
    names: list[str], meta: dict[str, str], rows: int
) -> None:
    tensors = {n: np.zeros((rows, len(n) % 3 + 1), dtype=np.float32) for n in names}
    raw = write_safetensors(tensors, meta)
    assert raw[0] != 0x80
    assert_no_pickle({"weights.safetensors": raw})
    out, back = read_safetensors(raw)
    assert sorted(out) == sorted(names) and back == meta


def test_a_header_that_would_start_with_0x80_is_padded() -> None:
    hit = 0
    for k in range(1, 400):
        raw = write_safetensors({"x" * k: np.zeros(1, dtype=np.float32)})
        n = struct.unpack("<Q", raw[:8])[0]  # grows with k: every residue mod 256 is met
        assert raw[0] != 0x80 and n % 8 == 0
        hit += n & 0xFF == 0x88
    assert hit  # some names needed the 8 extra bytes
    assert_no_pickle({"w": write_safetensors({"x" * 101: np.zeros(1, dtype=np.float32)})})


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\x01\x00", "shorter than"),
        (struct.pack("<Q", 100) + b"{}", "header length"),
        (_raw({"a": {"dtype": "F32", "shape": [2], "data_offsets": [0, 4]}}, b"\0" * 4), "match"),
        (_raw({"a": {"dtype": "C64", "shape": [1], "data_offsets": [0, 8]}}, b"\0" * 8), "dtype"),
        (_raw({"a": {"dtype": "F32", "shape": [-1], "data_offsets": [0, 0]}}), "shape"),
        (
            _raw(
                {
                    "a": {"dtype": "U8", "shape": [2], "data_offsets": [0, 2]},
                    "b": {"dtype": "U8", "shape": [2], "data_offsets": [1, 3]},
                },
                b"\0" * 3,
            ),
            "overlaps",
        ),
        (_raw({"a": {"dtype": "U8", "shape": [1], "data_offsets": [0, 1]}}, b"\0\0"), "end"),
        (_raw({"a": {"dtype": "U8", "shape": [1], "data_offsets": [0, 1], "x": 1}}, b"\0"), "only"),
        (_raw({"__metadata__": {"k": 1}}), "strings"),
        (_raw([]), "object"),  # type: ignore[arg-type]
        (struct.pack("<Q", 4) + b"{,}}", "JSON"),
        (_raw({"a": {"dtype": "BOOL", "shape": [1], "data_offsets": [0, 1]}}, b"\x02"), "BOOL"),
    ],
)
def test_invalid_files_are_refused(raw: bytes, message: str) -> None:
    with pytest.raises(MapiError, match=message) as err:
        read_safetensors(raw)
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_duplicate_keys_are_refused() -> None:
    entry = b'{"dtype":"U8","shape":[0],"data_offsets":[0,0]}'
    text = b'{"a":' + entry + b',"a":' + entry + b"}"
    with pytest.raises(MapiError, match="two times"):
        read_safetensors(struct.pack("<Q", len(text)) + text)


def test_caps() -> None:
    raw = write_safetensors({"a": np.zeros(10, np.float32), "b": np.zeros(10, np.float32)})
    with pytest.raises(MapiError, match="tensors"):
        read_safetensors(raw, max_tensors=1)
    with pytest.raises(MapiError, match="parameters"):
        read_safetensors(raw, max_parameters=15)
    with pytest.raises(MapiError, match="header has"):
        read_safetensors(raw, max_header=16)


def test_write_refuses_bad_names_and_dtypes() -> None:
    with pytest.raises(MapiError):
        write_safetensors({"__metadata__": np.zeros(1)})
    with pytest.raises(MapiError, match="dtype"):
        write_safetensors({"c": np.zeros(1, dtype=np.complex64)})
    with pytest.raises(MapiError, match="strings"):
        write_safetensors({"a": np.zeros(1)}, {"k": 1})  # type: ignore[dict-item]


def test_cross_read_with_the_safetensors_package() -> None:
    st_numpy = pytest.importorskip("safetensors.numpy")
    tensors = {k: v for k, v in ALL.items() if k != "scalar"}
    raw = write_safetensors(tensors, {"k": "v"})
    loaded = st_numpy.load(raw)
    for k, v in tensors.items():
        np.testing.assert_array_equal(loaded[k], v)
    out, _ = read_safetensors(st_numpy.save(tensors))
    for k, v in tensors.items():
        np.testing.assert_array_equal(out[k], v)


# ============================================================================ weights dataset


def test_weights_dataset_round_trip() -> None:
    arch = {"kind": "resnet", "widths": [4, 8]}
    meta = {"mapi_arch": json.dumps(arch), "source": "test"}
    tensors = {"backbone.conv": np.arange(6, dtype=np.float32).reshape(2, 3), "head.b": ALL["i8"]}
    ds = weights_dataset(tensors, meta, sha256="ab" * 32, dtypes={"backbone.conv": "BF16"})
    assert ds[WEIGHTS_VAR].dims == (WEIGHTS_DIM,) and ds[WEIGHTS_VAR].dtype == np.float32
    assert ds.sizes[WEIGHTS_DIM] == 8
    doc = json.loads(ds[WEIGHTS_VAR].attrs[WEIGHTS_ATTR])
    assert doc["arch"] == arch and doc["parameters"] == 8 and doc["sha256"] == "ab" * 32
    assert [t["dtype"] for t in doc["tensors"]] == ["BF16", "I8"]
    assert [t["offset"] for t in doc["tensors"]] == [0, 6]
    back, doc2 = tensors_from_dataset(ds)
    assert doc2 == doc
    np.testing.assert_array_equal(back["backbone.conv"], tensors["backbone.conv"])
    np.testing.assert_array_equal(back["head.b"], ALL["i8"].astype(np.float32))


def test_weights_dataset_refuses_inexact_integers() -> None:
    with pytest.raises(MapiError, match="exactly"):
        weights_dataset({"i": np.array([2**30], dtype=np.int64)})


@pytest.mark.parametrize("bad", ["no var", "no attr", "bad json", "out of range"])
def test_tensors_from_dataset_refuses_other_datasets(bad: str) -> None:
    ds = weights_dataset({"a": np.zeros(3, dtype=np.float32)})
    if bad == "no var":
        ds = ds.rename({WEIGHTS_VAR: "other"})
    elif bad == "no attr":
        ds[WEIGHTS_VAR].attrs = {}
    elif bad == "bad json":
        ds[WEIGHTS_VAR].attrs[WEIGHTS_ATTR] = "{"
    else:
        doc = json.loads(ds[WEIGHTS_VAR].attrs[WEIGHTS_ATTR])
        doc["tensors"][0]["shape"] = [4]
        ds[WEIGHTS_VAR].attrs[WEIGHTS_ATTR] = json.dumps(doc)
    with pytest.raises(MapiError, match="not a weights dataset") as err:
        tensors_from_dataset(ds)
    assert err.value.code is ErrorCode.SCHEMA_INVALID
