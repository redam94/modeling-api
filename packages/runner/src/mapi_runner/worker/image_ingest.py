"""``image_folder`` ingest (v0.14): a zip of images and a manifest table becomes one dataset.

The upload is a zip with a manifest (``.csv``, ``.tsv`` or ``.parquet``) that names one image
per row (``path_column``, relative to the zip root) and, optionally, its id, class label,
split and other columns (:class:`~mapi_spec.ingest.ImageFolderOptions`). The dataset::

    pixels(image, y, x, channel)  uint8, role covariate, attrs {"mapi_image": "1"}
    label(image)                  str, role target (only with label_column; "" when empty)
    coords on image               path, split, the kept manifest columns,
                                  source_height / source_width (when images were resized)

``channel`` is ``["gray"]`` or ``["r", "g", "b"]``. The image dim is ``image`` by default:
``sample`` (and the other draw dims) are reserved for posterior draws.

Every byte is untrusted and decoded here, in the sandbox, in this order: the zip-bomb check
of the archive; safe entry names; the manifest through the tabular readers; a first pass that
opens every image *lazily* (Pillow reads the header only) and checks its pixel count
(:data:`~mapi_spec.ingest.MAX_IMAGE_PIXELS`, ``DecompressionBombWarning`` raised as an error),
its mode (8-bit only) and its size; the value cap of the whole array; then one preallocated
``uint8`` array that a second pass decodes into, one image at a time (never a list of arrays).
EXIF orientation is not applied: the pixels are used as stored.
"""

from __future__ import annotations

import io
import posixpath
import re
import stat
import tempfile
import warnings
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec._types import NAME_RE
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.ingest import (
    IMAGE_FOLDER_MAX_CELLS,
    IMAGE_SUFFIXES,
    RESERVED_IMAGE_DIMS,
    ImageFolderOptions,
)
from mapi_spec.schema import DataSchema, DimPolicy, DimSpec, Role, VarSpec

from mapi_runner.worker.tabular import IngestLimits, check_zip_archive, problem, read_table

if TYPE_CHECKING:
    from PIL.Image import Image as PilImage

__all__ = ["IMAGE_ATTR", "LABEL_VAR", "read_image_folder"]

IMAGE_ATTR: Final = "mapi_image"
"""Attribute (``"1"``) of the pixel variable an ``image_folder`` ingest writes."""
LABEL_VAR: Final = "label"
"""The data variable of the class labels (``label_column``)."""
_POINTER: Final = "/mapping/images"
_MANIFEST_SUFFIXES: Final = (".csv", ".tsv", ".parquet")
_PIL_FORMATS: Final = ("PNG", "JPEG", "TIFF")
"""The only Pillow decoders an ingest runs, whatever a file's suffix or bytes say."""
_GRAY_MODES: Final = frozenset({"1", "L", "LA"})
_COLOUR_MODES: Final = frozenset({"P", "RGB", "RGBA", "CMYK", "YCbCr"})
_ALPHA_MODES: Final = frozenset({"LA", "RGBA"})
_MAX_LISTED: Final = 20
_EXIF_ORIENTATION: Final = 0x0112
_DRIVE: Final = re.compile(r"^[A-Za-z]:")
_RESERVED_NAMES: Final = frozenset({"path", "source_height", "source_width"})


def _err(
    message: str, pointer: str = _POINTER, action: str | None = None, untrusted: str | None = None
) -> MapiError:
    return problem(
        ErrorCode.SCHEMA_INVALID, message, pointer=pointer, action=action, untrusted=untrusted
    )


def _too_large(message: str, pointer: str, action: str) -> MapiError:
    return problem(ErrorCode.PAYLOAD_TOO_LARGE, message, pointer=pointer, action=action)


# ================================================================================== the zip


def _unsafe(name: str) -> bool:
    """An entry or manifest path that is not a plain relative POSIX path: absolute, with a
    drive, a backslash, a NUL, a ``..`` or ``.`` part, or not normalised (``a//b``)."""
    if not name or name.startswith("/") or _DRIVE.match(name):
        return True
    if "\\" in name or "\x00" in name:
        return True
    parts = name.split("/")
    return any(p in ("", ".", "..") for p in parts) or posixpath.normpath(name) != name


def _entries(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """The regular files of the archive by name (directories skipped; every name safe)."""
    out: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if _unsafe(name):
            raise _err(
                "the zip has an entry with an unsafe name (an absolute path, '..' or a backslash)",
                action="Make the zip again from the folder of images. Use relative paths only.",
                untrusted=name[:500],
            )
        if name in out:
            raise _err(
                "the zip has two entries with the same name",
                action="Make the zip again. Each file must occur one time only.",
                untrusted=name[:500],
            )
        if info.flag_bits & 0x1:
            raise _err(
                "the zip has encrypted entries",
                action="Make the zip again without a password.",
                untrusted=name[:500],
            )
        ftype = stat.S_IFMT(info.external_attr >> 16)
        if ftype and ftype != stat.S_IFREG:
            raise _err(
                "the zip has an entry that is not a regular file (a link)",
                action="Make the zip again with the image files themselves, not links.",
                untrusted=name[:500],
            )
        out[name] = info
    return out


def _read_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo, cap: int) -> bytes:
    with zf.open(info) as fh:
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise ValueError("the entry is larger than its declared size")
    return data


# ============================================================================= the manifest


def _manifest(
    zf: zipfile.ZipFile,
    entries: dict[str, zipfile.ZipInfo],
    opts: ImageFolderOptions,
    limits: IngestLimits,
    work: Path,
) -> pd.DataFrame:
    name = opts.manifest
    pointer = f"{_POINTER}/manifest"
    if _unsafe(name):
        raise _err(
            "the manifest path is not a relative path in the zip",
            pointer,
            action="Give the path of the manifest file in the zip, such as manifest.csv.",
            untrusted=name[:500],
        )
    suffix = posixpath.splitext(name)[1].lower()
    if suffix not in _MANIFEST_SUFFIXES:
        raise _err(
            "the manifest must be a .csv, .tsv or .parquet file",
            pointer,
            action="Save the manifest table as CSV or Parquet.",
            untrusted=name[:500],
        )
    info = entries.get(name)
    if info is None:
        tables = [n for n in entries if n.lower().endswith(_MANIFEST_SUFFIXES)][:_MAX_LISTED]
        raise _err(
            "the zip has no manifest file at this path",
            pointer,
            action="Put a manifest table (one row for each image) in the zip, or set manifest "
            "to its path."
            + (" The zip has these tables: " + ", ".join(tables) + "." if tables else ""),
            untrusted=name[:500],
        )
    local = work / ("manifest" + suffix)
    try:
        local.write_bytes(_read_entry(zf, info, info.file_size))
    except (OSError, ValueError, zipfile.BadZipFile, EOFError) as exc:
        raise _err(
            "the server cannot read the manifest from the zip",
            pointer,
            action="Make the zip again.",
            untrusted=str(exc),
        ) from None
    try:
        return read_table(local, "parquet" if suffix == ".parquet" else "csv", limits)
    except MapiError as exc:
        issue = exc.issues[0] if exc.issues else None
        raise problem(
            exc.code,
            f"the manifest cannot be read: {exc.detail}",
            pointer=pointer,
            action=issue.suggested_action if issue is not None else None,
            untrusted=exc.untrusted_text,
        ) from None
    finally:
        local.unlink(missing_ok=True)


def _column(df: pd.DataFrame, column: str, pointer: str) -> pd.Series:
    if column in df.columns:
        return df[column]
    present = [str(c) for c in df.columns]
    raise _err(
        "a column of the options is not in the manifest",
        pointer,
        action="Use the name of a column of the manifest. The manifest has these columns: "
        + ", ".join(present[:_MAX_LISTED])
        + (" ..." if len(present) > _MAX_LISTED else "")
        + ".",
        untrusted=column[:256],
    )


def _text(value: Any) -> str:
    """A manifest cell as text: ``""`` for a missing cell, ``3`` (not ``3.0``) for an
    integral number."""
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return ""
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if value is pd.NA or value is pd.NaT:
        return ""
    return str(value).strip()


def _texts(series: pd.Series) -> list[str]:
    return [_text(v) for v in series.tolist()]


def _ids(df: pd.DataFrame, opts: ImageFolderOptions) -> np.ndarray:
    n = len(df)
    if opts.id_column is None:
        width = len(str(max(n - 1, 0)))
        return np.array([str(i).zfill(width) for i in range(n)], dtype=str)
    pointer = f"{_POINTER}/id_column"
    ids = _texts(_column(df, opts.id_column, pointer))
    empty = [i for i, v in enumerate(ids) if not v]
    if empty:
        raise _err(
            f"the id column has {len(empty)} empty cells (first in row {empty[0] + 2})",
            pointer,
            action="Give each image an id, or do not set id_column.",
        )
    seen: set[str] = set()
    for i, v in enumerate(ids):
        if v in seen:
            raise _err(
                f"the id column has the same id two times (row {i + 2})",
                pointer,
                action="Give each image a different id, or do not set id_column.",
                untrusted=v[:256],
            )
        seen.add(v)
    return np.array(ids, dtype=str)


def _paths(
    df: pd.DataFrame,
    opts: ImageFolderOptions,
    entries: dict[str, zipfile.ZipInfo],
    limits: IngestLimits,
) -> list[str]:
    pointer = f"{_POINTER}/path_column"
    paths = _texts(_column(df, opts.path_column, pointer))
    for i, p in enumerate(paths):
        row = f"row {i + 2}"
        if not p:
            raise _err(
                f"the path column is empty in {row}",
                pointer,
                action="Give the path of an image in the zip in each row.",
            )
        if _unsafe(p):
            raise _err(
                f"the path in {row} is not a plain relative path (an absolute path, '..', "
                "'./' or a backslash)",
                pointer,
                action="Write the paths relative to the root of the zip with '/', such as "
                "images/cat_001.png.",
                untrusted=p[:500],
            )
        if not p.lower().endswith(IMAGE_SUFFIXES):
            raise _err(
                f"the file in {row} is not a PNG, JPEG or TIFF image",
                pointer,
                action="Use images with the extension "
                + ", ".join(IMAGE_SUFFIXES)
                + ". Convert other formats first.",
                untrusted=p[:500],
            )
        info = entries.get(p)
        if info is None:
            missing = sum(1 for q in paths if q and q not in entries)
            raise _err(
                f"{missing} paths of the manifest are not in the zip (first in {row})",
                pointer,
                action="Make sure that the paths are relative to the root of the zip and "
                "that the spelling (also upper and lower case) is correct.",
                untrusted=p[:500],
            )
        if info.file_size > limits.max_image_file_bytes:
            raise _too_large(
                f"the image in {row} has {info.file_size:,} bytes. The maximum is "
                f"{limits.max_image_file_bytes:,}",
                pointer,
                "Make the image smaller, or save it with compression.",
            )
    return paths


# =============================================================================== the images


@contextmanager
def _pillow(max_pixels: int) -> Iterator[Any]:
    """Pillow with ``MAX_IMAGE_PIXELS`` set to ``max_pixels`` and the decompression-bomb
    warning raised as an error (other Pillow warnings ignored); restored on exit."""
    from PIL import Image

    before = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            yield Image
    finally:
        Image.MAX_IMAGE_PIXELS = before


@dataclass(slots=True)
class _Notes:
    alpha: int = 0
    palette: int = 0
    pages: int = 0
    resized: int = 0
    to_gray: int = 0
    exif: int = 0


def _open(Image: Any, data: bytes, row: int, path: str, limits: IngestLimits) -> PilImage:
    """The image, opened lazily (Pillow reads the header): too many pixels and unreadable
    files are refused here, before any pixel is decoded. Only the PNG, JPEG and TIFF decoders
    run (a ``.png`` holding an EPS, PSD or GIF is unreadable, not decoded by its plugin)."""
    pointer = f"{_POINTER}/path_column"
    try:
        im = Image.open(io.BytesIO(data), formats=_PIL_FORMATS)
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise _too_large(
            f"the image in row {row} has more than {limits.max_image_pixels:,} pixels",
            pointer,
            "Make the image smaller before you upload it.",
        ) from None
    except Exception as exc:
        raise _err(
            f"the server cannot read the image in row {row}",
            pointer,
            action="Make sure that the file is a complete PNG, JPEG or TIFF image.",
            untrusted=f"{path[:300]}: {type(exc).__name__}: {exc}",
        ) from None
    w, h = im.size
    if w * h > limits.max_image_pixels:
        raise _too_large(
            f"the image in row {row} has {w:,} x {h:,} pixels. The maximum is "
            f"{limits.max_image_pixels:,} pixels",
            pointer,
            "Make the image smaller before you upload it.",
        )
    if im.mode not in _GRAY_MODES | _COLOUR_MODES:
        raise _err(
            f"the image in row {row} has the pixel mode {im.mode!r} (16-bit, 32-bit or float "
            "values)",
            pointer,
            action="Keep 8-bit images or convert them (gray or RGB) before you upload them.",
            untrusted=path[:500],
        )
    return cast("PilImage", im)


def _convert(im: PilImage, rgb: bool, notes: _Notes) -> PilImage:
    """``im`` as ``L`` (gray) or ``RGB``: alpha dropped, palettes and CMYK/YCbCr converted."""
    mode = im.mode
    if mode in _ALPHA_MODES:
        notes.alpha += 1
    if mode == "P":
        notes.palette += 1
        im = im.convert("RGBA" if "transparency" in im.info else "RGB")
        mode = im.mode
    if rgb:
        return im if mode == "RGB" else im.convert("RGB")
    if mode in _COLOUR_MODES:
        notes.to_gray += 1
    return im if mode == "L" else im.convert("L")


def _fit(Image: Any, im: PilImage, size: tuple[int, int], how: str) -> PilImage:
    """``im`` resized to ``size`` (height, width): ``stretch``, ``crop`` (centre crop to the
    target aspect, then resize) or ``pad`` (fit inside, then pad with zeros)."""
    th, tw = size
    w, h = im.size
    if how == "crop":
        target = tw / th
        if w / h > target:
            cw = max(1, round(h * target))
            left = (w - cw) // 2
            im = im.crop((left, 0, left + cw, h))
        elif w / h < target:
            ch = max(1, round(w / target))
            top = (h - ch) // 2
            im = im.crop((0, top, w, top + ch))
        return im.resize((tw, th), Image.BILINEAR)
    if how == "pad":
        scale = min(tw / w, th / h)
        nw, nh = max(1, min(tw, round(w * scale))), max(1, min(th, round(h * scale)))
        inner = im.resize((nw, nh), Image.BILINEAR) if (nw, nh) != (w, h) else im
        canvas = cast("PilImage", Image.new(im.mode, (tw, th), 0))
        canvas.paste(inner, ((tw - nw) // 2, (th - nh) // 2))
        return canvas
    return im.resize((tw, th), Image.BILINEAR)


def _check_sizes(sizes: Sequence[tuple[int, int]], paths: Sequence[str]) -> None:
    first = sizes[0]
    odd = [i for i, s in enumerate(sizes) if s != first]
    if not odd:
        return
    listed = [paths[0], *(paths[i] for i in odd[:5])]
    shown = [sizes[0], *(sizes[i] for i in odd[:5])]
    raise _err(
        f"{len(odd)} images do not have the size of the first image "
        f"({first[0]} x {first[1]} pixels, height x width)",
        f"{_POINTER}/size",
        action="Set size (height and width) to resize all the images to one size, or make "
        "the images the same size before you upload them.",
        untrusted="; ".join(
            f"{p[:200]}: {h} x {w}" for p, (h, w) in zip(listed, shown, strict=True)
        ),
    )


def _cap(n: int, h: int, w: int, c: int, extra: int, limits: IngestLimits) -> None:
    cells = n * h * w * c
    cap = min(IMAGE_FOLDER_MAX_CELLS, limits.max_uncompressed_bytes) - extra
    if cells <= cap:
        return
    n_fit = max(0, cap // max(1, h * w * c))
    raise _too_large(
        f"The images need {cells:,} values. The maximum is {cap:,} (about {n_fit:,} images "
        "of this size).",
        f"{_POINTER}/size",
        "Make the images smaller (set size) or use fewer images.",
    )


# ============================================================================ manifest data


def _coord_values(series: pd.Series) -> np.ndarray:
    """A manifest column as a coord: bool, int64 (no missing), float64 or str ('' missing)."""
    if pd.api.types.is_bool_dtype(series) and not series.isna().any():
        return np.asarray(series.to_numpy(dtype=bool), dtype=bool)
    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        if pd.api.types.is_integer_dtype(series) and not series.isna().any():
            return np.asarray(series.to_numpy(dtype=np.int64), dtype=np.int64)
        return np.asarray(series.to_numpy(dtype=np.float64, na_value=np.nan), dtype=np.float64)
    if pd.api.types.is_datetime64_any_dtype(series):
        return np.array(
            ["" if pd.isna(v) else pd.Timestamp(v).isoformat() for v in series], dtype=str
        )
    return np.array(_texts(series), dtype=str)


def _kept(
    df: pd.DataFrame, opts: ImageFolderOptions, taken: set[str], notes: list[dict[str, str]]
) -> dict[str, np.ndarray]:
    """The other manifest columns kept as coords on the image dim."""
    used = {opts.path_column, opts.id_column, opts.label_column, opts.split_column}
    out: dict[str, np.ndarray] = {}
    if opts.columns is not None:
        for i, col in enumerate(opts.columns):
            pointer = f"{_POINTER}/columns/{i}"
            series = _column(df, col, pointer)
            if not re.match(NAME_RE, col) or col in taken:
                raise _err(
                    "a kept column has a name that cannot be a coordinate (it is used by the "
                    "dataset, or it has spaces or other special characters)",
                    pointer,
                    action="Rename the column in the manifest, or remove it from columns.",
                    untrusted=col[:256],
                )
            out[col] = _coord_values(series)
        return out
    skipped: list[str] = []
    for col in df.columns:
        name = str(col)
        if name in used:
            continue
        if not re.match(NAME_RE, name) or name in taken:
            skipped.append(name)
            continue
        out[name] = _coord_values(df[col])
    if skipped:
        notes.append(
            {
                "pointer": f"{_POINTER}/columns",
                "message": f"{len(skipped)} manifest columns were not kept: their names are "
                "used by the dataset or have special characters",
            }
        )
    return out


# ===================================================================================== read


def read_image_folder(
    path: Path, opts: ImageFolderOptions, limits: IngestLimits
) -> tuple[xr.Dataset, DataSchema, list[dict[str, str]]]:
    """Read an ``image_folder`` upload: ``(dataset, schema, notes)`` (see the module
    docstring). Every problem is a ``MapiError`` with a pointer under ``/mapping/images``."""
    if opts.dim in RESERVED_IMAGE_DIMS:
        raise _err(
            f"the image dim cannot be called {opts.dim!r}: the name is kept for draws",
            f"{_POINTER}/dim",
            action="Use the default name image.",
        )
    if len({opts.dim, opts.var, LABEL_VAR, "y", "x", "channel"}) < 6:
        raise _err(
            "the names of the image dim and the pixel variable must differ from y, x, channel "
            "and label (and from each other)",
            f"{_POINTER}/var",
            action="Use the default names: pixels and image.",
        )
    check_zip_archive(
        path,
        limits,
        action="Upload a .zip of the images and the manifest table. Make the zip again if it "
        "is damaged.",
    )
    notes: list[dict[str, str]] = []
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:  # pragma: no cover - checked above
        raise _err("the upload is not a readable zip archive", untrusted=str(exc)) from None
    with zf, tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        entries = _entries(zf)
        df = _manifest(zf, entries, opts, limits, Path(tmp))
        n = len(df)
        max_images = min(opts.max_images, limits.max_images)
        if n == 0:
            raise _err(
                "the manifest has no rows",
                f"{_POINTER}/manifest",
                action="Add one row for each image to the manifest.",
            )
        if n > max_images:
            raise _too_large(
                f"the manifest has {n:,} images. The maximum is {max_images:,}",
                f"{_POINTER}/max_images",
                "Use fewer images, or set max_images higher (the maximum is "
                f"{limits.max_images:,}).",
            )
        paths = _paths(df, opts, entries, limits)
        ids = _ids(df, opts)
        labels = (
            _texts(_column(df, opts.label_column, f"{_POINTER}/label_column"))
            if opts.label_column is not None
            else None
        )
        splits = (
            _texts(_column(df, opts.split_column, f"{_POINTER}/split_column"))
            if opts.split_column is not None
            else None
        )
        taken = {opts.dim, opts.var, "y", "x", "channel", *_RESERVED_NAMES}
        if labels is not None:
            taken.add(LABEL_VAR)
        if splits is not None:
            taken.add("split")
        kept = _kept(df, opts, taken, notes)

        with _pillow(limits.max_image_pixels) as Image:
            # pass 1: headers only (size, mode, pages); nothing is decoded
            sizes: list[tuple[int, int]] = []
            colour = False
            note = _Notes()
            for i, p in enumerate(paths):
                info = entries[p]
                try:
                    data = _read_entry(zf, info, limits.max_image_file_bytes)
                except (OSError, ValueError, zipfile.BadZipFile, EOFError) as exc:
                    raise _err(
                        f"the server cannot read the image in row {i + 2} from the zip",
                        f"{_POINTER}/path_column",
                        action="Make the zip again.",
                        untrusted=f"{p[:300]}: {exc}",
                    ) from None
                im = _open(Image, data, i + 2, p, limits)
                w, h = im.size
                sizes.append((h, w))
                colour = colour or im.mode in _COLOUR_MODES
                if getattr(im, "n_frames", 1) > 1:
                    note.pages += 1
                im.close()
            if opts.size is None:
                _check_sizes(sizes, paths)
                height, width = sizes[0]
            else:
                height, width = opts.size
            rgb = opts.mode == "rgb" or (opts.mode == "auto" and colour)
            channels = 3 if rgb else 1
            extra = n * (len(kept) + 6) + height + width + channels
            _cap(n, height, width, channels, extra, limits)

            # pass 2: decode into one preallocated array
            pixels = np.empty((n, height, width, channels), dtype=np.uint8)
            for i, p in enumerate(paths):
                data = _read_entry(zf, entries[p], limits.max_image_file_bytes)
                im = _open(Image, data, i + 2, p, limits)
                try:
                    orientation = im.getexif().get(_EXIF_ORIENTATION)
                except Exception:
                    orientation = None
                if orientation not in (None, 1):
                    note.exif += 1
                try:
                    im.load()
                    out = _convert(im, rgb, note)
                    if out.size != (width, height):
                        out = _fit(Image, out, (height, width), opts.resize)
                        note.resized += 1
                    arr = np.asarray(out, dtype=np.uint8)
                except MapiError:
                    raise
                except (Image.DecompressionBombWarning, Image.DecompressionBombError):
                    raise _too_large(
                        f"the image in row {i + 2} has more than "
                        f"{limits.max_image_pixels:,} pixels",
                        f"{_POINTER}/path_column",
                        "Make the image smaller before you upload it.",
                    ) from None
                except Exception as exc:
                    raise _err(
                        f"the server cannot decode the image in row {i + 2}",
                        f"{_POINTER}/path_column",
                        action="Make sure that the file is a complete PNG, JPEG or TIFF image.",
                        untrusted=f"{p[:300]}: {type(exc).__name__}: {exc}",
                    ) from None
                finally:
                    im.close()
                pixels[i] = arr.reshape(height, width, channels)
                del arr, data

    _image_notes(note, opts, notes)
    coords: dict[str, Any] = {
        opts.dim: ids,
        "y": np.arange(height, dtype=np.int64),
        "x": np.arange(width, dtype=np.int64),
        "channel": np.array(["r", "g", "b"] if rgb else ["gray"], dtype=str),
        "path": (opts.dim, np.array(paths, dtype=str)),
    }
    if splits is not None:
        coords["split"] = (opts.dim, np.array(splits, dtype=str))
    for name, values in kept.items():
        coords[name] = (opts.dim, values)
    if note.resized:
        coords["source_height"] = (opts.dim, np.array([s[0] for s in sizes], dtype=np.int64))
        coords["source_width"] = (opts.dim, np.array([s[1] for s in sizes], dtype=np.int64))
    data_vars: dict[str, Any] = {
        opts.var: xr.Variable((opts.dim, "y", "x", "channel"), pixels, {IMAGE_ATTR: "1"}),
    }
    vars_: dict[str, VarSpec] = {
        opts.var: VarSpec(
            dims=(opts.dim, "y", "x", "channel"),
            dtype="uint8",
            role=Role.COVARIATE,
            missing="forbid",
            description="Pixel values (0 to 255) of the images",
        )
    }
    if labels is not None:
        data_vars[LABEL_VAR] = ((opts.dim,), np.array(labels, dtype=str))
        vars_[LABEL_VAR] = VarSpec(
            dims=(opts.dim,),
            dtype="str",
            role=Role.TARGET,
            missing="allow",
            description="The class label of each image",
        )
    ds = xr.Dataset(data_vars, coords=coords)
    schema = DataSchema(
        dims={
            opts.dim: DimSpec(
                kind="categorical", policy=DimPolicy.NEW_LEVELS, new_level_strategy="pooled"
            ),
            "y": DimSpec(kind="integer", policy=DimPolicy.FIXED),
            "x": DimSpec(kind="integer", policy=DimPolicy.FIXED),
            "channel": DimSpec(kind="categorical", policy=DimPolicy.FIXED),
        },
        vars=vars_,
    )
    return ds, schema, notes


def _image_notes(note: _Notes, opts: ImageFolderOptions, notes: list[dict[str, str]]) -> None:
    def add(pointer: str, message: str) -> None:
        notes.append({"pointer": f"{_POINTER}{pointer}", "message": message})

    if note.alpha:
        add("/mode", f"{note.alpha} images have transparency (alpha). The server removed it")
    if note.palette:
        add("/mode", f"{note.palette} images have a colour palette. The server converted them")
    if note.to_gray:
        add("/mode", f"{note.to_gray} colour images were converted to gray")
    if note.pages:
        add(
            "/path_column",
            f"{note.pages} images have more than one page or frame. The server used only the first",
        )
    if note.resized:
        how = {"stretch": "stretched", "crop": "cropped", "pad": "padded"}[opts.resize]
        add("/size", f"{note.resized} images were resized ({how}) to the size of the dataset")
    if note.exif:
        add(
            "/path_column",
            f"{note.exif} images have an EXIF orientation. The server did not turn them: the "
            "pixels are as stored",
        )
