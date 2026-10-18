"""The HDF5 guard (M4): every unsafe file feature is refused with ``INGEST_UNSAFE_FILE``
before xarray or h5py's high-level API opens the upload - in ingest and in inspect - while
ordinary NetCDF-4 and h5py files pass. The files are built in temp dirs."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pytest
from h5py import h5d, h5p, h5s, h5t, h5z
from mapi_runner.jobs import IngestJob
from mapi_runner.worker.hdf5_guard import (
    ALLOWED_FILTERS,
    check_hdf5,
    disable_plugins,
    walk_hdf5,
)
from mapi_runner.worker.ingest import build_upload
from mapi_runner.worker.inspection import inspect_upload
from mapi_runner.worker.tabular import IngestLimits
from mapi_spec.errors import ErrorCode, MapiError, UnsafeUpload
from mapi_spec.ingest import IngestMapping
from mapi_spec.jobs import InspectJob
from tree_fixtures import write_netcdf_root, write_netcdf_tree, write_raw_hdf5

pytestmark = pytest.mark.unit

Writer = Callable[[h5py.File, Path], None]


def _make(tmp: Path, name: str, fill: Writer, **kw: Any) -> Path:
    path = tmp / name
    with h5py.File(path, "w", **kw) as f:
        fill(f, tmp)
    return path


def _external_link(f: h5py.File, _tmp: Path) -> None:
    f["x"] = np.arange(3.0)
    f["secret"] = h5py.ExternalLink("/etc/passwd", "/data")


def _virtual_dataset(f: h5py.File, tmp: Path) -> None:
    src = tmp / "source.h5"
    with h5py.File(src, "w") as s:
        s["x"] = np.arange(4.0)
    layout = h5py.VirtualLayout(shape=(4,), dtype="f8")
    layout[:] = h5py.VirtualSource(str(src), "x", shape=(4,))
    f.create_virtual_dataset("v", layout)


def _external_storage(f: h5py.File, tmp: Path) -> None:
    raw = tmp / "raw.bin"
    raw.write_bytes(b"\0" * 32)
    f.create_dataset("x", shape=(4,), dtype="f8", external=[(str(raw), 0, 32)])


def _filter(code: int) -> Writer:
    def fill(f: h5py.File, _tmp: Path) -> None:
        dcpl = h5p.create(h5p.DATASET_CREATE)
        dcpl.set_chunk((5,))
        dcpl.set_filter(code, h5z.FLAG_OPTIONAL, (4, 8))
        h5d.create(f.id, b"x", h5t.IEEE_F64LE, h5s.create_simple((10,)), dcpl=dcpl)

    return fill


def _huge_shape(f: h5py.File, _tmp: Path) -> None:
    # 8 TB declared by a file of a few KB: missing chunks decode to the fill value
    f.create_dataset("x", shape=(10**6, 10**6), dtype="f8", chunks=(1, 1))


def _huge_chunk(f: h5py.File, _tmp: Path) -> None:
    f.create_dataset("x", shape=(10,), maxshape=(None,), dtype="f8", chunks=(2**28,))


def _deep(f: h5py.File, _tmp: Path) -> None:
    g: h5py.Group = f
    for i in range(40):
        g = g.create_group(f"g{i}")


def _soft_cycle(f: h5py.File, _tmp: Path) -> None:
    f["a"] = h5py.SoftLink("/b")
    f["b"] = h5py.SoftLink("/a")


def _soft_dangling(f: h5py.File, _tmp: Path) -> None:
    f["a"] = h5py.SoftLink("/nowhere")


def _soft_to_group(f: h5py.File, _tmp: Path) -> None:
    f.create_group("g")["x"] = np.arange(2.0)
    f["again"] = h5py.SoftLink("/g")


def _hard_cycle(f: h5py.File, _tmp: Path) -> None:
    g = f.create_group("g")
    g["loop"] = g


def _big_attr(f: h5py.File, _tmp: Path) -> None:
    f["x"] = np.arange(3.0)
    f["x"].attrs["big"] = np.zeros(20_000)  # 160 KB (dense attribute storage)


def _long_text_attr(f: h5py.File, _tmp: Path) -> None:
    f["x"] = np.arange(3.0)
    f["x"].attrs["note"] = "a" * 100_000


def _many(f: h5py.File, _tmp: Path) -> None:
    for i in range(40):
        f[f"x{i}"] = np.arange(2.0)


def _vlen(f: h5py.File, _tmp: Path) -> None:
    f.create_dataset("x", (2,), dtype=h5py.vlen_dtype("f8"))


def _compound(f: h5py.File, _tmp: Path) -> None:
    f["x"] = np.zeros(3, dtype=[("a", "f8"), ("b", "i4")])


def _many_dims(f: h5py.File, _tmp: Path) -> None:
    f.create_dataset("x", shape=(1,) * 8, dtype="f8")


def _plain(f: h5py.File, _tmp: Path) -> None:
    f["x"] = np.arange(3.0)


MALICIOUS: dict[str, tuple[Writer, dict[str, Any], IngestLimits, str]] = {
    "external link to /etc/passwd": (_external_link, {}, IngestLimits(), "external link"),
    "virtual dataset": (_virtual_dataset, {}, IngestLimits(), "virtual dataset"),
    "external raw storage": (_external_storage, {}, IngestLimits(), "stored in another file"),
    "szip filter": (_filter(h5z.FILTER_SZIP), {}, IngestLimits(), "compression filter 4"),
    "unknown plugin filter": (_filter(32001), {}, IngestLimits(), "compression filter 32001"),
    "huge declared shape": (_huge_shape, {}, IngestLimits(), "declare"),
    "huge chunks": (_huge_chunk, {}, IngestLimits(), "chunks of"),
    "deep nesting": (_deep, {}, IngestLimits(), "deeper than 32"),
    "soft-link cycle": (_soft_cycle, {}, IngestLimits(), "cycle of soft links"),
    "dangling soft link": (_soft_dangling, {}, IngestLimits(), "does not exist"),
    "soft link to a group": (_soft_to_group, {}, IngestLimits(), "soft link to a group"),
    "hard-link cycle": (_hard_cycle, {}, IngestLimits(), "contains itself"),
    "oversized attribute": (_big_attr, {"libver": "latest"}, IngestLimits(), "attribute holds"),
    "oversized text attribute": (_long_text_attr, {}, IngestLimits(), "characters of text"),
    "too many objects": (_many, {}, IngestLimits(max_hdf5_objects=25), "more than 25"),
    "variable-length sequences": (_vlen, {}, IngestLimits(), "variable-length sequences"),
    "compound records": (_compound, {}, IngestLimits(), "compound records"),
    "too many dims": (_many_dims, {}, IngestLimits(max_ndim=4), "8 dims"),
    "large user block": (_plain, {"userblock_size": 131_072}, IngestLimits(), "user block"),
}


@pytest.mark.parametrize("case", list(MALICIOUS))
def test_unsafe_files_are_refused(tmp_path: Path, case: str) -> None:
    fill, kw, limits, message = MALICIOUS[case]
    path = _make(tmp_path, "upload.h5", fill, **kw)
    with pytest.raises(UnsafeUpload) as exc:
        check_hdf5(path, limits)
    assert exc.value.code is ErrorCode.INGEST_UNSAFE_FILE
    assert message in exc.value.detail
    assert exc.value.issues and exc.value.issues[0].suggested_action
    # object names travel only as untrusted text
    assert "/etc/passwd" not in exc.value.detail


def test_external_link_target_is_reported_as_untrusted_text(tmp_path: Path) -> None:
    path = _make(tmp_path, "upload.h5", _external_link)
    with pytest.raises(UnsafeUpload) as exc:
        check_hdf5(path, IngestLimits())
    assert exc.value.untrusted_text is not None and "/etc/passwd" in exc.value.untrusted_text


@pytest.mark.parametrize("case", ["external link to /etc/passwd", "virtual dataset", "szip filter"])
def test_ingest_refuses_before_reading_netcdf_and_hdf5(tmp_path: Path, case: str) -> None:
    fill, kw, _limits, _message = MALICIOUS[case]
    path = _make(tmp_path, "upload.nc", fill, **kw)
    for mapping in (
        IngestMapping(format="netcdf"),
        IngestMapping.model_validate(
            {"format": "hdf5", "hdf5": {"vars": [{"path": "/x", "name": "x", "dims": ["i"]}]}}
        ),
    ):
        with pytest.raises(MapiError) as exc:
            build_upload(path, mapping, IngestLimits())
        assert exc.value.code is ErrorCode.INGEST_UNSAFE_FILE


def test_inspect_refuses_unsafe_files(tmp_path: Path) -> None:
    path = _make(tmp_path, "upload.h5", _soft_cycle)
    with pytest.raises(UnsafeUpload):
        inspect_upload(path, InspectJob())


def test_root_only_netcdf_is_checked_too(tmp_path: Path) -> None:
    path = _make(tmp_path, "upload.nc", _many)
    job = IngestJob(mapping=IngestMapping(format="netcdf"), max_hdf5_objects=10)
    with pytest.raises(UnsafeUpload):
        build_upload(path, IngestMapping(format="netcdf"), IngestLimits.from_job(job))


def test_ordinary_files_pass(tmp_path: Path) -> None:
    tree = walk_hdf5(write_netcdf_tree(tmp_path / "tree.nc"), IngestLimits())
    assert tree.groups == ["/", "/instrument", "/instrument/psf"]
    root = walk_hdf5(write_netcdf_root(tmp_path / "root.nc"), IngestLimits())
    assert root.groups == ["/"] and {a.path for a in root.arrays} >= {"/sales", "/time"}
    raw = walk_hdf5(write_raw_hdf5(tmp_path / "raw.h5"), IngestLimits(), collect_attrs=True)
    assert {a.path for a in raw.arrays} >= {"/observations/image", "/psf/kernel"}


def test_soft_links_to_datasets_and_allowed_filters_pass(tmp_path: Path) -> None:
    def fill(f: h5py.File, _tmp: Path) -> None:
        f.create_dataset("z", data=np.arange(100.0), chunks=(10,), compression="gzip", shuffle=True)
        f.create_dataset("l", data=np.arange(100.0), chunks=(10,), compression="lzf")
        f.create_dataset("c", data=np.arange(100.0), chunks=(10,), fletcher32=True)
        f["alias"] = h5py.SoftLink("/z")
        f["z"].attrs["units"] = "m"

    walk = walk_hdf5(_make(tmp_path, "ok.h5", fill), IngestLimits(), collect_attrs=True)
    filters = {a.path: a.filters for a in walk.arrays}
    assert filters["/z"] == ["shuffle", "deflate"] and filters["/l"] == ["lzf"]
    assert set(ALLOWED_FILTERS.values()) >= set(filters["/c"])
    assert next(a for a in walk.arrays if a.path == "/z").attrs == {"units": "m"}


def test_second_hard_name_is_listed_once_with_a_warning(tmp_path: Path) -> None:
    def fill(f: h5py.File, _tmp: Path) -> None:
        f["x"] = np.arange(3.0)
        f["y"] = f["x"]

    walk = walk_hdf5(_make(tmp_path, "twice.h5", fill), IngestLimits())
    assert len(walk.arrays) == 1 and "second name" in walk.warnings[0]


def test_not_hdf5_is_schema_invalid(tmp_path: Path) -> None:
    path = tmp_path / "junk.h5"
    path.write_bytes(b"\x89HDF\r\n\x1a\n" + b"\0" * 64)
    with pytest.raises(MapiError) as exc:
        check_hdf5(path, IngestLimits())
    assert exc.value.code is ErrorCode.SCHEMA_INVALID


def test_plugins_are_disabled_in_process() -> None:
    from h5py import h5pl

    disable_plugins()
    plugin_dir = os.environ["HDF5_PLUGIN_PATH"]
    assert Path(plugin_dir).is_dir() and not any(Path(plugin_dir).iterdir())
    assert os.environ["HDF5_USE_FILE_LOCKING"] == "FALSE"
    assert [h5pl.get(i) for i in range(h5pl.size())] == [plugin_dir.encode()]


def _text_attr(n: int) -> Writer:
    def fill(f: h5py.File, _tmp: Path) -> None:
        d = f.create_dataset("x", data=np.arange(4.0))
        texts = np.array([f"label {i}" for i in range(n)], dtype=object)
        d.attrs.create("names", texts if n > 1 else texts[0], dtype=h5py.string_dtype())

    return fill


def test_a_text_attribute_is_bounded_before_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mapi_runner.worker import hdf5_guard

    # a budget of exactly the file's size: elements x file size cannot fit it
    tight = IngestLimits(max_decompression_ratio=1.0, ratio_floor_bytes=0)
    path = _make(tmp_path, "many.h5", _text_attr(40))

    def never(_attr: Any) -> list[Any]:
        raise AssertionError("the attribute was read before its worst case was bounded")

    with monkeypatch.context() as m:
        m.setattr(hdf5_guard, "_read_attr", never)
        with pytest.raises(UnsafeUpload) as exc:
            check_hdf5(path, tight)
    assert "40 elements, which could decode to" in str(exc.value)
    assert exc.value.untrusted_text == "/x @names"
    # the same file passes the default budget; one text element is bounded by the file
    check_hdf5(path, IngestLimits())
    check_hdf5(_make(tmp_path, "one.h5", _text_attr(1)), tight)


def test_netcdf3_with_the_hdf5_signature_in_its_data_is_read_as_netcdf3(tmp_path: Path) -> None:
    import xarray as xr

    path = tmp_path / "classic.nc"
    xr.Dataset({"v": ("t", np.zeros(2048, dtype="u1"))}).to_netcdf(
        path, engine="scipy", format="NETCDF3_CLASSIC"
    )
    raw = bytearray(path.read_bytes())
    raw[512:520] = b"\x89HDF\r\n\x1a\n"  # inside v's values (the header is ~100 bytes)
    path.write_bytes(bytes(raw))
    assert int(xr.open_dataset(path, engine="scipy")["v"].sum()) > 0

    upload = build_upload(path, IngestMapping(format="netcdf"), IngestLimits())
    assert upload.data["v"].shape == (2048,)
    listed = inspect_upload(path, InspectJob())
    assert [a.path for a in listed.arrays] == ["/v"]
    # never handed to libhdf5: as raw HDF5 it is not an HDF5 file
    hdf5 = IngestMapping.model_validate(
        {"format": "hdf5", "hdf5": {"vars": [{"path": "/v", "name": "v", "dims": ["t"]}]}}
    )
    with pytest.raises(MapiError) as exc:
        build_upload(path, hdf5, IngestLimits())
    assert exc.value.code is ErrorCode.SCHEMA_INVALID
