"""Image decoding (v0.15): untrusted PNG, JPEG and TIFF bytes to ``uint8`` pixel arrays.

The rules are those of the ``image_folder`` ingest (v0.14), so an image decodes to the same
pixels in an ingest, a collection loader and a model:

* only the PNG, JPEG and TIFF decoders run, whatever the bytes say;
* the pixel count is checked from the header before anything is decoded, and Pillow's
  ``DecompressionBombWarning`` is an error;
* 8-bit modes only (``1``, ``L``, ``LA``, ``P``, ``RGB``, ``RGBA``, ``CMYK``, ``YCbCr``);
  16-bit, 32-bit and float images are refused;
* alpha is dropped, palettes and CMYK/YCbCr are converted; EXIF orientation is NOT applied
  (the pixels are used as stored);
* resizing is bilinear: ``stretch``, ``crop`` (centre crop to the target aspect) or ``pad``
  (fit inside, zero padding).

Pillow is imported lazily (the ``mapi-sdk[images]`` extra).
"""

from __future__ import annotations

import io
import threading
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

import numpy as np
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.ingest import MAX_IMAGE_PIXELS

__all__ = [
    "ImageDecodeError",
    "ImageHeader",
    "decode_image",
    "image_header",
    "resize_array",
]

ImageFormat = Literal["png", "jpeg", "tiff"]
ResizeMode = Literal["stretch", "crop", "pad"]

_PIL_FORMATS: Final = ("PNG", "JPEG", "TIFF")
_GRAY_MODES: Final = frozenset({"1", "L", "LA"})
_COLOUR_MODES: Final = frozenset({"P", "RGB", "RGBA", "CMYK", "YCbCr"})
_RESIZE: Final = ("stretch", "crop", "pad")


class ImageDecodeError(MapiError):
    """Image bytes that cannot be read, are too large or have an unsupported mode."""

    default_code = ErrorCode.SCHEMA_INVALID


@dataclass(frozen=True, slots=True)
class ImageHeader:
    """What the header of an image says (nothing decoded)."""

    format: ImageFormat
    width: int
    height: int
    mode: str
    colour: bool
    """Whether the image has colour (a palette or a colour mode)."""


_PILLOW_LOCK: Final = threading.Lock()


@contextmanager
def _pillow(max_pixels: int) -> Iterator[Any]:
    """Pillow, safe to use from many threads (the collection loader decodes in a pool).

    The pixel cap is checked here from the header (:func:`_open`), so Pillow's own
    decompression-bomb limit (a module global, by default far above
    :data:`~mapi_spec.ingest.MAX_IMAGE_PIXELS`) is left alone. Only a cap above Pillow's limit
    raises the global, under a lock, with the bomb warning as an error.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - the images extra is not installed
        raise MapiError(
            "decoding images needs Pillow: install mapi-sdk[images]",
            code=ErrorCode.INTERNAL_ERROR,
        ) from None
    limit = Image.MAX_IMAGE_PIXELS
    if limit is not None and max_pixels <= limit:
        yield Image
        return
    with _PILLOW_LOCK:
        before = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = max_pixels
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                yield Image
        finally:
            Image.MAX_IMAGE_PIXELS = before


def _too_large(message: str) -> ImageDecodeError:
    return ImageDecodeError(message, code=ErrorCode.PAYLOAD_TOO_LARGE)


def _open(Image: Any, data: bytes, max_pixels: int) -> Any:
    try:
        im = Image.open(io.BytesIO(data), formats=_PIL_FORMATS)
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise _too_large(f"the image has more than {max_pixels:,} pixels") from None
    except Exception as exc:
        raise ImageDecodeError(
            "the image is not a readable PNG, JPEG or TIFF file",
            untrusted_text=f"{type(exc).__name__}: {exc}"[:500],
        ) from None
    w, h = im.size
    if w * h > max_pixels:
        im.close()
        raise _too_large(f"the image has {w:,} x {h:,} pixels. The maximum is {max_pixels:,}")
    if im.mode not in _GRAY_MODES | _COLOUR_MODES:
        mode = im.mode
        im.close()
        raise ImageDecodeError(
            f"the image has the pixel mode {mode!r} (16-bit, 32-bit or float values); only "
            "8-bit images are read"
        )
    return im


def image_header(data: bytes, *, max_pixels: int = MAX_IMAGE_PIXELS) -> ImageHeader:
    """The header of an image (Pillow opens it lazily; no pixel is decoded).

    Raises:
        ImageDecodeError: unreadable (``SCHEMA_INVALID``), too many pixels
            (``PAYLOAD_TOO_LARGE``) or not 8-bit.
    """
    with _pillow(max_pixels) as Image:
        im = _open(Image, data, max_pixels)
        try:
            fmt = cast(ImageFormat, str(im.format).lower())
            w, h = im.size
            return ImageHeader(fmt, int(w), int(h), str(im.mode), im.mode in _COLOUR_MODES)
        finally:
            im.close()


def _convert(im: Any, rgb: bool) -> Any:
    """``im`` as ``L`` (gray) or ``RGB``: alpha dropped, palettes and CMYK/YCbCr converted."""
    if im.mode == "P":
        im = im.convert("RGBA" if "transparency" in im.info else "RGB")
    if rgb:
        return im if im.mode == "RGB" else im.convert("RGB")
    return im if im.mode == "L" else im.convert("L")


def _fit(Image: Any, im: Any, size: tuple[int, int], how: str) -> Any:
    """``im`` resized to ``size`` (height, width) as the ``image_folder`` ingest does."""
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
        canvas = Image.new(im.mode, (tw, th), 0)
        canvas.paste(inner, ((tw - nw) // 2, (th - nh) // 2))
        return canvas
    return im.resize((tw, th), Image.BILINEAR)


def _check_size(size: tuple[int, int] | None, resize: str) -> None:
    if resize not in _RESIZE:
        raise ValueError(f"resize must be one of {', '.join(_RESIZE)}, not {resize!r}")
    if size is not None and (len(size) != 2 or min(size) < 1):
        raise ValueError(f"size must be (height, width) with both >= 1, not {size!r}")


def decode_image(
    data: bytes,
    *,
    mode: Literal["gray", "rgb"],
    size: tuple[int, int] | None = None,
    resize: ResizeMode = "stretch",
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> np.ndarray:
    """The pixels of an image as ``uint8 (H, W, C)`` (``C`` = 1 gray, 3 rgb), resized to
    ``size`` (height, width) when given.

    Raises:
        ImageDecodeError: as :func:`image_header`, or when the pixel data is damaged.
    """
    _check_size(size, resize)
    with _pillow(max_pixels) as Image:
        im = _open(Image, data, max_pixels)
        try:
            im.load()
            out = _convert(im, mode == "rgb")
            if size is not None and out.size != (size[1], size[0]):
                out = _fit(Image, out, size, resize)
            arr = np.asarray(out, dtype=np.uint8)
        except MapiError:
            raise
        except (Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise _too_large(f"the image has more than {max_pixels:,} pixels") from None
        except Exception as exc:
            raise ImageDecodeError(
                "the image data cannot be decoded (the file is damaged or incomplete)",
                untrusted_text=f"{type(exc).__name__}: {exc}"[:500],
            ) from None
        finally:
            im.close()
    channels = 3 if mode == "rgb" else 1
    return np.ascontiguousarray(arr.reshape(arr.shape[0], arr.shape[1], channels))


def resize_array(arr: np.ndarray, size: tuple[int, int], resize: str = "stretch") -> np.ndarray:
    """``uint8 (H, W, C)`` pixels (``C`` 1 or 3) resized to ``size`` (height, width) with the
    rules of :func:`decode_image`; the input is returned when it has that size already."""
    _check_size(size, resize)
    a = np.asarray(arr)
    if a.dtype != np.uint8 or a.ndim != 3 or a.shape[2] not in (1, 3):
        raise ValueError("resize_array takes uint8 pixels (height, width, 1 or 3 channels)")
    if a.shape[:2] == tuple(size):
        return a
    with _pillow(MAX_IMAGE_PIXELS) as Image:
        im = Image.fromarray(
            a[:, :, 0] if a.shape[2] == 1 else a, "L" if a.shape[2] == 1 else "RGB"
        )
        out = np.asarray(_fit(Image, im, size, resize), dtype=np.uint8)
    return np.ascontiguousarray(out.reshape(size[0], size[1], a.shape[2]))
