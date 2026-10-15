#!/usr/bin/env python3
"""Shrink documentation screenshots: 256-colour palette PNGs, at most ``--max-kb`` each.

    uv run python scripts/optimise_screenshots.py .local/screenshots docs/assets/screenshots
    uv run python scripts/optimise_screenshots.py SRC DEST --only editor,scorecard

UI screenshots have few distinct colours, so an undithered median-cut palette is visually
lossless and typically cuts the size by 3-5x. A file that is still too large is scaled down
until it fits. Pillow is already in the workspace environment (matplotlib depends on it).
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from PIL import Image


def optimise(src: Path, max_bytes: int) -> bytes:
    """The smallest-effort palette PNG of ``src`` that fits in ``max_bytes``."""
    image = Image.open(src).convert("RGB")
    scale = 1.0
    while True:
        frame = image
        if scale < 1.0:
            size = (round(image.width * scale), round(image.height * scale))
            frame = image.resize(size, Image.Resampling.LANCZOS)
        palette = frame.quantize(
            colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
        )
        buf = io.BytesIO()
        palette.save(buf, format="PNG", optimize=True)
        if buf.tell() <= max_bytes or scale <= 0.5:
            return buf.getvalue()
        scale -= 0.1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("src", type=Path, help="directory of raw PNG screenshots")
    parser.add_argument("dest", type=Path, help="output directory")
    parser.add_argument("--max-kb", type=int, default=300)
    parser.add_argument("--only", default="", help="comma-separated names (without .png)")
    args = parser.parse_args(argv)
    names = {n.strip() for n in args.only.split(",") if n.strip()}
    args.dest.mkdir(parents=True, exist_ok=True)
    pngs = sorted(p for p in args.src.glob("*.png") if not names or p.stem in names)
    if not pngs:
        print(f"no screenshots in {args.src}", file=sys.stderr)
        return 1
    for png in pngs:
        data = optimise(png, args.max_kb * 1024)
        (args.dest / png.name).write_bytes(data)
        print(f"{png.name}: {png.stat().st_size // 1024} KiB -> {len(data) // 1024} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
