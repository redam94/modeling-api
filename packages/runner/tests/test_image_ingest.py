"""``image_folder`` ingest (v0.14): a zip of images and a manifest table becomes
``pixels(image, y, x, channel)`` uint8. The zips are built here (gray and RGB PNGs, JPEGs,
RGBA and palette PNGs, a multi-page TIFF; CSV, TSV and Parquet manifests), with every refusal:
unsafe paths, missing columns, duplicate ids, too many images, too large files, files that are
not images, decompression bombs (from the header, before a pixel is decoded), 16-bit modes,
sizes that differ and the value cap. The end-to-end run writes the dataset, a small preview and
profile, and the Data Explorer's ``images`` (RGB) and ``gallery`` views."""

from __future__ import annotations

import dataclasses
import io
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_runner.jobs import IngestJob
from mapi_runner.worker.image_ingest import read_image_folder
from mapi_runner.worker.ingest import build_upload, preview, profile, run_ingest
from mapi_runner.worker.tabular import IngestLimits
from mapi_sdk.schema import validate_dataset
from mapi_sdk.wire import read_zarr_zip
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.ingest import ImageFolderOptions as Opts
from mapi_spec.ingest import IngestAssembly, IngestMapping
from mapi_spec.jobs import upload_input
from mapi_spec.schema import DimPolicy, Role
from mapi_spec.views import GalleryView, ImagesView
from PIL import Image
from pydantic import ValidationError
from tree_fixtures import IngestCtx

pytestmark = pytest.mark.unit

LIMITS = IngestLimits()


def _png(arr: np.ndarray, mode: str | None = None) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr, mode=mode).save(buf, format="PNG")
    return buf.getvalue()


def _encoded(im: Image.Image, fmt: str, **kw: Any) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format=fmt, **kw)
    return buf.getvalue()


def _gray(i: int, h: int = 8, w: int = 6) -> np.ndarray:
    return ((np.arange(h * w).reshape(h, w) * 3 + i * 10) % 256).astype(np.uint8)


def _zip(
    tmp: Path,
    files: dict[str, bytes],
    manifest: pd.DataFrame | bytes | None,
    name: str = "manifest.csv",
    zname: str = "images.zip",
) -> Path:
    path = tmp / zname
    with zipfile.ZipFile(path, "w") as zf:
        for n, data in files.items():
            zf.writestr(n, data)
        if isinstance(manifest, pd.DataFrame):
            if name.endswith(".parquet"):
                buf = io.BytesIO()
                manifest.to_parquet(buf)
                zf.writestr(name, buf.getvalue())
            else:
                zf.writestr(
                    name, manifest.to_csv(index=False, sep="\t" if name.endswith(".tsv") else ",")
                )
        elif manifest is not None:
            zf.writestr(name, manifest)
    return path


def _gray_zip(tmp: Path, n: int = 6, **kw: Any) -> Path:
    files = {f"img/{i}.png": _png(_gray(i)) for i in range(n)}
    df = pd.DataFrame(
        {
            "path": list(files),
            "label": [["cat", "dog", "bird"][i % 3] for i in range(n)],
            "split": ["test" if i % 3 == 0 else "train" for i in range(n)],
            "site": [i % 2 for i in range(n)],
            "weight": [0.5 + i for i in range(n)],
        }
    )
    return _zip(tmp, files, df, **kw)


def _read(
    path: Path, limits: IngestLimits = LIMITS, **opts: Any
) -> tuple[xr.Dataset, Any, list[dict[str, str]]]:
    return read_image_folder(path, Opts.model_validate(opts), limits)


def _refused(
    path: Path,
    code: ErrorCode = ErrorCode.SCHEMA_INVALID,
    limits: IngestLimits = LIMITS,
    **opts: Any,
) -> MapiError:
    with pytest.raises(MapiError) as err:
        _read(path, limits, **opts)
    assert err.value.code is code, err.value
    assert err.value.pointer.startswith("/mapping/images"), err.value.pointer
    assert err.value.issues and err.value.issues[0].suggested_action
    return err.value


# ================================================================================ datasets


def test_gray_pngs_with_a_csv_manifest(tmp_path: Path) -> None:
    ds, schema, notes = _read(_gray_zip(tmp_path), label_column="label", split_column="split")
    assert dict(ds.sizes) == {"image": 6, "y": 8, "x": 6, "channel": 1}
    px = ds["pixels"]
    assert px.dims == ("image", "y", "x", "channel") and px.dtype == np.uint8
    assert px.attrs == {"mapi_image": "1"}
    assert np.array_equal(px.values[2, :, :, 0], _gray(2))
    assert list(ds["image"].values) == ["0", "1", "2", "3", "4", "5"]
    assert list(ds["channel"].values) == ["gray"]
    assert ds["y"].dtype == np.int64 and ds["x"].dtype == np.int64
    assert list(ds["label"].values) == ["cat", "dog", "bird"] * 2
    assert ds["label"].dims == ("image",) and "label" in ds.data_vars
    assert list(ds["split"].values[:3]) == ["test", "train", "train"]
    # the other columns are coords as read
    assert ds["site"].dtype == np.int64 and ds["weight"].dtype == np.float64
    assert ds["path"].values[0] == "img/0.png"
    assert "source_height" not in ds.coords and notes == []
    # the schema
    assert schema.vars["pixels"].dtype == "uint8" and schema.vars["pixels"].role is Role.COVARIATE
    assert schema.vars["pixels"].missing == "forbid"
    assert schema.vars["label"].role is Role.TARGET and schema.vars["label"].missing == "allow"
    assert schema.dims["image"].policy is DimPolicy.NEW_LEVELS
    assert schema.dims["image"].new_level_strategy == "pooled"
    assert schema.dims["y"].kind == "integer" and schema.dims["channel"].kind == "categorical"
    assert not [i for i in validate_dataset(schema, ds) if i.severity.value == "error"]


def test_colour_images_and_the_auto_mode(tmp_path: Path) -> None:
    yy, xx = np.mgrid[0:8, 0:6]
    rgb = np.dstack([yy * 30, xx * 40, np.full((8, 6), 90)]).astype(
        np.uint8
    )  # smooth: JPEG keeps it
    rgba = np.dstack([rgb, np.full((8, 6), 128, np.uint8)])
    pal = Image.fromarray(rgb).convert("P", palette=Image.Palette.ADAPTIVE, colors=16)
    tiff = Image.fromarray(rgb)
    files = {
        "a.png": _png(rgb),
        "b.png": _png(rgba),
        "c.png": _encoded(pal, "PNG"),
        "d.jpg": _encoded(Image.fromarray(rgb), "JPEG", quality=95),
        "e.tif": _encoded(tiff, "TIFF", save_all=True, append_images=[Image.fromarray(rgb[::-1])]),
        "f.png": _png(_gray(0)),
    }
    df = pd.DataFrame({"file": list(files), "id": ["a", "b", "c", "d", "e", "f"]})
    path = _zip(tmp_path, files, df, name="meta/list.tsv")
    ds, _schema, notes = _read(path, manifest="meta/list.tsv", path_column="file", id_column="id")
    assert list(ds["channel"].values) == ["r", "g", "b"]
    assert np.array_equal(ds["pixels"].values[0], rgb)
    assert np.array_equal(ds["pixels"].values[1], rgb)  # alpha dropped
    assert np.array_equal(ds["pixels"].values[4], rgb)  # the first page of the TIFF
    assert np.abs(ds["pixels"].values[3].astype(int) - rgb).mean() < 12  # JPEG
    gray = ds["pixels"].values[5]
    assert np.array_equal(gray[..., 0], gray[..., 1]) and np.array_equal(gray[..., 0], _gray(0))
    text = " ".join(n["message"] for n in notes)
    assert "1 images have transparency" in text
    assert "1 images have a colour palette" in text
    assert "1 images have more than one page" in text
    assert list(ds["image"].values) == ["a", "b", "c", "d", "e", "f"]
    # gray mode converts the colour images (and says so)
    ds, _schema, notes = _read(path, manifest="meta/list.tsv", path_column="file", mode="gray")
    assert ds.sizes["channel"] == 1
    assert any("colour images were converted to gray" in n["message"] for n in notes)
    # rgb mode makes gray images three equal channels
    sub = tmp_path / "gray"
    sub.mkdir()
    ds, *_ = _read(_gray_zip(sub), mode="rgb")
    assert ds.sizes["channel"] == 3


def test_a_parquet_manifest_with_number_labels_and_ids(tmp_path: Path) -> None:
    files = {f"{i}.png": _png(_gray(i)) for i in range(4)}
    df = pd.DataFrame(
        {"path": list(files), "digit": [3.0, 7.0, np.nan, 3.0], "key": [10, 11, 12, 13]}
    )
    path = _zip(tmp_path, files, df, name="labels.parquet")
    ds, _schema, _notes = _read(
        path, manifest="labels.parquet", label_column="digit", id_column="key"
    )
    assert list(ds["label"].values) == ["3", "7", "", "3"]  # text, "" for an empty cell
    assert list(ds["image"].values) == ["10", "11", "12", "13"]
    # ten or more rows: the default ids are zero-padded
    files = {f"{i}.png": _png(_gray(i)) for i in range(11)}
    ds, *_ = _read(_zip(tmp_path, files, pd.DataFrame({"path": list(files)}), zname="b.zip"))
    assert ds["image"].values[0] == "00" and ds["image"].values[-1] == "10"


def test_explicit_columns_and_columns_that_cannot_be_coords(tmp_path: Path) -> None:
    files = {f"{i}.png": _png(_gray(i)) for i in range(3)}
    df = pd.DataFrame(
        {"path": list(files), "a": [1, 2, 3], "has space": ["x", "y", "z"], "y": [0, 1, 2]}
    )
    path = _zip(tmp_path, files, df)
    ds, _s, notes = _read(path)
    assert "a" in ds.coords and "has space" not in ds.coords
    assert ds["y"].values.tolist() == list(range(8))  # the dim, not the column
    assert any("2 manifest columns were not kept" in n["message"] for n in notes)
    ds, _s, notes = _read(path, columns=["a"])
    assert "a" in ds.coords and not notes
    err = _refused(path, columns=["has space"])
    assert err.pointer == "/mapping/images/columns/0"
    err = _refused(path, columns=["nope"])
    assert "path, a, has space, y." in (err.issues[0].suggested_action or "")


@pytest.mark.parametrize("how", ["stretch", "crop", "pad"])
def test_resize_modes(tmp_path: Path, how: str) -> None:
    wide = np.full((10, 20), 200, np.uint8)
    tall = np.full((20, 10), 100, np.uint8)
    files = {"w.png": _png(wide), "t.png": _png(tall), "s.png": _png(np.full((8, 8), 50, np.uint8))}
    path = _zip(tmp_path, files, pd.DataFrame({"path": list(files)}))
    ds, _s, notes = _read(path, size=(8, 8), resize=how)
    assert dict(ds.sizes) == {"image": 3, "y": 8, "x": 8, "channel": 1}
    assert ds["source_height"].values.tolist() == [10, 20, 8]
    assert ds["source_width"].values.tolist() == [20, 10, 8]
    assert any("2 images were resized" in n["message"] for n in notes)
    w = ds["pixels"].values[0, :, :, 0]
    if how == "pad":
        # fit inside (8 x 4 of the wide image), then black borders above and below
        assert (w[0] == 0).all() and (w[-1] == 0).all() and (w[3:5] == 200).all()
    else:
        assert (w == 200).all()
    assert (ds["pixels"].values[2] == 50).all()


def test_sizes_that_differ_without_size_are_refused_with_the_first_five(tmp_path: Path) -> None:
    files = {f"{i}.png": _png(_gray(i, h=8 if i < 2 else 9)) for i in range(9)}
    path = _zip(tmp_path, files, pd.DataFrame({"path": list(files)}))
    err = _refused(path)
    assert "7 images do not have the size of the first image (8 x 6 pixels" in err.detail
    assert err.pointer == "/mapping/images/size"
    untrusted = err.issues[0].untrusted_text or ""
    assert untrusted.startswith("0.png: 8 x 6; 2.png: 9 x 6") and untrusted.count(";") == 5


# =============================================================================== refusals


def test_the_manifest_must_be_there_and_readable(tmp_path: Path) -> None:
    files = {"a.png": _png(_gray(0))}
    path = _zip(tmp_path, files, pd.DataFrame({"path": ["a.png"]}), name="other.csv")
    err = _refused(path)
    assert err.pointer == "/mapping/images/manifest" and "other.csv" in (
        err.issues[0].suggested_action or ""
    )
    path = _zip(tmp_path, files, b"", zname="empty.zip")
    err = _refused(path)
    assert "the manifest cannot be read" in err.detail
    path = _zip(tmp_path, files, b"path\n", zname="norows.zip")
    assert "no rows" in _refused(path).detail


def test_a_missing_column_lists_the_columns_of_the_manifest(tmp_path: Path) -> None:
    err = _refused(_gray_zip(tmp_path), label_column="class")
    assert err.pointer == "/mapping/images/label_column"
    assert "path, label, split, site, weight" in (err.issues[0].suggested_action or "")
    assert err.issues[0].untrusted_text == "class"
    assert (
        _refused(_gray_zip(tmp_path), path_column="file").pointer == "/mapping/images/path_column"
    )


def test_duplicate_or_empty_ids_are_refused(tmp_path: Path) -> None:
    files = {f"{i}.png": _png(_gray(i)) for i in range(3)}
    path = _zip(tmp_path, files, pd.DataFrame({"path": list(files), "id": ["a", "b", "a"]}))
    err = _refused(path, id_column="id")
    assert "the same id two times (row 4)" in err.detail and err.issues[0].untrusted_text == "a"
    path = _zip(
        tmp_path, files, pd.DataFrame({"path": list(files), "id": ["a", None, "c"]}), zname="e.zip"
    )
    assert "1 empty cells" in _refused(path, id_column="id").detail


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ("../a.png", "not a plain relative path"),
        ("/abs/a.png", "not a plain relative path"),
        ("img\\a.png", "not a plain relative path"),
        ("./a.png", "not a plain relative path"),
        ("img//a.png", "not a plain relative path"),
        ("C:/a.png", "not a plain relative path"),
        ("missing.png", "1 paths of the manifest are not in the zip"),
        ("a.gif", "not a PNG, JPEG or TIFF image"),
        ("", "the path column is empty in row 3"),
    ],
)
def test_unsafe_unknown_and_unsupported_paths(tmp_path: Path, bad: str, message: str) -> None:
    files = {"a.png": _png(_gray(0)), "a.gif": b"GIF89a"}
    path = _zip(tmp_path, files, pd.DataFrame({"path": ["a.png", bad]}))
    err = _refused(path)
    assert message in err.detail and err.pointer == "/mapping/images/path_column"


def test_unsafe_names_in_the_zip_itself_are_refused(tmp_path: Path) -> None:
    path = _zip(tmp_path, {"../evil.png": _png(_gray(0))}, pd.DataFrame({"path": ["a.png"]}))
    err = _refused(path)
    assert "unsafe name" in err.detail and err.issues[0].untrusted_text == "../evil.png"


def test_too_many_images_and_too_large_files(tmp_path: Path) -> None:
    path = _gray_zip(tmp_path, n=6)
    err = _refused(path, ErrorCode.PAYLOAD_TOO_LARGE, max_images=5)
    assert (
        "6 images. The maximum is 5" in err.detail and err.pointer == "/mapping/images/max_images"
    )
    small = dataclasses.replace(LIMITS, max_images=4)
    assert "The maximum is 4" in _refused(path, ErrorCode.PAYLOAD_TOO_LARGE, small).detail
    tiny = dataclasses.replace(LIMITS, max_image_file_bytes=50)
    err = _refused(path, ErrorCode.PAYLOAD_TOO_LARGE, tiny)
    assert "The maximum is 50" in err.detail


def test_a_file_that_is_not_an_image_quotes_pillow(tmp_path: Path) -> None:
    files = {"a.png": _png(_gray(0)), "b.png": b"\x89PNG not really"}
    path = _zip(tmp_path, files, pd.DataFrame({"path": list(files)}))
    err = _refused(path)
    assert "cannot read the image in row 3" in err.detail
    assert (err.issues[0].untrusted_text or "").startswith("b.png: UnidentifiedImageError")


@pytest.mark.parametrize("fmt", ["GIF", "BMP", "PCX", "EPS"])
def test_only_the_png_jpeg_and_tiff_decoders_run(
    tmp_path: Path, fmt: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another format under a ``.png`` name is refused unread, not decoded by its plugin."""
    if fmt == "EPS":  # Pillow needs Ghostscript to write one; the header is enough here
        other = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 6 8\n%%EndComments\n"
    else:
        other = _encoded(Image.fromarray(_gray(1)), fmt)
    assert Image.open(io.BytesIO(other)).format == fmt  # Pillow alone would decode it
    opened: list[str] = []
    real = Image.Image.load

    def load(im: Image.Image) -> Any:
        opened.append(str(im.format))
        return real(im)

    monkeypatch.setattr(Image.Image, "load", load)
    files = {"a.png": _png(_gray(0)), "cat.png": other}
    err = _refused(_zip(tmp_path, files, pd.DataFrame({"path": list(files)})))
    assert "cannot read the image in row 3" in err.detail
    assert (err.issues[0].untrusted_text or "").startswith("cat.png: UnidentifiedImageError")
    assert fmt not in opened


def _bomb_png(width: int, height: int) -> bytes:
    """A PNG whose header claims ``width`` x ``height`` gray pixels, with a tiny IDAT."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00" * 64))
        + chunk(b"IEND", b"")
    )


def test_a_decompression_bomb_is_refused_from_its_header(tmp_path: Path) -> None:
    before = Image.MAX_IMAGE_PIXELS
    files = {"a.png": _png(_gray(0)), "bomb.png": _bomb_png(20_000, 20_000)}
    path = _zip(tmp_path, files, pd.DataFrame({"path": list(files)}))
    err = _refused(path, ErrorCode.PAYLOAD_TOO_LARGE)
    assert "row 3 has more than 16,777,216 pixels" in err.detail
    # between the limit and twice it Pillow only warns: the warning is an error too
    small = dataclasses.replace(LIMITS, max_image_pixels=40)
    files = {"a.png": _png(_gray(0, h=7, w=7))}  # 49 pixels
    path = _zip(tmp_path, files, pd.DataFrame({"path": list(files)}), zname="warn.zip")
    err = _refused(path, ErrorCode.PAYLOAD_TOO_LARGE, small)
    assert "more than 40 pixels" in err.detail
    # Pillow's own limit is restored
    assert before == Image.MAX_IMAGE_PIXELS


def test_16_bit_images_are_refused(tmp_path: Path) -> None:
    deep = (np.arange(48).reshape(8, 6) * 1000).astype(np.uint16)
    files = {"a.png": _encoded(Image.fromarray(deep), "PNG")}
    path = _zip(tmp_path, files, pd.DataFrame({"path": list(files)}))
    err = _refused(path)
    assert "pixel mode 'I;16'" in err.detail
    assert "8-bit" in (err.issues[0].suggested_action or "")


def test_the_value_cap_says_how_many_images_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mapi_runner.worker import image_ingest

    monkeypatch.setattr(image_ingest, "IMAGE_FOLDER_MAX_CELLS", 300)
    path = _gray_zip(tmp_path, n=6)
    err = _refused(path, ErrorCode.PAYLOAD_TOO_LARGE)
    # the other values of the dataset (coords, ids) count too: 6 x (4 columns + 6) + 8 + 6 + 1
    cap = 300 - 75
    assert err.detail == (
        f"The images need 288 values. The maximum is {cap:,} (about {cap // 48:,} images of "
        "this size)."
    )
    assert (
        err.issues[0].suggested_action == "Make the images smaller (set size) or use fewer images."
    )


def test_options_validate_names_and_pairs() -> None:
    with pytest.raises(ValidationError):
        Opts(dim="sample")
    with pytest.raises(ValidationError):
        Opts(resize="crop")
    with pytest.raises(ValidationError):
        Opts(label_column="a", split_column="a")
    with pytest.raises(ValidationError):
        IngestMapping.model_validate({"format": "csv", "images": {}})


# ============================================================================ the ingest


def test_build_upload_reads_a_zip_and_refuses_another_file(tmp_path: Path) -> None:
    path = _gray_zip(tmp_path)
    up = build_upload(path, IngestMapping(format="image_folder"), LIMITS)
    assert isinstance(up.data, xr.Dataset) and up.columns is None and up.notes == []
    # no label_column: no label variable, the column is a coord like the others
    assert "label" not in up.data.data_vars and "label" in up.data.coords
    csv = tmp_path / "a.csv"
    csv.write_text("a,b\n1,2\n")
    with pytest.raises(MapiError) as err:
        build_upload(csv, IngestMapping(format="image_folder"), LIMITS)
    assert err.value.pointer == "/mapping/format" and "not a zip file" in err.value.detail


def test_preview_and_profile_stay_small(tmp_path: Path) -> None:
    ds, schema, _n = _read(_gray_zip(tmp_path, n=30), label_column="label", split_column="split")
    pv = preview(ds, 20)
    assert pv["total_rows"] == 30 and len(pv["rows"]) == 20
    names = [c["name"] for c in pv["columns"]]
    assert (
        names[0] == "image" and "pixels" not in names and {"label", "split", "path"} <= set(names)
    )
    prof = profile(ds, schema, [], b"x")
    px = prof["vars"]["pixels"]
    values = ds["pixels"].values.astype(float)
    assert px["count"] == values.size and px["missing"] == 0
    assert px["mean"] == pytest.approx(values.mean()) and px["std"] == pytest.approx(values.std())
    assert px["min"] == values.min() and px["max"] == values.max()
    assert px["p50"] == pytest.approx(np.quantile(values, 0.5), abs=1)
    assert px["zeros"] == int((values == 0).sum())


def test_an_image_folder_must_be_the_only_source(tmp_path: Path) -> None:
    src = {"upload_id": "upl_" + "0" * 26, "mapping": {"format": "image_folder"}}
    with pytest.raises(ValidationError):
        IngestAssembly.model_validate({"sources": [src, {**src, "upload_id": "upl_" + "1" * 26}]})
    # the runner refuses it too (a job built without the spec's check)
    one = IngestAssembly.model_validate({"sources": [src]})
    other = one.sources[0].model_copy(update={"upload_id": "upl_" + "1" * 26})
    two = one.model_copy(update={"sources": [one.sources[0], other]})
    path = _gray_zip(tmp_path)
    ctx = IngestCtx(tmp_path / "out", {upload_input(0): path, upload_input(1): path})
    with pytest.raises(MapiError) as err:
        run_ingest(ctx, IngestJob.model_construct(assembly=two))  # type: ignore[arg-type]
    assert err.value.pointer == "/assembly/sources/0/mapping/format"
    assert "only upload of a version" in err.value.detail


def test_run_ingest_writes_the_images_and_gallery_views(tmp_path: Path) -> None:
    rng = np.random.default_rng(11)
    files: dict[str, bytes] = {}
    rows = []
    for i in range(24):
        arr = rng.integers(0, 256, (16, 12, 3), dtype=np.uint8)
        arr[..., 0] = i * 10  # the red channel tells the images apart
        files[f"imgs/{i:02d}.png"] = _png(arr)
        rows.append(
            {
                "path": f"imgs/{i:02d}.png",
                "class": ["a", "b", "c"][i % 3],
                "fold": "test" if i % 4 == 0 else "train",
            }
        )
    path = _zip(tmp_path, files, pd.DataFrame(rows))
    mapping = IngestMapping.model_validate(
        {"format": "image_folder", "images": {"label_column": "class", "split_column": "fold"}}
    )
    ctx = IngestCtx(tmp_path / "out", {"upload": path})
    schema = run_ingest(ctx, IngestJob(mapping=mapping))  # type: ignore[arg-type]
    assert not ctx.warnings, ctx.warnings
    assert schema.vars["pixels"].dtype == "uint8"
    back = read_zarr_zip(ctx.outputs["data.zarr.zip"])
    assert back["pixels"].dtype == np.uint8 and back.sizes["image"] == 24
    assert np.array_equal(back["pixels"].values[3, :, :, 0], np.full((16, 12), 30))
    assert list(back["label"].values[:3]) == ["a", "b", "c"]
    assert len(ctx.json("preview.json")["rows"]) == 24
    index = ctx.json("ui/explore/index.json")
    status = {s["section"]: s.get("status", "ok") for s in index["sections"]}
    assert status["images"] == "ok" and status["gallery"] == "ok" and status["structure"] == "ok"
    assert index["order"].index("images") < index["order"].index("gallery")
    images = ImagesView.model_validate(ctx.json("ui/explore/images.json"))
    e = images.fields[0]
    assert e.color == "rgb" and e.group_by == "label" and len(e.groups) == 3
    gallery = GalleryView.model_validate(ctx.json("ui/explore/gallery.json"))
    assert gallery.selection == "all" and gallery.n_images == 24 and gallery.color == "rgb"
    assert [f.name for f in gallery.facets] == ["label", "split"]
    assert gallery.facets[0].source == "var" and gallery.facets[0].counts == {
        "a": 8,
        "b": 8,
        "c": 8,
    }
