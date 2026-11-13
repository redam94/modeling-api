"""Test-only wheel builder for the ``plugin_inspect`` tests (v0.19).

Builds pure-Python wheels, good and hostile, as zip files in a tmp directory. Nothing here is
ever installed, extracted or imported: the tests only hand the files to
:func:`mapi_runner.plugin_inspect.inspect_wheel`.

* :func:`wheel_members` - the members of a well-formed upload (package, ``.dist-info`` with
  ``METADATA``, ``WHEEL``, ``entry_points.txt`` and a matching ``RECORD``), with overrides;
* :func:`write_wheel` - writes members with :mod:`zipfile` (per-member ``ZipInfo`` tweaks
  for links, encryption flags and compression methods);
* :func:`raw_zip` - a minimal hand-written zip for structures :mod:`zipfile` will not write
  (overlapping entries, ZIP64 size lies, local headers that disagree with the directory);
* :func:`patch_bytes` - byte-level edits of a stored wheel (NUL in a name, header mismatch).
"""

from __future__ import annotations

import base64
import hashlib
import stat
import struct
import warnings
import zipfile
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from mapi_runner.plugin_inspect import InstalledIndex
from mapi_spec.jobs import PluginInspectJob
from mapi_spec.plugins import ArchiveLimits

NS = "t_k3vq7m_acme"
DIST = "t-k3vq7m-acme"
VERSION = "0.1.0"
PVID = "plv_01jbz3q2x8m9v7k5h4g3f2e1d1"
DIST_INFO = f"{NS}-{VERSION}.dist-info"

ENTRY_POINTS = f"""\
[mapi.models]
store_mean = {NS}.model:MODELS

[mapi.steps]
winsorize = {NS}.steps:STEPS
"""

PACKAGE: dict[str, bytes] = {
    f"{NS}/__init__.py": b'"""Acme models."""\n',
    f"{NS}/model.py": b"MODELS = []\n",
    f"{NS}/steps.py": b"STEPS = []\n",
}


def index(**over: object) -> InstalledIndex:
    """A synthetic base image: what the requirement and shadowing checks resolve against."""
    base: dict[str, object] = {
        "dists": {
            "mapi-sdk": "0.16.0",
            "mapi-spec": "0.16.0",
            "mapi-plots": "0.16.0",
            "mapi-workflows": "0.16.0",
            "mapi-plugin-mmm": "0.16.0",
            "numpy": "2.1.3",
            "pandas": "2.2.3",
            "altair": "5.4.1",
            "typing-extensions": "4.12.2",
        },
        "top_level": frozenset(
            {
                "mapi_sdk",
                "mapi_spec",
                "mapi_plots",
                "numpy",
                "pandas",
                "altair",
                "typing_extensions",
            }
        ),
        "requires_python": "3.12.15",
        "requires": {
            "mapi-plots": (
                "pydantic>=2",
                "altair>=5; extra == 'recipes'",
                "plotly>=5; extra == 'recipes'",
                "pytest; extra == 'test'",
            ),
            "mapi-workflows": ("numpy>=1.26; extra == 'runtime'",),
        },
        "image_digest": "sha256:" + "ab" * 32,
    }
    base.update(over)
    return InstalledIndex(**base)  # type: ignore[arg-type]


def metadata(
    *,
    name: str = DIST,
    version: str = VERSION,
    requires: Iterable[str] = ("mapi-sdk>=0.16", "pytest>=8; extra == 'test'"),
    requires_python: str | None = ">=3.11",
    extra: str = "",
) -> bytes:
    lines = ["Metadata-Version: 2.3", f"Name: {name}", f"Version: {version}"]
    if requires_python is not None:
        lines.append(f"Requires-Python: {requires_python}")
    lines += [f"Requires-Dist: {r}" for r in requires]
    lines.append("License-Expression: MIT")
    return ("\n".join(lines) + "\n" + extra + "\n").encode()


def wheel_file(*, tags: Iterable[str] = ("py3-none-any",), purelib: str = "true") -> bytes:
    lines = ["Wheel-Version: 1.0", "Generator: hatchling 1.27.0", f"Root-Is-Purelib: {purelib}"]
    lines += [f"Tag: {t}" for t in tags]
    return ("\n".join(lines) + "\n").encode()


def record_for(members: Mapping[str, bytes], record_name: str) -> bytes:
    rows = []
    for name, data in members.items():
        if name.endswith("/"):
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rows.append(f"{name},sha256={digest},{len(data)}")
    rows.append(f"{record_name},,")
    return ("\n".join(rows) + "\n").encode()


def wheel_members(
    *,
    files: Mapping[str, bytes] | None = None,
    extra: Mapping[str, bytes] | None = None,
    drop: Iterable[str] = (),
    dist_info: str = DIST_INFO,
    meta: bytes | None = None,
    wheel: bytes | None = None,
    entry_points: str | None = ENTRY_POINTS,
    record: bool = True,
) -> dict[str, bytes]:
    """The members of an upload, ``RECORD`` last and computed over everything else."""
    out: dict[str, bytes] = dict(PACKAGE if files is None else files)
    out.update(extra or {})
    out[f"{dist_info}/METADATA"] = metadata() if meta is None else meta
    out[f"{dist_info}/WHEEL"] = wheel_file() if wheel is None else wheel
    if entry_points is not None:
        out[f"{dist_info}/entry_points.txt"] = entry_points.encode()
    for name in drop:
        out.pop(name, None)
    if record:
        out[f"{dist_info}/RECORD"] = record_for(out, f"{dist_info}/RECORD")
    return out


ZipTweak = Callable[[zipfile.ZipInfo], None]


def write_wheel(
    dest: Path,
    members: Mapping[str, bytes] | Iterable[tuple[str, bytes]],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
    tweaks: Mapping[str, ZipTweak] | None = None,
) -> Path:
    """Write ``members`` with :mod:`zipfile` (``tweaks`` edit one member's ``ZipInfo``)."""
    items = list(members.items()) if isinstance(members, Mapping) else list(members)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate names on purpose
        with zipfile.ZipFile(dest, "w", compression=compression) as zf:
            for name, data in items:
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = compression
                # a directory entry the way zipfile.mkdir writes one (the mount extractor
                # refuses a file mode on a name that ends with '/')
                info.external_attr = (
                    (stat.S_IFDIR | 0o755) << 16 | 0x10
                    if name.endswith("/")
                    else (stat.S_IFREG | 0o644) << 16
                )
                if tweaks and name in tweaks:
                    tweaks[name](info)
                zf.writestr(info, data)
    return dest


def symlink(info: zipfile.ZipInfo) -> None:
    info.external_attr = (stat.S_IFLNK | 0o777) << 16


def fifo(info: zipfile.ZipInfo) -> None:
    info.external_attr = (stat.S_IFIFO | 0o644) << 16


def bzip2(info: zipfile.ZipInfo) -> None:
    info.compress_type = zipfile.ZIP_BZIP2


def patch_bytes(path: Path, old: bytes, new: bytes, *, count: int = -1) -> Path:
    """Replace ``old`` with ``new`` (same length) in the file bytes."""
    assert len(old) == len(new)
    data = path.read_bytes()
    assert old in data
    path.write_bytes(data.replace(old, new, count))
    return path


@dataclass
class RawEntry:
    name: bytes
    data: bytes
    deflate: bool = False
    flags: int = 0
    external_attr: int = (stat.S_IFREG | 0o644) << 16
    local_name: bytes | None = None
    """A different name in the local header (the directory keeps ``name``)."""
    cd_offset: int | None = None
    """Point the directory entry at another entry's local header (overlap)."""
    zip64_size: int | None = None
    """Claim this uncompressed size through a ZIP64 extra field in the directory."""
    write_local: bool = True


def raw_zip(dest: Path, entries: Iterable[RawEntry], *, prefix: bytes = b"") -> Path:
    """A minimal zip writer for structures :mod:`zipfile` refuses to produce."""
    out = bytearray(prefix)
    directory = bytearray()
    offsets: list[int] = []
    metas: list[tuple[int, int, int]] = []  # (method, crc, compressed size)
    rows = list(entries)
    for e in rows:
        payload = e.data
        method = 0
        if e.deflate:
            c = zlib.compressobj(9, zlib.DEFLATED, -15)
            payload = c.compress(e.data) + c.flush()
            method = 8
        crc = zlib.crc32(e.data)
        offsets.append(len(out))
        if e.write_local:
            lname = e.name if e.local_name is None else e.local_name
            out += struct.pack(
                "<IHHHHHIIIHH", 0x04034B50, 20, e.flags, method, 0, 0x5C21, crc,
                len(payload), len(e.data), len(lname), 0,
            )  # fmt: skip
            out += lname + payload
        metas.append((method, crc, len(payload)))
    for i, e in enumerate(rows):
        method, crc, csize = metas[i]
        usize = len(e.data)
        extra = b""
        if e.zip64_size is not None:
            usize = 0xFFFFFFFF
            extra = struct.pack("<HHQ", 0x0001, 8, e.zip64_size)
        offset = offsets[i] if e.cd_offset is None else e.cd_offset
        directory += struct.pack(
            "<IHHHHHHIIIHHHHHII", 0x02014B50, 0x031E, 20, e.flags, method, 0, 0x5C21, crc,
            csize, usize, len(e.name), len(extra), 0, 0, 0, e.external_attr, offset,
        )  # fmt: skip
        directory += e.name + extra
    cd_start = len(out)
    out += directory
    out += struct.pack(
        "<IHHHHIIH", 0x06054B50, 0, 0, len(rows), len(rows), len(directory), cd_start, 0
    )
    dest.write_bytes(bytes(out))
    return dest


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def job_for(path: Path, **limits: int) -> PluginInspectJob:
    """The inspect job of ``path`` (its real sha256; ``limits`` override the caps)."""
    return PluginInspectJob(
        plugin_version_id=PVID,
        namespace=NS,
        dist_name=DIST,
        version=VERSION,
        expected_sha256=sha(path),
        limits=ArchiveLimits(**limits),
    )
