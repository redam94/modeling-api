"""v0.15: image headers and decoding with the ``image_folder`` rules."""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from mapi_sdk.images import ImageDecodeError, ImageHeader, decode_image, image_header, resize_array
from mapi_sdk.media import encode_png
from mapi_spec.errors import ErrorCode

pytestmark = pytest.mark.unit

Image = pytest.importorskip("PIL.Image")


def _save(im: object, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    im.save(buf, format=fmt)  # type: ignore[attr-defined]
    return buf.getvalue()


def _rgb(h: int = 6, w: int = 8) -> np.ndarray:
    return (np.arange(h * w * 3).reshape(h, w, 3) * 5 % 256).astype(np.uint8)


def test_header_reads_without_decoding() -> None:
    png, _, _ = encode_png(_rgb())
    assert image_header(png) == ImageHeader("png", 8, 6, "RGB", True)
    jpg = _save(Image.new("L", (5, 3)), "JPEG")
    assert image_header(jpg) == ImageHeader("jpeg", 5, 3, "L", False)
    tif = _save(Image.new("RGB", (2, 2)), "TIFF")
    assert image_header(tif).format == "tiff"


def test_decode_gray_and_rgb() -> None:
    px = _rgb()
    png, _, _ = encode_png(px)
    rgb = decode_image(png, mode="rgb")
    assert rgb.dtype == np.uint8 and rgb.shape == (6, 8, 3)
    np.testing.assert_array_equal(rgb, px)
    gray = decode_image(png, mode="gray")
    expected = np.asarray(Image.fromarray(px, "RGB").convert("L"))
    np.testing.assert_array_equal(gray[..., 0], expected)
    assert gray.shape == (6, 8, 1)


def test_alpha_palette_and_cmyk_are_converted() -> None:
    rgba = Image.new("RGBA", (4, 4), (10, 20, 30, 0))
    assert decode_image(_save(rgba), mode="rgb")[0, 0].tolist() == [10, 20, 30]
    la = Image.new("LA", (3, 3), (90, 5))
    assert decode_image(_save(la), mode="gray")[0, 0, 0] == 90
    pal = Image.new("RGB", (4, 4), (200, 0, 0)).convert("P", palette=Image.Palette.ADAPTIVE)
    assert image_header(_save(pal)).colour
    assert decode_image(_save(pal), mode="rgb")[1, 1].tolist() == [200, 0, 0]
    cmyk = Image.new("CMYK", (2, 2), (0, 0, 0, 0))
    assert decode_image(_save(cmyk, "JPEG"), mode="rgb").shape == (2, 2, 3)


def test_16_bit_images_are_refused() -> None:
    im = Image.fromarray(np.full((4, 4), 4000, dtype=np.uint16))
    with pytest.raises(ImageDecodeError, match="8-bit") as err:
        image_header(_save(im))
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_too_many_pixels_is_refused_before_decoding() -> None:
    png, _, _ = encode_png(np.zeros((40, 40), dtype=np.uint8))
    with pytest.raises(ImageDecodeError, match="pixels") as err:
        decode_image(png, mode="gray", max_pixels=1000)
    assert err.value.code is ErrorCode.PAYLOAD_TOO_LARGE
    # a cap above Pillow's own limit raises it for the call only
    before = Image.MAX_IMAGE_PIXELS
    assert image_header(png, max_pixels=10**12).width == 40
    assert before == Image.MAX_IMAGE_PIXELS


@pytest.mark.parametrize(
    "data", [b"not an image", _save(Image.new("RGB", (2, 2)), "GIF"), b"\x89PNG\r\n\x1a\n"]
)
def test_unreadable_or_other_formats_are_refused(data: bytes) -> None:
    with pytest.raises(ImageDecodeError, match="PNG, JPEG or TIFF"):
        image_header(data)


def test_truncated_pixel_data_is_refused() -> None:
    png, _, _ = encode_png(np.random.default_rng(0).integers(0, 255, (32, 32), dtype=np.uint8))
    with pytest.raises(ImageDecodeError):
        decode_image(png[: len(png) // 2] + png[-12:], mode="gray")


@pytest.mark.parametrize("how", ["stretch", "crop", "pad"])
def test_resize_modes_match_pillow(how: str) -> None:
    px = _rgb(6, 12)
    png, _, _ = encode_png(px)
    out = decode_image(png, mode="rgb", size=(4, 4), resize=how)  # type: ignore[arg-type]
    assert out.shape == (4, 4, 3)
    np.testing.assert_array_equal(resize_array(px, (4, 4), how), out)
    if how == "pad":
        assert (out[0] == 0).all()  # a wide image gets bands at top and bottom
    if how == "stretch":
        ref = np.asarray(Image.fromarray(px, "RGB").resize((4, 4), Image.BILINEAR))
        np.testing.assert_array_equal(out, ref)


def test_resize_array_keeps_gray_channels_and_same_size() -> None:
    px = np.arange(36, dtype=np.uint8).reshape(6, 6, 1)
    assert resize_array(px, (6, 6)) is px
    assert resize_array(px, (3, 2)).shape == (3, 2, 1)
    with pytest.raises(ValueError):
        resize_array(px.astype(np.float32), (2, 2))
    with pytest.raises(ValueError):
        resize_array(px, (2, 2), "zoom")


def test_decoding_is_thread_safe_and_deterministic() -> None:
    imgs = [encode_png(_rgb(8, 8) + i)[0] for i in range(16)]
    one = [decode_image(b, mode="gray", size=(5, 5)) for b in imgs]
    with ThreadPoolExecutor(8) as pool:
        many = list(pool.map(lambda b: decode_image(b, mode="gray", size=(5, 5)), imgs))
    for a, b in zip(one, many, strict=True):
        np.testing.assert_array_equal(a, b)
