"""Mount uploaded wheels for the worker (v0.19, D17). Used by the supervisor; stdlib only.

An upload reaches a run as an :class:`~mapi_spec.run.OverlayMount` in the RunSpec: the pin
the control plane stored (plugin version id, namespace, sha256 of the wheel) and a signed GET
minted for this run's tenant only. For each mount the supervisor

1. downloads the wheel into memory (at most the declared size and the archive cap), checks
   its size and **sha256 before any byte reaches the disk** (:func:`fetch_overlay`; a
   mismatch is ``INTEGRITY_FAILURE``), then writes ``<root>/<pvid>.whl``;
2. extracts it with a whitelist extractor (:func:`extract_wheel`): regular files and
   directories only; links, special files, absolute paths, ``..``, backslashes, NUL bytes,
   duplicate and case-colliding names, encrypted members and unknown compression methods are
   refused (``SCHEMA_INVALID``); member count, sizes, compression ratio, path length and depth
   are capped by :class:`~mapi_spec.plugins.ArchiveLimits`. Files end up mode ``0444``,
   directories ``0555``, owned by the supervisor (root in a container), so the worker
   (uid 10001) can read but never change them;
3. deletes the wheel and returns the directory, which the worker appends to ``sys.path``.

Nothing here imports, compiles or executes anything from the archive: ``zipfile`` reads the
central directory and streams member bytes. Transport failures (connection refused, HTTP
errors, a timeout) raise :class:`OSError` so the supervisor reports an infrastructure failure
the platform may retry. The exceptions are the answers that say the pinned object itself is
gone: ``404``/``410`` (no such object at the pinned generation: it was replaced or deleted)
and ``412`` (the generation precondition failed). They raise :class:`OverlayError`
(``INTEGRITY_FAILURE``): no retry can bring the approved bytes back.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from mapi_spec.plugins import ArchiveLimits
from mapi_spec.run import OverlayMount

__all__ = [
    "MountedOverlay",
    "OverlayError",
    "extract_wheel",
    "fetch_overlay",
    "mount_overlays",
]

OverlayErrorCode = Literal["INTEGRITY_FAILURE", "SCHEMA_INVALID"]

_CHUNK: Final = 256 * 1024
_FILE_MODE: Final = 0o444
_DIR_MODE: Final = 0o555
_ALLOWED_METHODS: Final = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_GONE_STATUSES: Final = frozenset({404, 410, 412})
"""HTTP answers that mean the pinned generation of the wheel no longer exists."""


class OverlayError(Exception):
    """An overlay cannot be mounted. ``code`` is ``INTEGRITY_FAILURE`` (the bytes are not
    the ones the control plane pinned) or ``SCHEMA_INVALID`` (the archive is not safe to
    extract). The message is platform text; archive names are never echoed raw."""

    def __init__(self, message: str, *, code: OverlayErrorCode) -> None:
        super().__init__(message)
        self.code: OverlayErrorCode = code


@dataclass(frozen=True, slots=True)
class MountedOverlay:
    """One extracted upload: where it is and which pin it satisfies."""

    dir: Path
    namespace: str
    pvid: str
    sha256: str

    @property
    def argv(self) -> list[str]:
        """The worker arguments that mount it."""
        return ["--overlay", f"{self.dir}={self.namespace}={self.pvid}"]


# ===================================================================================== fetch


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A signed URL is fetched exactly where it points: redirects are refused."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _opener() -> urllib.request.OpenerDirector:
    # no proxies from the environment, no redirects
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _hex(sha: str) -> str:
    algo, _, value = sha.partition(":")
    return value if algo == "sha256" else ""


def fetch_overlay(
    mount: OverlayMount,
    dest_dir: Path,
    *,
    timeout_s: float,
    max_bytes: int | None = None,
) -> Path:
    """Download the wheel of ``mount`` to ``dest_dir/<pvid>.whl`` and return the path.

    The body is held in memory (it is at most ``max_bytes``, default the archive cap of
    :class:`~mapi_spec.plugins.ArchiveLimits`) and written only after its size equals
    ``mount.size`` and its sha256 equals ``mount.pin.sha256``; anything else raises
    :class:`OverlayError` (``INTEGRITY_FAILURE``) and nothing is written. ``timeout_s`` bounds
    the whole download. Transport errors raise :class:`OSError`; an answer saying the
    pinned generation is gone (``404``/``410``/``412``) is ``INTEGRITY_FAILURE`` too.
    """
    cap = ArchiveLimits().max_bytes if max_bytes is None else max_bytes
    expected = _hex(mount.pin.sha256)
    if mount.size > cap or mount.get.size not in (0, mount.size):
        raise OverlayError(
            f"the uploaded wheel declares {mount.size} bytes; the limit is {cap}",
            code="INTEGRITY_FAILURE",
        )
    url = mount.get.url
    if not url.lower().startswith(("http://", "https://")):
        raise OverlayError("the overlay URL is not an HTTP(S) URL", code="INTEGRITY_FAILURE")
    deadline = time.monotonic() + max(timeout_s, 0.001)
    hasher = hashlib.sha256()
    parts: list[bytes] = []
    got = 0
    request = urllib.request.Request(url, method="GET")
    try:
        resp_cm = _opener().open(request, timeout=max(timeout_s, 0.001))
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in _GONE_STATUSES:
            raise OverlayError(
                "the stored copy of the uploaded wheel is gone or was replaced "
                f"(HTTP {exc.code} for the pinned generation)",
                code="INTEGRITY_FAILURE",
            ) from None
        raise
    with resp_cm as resp:
        if getattr(resp, "status", 200) != 200:  # 206 or another 2xx: not the whole object
            raise OSError(f"the overlay download answered HTTP {resp.status}")
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("the overlay download took too long")
            chunk = resp.read(min(_CHUNK, mount.size - got + 1))
            if not chunk:
                break
            got += len(chunk)
            if got > mount.size:
                raise OverlayError(
                    "the uploaded wheel is larger than the size the platform recorded",
                    code="INTEGRITY_FAILURE",
                )
            hasher.update(chunk)
            parts.append(chunk)
    if got != mount.size:
        raise OverlayError(
            f"the uploaded wheel has {got} bytes; the platform recorded {mount.size}",
            code="INTEGRITY_FAILURE",
        )
    if not expected or hasher.hexdigest() != expected:
        raise OverlayError(
            "the sha256 of the uploaded wheel does not match the pinned version",
            code="INTEGRITY_FAILURE",
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{mount.pin.plugin_version_id}.whl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, "wb") as fh:
        for part in parts:
            fh.write(part)
    return path


# =================================================================================== extract


def _bad(message: str) -> OverlayError:
    return OverlayError(f"the uploaded wheel cannot be mounted: {message}", code="SCHEMA_INVALID")


def _member_parts(info: zipfile.ZipInfo, limits: ArchiveLimits) -> tuple[str, ...]:
    """The path components of a member, refusing anything that is not a plain relative
    POSIX path inside the archive root."""
    name = info.filename
    if not name or "\x00" in name or "\\" in name:
        raise _bad("a member has an empty name, a NUL byte or a backslash")
    if len(name) > limits.max_path:
        raise _bad(f"a member path is longer than {limits.max_path} characters")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise _bad("a member has an absolute path")
    body = name[:-1] if name.endswith("/") else name
    parts = tuple(body.split("/"))
    if any(p in ("", ".", "..") for p in parts):
        raise _bad("a member path has an empty, '.' or '..' component")
    if len(parts) > limits.max_depth:
        raise _bad(f"a member path is deeper than {limits.max_depth} directories")
    if any(any(ord(c) < 32 or ord(c) == 127 for c in p) for p in parts):
        raise _bad("a member path holds control characters")
    return parts


def _check_type(info: zipfile.ZipInfo) -> bool:
    """True for a directory entry; refuses links and special files (by the Unix mode the
    archive records, when it records one)."""
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    is_dir = info.filename.endswith("/")
    if kind == 0:  # no Unix mode recorded (e.g. written on Windows): trust the name
        return is_dir
    if kind == stat.S_IFDIR:
        if not is_dir:
            raise _bad("a member is a directory without a trailing '/'")
        return True
    if kind != stat.S_IFREG or is_dir:
        raise _bad("a member is a link or a special file")
    return False


def _plan(
    zf: zipfile.ZipFile, limits: ArchiveLimits
) -> list[tuple[zipfile.ZipInfo, tuple[str, ...], bool]]:
    infos = zf.infolist()
    if len(infos) > limits.max_members:
        raise _bad(f"the archive has more than {limits.max_members} members")
    seen: set[str] = set()
    folded: set[str] = set()
    total = 0
    out: list[tuple[zipfile.ZipInfo, tuple[str, ...], bool]] = []
    for info in infos:
        parts = _member_parts(info, limits)
        is_dir = _check_type(info)
        key = "/".join(parts)
        if key in seen:
            raise _bad("two members have the same name")
        if key.casefold() in folded:
            raise _bad("two members differ only in case")
        seen.add(key)
        folded.add(key.casefold())
        if info.flag_bits & 0x1:
            raise _bad("a member is encrypted")
        if info.compress_type not in _ALLOWED_METHODS:
            raise _bad("a member uses a compression method other than stored or deflate")
        if not is_dir:
            if info.file_size > limits.max_member:
                raise _bad(f"a member is larger than {limits.max_member} bytes")
            if info.file_size > limits.max_ratio * max(info.compress_size, 1):
                raise _bad(f"a member is compressed more than {limits.max_ratio} times")
            total += info.file_size
            if total > limits.max_uncompressed:
                raise _bad(f"the archive expands to more than {limits.max_uncompressed} bytes")
        out.append((info, parts, is_dir))
    # a file and a directory (or a file "under" a file) with the same path
    files = {"/".join(p) for _i, p, d in out if not d}
    for _info, parts, _is_dir in out:
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in files:
                raise _bad("a member is inside a path that is a file")
    return out


def _mkdirs(root: Path, parts: Sequence[str], made: set[Path]) -> Path:
    here = root
    for part in parts:
        here = here / part
        if here in made:
            continue
        try:
            os.mkdir(here, 0o700)
        except FileExistsError:
            st = os.lstat(here)
            if not stat.S_ISDIR(st.st_mode):
                raise _bad("a member is inside a path that is a file") from None
        made.add(here)
    return here


def _write_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, path: Path) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    written = 0
    with os.fdopen(fd, "wb") as out, zf.open(info) as src:
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > info.file_size:
                raise _bad("a member expands past its declared size")
            out.write(chunk)
    if written != info.file_size:
        raise _bad("a member is shorter than its declared size")


def _seal(root: Path) -> None:
    """Files 0444, directories 0555 (bottom-up, the root last)."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), _FILE_MODE, follow_symlinks=False)
        for name in dirnames:
            os.chmod(os.path.join(dirpath, name), _DIR_MODE, follow_symlinks=False)
    os.chmod(root, _DIR_MODE)


def _remove(root: Path) -> None:
    """Remove a (possibly sealed) tree."""
    if not root.exists():
        return
    for dirpath, dirnames, _files in os.walk(root, followlinks=False):
        with contextlib.suppress(OSError):
            os.chmod(dirpath, 0o700)
        for name in dirnames:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(dirpath, name), 0o700, follow_symlinks=False)
    shutil.rmtree(root, ignore_errors=True)


def extract_wheel(wheel: Path, dest: Path, *, limits: ArchiveLimits) -> None:
    """Extract ``wheel`` into the new directory ``dest`` with the whitelist rules (module
    docs). ``dest`` must not exist. On any refusal nothing is left behind and
    :class:`OverlayError` (``SCHEMA_INVALID``) is raised."""
    st = os.lstat(wheel)
    if not stat.S_ISREG(st.st_mode):
        raise _bad("the wheel is not a regular file")
    if st.st_size > limits.max_bytes:
        raise _bad(f"the wheel is larger than {limits.max_bytes} bytes")
    os.mkdir(dest, 0o700)  # exclusive: never extract into something that already exists
    try:
        with zipfile.ZipFile(wheel) as zf:
            members = _plan(zf, limits)
            made: set[Path] = {dest}
            for info, parts, is_dir in members:
                if is_dir:
                    _mkdirs(dest, parts, made)
                    continue
                parent = _mkdirs(dest, parts[:-1], made)
                _write_member(zf, info, parent / parts[-1])
        _seal(dest)
    except OverlayError:
        _remove(dest)
        raise
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        EOFError,
        ValueError,
        NotImplementedError,
    ) as exc:
        _remove(dest)
        raise _bad(f"the archive is not a valid zip file ({type(exc).__name__})") from None
    except BaseException:
        _remove(dest)
        raise


# ===================================================================================== mount


def mount_overlays(
    mounts: Sequence[OverlayMount],
    root: Path,
    *,
    timeout_s: float,
    limits: ArchiveLimits | None = None,
) -> list[MountedOverlay]:
    """Fetch, verify and extract every mount into ``root/<pvid>/`` (``root`` is created
    ``0555`` once everything is in place). The wheels are deleted after extraction. Raises
    :class:`OverlayError` or :class:`OSError`; a partial ``root`` is removed."""
    lim = limits or ArchiveLimits()
    deadline = time.monotonic() + timeout_s
    root.mkdir(mode=0o700)
    out: list[MountedOverlay] = []
    try:
        for mount in mounts:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("mounting the uploaded code took too long")
            pvid = mount.pin.plugin_version_id
            wheel = fetch_overlay(mount, root, timeout_s=left, max_bytes=lim.max_bytes)
            try:
                extract_wheel(wheel, root / pvid, limits=lim)
            finally:
                with contextlib.suppress(OSError):
                    os.chmod(wheel, 0o600)
                    os.unlink(wheel)
            out.append(MountedOverlay(root / pvid, mount.pin.namespace, pvid, mount.pin.sha256))
        os.chmod(root, _DIR_MODE)
    except BaseException:
        _remove(root)
        raise
    return out
