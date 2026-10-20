"""Image test data (v0.15): small synthetic images, image parts and local collections.

Everything here is deterministic for a seed and tiny by default (8 images per class of
12 x 12 pixels), so tests of loaders, ingests and image models run in seconds:

* :func:`shape_images` - PNG bytes of circles, squares and crosses (one class each);
* :func:`build_parts` - deterministic STORE zips of images named ``<sha256>.<ext>`` (the
  client part format);
* :func:`local_collection` - a :class:`~mapi_sdk.collection.Collection` on local files
  (packs, manifest, info) with a stratified ``train``/``test`` split;
* :func:`image_dataset` - the same images as an in-memory v0.14 image dataset.
"""

from __future__ import annotations

import base64
import hashlib
import io
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import google_crc32c
import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.collection import (
    DEFAULT_PART_BYTES,
    MAX_PART_ENTRIES,
    ClassCount,
    CollectionInfo,
    PackInfo,
    part_input,
)

from mapi_sdk.collection import (
    LABEL_VAR,
    Collection,
    LocalPackSource,
    PartEntry,
    collection_schema,
    index_part,
    write_manifest,
)
from mapi_sdk.media import encode_png

__all__ = ["build_parts", "image_dataset", "local_collection", "shape_images"]

_SHAPES = ("circle", "square", "cross", "hline", "vline", "diag")
_COLOURS = ((1.0, 0.3, 0.3), (0.3, 1.0, 0.3), (0.3, 0.3, 1.0), (1.0, 1.0, 0.3), (0.3, 1.0, 1.0))
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def _shape(kind: str, size: int, rng: np.random.Generator) -> np.ndarray:
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    c = (size - 1) / 2
    cy, cx = c + rng.uniform(-size / 10, size / 10, size=2)
    r = size * rng.uniform(0.25, 0.35)
    dy, dx = yy - cy, xx - cx
    match kind:
        case "circle":
            mask = dy**2 + dx**2 <= r**2
        case "square":
            mask = (np.abs(dy) <= r * 0.8) & (np.abs(dx) <= r * 0.8)
        case "cross":
            t = max(r / 3, 0.75)
            mask = ((np.abs(dy) <= t) & (np.abs(dx) <= r)) | ((np.abs(dx) <= t) & (np.abs(dy) <= r))
        case "hline":
            mask = np.abs(dy) <= max(r / 3, 0.75)
        case "vline":
            mask = np.abs(dx) <= max(r / 3, 0.75)
        case _:
            mask = np.abs(dy - dx) <= max(r / 3, 0.75)
    return np.asarray(mask, dtype=np.float64)


def shape_images(
    n_per_class: int = 8,
    size: int = 12,
    classes: Sequence[str] = ("circle", "square", "cross"),
    rgb: bool = False,
    noise: float = 0.1,
    seed: int = 0,
) -> tuple[list[bytes], list[str]]:
    """``(png_bytes, labels)``: ``n_per_class`` images of each class, classes interleaved.

    Class ``k`` draws its own shape (``circle``/``square``/``cross``/... by name, else by
    position) with a random centre and radius, plus Gaussian ``noise``; with ``rgb`` each
    class has its own colour. Every image is distinct (a repeat is perturbed).
    """
    rng = np.random.default_rng(seed)
    images: list[bytes] = []
    labels: list[str] = []
    seen: set[bytes] = set()
    for _ in range(n_per_class):
        for k, label in enumerate(classes):
            kind = label if label in _SHAPES else _SHAPES[k % len(_SHAPES)]
            img = _shape(kind, size, rng)
            img = np.clip(img + rng.normal(0.0, noise, size=img.shape), 0.0, 1.0)
            if rgb:
                colour = np.array(_COLOURS[k % len(_COLOURS)])
                arr = img[..., None] * colour
            else:
                arr = img[..., None]
            px = np.rint(arr * 255).astype(np.uint8)
            png, _, _ = encode_png(px)
            while png in seen:  # keep images distinct: one sha256 per image
                px[int(rng.integers(size)), int(rng.integers(size)), 0] ^= 1
                png, _, _ = encode_png(px)
            seen.add(png)
            images.append(png)
            labels.append(str(label))
    return images, labels


def _zip(entries: Sequence[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.create_system = 0
            info.external_attr = 0
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, data)
    return buf.getvalue()


def build_parts(
    images: Sequence[bytes], *, ext: str = "png", part_bytes: int = DEFAULT_PART_BYTES
) -> list[bytes]:
    """Deterministic STORE zips of the distinct ``images``, entries ``<sha256>.<ext>``
    sorted by sha256, each zip at most about ``part_bytes`` (at least one entry)."""
    by_sha = {hashlib.sha256(data).hexdigest(): data for data in images}
    parts: list[bytes] = []
    current: list[tuple[str, bytes]] = []
    used = 22
    for sha in sorted(by_sha):
        name = f"{sha}.{ext}"
        data = by_sha[sha]
        cost = len(data) + 30 + 46 + 2 * len(name)
        if current and (used + cost > part_bytes or len(current) >= MAX_PART_ENTRIES):
            parts.append(_zip(current))
            current, used = [], 22
        current.append((name, data))
        used += cost
    if current:
        parts.append(_zip(current))
    return parts


def _reader(data: bytes) -> Callable[[int, int], bytes]:
    def read(offset: int, n: int) -> bytes:
        return data[offset : offset + n]

    return read


def _b64(digest: bytes) -> str:
    return base64.b64encode(digest).decode("ascii")


def local_collection(
    root: Path,
    *,
    n_per_class: int = 8,
    size: int = 12,
    classes: Sequence[str] = ("circle", "square", "cross"),
    rgb: bool = False,
    test_fraction: float = 0.25,
    seed: int = 0,
    part_bytes: int = 64 * 1024,
) -> Collection:
    """A collection of :func:`shape_images` on local files under ``root``.

    Packs are the :func:`build_parts` zips, named by the sha256 of their bytes (a stand-in
    for the server's pack digest). Image ids are the paths ``<label>/<label>_<j>.png``;
    the manifest has ``label``, a stratified ``split`` (``train``/``test``) and ``path``.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    images, labels = shape_images(n_per_class, size, classes, rgb=rgb, seed=seed)
    parts = build_parts(images, part_bytes=part_bytes)
    where: dict[str, PartEntry] = {}
    packs: dict[str, Path] = {}
    infos: list[PackInfo] = []
    for i, part in enumerate(parts):
        digest = hashlib.sha256(part).hexdigest()
        path = root / f"{digest}.zip"
        path.write_bytes(part)
        packs[digest] = path
        entries = index_part(_reader(part), len(part), pack=digest)
        for e in entries:
            where[e.sha256] = e
        crc = google_crc32c.Checksum(part).digest()  # type: ignore[no-untyped-call,unused-ignore]
        infos.append(
            PackInfo(
                digest=digest,
                size=len(part),
                crc32c=_b64(crc),
                md5=_b64(hashlib.md5(part).digest()),
                entries=len(entries),
                images=len(entries),
                source="upload",
                input=part_input(i),
            )
        )
    rng = np.random.default_rng(seed)
    split = np.full(len(images), "train", dtype=object)
    for label in classes:
        members = np.array([i for i, v in enumerate(labels) if v == label])
        k = round(members.size * test_fraction)
        split[rng.permutation(members)[:k]] = "test"
    rows: list[dict[str, Any]] = []
    counters: dict[str, int] = {}
    for i, (data, label) in enumerate(zip(images, labels, strict=True)):
        sha = hashlib.sha256(data).hexdigest()
        loc = where[sha].location
        j = counters.get(label, 0)
        counters[label] = j + 1
        name = f"{label}/{label}_{j:03d}.png"
        rows.append(
            {
                "image_id": name,
                "sha256": sha,
                "pack": loc.pack,
                "offset": loc.offset,
                "stored": loc.stored,
                "method": loc.method,
                "crc32": loc.crc32,
                "nbytes": loc.nbytes,
                "format": "png",
                "width": size,
                "height": size,
                "mode": "RGB" if rgb else "L",
                LABEL_VAR: label,
                "split": str(split[i]),
                "path": name,
            }
        )
    df = pd.DataFrame(rows)
    raw = write_manifest(df)
    counts = pd.Series(labels).value_counts()
    info = CollectionInfo(
        n_images=len(images),
        dim="image",
        var="pixels",
        label=True,
        split=True,
        classes=[
            ClassCount(label=str(k), count=int(v))
            for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
        ],
        splits={str(k): int(v) for k, v in pd.Series(split).value_counts().sort_index().items()},
        mode="rgb" if rgb else "gray",
        size=(size, size),
        min_size=(size, size),
        max_size=(size, size),
        formats={"png": len(images)},
        total_bytes=sum(len(d) for d in images),
        packs=infos,
        new_images=len(images),
        reused_images=0,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
    )
    collection = Collection.open(info, raw, LocalPackSource(packs))
    collection.schema = collection_schema(info, collection.manifest)
    return collection


def image_dataset(**kw: Any) -> xr.Dataset:
    """:func:`local_collection` (same keywords, without ``root``) as an in-memory image
    dataset: ``pixels(image, y, x, channel)`` uint8, ``label(image)``, coords split/path."""
    with tempfile.TemporaryDirectory(prefix="mapi-images-") as tmp:
        return local_collection(Path(tmp), **kw).loader(threads=1).to_xarray().load()
