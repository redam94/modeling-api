"""``safetensors`` ingest (v0.15): a tiny file written by ``write_safetensors`` becomes a
weights dataset (``weights(param)`` float32 + the ``mapi_weights`` tensor table); BF16 and
integer tensors are cast; the architecture comes from the file metadata or the options; and
the refusals: a header larger than its cap, overlapping offsets, too many parameters."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
import pytest
from mapi_runner.jobs import IngestJob
from mapi_runner.worker.ingest import build_upload, run_ingest
from mapi_runner.worker.tabular import IngestLimits
from mapi_runner.worker.weights_ingest import read_weights
from mapi_sdk.safetensors import tensors_from_dataset, write_safetensors
from mapi_sdk.schema import validate_dataset
from mapi_sdk.wire import read_zarr_zip
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.ingest import WEIGHTS_DIM, WEIGHTS_VAR, IngestMapping, WeightsOptions
from mapi_spec.schema import DimPolicy, Role
from tree_fixtures import IngestCtx

pytestmark = pytest.mark.unit

LIMITS = IngestLimits()
ARCH = {"kind": "resnet", "channels": 1, "widths": [4, 8], "blocks": [1, 1], "groups": 2}


def _tensors() -> dict[str, np.ndarray]:
    rng = np.random.default_rng(0)
    return {
        "backbone.stem.kernel": rng.normal(size=(3, 3, 1, 4)).astype(np.float32),
        "backbone.stem.bias": np.zeros(4, np.float32),
        "head.kernel": rng.normal(size=(8, 3)).astype(np.float64),
        "steps": np.array([7], np.int64),
    }


def _file(tmp: Path, data: bytes, name: str = "w.safetensors") -> Path:
    path = tmp / name
    path.write_bytes(data)
    return path


def _bf16(values: np.ndarray) -> bytes:
    """A one-tensor safetensors file holding ``values`` (float32, exact in bf16) as BF16."""
    raw = (values.astype(np.float32).view(np.uint32) >> 16).astype("<u2").tobytes()
    header = {"x": {"dtype": "BF16", "shape": list(values.shape), "data_offsets": [0, len(raw)]}}
    text = json.dumps(header).encode()
    text += b" " * (-len(text) % 8)
    return struct.pack("<Q", len(text)) + text + raw


def test_weights_dataset_round_trips_the_tensors(tmp_path: Path) -> None:
    tensors = _tensors()
    meta = {"mapi_arch": json.dumps(ARCH), "source": "test"}
    up = build_upload(
        _file(tmp_path, write_safetensors(tensors, meta)),
        IngestMapping(format="safetensors"),
        LIMITS,
    )
    ds, schema = up.data, up.schema
    assert ds[WEIGHTS_VAR].dims == (WEIGHTS_DIM,)
    assert ds[WEIGHTS_VAR].dtype == np.float32
    assert ds.sizes[WEIGHTS_DIM] == sum(t.size for t in tensors.values())
    assert schema.vars[WEIGHTS_VAR].role is Role.CONSTANT
    assert schema.dims[WEIGHTS_DIM].policy is DimPolicy.FIXED
    # no coord on param (positions are the labels): only that warning
    assert [i.severity.value for i in validate_dataset(schema, ds)] in ([], ["warning"])
    got, doc = tensors_from_dataset(ds)
    for name, arr in tensors.items():
        np.testing.assert_allclose(got[name], arr.astype(np.float32))
    assert doc["arch"] == ARCH
    assert doc["metadata"] == meta
    assert doc["parameters"] == ds.sizes[WEIGHTS_DIM]
    dtypes = {t["name"]: t["dtype"] for t in doc["tensors"]}  # type: ignore[index, union-attr]
    assert dtypes["head.kernel"] == "F64" and dtypes["steps"] == "I64"
    messages = " ".join(n["message"] for n in up.notes)
    assert "4 tensors" in messages and "2 tensors are stored as float32" in messages


def test_bf16_tensors_are_read_as_float32(tmp_path: Path) -> None:
    values = np.array([[1.0, -2.5], [0.15625, 3.0]], np.float32)
    up = read_weights(_file(tmp_path, _bf16(values)), WeightsOptions(arch=ARCH), LIMITS)
    got, doc = tensors_from_dataset(up.data)  # type: ignore[arg-type]
    np.testing.assert_array_equal(got["x"], values)
    assert doc["tensors"][0]["dtype"] == "BF16"  # type: ignore[index]
    assert doc["arch"] == ARCH  # no mapi_arch in the file: the option is used


def test_file_arch_wins_over_the_option(tmp_path: Path) -> None:
    data = write_safetensors({"a": np.ones(2, np.float32)}, {"mapi_arch": json.dumps(ARCH)})
    up = read_weights(_file(tmp_path, data), WeightsOptions(arch={"kind": "mlp"}), LIMITS)
    _, doc = tensors_from_dataset(up.data)  # type: ignore[arg-type]
    assert doc["arch"] == ARCH
    assert any("is not used" in n["message"] for n in up.notes)


def test_no_arch_leaves_a_note(tmp_path: Path) -> None:
    up = read_weights(
        _file(tmp_path, write_safetensors({"a": np.ones(2, np.float32)})), None, LIMITS
    )
    assert any("no architecture" in n["message"] for n in up.notes)


def test_run_ingest_writes_the_weights_version(tmp_path: Path) -> None:
    data = write_safetensors(_tensors(), {"mapi_arch": json.dumps(ARCH)})
    ctx = IngestCtx(tmp_path / "out", {"upload": _file(tmp_path, data)})
    job = IngestJob(mapping=IngestMapping(format="safetensors"))  # type: ignore[call-arg]
    schema = run_ingest(ctx, job)  # type: ignore[arg-type]
    assert WEIGHTS_VAR in schema.vars
    ds = read_zarr_zip(ctx.outputs["data.zarr.zip"])
    got, _ = tensors_from_dataset(ds)
    assert set(got) == set(_tensors())
    assert ctx.json("preview.json")["total_rows"] == ds.sizes[WEIGHTS_DIM]


def test_refuses_a_header_larger_than_the_cap(tmp_path: Path) -> None:
    data = struct.pack("<Q", 9 * 1024**2) + b"{}"
    with pytest.raises(MapiError) as err:
        read_weights(_file(tmp_path, data), None, LIMITS)
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    assert err.value.issues[0].pointer == "/mapping/weights"


def test_refuses_overlapping_offsets(tmp_path: Path) -> None:
    header = {
        "a": {"dtype": "F32", "shape": [2], "data_offsets": [0, 8]},
        "b": {"dtype": "F32", "shape": [2], "data_offsets": [4, 12]},
    }
    text = json.dumps(header).encode()
    text += b" " * (-len(text) % 8)
    data = struct.pack("<Q", len(text)) + text + bytes(12)
    with pytest.raises(MapiError) as err:
        read_weights(_file(tmp_path, data), None, LIMITS)
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_refuses_more_parameters_than_allowed(tmp_path: Path) -> None:
    data = write_safetensors({"a": np.ones(64, np.float32)})
    with pytest.raises(MapiError) as err:
        read_weights(_file(tmp_path, data), WeightsOptions(max_parameters=10), LIMITS)
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    assert "max_parameters" in (err.value.issues[0].suggested_action or "")


def test_refuses_an_empty_file_of_tensors(tmp_path: Path) -> None:
    with pytest.raises(MapiError, match="no tensors"):
        read_weights(_file(tmp_path, write_safetensors({})), None, LIMITS)
