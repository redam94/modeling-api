#!/usr/bin/env python3
"""Vendor the Redoc standalone bundle that renders the docs' OpenAPI explorer.

The explorer (``docs/reference/openapi-viewer.html``) must not load anything from a CDN at
run time, so the bundle lives in ``docs/assets/vendor/redoc/``. This script is the only way it
gets there: it downloads the pinned npm tarball, checks the registry's SHA-512 integrity
string, extracts ``bundles/redoc.standalone.js`` and the licence files, drops the source-map
comment (the 3.9 MB map is not vendored), points the footer logo at an empty ``data:`` URL
instead of a CDN, and checks the result against ``BUNDLE_SHA256``.

    uv run python scripts/vendor_redoc.py           # (re)write the vendored files
    uv run python scripts/vendor_redoc.py --check   # verify what is committed

To upgrade: bump ``VERSION`` to a release that is at least two weeks old, set ``INTEGRITY``
from ``npm view redoc@<version> dist.integrity``, run once with ``--update-hash`` and review the
diff.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "2.5.4"
TARBALL = f"https://registry.npmjs.org/redoc/-/redoc-{VERSION}.tgz"
INTEGRITY = (
    "sha512-"
    "M6jWhG1qoBnH6TFmzJnstyCZ87HmOY/UzDm78mHiYihEdlV/YcS9ogOo1NlElnJMeLsyxHFe2yFc4sNjHTrABQ=="
)
BUNDLE_SHA256 = "ffa870731f36b54694a4a6ba9c0a4e35d7fb64d7755e4378fbc7245b1b4d3510"

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "docs" / "assets" / "vendor" / "redoc"
BUNDLE = DEST / "redoc.standalone.js"
MEMBERS = {
    "package/bundles/redoc.standalone.js": "redoc.standalone.js",
    "package/bundles/redoc.standalone.js.LICENSE.txt": "redoc.standalone.js.LICENSE.txt",
    "package/LICENSE": "LICENSE.txt",
}
SOURCE_MAP = b"\n//# sourceMappingURL=redoc.standalone.js.map"
LOGO_URL = b"https://cdn.redoc.ly/redoc/logo-mini.svg"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch() -> dict[str, bytes]:
    """The vendored files, from the pinned tarball, integrity-checked."""
    with urllib.request.urlopen(TARBALL, timeout=60) as resp:  # a pinned https URL
        tarball = resp.read()
    algo, _, expected = INTEGRITY.partition("-")
    actual = base64.b64encode(hashlib.new(algo, tarball).digest()).decode()
    if actual != expected:
        raise SystemExit(f"integrity mismatch for {TARBALL}: {algo}-{actual}")
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        for member, name in MEMBERS.items():
            extracted = tar.extractfile(member)
            if extracted is None:
                raise SystemExit(f"{member} is missing from {TARBALL}")
            files[name] = extracted.read()
    bundle = files["redoc.standalone.js"].rstrip(b"\n")
    if bundle.endswith(SOURCE_MAP):
        bundle = bundle[: -len(SOURCE_MAP)]
    # The "API docs by Redocly" footer loads its logo from a CDN; an empty data: URL fails to
    # decode, and Redoc's own onError handler then hides the image. The text link stays.
    if bundle.count(LOGO_URL) != 1:
        raise SystemExit(f"expected one {LOGO_URL!r} in the bundle")
    bundle = bundle.replace(LOGO_URL, b"data:,")
    header = f"/* redoc {VERSION}, vendored by scripts/vendor_redoc.py from {TARBALL} */\n"
    files["redoc.standalone.js"] = header.encode() + bundle + b"\n"
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify the committed bundle only")
    parser.add_argument("--update-hash", action="store_true", help="print the new bundle hash")
    args = parser.parse_args(argv)
    if args.check:
        actual = _sha256(BUNDLE.read_bytes())
        if actual != BUNDLE_SHA256:
            print(f"{BUNDLE} has sha256 {actual}, expected {BUNDLE_SHA256}", file=sys.stderr)
            return 1
        print(f"{BUNDLE.name}: redoc {VERSION} ok")
        return 0
    files = fetch()
    digest = _sha256(files["redoc.standalone.js"])
    if digest != BUNDLE_SHA256 and not args.update_hash:
        raise SystemExit(f"bundle sha256 {digest} != BUNDLE_SHA256 (use --update-hash)")
    DEST.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (DEST / name).write_bytes(data)
        print(f"wrote {(DEST / name).relative_to(ROOT)} ({len(data) // 1024} KiB)")
    if args.update_hash:
        print(f"BUNDLE_SHA256 = {digest!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
