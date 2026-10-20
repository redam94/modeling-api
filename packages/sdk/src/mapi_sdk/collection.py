"""Image collections and image loaders (v0.15).

A collection version (:mod:`mapi_spec.collection`) is a manifest (one row per image) plus
**packs**: zip files whose entries are ``<sha256>.<ext>`` images. Nothing is decoded until a
batch needs it:

* :class:`Collection` - the parsed ``collection.json`` and ``manifest.parquet`` of a version
  and a :class:`BlobSource` that reads byte ranges of its packs (the runner supplies one
  that range-reads signed URLs; :class:`LocalPackSource` reads local files);
* :class:`ImageLoader` - the protocol a ``fit_batches`` model receives:
  :class:`CollectionLoader` (lazy, over a collection) and :class:`ArrayImageLoader` (over an
  in-memory ``pixels(image, y, x, channel)`` dataset: ``image_folder`` data and tests);
* :class:`Augment` - per-sample augmentation applied in the loader;
* :func:`index_part`, :func:`read_blob` - the zip index and verified reads of one image;
* :func:`collection_read` - a seeded sample as the v0.14 image dataset, for models that
  read datasets (``data.collection``).

Batches are deterministic. The order of an epoch is a pure function of ``(seed, epoch)``
("block shuffle": chunks of at most 64 images that lie next to each other in a pack are
permuted, then images are shuffled inside windows of 8 chunks); the augmentation of the
image at loader position ``p`` uses ``default_rng([seed, epoch, p])``; batches are put
together in order. So the thread count and prefetch depth never change a result. Every read
verifies the image's CRC-32 and sha256.
"""

from __future__ import annotations

import io
import math
import os
import re
import threading
import zlib
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Protocol, Self, cast, runtime_checkable

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.collection import (
    MANIFEST_FIXED,
    MAX_COLLECTION_IMAGES,
    MAX_PART_ENTRIES,
    PART_ENTRY_RE,
    CollectionInfo,
    CollectionRead,
)
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.ingest import IMAGE_FOLDER_MAX_CELLS, MAX_IMAGE_FILE_BYTES
from mapi_spec.schema import DataSchema, DimPolicy, DimSpec, Role, VarSpec
from pydantic import BaseModel, ConfigDict, Field

from mapi_sdk.images import decode_image, resize_array

__all__ = [
    "CHUNK_IMAGES",
    "IMAGE_ATTR",
    "LABEL_VAR",
    "MAX_SPAN_BYTES",
    "WINDOW_CHUNKS",
    "ArrayImageLoader",
    "Augment",
    "Batch",
    "BlobLocation",
    "BlobSource",
    "Collection",
    "CollectionError",
    "CollectionLoader",
    "ImageLoader",
    "LocalPackSource",
    "PartEntry",
    "collection_read",
    "collection_schema",
    "index_part",
    "members_bin",
    "read_blob",
    "read_manifest",
    "write_manifest",
]

LABEL_VAR: Final = "label"
"""The class label variable of a collection (``label(image)``, text)."""
IMAGE_ATTR: Final = "mapi_image"
"""Attribute (``"1"``) of a pixel variable (as the ``image_folder`` ingest writes it)."""
CHUNK_IMAGES: Final = 64
"""Images per chunk: the unit of reads and of the block shuffle."""
WINDOW_CHUNKS: Final = 8
"""Chunks per shuffle window."""
MAX_SPAN_BYTES: Final = 64 * 1024**2
"""Largest byte range one read asks for."""
_SPAN_GAP: Final = 256 * 1024
"""Unused bytes between two images that one read still spans."""
_SHA_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_ENTRY_RE: Final = re.compile(PART_ENTRY_RE)
_EOCD: Final = b"PK\x05\x06"
_ZIP64_LOCATOR: Final = b"PK\x06\x07"
_CENTRAL: Final = b"PK\x01\x02"
_LOCAL: Final = b"PK\x03\x04"
_EOCD_SIZE: Final = 22
_CENTRAL_SIZE: Final = 46
_LOCAL_SIZE: Final = 30
_HEADER_WINDOW: Final = 4 * 1024**2
_CHANNELS: Final = {"gray": ["gray"], "rgb": ["r", "g", "b"]}
_RESIZE: Final = ("stretch", "crop", "pad")


class CollectionError(MapiError):
    """A pack, manifest or collection document that is not valid."""

    default_code = ErrorCode.SCHEMA_INVALID


def _integrity(message: str, untrusted: str | None = None) -> MapiError:
    return MapiError(message, code=ErrorCode.INTEGRITY_FAILURE, untrusted_text=untrusted)


# ============================================================================== augmentation


class Augment(BaseModel):
    """Random per-image transforms a loader applies to training batches.

    Flips and 90-degree turns (only half turns for non-square images) keep the pixels;
    ``crop_pad`` pads by ``n`` pixels (edge values) and crops back to the size at a random
    offset; ``brightness``/``contrast`` add or scale by a random amount up to the value (in
    units of the 0..255 range); ``noise`` adds Gaussian noise with that standard deviation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    flip_x: bool = Field(False, description="Mirror left-right with probability 0.5")
    flip_y: bool = Field(False, description="Mirror top-bottom with probability 0.5")
    rotate90: bool = Field(False, description="Turn by a random multiple of 90 degrees")
    crop_pad: int = Field(0, ge=0, le=64, description="Pad by n pixels, then crop at random")
    brightness: float = Field(0, ge=0, le=1, description="Largest brightness change")
    contrast: float = Field(0, ge=0, le=1, description="Largest relative contrast change")
    noise: float = Field(0, ge=0, le=0.5, description="Gaussian noise sd (0..1 units)")

    @property
    def active(self) -> bool:
        """Whether any transform is on."""
        return bool(
            self.flip_x
            or self.flip_y
            or self.rotate90
            or self.crop_pad
            or self.brightness
            or self.contrast
            or self.noise
        )

    def apply(self, x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        """``x`` (uint8 ``(H, W, C)``) transformed; same shape and dtype."""
        h, w = x.shape[:2]
        y = x
        if self.flip_x and rng.random() < 0.5:
            y = y[:, ::-1]
        if self.flip_y and rng.random() < 0.5:
            y = y[::-1]
        if self.rotate90:
            k = int(rng.integers(4)) if h == w else 2 * int(rng.integers(2))
            y = np.rot90(y, k, axes=(0, 1))
        if self.crop_pad:
            n = self.crop_pad
            padded = np.pad(y, ((n, n), (n, n), (0, 0)), mode="edge")
            dy, dx = (int(v) for v in rng.integers(0, 2 * n + 1, size=2))
            y = padded[dy : dy + h, dx : dx + w]
        if self.brightness or self.contrast or self.noise:
            f = y.astype(np.float32)
            if self.brightness:
                f += np.float32(rng.uniform(-self.brightness, self.brightness) * 255.0)
            if self.contrast:
                mean = f.mean()
                f = (f - mean) * np.float32(1.0 + rng.uniform(-self.contrast, self.contrast)) + mean
            if self.noise:
                f += rng.normal(0.0, self.noise * 255.0, size=f.shape).astype(np.float32)
            y = np.clip(np.rint(f), 0, 255).astype(np.uint8)
        return np.ascontiguousarray(y, dtype=np.uint8)


# ================================================================================== batches


@dataclass(frozen=True, slots=True)
class Batch:
    """One batch of images, in the order of the epoch."""

    ids: np.ndarray
    """(B,) object str: the image ids."""
    pixels: np.ndarray
    """(B, H, W, C) uint8."""
    labels: np.ndarray | None
    """(B,) object str (``""`` = missing), or ``None`` without labels."""
    codes: np.ndarray | None
    """(B,) int32 index in the loader's classes; -1 for a missing or unknown label."""
    index: np.ndarray
    """(B,) int64 positions in the loader."""


@runtime_checkable
class ImageLoader(Protocol):
    """Images along one dim, read in batches (``fit_batches``/``score_batches``)."""

    @property
    def n(self) -> int: ...

    @property
    def dim(self) -> str: ...

    @property
    def ids(self) -> np.ndarray: ...

    @property
    def labels(self) -> np.ndarray | None: ...

    @property
    def classes(self) -> list[str]: ...

    @property
    def size(self) -> tuple[int, int]: ...

    @property
    def channels(self) -> int: ...

    def manifest(self) -> xr.Dataset: ...

    def subset(self, which: np.ndarray | Sequence[str]) -> ImageLoader: ...

    def with_classes(self, classes: Sequence[str]) -> ImageLoader: ...

    def with_size(self, size: tuple[int, int], resize: str = "stretch") -> ImageLoader: ...

    def with_mode(self, mode: Literal["gray", "rgb"]) -> ImageLoader: ...

    def split(
        self, fraction: float, *, seed: int, stratify: bool = True
    ) -> tuple[ImageLoader, ImageLoader]: ...

    def batches(
        self,
        batch_size: int,
        *,
        epoch: int = 0,
        shuffle: bool = False,
        seed: int = 0,
        augment: Augment | None = None,
        drop_last: bool = False,
        shard: tuple[int, int] = (0, 1),
        prefetch: int = 2,
    ) -> Iterator[Batch]: ...

    def to_xarray(
        self,
        max_samples: int | None = None,
        size: tuple[int, int] | None = None,
        *,
        seed: int = 0,
        stratify: str | None = "label",
        resize: str = "stretch",
    ) -> xr.Dataset: ...


def _text_array(values: Iterable[Any]) -> np.ndarray:
    """Values as an object array of str (``""`` for missing)."""
    out = []
    for v in values:
        if v is None or v is pd.NA or (isinstance(v, float) and not math.isfinite(v)):
            out.append("")
        else:
            out.append(str(v))
    return np.array(out, dtype=object)


def _coord_values(values: Any) -> np.ndarray:
    """A manifest column as coord values: bool, int64, float64 or str (``""`` missing)."""
    series = values if isinstance(values, pd.Series) else pd.Series(values)
    if pd.api.types.is_bool_dtype(series) and not series.isna().any():
        return np.asarray(series.to_numpy(dtype=bool), dtype=bool)
    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        if pd.api.types.is_integer_dtype(series) and not series.isna().any():
            return np.asarray(series.to_numpy(dtype=np.int64), dtype=np.int64)
        return np.asarray(series.to_numpy(dtype=np.float64, na_value=np.nan), dtype=np.float64)
    return np.array(_text_array(series.tolist()).tolist(), dtype=str)


class _LoaderBase:
    """What both loaders share: rows, settings, order, batching and samples.

    Subclasses hold the pixels (or where to read them) and implement :meth:`_take`,
    :meth:`_read_chunk` and :meth:`_native_size`.
    """

    _dim: str
    _var: str
    _label_name: str
    _ids: np.ndarray
    _labels: np.ndarray | None
    _coords: dict[str, np.ndarray]
    _classes: list[str] | None
    _size: tuple[int, int] | None
    _resize: str
    _mode: Literal["gray", "rgb"]
    _threads: int

    # -- rows -----------------------------------------------------------------------
    @property
    def n(self) -> int:
        return int(self._ids.shape[0])

    @property
    def dim(self) -> str:
        return self._dim

    @property
    def ids(self) -> np.ndarray:
        return self._ids

    @property
    def labels(self) -> np.ndarray | None:
        return self._labels

    @property
    def classes(self) -> list[str]:
        if self._classes is not None:
            return list(self._classes)
        if self._labels is None:
            return []
        return sorted({str(v) for v in self._labels if v})

    @property
    def size(self) -> tuple[int, int]:
        size = self._size or self._native_size()
        if size is None:
            raise MapiError(
                "the images do not have one size: set a size (height and width)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data/collection/size",
            )
        return size

    @property
    def channels(self) -> int:
        return 3 if self._mode == "rgb" else 1

    @property
    def mode(self) -> Literal["gray", "rgb"]:
        return self._mode

    def _native_size(self) -> tuple[int, int] | None:
        raise NotImplementedError

    def _take(self, positions: np.ndarray) -> Self:
        raise NotImplementedError

    def _read_chunk(self, positions: np.ndarray) -> list[np.ndarray]:
        """The images at ``positions`` decoded to the loader's size and mode."""
        raise NotImplementedError

    def _io_chunks(self) -> list[np.ndarray]:
        """Chunks for the block shuffle (default: runs of loader positions)."""
        return _runs(np.arange(self.n, dtype=np.int64))

    def _copy(self, **changes: Any) -> Self:
        out = self._take(np.arange(self.n, dtype=np.int64))
        for k, v in changes.items():
            setattr(out, k, v)
        return out

    def manifest(self) -> xr.Dataset:
        """The rows as a 1-D dataset along the dim: the label variable and the coords."""
        data_vars: dict[str, Any] = {}
        if self._labels is not None:
            data_vars[self._label_name] = (
                (self._dim,),
                np.array(self._labels.tolist(), dtype=str),
            )
        coords: dict[str, Any] = {self._dim: np.array(self._ids.tolist(), dtype=str)}
        for name, values in self._coords.items():
            coords[name] = (self._dim, values)
        return xr.Dataset(data_vars, coords=coords)

    def subset(self, which: np.ndarray | Sequence[str]) -> Self:
        """The rows ``which`` selects: a bool mask (loader order), int positions or ids (in
        the order given)."""
        return self._take(self._positions(which))

    def _positions(self, which: np.ndarray | Sequence[str]) -> np.ndarray:
        arr = np.asarray(which)
        if arr.dtype == bool:
            if arr.shape != (self.n,):
                raise ValueError(f"a mask must have {self.n} values, not {arr.shape}")
            return np.flatnonzero(arr).astype(np.int64)
        if arr.size == 0:
            return np.zeros(0, dtype=np.int64)
        if arr.dtype.kind in "iu":
            pos = arr.astype(np.int64).reshape(-1)
            if pos.min() < 0 or pos.max() >= self.n:
                raise IndexError("a position is outside the loader")
            return pos
        index = pd.Index(np.asarray(self._ids, dtype=object))
        wanted = [str(v) for v in arr.reshape(-1).tolist()]
        pos = np.asarray(index.get_indexer(wanted), dtype=np.int64)
        if (pos < 0).any():
            unknown = [w for w, p in zip(wanted, pos, strict=True) if p < 0]
            raise MapiError(
                f"{len(unknown)} image ids are not in the loader",
                code=ErrorCode.SCHEMA_INVALID,
                untrusted_text=", ".join(unknown[:20])[:1000],
            )
        return pos

    def with_classes(self, classes: Sequence[str]) -> Self:
        """The loader with a fixed class list (codes index into it; others are -1)."""
        listed = [str(c) for c in classes]
        if len(set(listed)) != len(listed):
            raise ValueError("classes must be unique")
        return self._copy(_classes=listed)

    def with_size(self, size: tuple[int, int], resize: str = "stretch") -> Self:
        """The loader with batches resized to ``size`` (height, width)."""
        if len(size) != 2 or min(size) < 1:
            raise ValueError(f"size must be (height, width) with both >= 1, not {size!r}")
        if resize not in _RESIZE:
            raise ValueError(f"resize must be one of {', '.join(_RESIZE)}")
        return self._copy(_size=(int(size[0]), int(size[1])), _resize=resize)

    def with_mode(self, mode: Literal["gray", "rgb"]) -> Self:
        """The loader with images decoded as ``gray`` (1 channel) or ``rgb`` (3)."""
        if mode not in _CHANNELS:
            raise ValueError("mode must be 'gray' or 'rgb'")
        return self._copy(_mode=mode)

    def split(self, fraction: float, *, seed: int, stratify: bool = True) -> tuple[Self, Self]:
        """``(rest, held)``: about ``fraction`` of the rows held out (per label when
        ``stratify`` and the loader has labels), both in loader order."""
        if not 0 < fraction < 1:
            raise ValueError("fraction must be between 0 and 1")
        rng = np.random.default_rng(seed)
        groups: dict[str, list[int]] = {}
        keys = self._labels if stratify and self._labels is not None else None
        for p in range(self.n):
            groups.setdefault("" if keys is None else str(keys[p]), []).append(p)
        held: list[int] = []
        for key in sorted(groups):
            members = np.array(groups[key], dtype=np.int64)
            k = round(members.size * fraction)
            held.extend(rng.permutation(members)[:k].tolist())
        if not held and self.n > 1:
            held = [int(rng.integers(self.n))]
        mask = np.zeros(self.n, dtype=bool)
        mask[held] = True
        return self._take(np.flatnonzero(~mask)), self._take(np.flatnonzero(mask))

    # -- batches --------------------------------------------------------------------
    def _codes(self, labels: np.ndarray) -> np.ndarray:
        lookup = {c: i for i, c in enumerate(self.classes)}
        return np.array([lookup.get(str(v), -1) for v in labels], dtype=np.int32)

    def _epoch_windows(
        self, epoch: int, shuffle: bool, seed: int, shard: tuple[int, int]
    ) -> list[tuple[list[np.ndarray], np.ndarray]]:
        """``[(chunks, positions in output order)]`` of one epoch of one shard."""
        i, n = shard
        if shuffle:
            chunks = self._io_chunks()
            rng = np.random.default_rng([seed, epoch])
            chunks = [chunks[k] for k in rng.permutation(len(chunks))]
        else:
            rng = None
            chunks = _runs(np.arange(self.n, dtype=np.int64))
        chunks = chunks[i::n]
        windows = []
        for w in range(0, len(chunks), WINDOW_CHUNKS):
            group = chunks[w : w + WINDOW_CHUNKS]
            order = np.concatenate(group)
            if rng is not None:
                order = order[rng.permutation(order.size)]
            windows.append((group, order))
        return windows

    def _chunk_task(
        self, positions: np.ndarray, epoch: int, seed: int, augment: Augment | None
    ) -> dict[int, np.ndarray]:
        images = self._read_chunk(positions)
        out: dict[int, np.ndarray] = {}
        for p, img in zip(positions.tolist(), images, strict=True):
            if augment is not None and augment.active:
                img = augment.apply(img, np.random.default_rng([seed, epoch, p]))
            out[p] = img
        return out

    def batches(
        self,
        batch_size: int,
        *,
        epoch: int = 0,
        shuffle: bool = False,
        seed: int = 0,
        augment: Augment | None = None,
        drop_last: bool = False,
        shard: tuple[int, int] = (0, 1),
        prefetch: int = 2,
    ) -> Iterator[Batch]:
        """Batches of one epoch (see the module docstring for the order).

        ``shuffle=False`` keeps loader order. ``shard=(i, n)`` gives chunk ``k`` of the
        epoch to shard ``k % n``. ``prefetch`` batches are read ahead in a thread pool.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if epoch < 0 or seed < 0:
            raise ValueError("epoch and seed must be >= 0")
        i, n = shard
        if n < 1 or not 0 <= i < n:
            raise ValueError("shard must be (i, n) with 0 <= i < n")
        h, w = self.size
        c = self.channels
        windows = self._epoch_windows(epoch, shuffle, seed, (i, n))
        return self._batches(
            windows, batch_size, epoch, seed, augment, drop_last, prefetch, h, w, c
        )

    def _batches(
        self,
        windows: list[tuple[list[np.ndarray], np.ndarray]],
        batch_size: int,
        epoch: int,
        seed: int,
        augment: Augment | None,
        drop_last: bool,
        prefetch: int,
        h: int,
        w: int,
        c: int,
    ) -> Iterator[Batch]:
        chunks = [chunk for group, _ in windows for chunk in group]
        ahead = WINDOW_CHUNKS + max(1, -(-max(prefetch, 0) * batch_size // CHUNK_IMAGES))
        pool = ThreadPoolExecutor(max_workers=max(1, self._threads), thread_name_prefix="mapi-load")
        pending: deque[Future[dict[int, np.ndarray]]] = deque()
        submitted = 0
        try:

            def fill() -> None:
                nonlocal submitted
                while submitted < len(chunks) and len(pending) < ahead:
                    pending.append(
                        pool.submit(self._chunk_task, chunks[submitted], epoch, seed, augment)
                    )
                    submitted += 1

            buf: list[int] = []
            images: dict[int, np.ndarray] = {}
            for group, order in windows:
                fill()
                for _ in group:
                    images.update(pending.popleft().result())
                    fill()
                for p in order.tolist():
                    buf.append(p)
                    if len(buf) == batch_size:
                        yield self._make_batch(buf, images, h, w, c)
                        buf = []
            if buf and not drop_last:
                yield self._make_batch(buf, images, h, w, c)
        finally:
            for f in pending:
                f.cancel()
            pool.shutdown(wait=True, cancel_futures=True)

    def _make_batch(
        self, positions: list[int], images: dict[int, np.ndarray], h: int, w: int, c: int
    ) -> Batch:
        idx = np.array(positions, dtype=np.int64)
        pixels = np.empty((idx.size, h, w, c), dtype=np.uint8)
        for k, p in enumerate(positions):
            pixels[k] = images.pop(p)
        labels = self._labels[idx] if self._labels is not None else None
        return Batch(
            ids=self._ids[idx],
            pixels=pixels,
            labels=labels,
            codes=self._codes(labels) if labels is not None else None,
            index=idx,
        )

    # -- samples --------------------------------------------------------------------
    def _sample(self, max_samples: int | None, seed: int, stratify: str | None) -> np.ndarray:
        if max_samples is None or self.n <= max_samples:
            return np.arange(self.n, dtype=np.int64)
        rng = np.random.default_rng(seed)
        keys: np.ndarray | None = None
        if stratify is not None:
            if stratify == self._label_name and self._labels is not None:
                keys = self._labels
            elif stratify in self._coords:
                keys = self._coords[stratify]
        if keys is None:
            return np.sort(rng.choice(self.n, size=max_samples, replace=False)).astype(np.int64)
        levels: dict[str, list[int]] = {}
        for p, v in enumerate(keys.tolist()):
            levels.setdefault(str(v), []).append(p)
        names = sorted(levels)
        exact = [max_samples * len(levels[k]) / self.n for k in names]
        quota = [math.floor(q) for q in exact]
        rest = max_samples - sum(quota)
        by_remainder = sorted(range(len(names)), key=lambda j: (-(exact[j] - quota[j]), j))
        for j in by_remainder[:rest]:
            quota[j] += 1
        picked: list[int] = []
        for name, q in zip(names, quota, strict=True):
            members = np.array(levels[name], dtype=np.int64)
            picked.extend(rng.choice(members, size=min(q, members.size), replace=False).tolist())
        return np.sort(np.array(picked, dtype=np.int64))

    def to_xarray(
        self,
        max_samples: int | None = None,
        size: tuple[int, int] | None = None,
        *,
        seed: int = 0,
        stratify: str | None = "label",
        resize: str = "stretch",
    ) -> xr.Dataset:
        """A sample as the v0.14 image dataset: ``pixels(dim, y, x, channel)`` uint8, the
        label variable and the coords on the dim.

        All rows when ``n <= max_samples``; else a seeded sample that keeps the share of
        each level of ``stratify`` (a variable or coord of :meth:`manifest`), in loader
        order. ``size`` defaults to the loader's size.

        Raises:
            MapiError: ``PAYLOAD_TOO_LARGE`` above ``IMAGE_FOLDER_MAX_CELLS`` values.
        """
        loader = self.with_size(size, resize) if size is not None else self
        h, w = loader.size
        c = loader.channels
        positions = loader._sample(max_samples, seed, stratify)
        cells = positions.size * h * w * c
        if cells > IMAGE_FOLDER_MAX_CELLS:
            raise MapiError(
                f"the sample needs {cells:,} values. The maximum is {IMAGE_FOLDER_MAX_CELLS:,}",
                code=ErrorCode.PAYLOAD_TOO_LARGE,
                pointer="/data/collection/max_samples",
            )
        part = loader._take(positions)
        pixels = np.empty((part.n, h, w, c), dtype=np.uint8)
        for batch in part.batches(256, shuffle=False):
            pixels[batch.index] = batch.pixels
        ds = part.manifest()
        ds = ds.assign_coords(
            y=np.arange(h, dtype=np.int64),
            x=np.arange(w, dtype=np.int64),
            channel=np.array(_CHANNELS[part.mode], dtype=str),
        )
        ds[part._var] = xr.Variable((part.dim, "y", "x", "channel"), pixels, {IMAGE_ATTR: "1"})
        order = [part._var, *(v for v in ds.data_vars if v != part._var)]
        return ds[order]


def _runs(positions: np.ndarray, size: int = CHUNK_IMAGES) -> list[np.ndarray]:
    return [positions[k : k + size] for k in range(0, positions.size, size)]


def _to_mode(arr: np.ndarray, mode: str) -> np.ndarray:
    """uint8 ``(..., C)`` pixels as 1 (gray: Pillow's ITU-R 601-2 luma) or 3 channels."""
    c = arr.shape[-1]
    if mode == "rgb" and c == 1:
        return np.repeat(arr, 3, axis=-1)
    if mode == "gray" and c == 3:
        rgb = arr.astype(np.uint32)
        luma = (rgb[..., 0] * 19595 + rgb[..., 1] * 38470 + rgb[..., 2] * 7471 + 0x8000) >> 16
        return luma.astype(np.uint8)[..., None]
    return arr


# ======================================================================== in-memory loader


class ArrayImageLoader(_LoaderBase):
    """An :class:`ImageLoader` over an in-memory image dataset.

    ``ds[pixels]`` has the dim ``dim`` first and then ``(y, x, channel)`` or ``(y, x)``.
    ``uint8`` values are used as they are; floats in ``[0, 1]`` are scaled by 255, other
    floats and integers are rounded and must lie in ``[0, 255]``. ``target`` (a variable or
    coord along ``dim``) gives the labels when present; the other 1-D variables and coords
    along ``dim`` are the manifest.
    """

    def __init__(
        self,
        ds: xr.Dataset,
        *,
        pixels: str = "pixels",
        target: str | None = "label",
        dim: str = "image",
    ) -> None:
        if pixels not in ds.data_vars:
            raise MapiError(
                f"the dataset has no pixel variable {pixels!r}", code=ErrorCode.SCHEMA_INVALID
            )
        da = ds[pixels]
        if dim not in da.dims or da.ndim not in (3, 4):
            raise MapiError(
                f"the pixel variable must have the dims ({dim}, y, x[, channel])",
                code=ErrorCode.SCHEMA_INVALID,
            )
        da = da.transpose(dim, *(d for d in da.dims if d != dim))
        self._pixels = _as_uint8(np.asarray(da.values))
        if self._pixels.ndim == 3:
            self._pixels = self._pixels[..., None]
        if self._pixels.shape[-1] not in (1, 3):
            raise MapiError(
                "images must have 1 (gray) or 3 (rgb) channels", code=ErrorCode.SCHEMA_INVALID
            )
        self._dim = dim
        self._var = pixels
        self._label_name = target or LABEL_VAR
        self._ids = (
            _text_array(ds[dim].values.tolist())
            if dim in ds.coords
            else _text_array(range(da.sizes[dim]))
        )
        self._labels = (
            _text_array(ds[target].values.tolist())
            if target is not None and target in ds.variables and ds[target].dims == (dim,)
            else None
        )
        self._coords = {}
        for name, var in {**ds.coords, **ds.data_vars}.items():
            key = str(name)
            if key in (dim, pixels, target) or var.dims != (dim,):
                continue
            self._coords[key] = np.asarray(var.values)
        self._classes = None
        self._size = None
        self._resize = "stretch"
        self._mode = "rgb" if self._pixels.shape[-1] == 3 else "gray"
        self._threads = 2
        self._rows = np.arange(self._pixels.shape[0], dtype=np.int64)

    def _native_size(self) -> tuple[int, int] | None:
        return int(self._pixels.shape[1]), int(self._pixels.shape[2])

    def _take(self, positions: np.ndarray) -> Self:
        out = object.__new__(type(self))
        out.__dict__.update(self.__dict__)
        out._ids = self._ids[positions]
        out._labels = self._labels[positions] if self._labels is not None else None
        out._coords = {k: v[positions] for k, v in self._coords.items()}
        out._rows = self._rows[positions]
        return out

    def _read_chunk(self, positions: np.ndarray) -> list[np.ndarray]:
        size = self.size
        out = []
        for row in self._rows[positions].tolist():
            img = _to_mode(self._pixels[row], self._mode)
            if img.shape[:2] != size:
                img = resize_array(img, size, self._resize)
            out.append(img)
        return out


def _as_uint8(values: np.ndarray) -> np.ndarray:
    if values.dtype == np.uint8:
        return values
    if values.dtype.kind == "f":
        if not np.isfinite(values).all():
            raise MapiError("the pixel values must be finite", code=ErrorCode.SCHEMA_INVALID)
        if values.size and values.max() <= 1.0:
            values = values * 255.0
        values = np.rint(values)
    elif values.dtype.kind not in "iub":
        raise MapiError("the pixel values must be numbers", code=ErrorCode.SCHEMA_INVALID)
    if values.size and (values.min() < 0 or values.max() > 255):
        raise MapiError(
            "the pixel values must lie in 0..255 (or 0..1 for floats)",
            code=ErrorCode.SCHEMA_INVALID,
        )
    return values.astype(np.uint8)


# ===================================================================================== packs


class BlobSource(Protocol):
    """Reads byte ranges of packs (thread-safe)."""

    def read(self, pack: str, offset: int, size: int) -> bytes:
        """``size`` bytes of pack ``pack`` (digest hex) from ``offset``."""
        ...


@dataclass(frozen=True, slots=True)
class BlobLocation:
    """Where one image's bytes are: the pack, the data offset and the zip entry facts."""

    pack: str
    offset: int
    """Offset of the entry's data (after its local header)."""
    stored: int
    """Stored (compressed) bytes."""
    method: int
    """0 STORE or 8 DEFLATE."""
    crc32: int
    nbytes: int
    """Uncompressed bytes."""


@dataclass(frozen=True, slots=True)
class PartEntry:
    """One image entry of a part (or pack)."""

    sha256: str
    ext: str
    location: BlobLocation


def _part_error(message: str, name: str | None = None) -> CollectionError:
    return CollectionError(
        f"the image part is not a valid zip: {message}",
        untrusted_text=None if name is None else name[:300],
    )


def index_part(
    read: Callable[[int, int], bytes],
    size: int,
    *,
    pack: str,
    max_entries: int = MAX_PART_ENTRIES,
) -> list[PartEntry]:
    """The image entries of a zip of ``size`` bytes, read through ``read(offset, n)``.

    Parses the end-of-central-directory record and the central directory, then the local
    headers to find each entry's data offset (headers are read in windows of a few MiB).

    Raises:
        CollectionError: names not ``<sha256>.<ext>``, directories, encrypted entries,
            methods other than STORE/DEFLATE, duplicate names, entries larger than
            ``MAX_IMAGE_FILE_BYTES``, ZIP64 records, overlapping entries or data past
            ``size`` (the message names the entry).
    """
    if size < _EOCD_SIZE:
        raise _part_error("it is too short")
    tail_at = max(0, size - (_EOCD_SIZE + 0xFFFF))
    tail = read(tail_at, size - tail_at)
    if len(tail) != size - tail_at:
        raise _part_error("a read returned the wrong number of bytes")
    pos = tail.rfind(_EOCD)
    while pos >= 0 and pos + _EOCD_SIZE + int.from_bytes(
        tail[pos + 20 : pos + 22], "little"
    ) != len(tail):
        pos = tail.rfind(_EOCD, 0, pos)
    if pos < 0:
        raise _part_error("it has no end-of-central-directory record")
    (disk, cd_disk, n_disk, n_total, cd_size, cd_offset) = (
        int.from_bytes(tail[pos + a : pos + b], "little")
        for a, b in ((4, 6), (6, 8), (8, 10), (10, 12), (12, 16), (16, 20))
    )
    eocd_at = tail_at + pos
    if (
        0xFFFF in (disk, cd_disk, n_disk, n_total)
        or 0xFFFFFFFF in (cd_size, cd_offset)
        or (pos >= 20 and tail[pos - 20 : pos - 16] == _ZIP64_LOCATOR)
    ):
        raise _part_error("it uses ZIP64 records")
    if disk or cd_disk or n_disk != n_total:
        raise _part_error("it spans several disks")
    if n_total > max_entries:
        raise _part_error(f"it has {n_total:,} entries. The maximum is {max_entries:,}")
    if cd_offset + cd_size > eocd_at:
        raise _part_error("the central directory is outside the file")
    cd = read(cd_offset, cd_size) if cd_size else b""
    if len(cd) != cd_size:
        raise _part_error("a read returned the wrong number of bytes")
    central: list[tuple[str, bytes, int, int, int, int, int]] = []
    seen: set[str] = set()
    at = 0
    for _ in range(n_total):
        if cd[at : at + 4] != _CENTRAL or at + _CENTRAL_SIZE > len(cd):
            raise _part_error("the central directory is damaged")
        flags, method = (int.from_bytes(cd[at + a : at + a + 2], "little") for a in (8, 10))
        crc, stored, nbytes = (
            int.from_bytes(cd[at + a : at + a + 4], "little") for a in (16, 20, 24)
        )
        nlen, elen, clen = (int.from_bytes(cd[at + a : at + a + 2], "little") for a in (28, 30, 32))
        header_at = int.from_bytes(cd[at + 42 : at + 46], "little")
        raw_name = cd[at + _CENTRAL_SIZE : at + _CENTRAL_SIZE + nlen]
        extra = cd[at + _CENTRAL_SIZE + nlen : at + _CENTRAL_SIZE + nlen + elen]
        at += _CENTRAL_SIZE + nlen + elen + clen
        if at > len(cd):
            raise _part_error("the central directory is damaged")
        name = raw_name.decode("utf-8", errors="replace")
        if name.endswith("/"):
            raise _part_error("it has a directory entry", name)
        if flags & 0x1:
            raise _part_error("it has an encrypted entry", name)
        if not _ENTRY_RE.match(name):
            raise _part_error("an entry name is not <sha256>.<png|jpg|jpeg|tif|tiff>", name)
        if 0xFFFFFFFF in (stored, nbytes, header_at) or _has_zip64_extra(extra):
            raise _part_error("an entry uses ZIP64 records", name)
        if method not in (0, 8):
            raise _part_error(f"an entry uses the compression method {method}", name)
        if name in seen:
            raise _part_error("it has the same entry name two times", name)
        if nbytes > MAX_IMAGE_FILE_BYTES:
            raise _part_error(f"an entry has more than {MAX_IMAGE_FILE_BYTES:,} bytes", name)
        if method == 0 and stored != nbytes:
            raise _part_error("a stored entry has two different sizes", name)
        seen.add(name)
        central.append((name, raw_name, method, crc, stored, nbytes, header_at))
    central.sort(key=lambda e: e[6])
    out: list[PartEntry] = []
    window = b""
    window_at = 0
    for k, (name, raw_name, method, crc, stored, nbytes, header_at) in enumerate(central):
        need = _LOCAL_SIZE + len(raw_name)
        if header_at + need > cd_offset:
            raise _part_error("an entry's local header is outside the data", name)
        if not window_at <= header_at or header_at + need > window_at + len(window):
            window_at = header_at
            window = read(header_at, min(_HEADER_WINDOW, cd_offset - header_at))
        lo = header_at - window_at
        if window[lo : lo + 4] != _LOCAL or len(window) < lo + need:
            raise _part_error("an entry's local header is damaged", name)
        lmethod = int.from_bytes(window[lo + 8 : lo + 10], "little")
        lnlen, lelen = (int.from_bytes(window[lo + a : lo + a + 2], "little") for a in (26, 28))
        if lmethod != method or window[lo + _LOCAL_SIZE : lo + _LOCAL_SIZE + lnlen] != raw_name:
            raise _part_error("an entry's local header does not match the directory", name)
        data_at = header_at + _LOCAL_SIZE + lnlen + lelen
        if lelen:
            extra_at = lo + _LOCAL_SIZE + lnlen
            extra = window[extra_at : extra_at + lelen]
            if len(extra) != lelen:
                extra = read(header_at + _LOCAL_SIZE + lnlen, lelen)
            if _has_zip64_extra(extra):
                raise _part_error("an entry uses ZIP64 records", name)
        end = data_at + stored
        nxt = central[k + 1][6] if k + 1 < len(central) else cd_offset
        if end > size:
            raise _part_error("an entry's data is past the end of the file", name)
        if end > nxt:
            raise _part_error("two entries overlap", name)
        stem, ext = name.split(".", 1)
        out.append(PartEntry(stem, ext, BlobLocation(pack, data_at, stored, method, crc, nbytes)))
    return out


def _has_zip64_extra(extra: bytes) -> bool:
    at = 0
    while at + 4 <= len(extra):
        tag = int.from_bytes(extra[at : at + 2], "little")
        n = int.from_bytes(extra[at + 2 : at + 4], "little")
        if tag == 0x0001:
            return True
        at += 4 + n
    return False


def _unpack(raw: bytes, loc: BlobLocation, sha256: str | None) -> bytes:
    """The image bytes of a stored entry: inflated, CRC-32 and sha256 checked."""
    if len(raw) != loc.stored:
        raise _integrity(f"image {sha256 or ''} has {len(raw)} stored bytes, not {loc.stored}")
    if loc.method == 0:
        data = raw
    elif loc.method == 8:
        inflater = zlib.decompressobj(-15)
        try:
            data = inflater.decompress(raw, loc.nbytes + 1)
        except zlib.error as exc:
            raise _integrity(f"image {sha256 or ''} cannot be inflated", str(exc)) from None
        if not inflater.eof or inflater.unconsumed_tail:
            raise _integrity(f"image {sha256 or ''} does not inflate to {loc.nbytes} bytes")
    else:
        raise CollectionError(f"compression method {loc.method} is not supported")
    if len(data) != loc.nbytes:
        raise _integrity(f"image {sha256 or ''} does not have {loc.nbytes} bytes")
    if zlib.crc32(data) != loc.crc32:
        raise _integrity(f"image {sha256 or ''} does not match its CRC-32")
    if sha256 is not None:
        import hashlib

        if hashlib.sha256(data).hexdigest() != sha256:
            raise _integrity(f"image {sha256} does not match its content")
    return data


def read_blob(source: BlobSource, loc: BlobLocation, *, sha256: str | None = None) -> bytes:
    """The bytes of one image (inflated, CRC-32 checked, sha256 checked when given).

    Raises:
        MapiError: ``INTEGRITY_FAILURE`` when the bytes do not match.
    """
    return _unpack(source.read(loc.pack, loc.offset, loc.stored), loc, sha256)


class LocalPackSource:
    """A :class:`BlobSource` over local pack files ``{digest: path}``."""

    def __init__(self, packs: Mapping[str, Path]) -> None:
        self._packs = {k: Path(v) for k, v in packs.items()}

    def read(self, pack: str, offset: int, size: int) -> bytes:
        path = self._packs.get(pack)
        if path is None:
            raise _integrity("the collection reads a pack that it does not have", pack[:64])
        if offset < 0 or size < 0:
            raise ValueError("offset and size must be >= 0")
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read(size)
        if len(data) != size:
            raise _integrity("a pack is shorter than its manifest says", pack[:64])
        return data


# ================================================================================ manifests

_PA_TYPES: Final = {"str": "string", "int64": "int64", "int8": "int8", "int32": "int32"}


def write_manifest(df: pd.DataFrame) -> bytes:
    """``manifest.parquet`` bytes: the :data:`MANIFEST_FIXED` columns first with their
    types, then the other columns; no pandas index or metadata (deterministic)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    fixed = [name for name, _ in MANIFEST_FIXED]
    missing = [c for c in fixed if c not in df.columns]
    if missing:
        raise CollectionError(f"the manifest has no column {missing[0]!r}")
    arrays: list[Any] = []
    names: list[str] = []
    for name, dtype in MANIFEST_FIXED:
        values = df[name].tolist()
        if dtype == "str":
            values = [str(v) for v in values]
        arrays.append(pa.array(values, type=getattr(pa, _PA_TYPES[dtype])()))
        names.append(name)
    others = [str(c) for c in df.columns if c not in fixed]
    if others:
        rest = pa.Table.from_pandas(df[others], preserve_index=False)
        for name in others:
            arrays.append(rest.column(name))
            names.append(name)
    table = pa.Table.from_arrays(arrays, names=names).replace_schema_metadata(None)
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def read_manifest(data: bytes | Path, *, max_rows: int = MAX_COLLECTION_IMAGES) -> pd.DataFrame:
    """A ``manifest.parquet`` as a DataFrame (text columns as Python str).

    Raises:
        CollectionError: unreadable, too many rows, or the fixed columns missing.
    """
    import pyarrow.parquet as pq

    try:
        pf = pq.ParquetFile(io.BytesIO(data) if isinstance(data, bytes) else str(data))
        rows = pf.metadata.num_rows
        if rows > max_rows:
            raise CollectionError(f"the manifest has {rows:,} rows. The maximum is {max_rows:,}")
        table = pf.read()
    except MapiError:
        raise
    except Exception as exc:
        raise CollectionError(
            "the manifest is not a readable Parquet file", untrusted_text=str(exc)[:500]
        ) from None
    names = table.column_names
    fixed = [name for name, _ in MANIFEST_FIXED]
    if names[: len(fixed)] != fixed:
        raise CollectionError(
            "the manifest does not start with the columns " + ", ".join(fixed),
            untrusted_text=", ".join(names[:20])[:500],
        )
    return table.to_pandas()


def members_bin(shas: Iterable[str]) -> bytes:
    """``members.bin``: the sorted unique raw 32-byte sha256 digests."""
    digests = set()
    for s in shas:
        if not _SHA_RE.match(s):
            raise CollectionError(
                "a sha256 is not 64 lowercase hex characters", untrusted_text=s[:100]
            )
        digests.add(bytes.fromhex(s))
    return b"".join(sorted(digests))


def collection_schema(info: CollectionInfo, manifest: pd.DataFrame) -> DataSchema:
    """The DataSchema of a collection version: the image dims, the pixel variable (virtual:
    its values are in the packs, not in the manifest) and the label (when present)."""
    vars_: dict[str, VarSpec] = {
        info.var: VarSpec(
            dims=(info.dim, "y", "x", "channel"),
            dtype="uint8",
            role=Role.COVARIATE,
            missing="forbid",
            description="Pixel values (0 to 255) of the images (virtual: read from the packs)",
        )
    }
    if info.label and LABEL_VAR in manifest.columns:
        vars_[LABEL_VAR] = VarSpec(
            dims=(info.dim,),
            dtype="str",
            role=Role.TARGET,
            missing="allow",
            description="The class label of each image",
        )
    return DataSchema(
        dims={
            info.dim: DimSpec(
                kind="categorical", policy=DimPolicy.NEW_LEVELS, new_level_strategy="pooled"
            ),
            "y": DimSpec(kind="integer", policy=DimPolicy.FIXED),
            "x": DimSpec(kind="integer", policy=DimPolicy.FIXED),
            "channel": DimSpec(kind="categorical", policy=DimPolicy.FIXED),
        },
        vars=vars_,
    )


# =============================================================================== collection

_MANIFEST_COORDS: Final = ("split", "path", "sha256", "width", "height")


@dataclass(slots=True)
class Collection:
    """A collection version: its info, manifest, pack source and schema."""

    info: CollectionInfo
    manifest: pd.DataFrame
    source: BlobSource
    schema: DataSchema | None = None

    def __post_init__(self) -> None:
        if self.schema is None:
            self.schema = collection_schema(self.info, self.manifest)

    @classmethod
    def open(
        cls,
        info: bytes | CollectionInfo,
        manifest: bytes | Path,
        source: BlobSource,
        schema: DataSchema | None = None,
        *,
        max_rows: int = MAX_COLLECTION_IMAGES,
    ) -> Collection:
        """Parse and check a version's ``collection.json`` and ``manifest.parquet``.

        Raises:
            CollectionError: bad columns or types, duplicate ids, bad sha256 or pack
                values, packs not in the info, or a row count other than ``n_images``.
        """
        if not isinstance(info, CollectionInfo):
            try:
                info = CollectionInfo.model_validate_json(info)
            except ValueError as exc:
                raise CollectionError(
                    "collection.json is not valid", untrusted_text=str(exc)[:1000]
                ) from None
        df = read_manifest(manifest, max_rows=max_rows)
        _check_manifest(df, info)
        return cls(info=info, manifest=df, source=source, schema=schema)

    def _columns(self) -> list[str]:
        cols = [c for c in _MANIFEST_COORDS if c in self.manifest.columns]
        cols += [c for c in self.info.columns if c in self.manifest.columns and c not in cols]
        return cols

    def manifest_dataset(self) -> xr.Dataset:
        """The manifest as a 1-D dataset along ``info.dim`` (labels = ``image_id``): the
        label variable and the coords split/path/sha256/width/height and user columns."""
        df = self.manifest
        dim = self.info.dim
        data_vars: dict[str, Any] = {}
        if self.info.label and LABEL_VAR in df.columns:
            data_vars[LABEL_VAR] = (
                (dim,),
                np.array(_text_array(df[LABEL_VAR]).tolist(), dtype=str),
            )
        coords: dict[str, Any] = {dim: np.array([str(v) for v in df["image_id"]], dtype=str)}
        for col in self._columns():
            coords[col] = (dim, _coord_values(df[col]))
        return xr.Dataset(data_vars, coords=coords)

    def loader(
        self,
        ids: np.ndarray | Sequence[str] | None = None,
        *,
        cache_dir: Path | None = None,
        cache_bytes: int = 0,
        threads: int = 8,
    ) -> CollectionLoader:
        """A lazy loader of the images ``ids`` (in that order; all in manifest order when
        ``None``). ``cache_dir``/``cache_bytes`` keep read chunks on disk for later epochs."""
        loader = CollectionLoader(
            self, cache_dir=cache_dir, cache_bytes=cache_bytes, threads=threads
        )
        return loader if ids is None else loader.subset(ids)


def _check_manifest(df: pd.DataFrame, info: CollectionInfo) -> None:
    for name, dtype in MANIFEST_FIXED:
        col = df[name]
        if dtype == "str":
            ok = col.map(lambda v: isinstance(v, str)).all()
        else:
            ok = pd.api.types.is_integer_dtype(col)
        if not ok:
            raise CollectionError(f"the manifest column {name!r} does not have the type {dtype}")
    if len(df) != info.n_images:
        raise CollectionError(
            f"the manifest has {len(df):,} rows, but collection.json says {info.n_images:,}"
        )
    ids = df["image_id"]
    if (ids == "").any() or ids.duplicated().any():
        dup = ids[ids.duplicated() | (ids == "")].iloc[0]
        raise CollectionError(
            "the image ids must be unique and not empty", untrusted_text=str(dup)[:200]
        )
    for col in ("sha256", "pack"):
        bad = ~df[col].str.fullmatch(r"[0-9a-f]{64}")
        if bad.any():
            raise CollectionError(
                f"the manifest column {col!r} has a value that is not a sha256",
                untrusted_text=str(df[col][bad].iloc[0])[:100],
            )
    known = {p.digest for p in info.packs}
    unknown = set(df["pack"].unique()) - known
    if unknown:
        raise CollectionError(
            "the manifest names packs that collection.json does not list",
            untrusted_text=", ".join(sorted(unknown)[:5]),
        )
    if not df["method"].isin([0, 8]).all():
        raise CollectionError("the manifest has a compression method other than 0 or 8")
    if (df["offset"] < 0).any() or (df["stored"] < 0).any():
        raise CollectionError("the manifest has a negative offset or size")
    if (df["nbytes"] < 0).any() or (df["nbytes"] > MAX_IMAGE_FILE_BYTES).any():
        raise CollectionError(f"an image has more than {MAX_IMAGE_FILE_BYTES:,} bytes")


# =================================================================== the collection loader


class _SpanCache:
    """Chunk bytes on local disk (``cache_dir``), up to ``budget`` bytes, shared by the
    threads of one loader."""

    def __init__(self, root: Path | None, budget: int) -> None:
        self.root = root
        self.budget = budget if root is not None else 0
        self.used = 0
        self.lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        if not self.budget or self.root is None:
            return None
        path = self.root / key
        try:
            return path.read_bytes()
        except OSError:
            return None

    def put(self, key: str, data: bytes) -> None:
        if not self.budget or self.root is None:
            return
        with self.lock:
            if self.used + len(data) > self.budget:
                return
            self.used += len(data)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f".{key}.{threading.get_ident()}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, self.root / key)


class CollectionLoader(_LoaderBase):
    """A lazy :class:`ImageLoader` over a :class:`Collection` (from
    :meth:`Collection.loader`). The size defaults to ``info.size`` and the mode to
    ``info.mode``; bytes are read in chunk spans (one ``source.read`` per span of at most
    64 MiB), decoded in a thread pool and verified (CRC-32 and sha256)."""

    def __init__(
        self,
        collection: Collection,
        *,
        cache_dir: Path | None = None,
        cache_bytes: int = 0,
        threads: int = 8,
    ) -> None:
        info, df = collection.info, collection.manifest
        self._collection = collection
        self._dim = info.dim
        self._var = info.var
        self._label_name = LABEL_VAR
        self._ids = _text_array(df["image_id"].tolist())
        self._labels = (
            _text_array(df[LABEL_VAR].tolist()) if info.label and LABEL_VAR in df.columns else None
        )
        self._coords = {c: _coord_values(df[c]) for c in collection._columns()}
        self._classes = None
        self._size = None
        self._resize = "stretch"
        self._mode = info.mode
        self._threads = max(1, int(threads))
        self._rows = np.arange(len(df), dtype=np.int64)
        self._sha = df["sha256"].to_numpy(dtype=object)
        self._pack = df["pack"].to_numpy(dtype=object)
        self._loc = df[["offset", "stored", "method", "crc32", "nbytes"]].to_numpy(dtype=np.int64)
        self._cache = _SpanCache(cache_dir, int(cache_bytes))

    @property
    def collection(self) -> Collection:
        return self._collection

    def _native_size(self) -> tuple[int, int] | None:
        size = self._collection.info.size
        return (int(size[0]), int(size[1])) if size is not None else None

    def _take(self, positions: np.ndarray) -> Self:
        out = object.__new__(type(self))
        out.__dict__.update(self.__dict__)
        out._ids = self._ids[positions]
        out._labels = self._labels[positions] if self._labels is not None else None
        out._coords = {k: v[positions] for k, v in self._coords.items()}
        out._rows = self._rows[positions]
        return out

    def _io_chunks(self) -> list[np.ndarray]:
        """Chunks of at most 64 images next to each other in one pack (by offset)."""
        positions = np.arange(self.n, dtype=np.int64)
        rows = self._rows
        packs = self._pack[rows]
        offsets = self._loc[rows, 0]
        order = np.lexsort((offsets, packs.astype(str)))
        chunks: list[np.ndarray] = []
        start = 0
        for k in range(1, order.size + 1):
            if (
                k == order.size
                or packs[order[k]] != packs[order[start]]
                or k - start == CHUNK_IMAGES
            ):
                chunks.append(positions[order[start:k]])
                start = k
        return chunks

    def _location(self, row: int) -> BlobLocation:
        offset, stored, method, crc, nbytes = (int(v) for v in self._loc[row])
        return BlobLocation(str(self._pack[row]), offset, stored, method, crc, nbytes)

    def _read_span(self, pack: str, start: int, end: int) -> bytes:
        key = f"{pack}.{start}.{end}"
        cached = self._cache.get(key)
        if cached is not None and len(cached) == end - start:
            return cached
        data = self._collection.source.read(pack, start, end - start)
        if len(data) != end - start:
            raise _integrity("a pack read returned the wrong number of bytes", pack[:64])
        self._cache.put(key, data)
        return data

    def _blobs(self, rows: Sequence[int]) -> dict[int, bytes]:
        """The verified bytes of ``rows`` (manifest rows), read in merged spans."""
        by_pack: dict[str, list[int]] = {}
        for r in rows:
            by_pack.setdefault(str(self._pack[r]), []).append(r)
        out: dict[int, bytes] = {}
        for pack, members in by_pack.items():
            members.sort(key=lambda r: int(self._loc[r, 0]))
            span: list[int] = []
            start = end = 0
            for r in [*members, -1]:
                if r >= 0:
                    lo = int(self._loc[r, 0])
                    hi = lo + int(self._loc[r, 1])
                    if span and lo - end <= _SPAN_GAP and max(hi, end) - start <= MAX_SPAN_BYTES:
                        span.append(r)
                        end = max(end, hi)
                        continue
                if span:
                    data = self._read_span(pack, start, end)
                    for s in span:
                        loc = self._location(s)
                        raw = data[loc.offset - start : loc.offset - start + loc.stored]
                        out[s] = _unpack(raw, loc, str(self._sha[s]))
                if r >= 0:
                    span, start, end = [r], lo, hi
        return out

    def _read_chunk(self, positions: np.ndarray) -> list[np.ndarray]:
        size = self.size
        rows = self._rows[positions].tolist()
        blobs = self._blobs(sorted(set(rows)))
        return [
            decode_image(blobs[r], mode=self._mode, size=size, resize=cast(Any, self._resize))
            for r in rows
        ]


# ============================================================================ dataset reads


def collection_read(
    collection: Collection, read: CollectionRead | None, *, seed: int
) -> xr.Dataset:
    """A seeded sample of a collection as the v0.14 image dataset, for a model that reads
    datasets (``data.collection``; defaults when ``read`` is ``None``).

    Raises:
        MapiError: ``SCHEMA_INVALID`` (pointer ``/data/collection/size``) when neither the
            read nor the collection gives a size.
    """
    read = read or CollectionRead()
    size = read.size or collection.info.size
    if size is None:
        raise MapiError(
            "the images of the collection do not have one size: set data.collection.size",
            code=ErrorCode.SCHEMA_INVALID,
            pointer="/data/collection/size",
        )
    loader = collection.loader()
    if read.mode != "auto":
        loader = loader.with_mode(read.mode)
    return loader.to_xarray(
        read.max_samples,
        (int(size[0]), int(size[1])),
        seed=read.seed if read.seed is not None else seed,
        stratify=read.stratify,
        resize=read.resize,
    )
