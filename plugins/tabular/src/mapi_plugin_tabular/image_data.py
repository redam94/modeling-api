"""Synthetic small images for ``tabular.image_logistic`` (v0.14 examples, docs and tests).

:func:`shapes_dataset` draws noisy pictures of simple shapes (a circle, a square, a cross by
default) in the layout an ``image_folder`` ingest writes (``mapi_spec.ingest``,
``ImageFolderOptions``)::

    dims    image ("000".."179"), y (0..H-1), x (0..W-1), channel (["gray"] or ["r","g","b"])
    vars    pixels(image, y, x, channel) uint8, attrs {"mapi_image": "1"}
            label(image) str (the shape)
    coords  path(image) str, split(image) str ("train"/"test", a stratified 25 % test share)

:func:`shapes_zip` writes the same images as PNG files plus a ``manifest.csv`` (columns
``path``, ``label``, ``split``) into a zip, the upload an ``image_folder`` ingest reads.
:func:`shapes_schema` is the raw schema such an ingest writes (new images are allowed).
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
from mapi_spec import DataSchema

__all__ = ["SHAPES", "shapes_dataset", "shapes_schema", "shapes_zip"]

SHAPES: tuple[str, ...] = ("circle", "square", "cross", "ring", "triangle")
"""Every shape :func:`shapes_dataset` can draw."""

_COLOURS = np.array(
    [[230, 80, 60], [60, 160, 230], [90, 200, 90], [240, 200, 60], [180, 90, 220]], dtype=float
)


def _mask(shape: str, size: int, rng: np.random.Generator) -> np.ndarray:
    """A ``(size, size)`` boolean mask of ``shape`` at a jittered centre and scale."""
    yy, xx = np.mgrid[0:size, 0:size].astype(float)
    cy, cx = (size - 1) / 2 + rng.uniform(-0.06, 0.06, 2) * size
    r = rng.uniform(0.3, 0.4) * size
    dy, dx = yy - cy, xx - cx
    match shape:
        case "circle":
            return np.asarray(dy**2 + dx**2 <= r**2)
        case "square":
            return np.asarray((np.abs(dy) <= 0.85 * r) & (np.abs(dx) <= 0.85 * r))
        case "cross":
            w = max(0.28 * r, 0.75)
            return np.asarray(
                ((np.abs(dy) <= w) & (np.abs(dx) <= r)) | ((np.abs(dx) <= w) & (np.abs(dy) <= r))
            )
        case "ring":
            d = np.sqrt(dy**2 + dx**2)
            return np.asarray((d <= r) & (d >= 0.55 * r))
        case "triangle":
            return np.asarray((dy <= 0.8 * r) & (dy >= -r + 1.6 * np.abs(dx)))
    raise ValueError(f"unknown shape {shape!r}; choose from {', '.join(SHAPES)}")


def shapes_dataset(
    n_per_class: int = 60,
    size: int = 16,
    classes: Sequence[str] = ("circle", "square", "cross"),
    rgb: bool = False,
    noise: float = 0.15,
    seed: int = 0,
) -> xr.Dataset:
    """``n_per_class`` noisy ``size x size`` pictures of each shape in ``classes``.

    Gray images draw a bright shape on a dark background; ``rgb`` images give every image a
    random shape colour. ``noise`` is the sd of the Gaussian pixel noise on the [0, 1] scale.
    A stratified 25 % of each class is the ``test`` split (coord ``split``).
    """
    if n_per_class < 1 or size < 4:
        raise ValueError("n_per_class must be at least 1 and size at least 4")
    rng = np.random.default_rng(seed)
    n = n_per_class * len(classes)
    channels = ["r", "g", "b"] if rgb else ["gray"]
    pixels = np.empty((n, size, size, len(channels)), dtype=np.uint8)
    labels: list[str] = []
    split = np.full(n, "train", dtype=object)
    i = 0
    for c in classes:
        n_test = round(n_per_class * 0.25)
        test = set(rng.choice(n_per_class, size=n_test, replace=False).tolist())
        for j in range(n_per_class):
            mask = _mask(c, size, rng)[..., None]
            if rgb:
                colour = _COLOURS[rng.integers(len(_COLOURS))] / 255 * rng.uniform(0.7, 1.0)
                img = np.where(mask, colour[None, None, :], 0.08)
            else:
                img = np.where(mask, rng.uniform(0.7, 1.0), 0.1)
            img = img + rng.normal(0.0, noise, img.shape)
            pixels[i] = np.clip(np.round(img * 255), 0, 255).astype(np.uint8)
            labels.append(c)
            if j in test:
                split[i] = "test"
            i += 1
    width = len(str(n - 1))
    ids = [str(k).zfill(width) for k in range(n)]
    return xr.Dataset(
        {
            "pixels": (("image", "y", "x", "channel"), pixels, {"mapi_image": "1"}),
            "label": (("image",), np.array(labels, dtype=object).astype(str)),
        },
        coords={
            "image": ids,
            "y": np.arange(size, dtype=np.int64),
            "x": np.arange(size, dtype=np.int64),
            "channel": channels,
            "path": (("image",), np.array([f"images/{k}.png" for k in ids])),
            "split": (("image",), split.astype(str)),
        },
    )


def shapes_schema(ds: xr.Dataset) -> DataSchema:
    """The raw schema of :func:`shapes_dataset`: ``label`` is the target, the ``image`` dim
    takes new images (``new_levels``, strategy ``pooled``) as an ``image_folder`` ingest
    declares it; ``y``, ``x`` and ``channel`` are fixed."""
    from mapi_sdk.schema import infer_schema

    schema = infer_schema(ds, targets=["label"], policies={"image": "new_levels"})
    image = schema.dims["image"].model_copy(update={"new_level_strategy": "pooled"})
    label = schema.vars["label"].model_copy(update={"missing": "allow"})
    return schema.model_copy(
        update={"dims": {**schema.dims, "image": image}, "vars": {**schema.vars, "label": label}}
    )


def shapes_zip(path: str | Path, **kw: Any) -> Path:
    """Write :func:`shapes_dataset` (``**kw``) as PNG files and ``manifest.csv`` into a zip."""
    from mapi_sdk.media import encode_png

    ds = shapes_dataset(**kw)
    out = Path(path)
    manifest = io.StringIO()
    writer = csv.writer(manifest, lineterminator="\n")
    writer.writerow(["path", "label", "split"])
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for k in range(ds.sizes["image"]):
            img = ds["pixels"].values[k]
            png, _, _ = encode_png(img[..., 0] if img.shape[-1] == 1 else img)
            name = str(ds["path"].values[k])
            zf.writestr(name, png)
            writer.writerow([name, str(ds["label"].values[k]), str(ds["split"].values[k])])
        zf.writestr("manifest.csv", manifest.getvalue())
    return out
