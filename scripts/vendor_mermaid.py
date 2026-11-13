#!/usr/bin/env python3
"""Vendor the mermaid bundle that draws the diagrams in the docs.

Material for MkDocs fetches mermaid from a CDN when a page has a diagram and no ``mermaid``
global exists. The docs must not load anything from another host at run time, so the bundle
lives in ``docs/assets/vendor/mermaid/`` and ``mkdocs.yml`` loads it with ``extra_javascript``.
This script is the only way it gets there: it downloads the pinned npm tarball, checks the
registry's SHA-512 integrity string, extracts ``dist/mermaid.min.js`` and the licence, drops a
source-map comment if there is one (the map is not vendored), and checks the result against
``BUNDLE_SHA256``.

    uv run python scripts/vendor_mermaid.py           # (re)write the vendored files
    uv run python scripts/vendor_mermaid.py --check   # verify what is committed

To upgrade: bump ``VERSION`` to a release that is at least two weeks old, set ``INTEGRITY``
from ``npm view mermaid@<version> dist.integrity``, run once with ``--update-hash`` and review
the diff.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "11.17.2"
TARBALL = f"https://registry.npmjs.org/mermaid/-/mermaid-{VERSION}.tgz"
INTEGRITY = (
    "sha512-"
    "V6K3C8EBdEsPFZXSKMJe6ppQOENxuHARr9GvHX4hh47lAbhMRD9qf4oEK7LoaRQxULMa80/qt5gHO73aCleBBg=="
)
BUNDLE_SHA256 = "893d8ba3493630bdb6a96a2a26c41094b6b7b159961d9c1ad979cd0b4acfd853"

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "docs" / "assets" / "vendor" / "mermaid"
BUNDLE = DEST / "mermaid.min.js"
MEMBERS = {
    "package/dist/mermaid.min.js": "mermaid.min.js",
    "package/LICENSE": "LICENSE.txt",
}
SOURCE_MAP = re.compile(rb"\n//# sourceMappingURL=\S+\s*$")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch() -> dict[str, bytes]:
    """The vendored files, from the pinned tarball, integrity-checked."""
    with urllib.request.urlopen(TARBALL, timeout=120) as resp:  # a pinned https URL
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
    bundle = SOURCE_MAP.sub(b"", files["mermaid.min.js"].rstrip(b"\n"))
    header = f"/* mermaid {VERSION}, vendored by scripts/vendor_mermaid.py from {TARBALL} */\n"
    files["mermaid.min.js"] = header.encode() + bundle + b"\n"
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
        print(f"{BUNDLE.name}: mermaid {VERSION} ok")
        return 0
    files = fetch()
    digest = _sha256(files["mermaid.min.js"])
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
