"""Safe collection of the worker's output files (the worker is untrusted).

Every file is opened relative to an already-open directory with ``O_NOFOLLOW``, so a
symlink planted by the worker can never make the supervisor upload one of its own files.
Symlinks, devices, FIFOs and sockets in the outputs tree are a ``SANDBOX_VIOLATION``;
names must be valid object names, and ``manifest.json`` and ``checkpoints/`` are reserved.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import Final

from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.run import MANIFEST_OBJECT_NAME
from mapi_spec.storage import check_object_name

__all__ = [
    "CHECKPOINT_DIR",
    "OutputFile",
    "OutputViolation",
    "collect_outputs",
    "content_type_for",
    "open_regular",
]

CHECKPOINT_DIR: Final = "checkpoints/"
_MAX_DEPTH: Final = 8
_CONTENT_TYPES: Final = {
    ".json": "application/json",
    ".zip": "application/zip",
    ".arrow": "application/vnd.apache.arrow.file",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".csv": "text/csv",
    ".txt": "text/plain",
}
_DIR_FLAGS: Final = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
_FILE_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)


class OutputViolation(MapiError):
    default_code = ErrorCode.SANDBOX_VIOLATION


@dataclass(slots=True)
class OutputFile:
    """One regular file, open for reading (the caller closes ``fd``)."""

    name: str
    fd: int
    size: int

    @property
    def content_type(self) -> str:
        return content_type_for(self.name)


def content_type_for(name: str) -> str:
    """Content type chosen by the supervisor from the extension (never by the worker)."""
    _, ext = os.path.splitext(name.lower())
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


def open_regular(dir_fd: int, name: str) -> tuple[int, int]:
    """Open ``name`` inside ``dir_fd`` without following links; returns ``(fd, size)``."""
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise OutputViolation(f"output {name!r} cannot be opened safely: {exc.strerror}") from None
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise OutputViolation(f"output {name!r} is not a regular file")
    return fd, st.st_size


def collect_outputs(root: str, *, max_objects: int) -> list[OutputFile]:
    """Every regular file under ``root``, opened safely, in name order."""
    found: list[OutputFile] = []
    try:
        root_fd = os.open(root, _DIR_FLAGS)
    except OSError as exc:
        raise OutputViolation(f"the outputs directory cannot be opened: {exc.strerror}") from None
    try:
        _walk(root_fd, "", 0, found, max_objects)
    except BaseException:
        for f in found:
            os.close(f.fd)
        raise
    finally:
        os.close(root_fd)
    found.sort(key=lambda f: f.name)
    return found


def _walk(dir_fd: int, prefix: str, depth: int, out: list[OutputFile], max_objects: int) -> None:
    if depth > _MAX_DEPTH:
        raise OutputViolation("the outputs tree is nested too deeply")
    with os.scandir(dir_fd) as it:
        entries = sorted(it, key=lambda e: e.name)
    for entry in entries:
        rel = prefix + entry.name
        st = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            try:
                sub = os.open(entry.name, _DIR_FLAGS, dir_fd=dir_fd)
            except OSError as exc:
                raise OutputViolation(
                    f"output dir {rel!r} cannot be opened: {exc.strerror}"
                ) from None
            try:
                _walk(sub, rel + "/", depth + 1, out, max_objects)
            finally:
                os.close(sub)
            continue
        if not stat.S_ISREG(st.st_mode):
            raise OutputViolation(f"output {rel!r} is not a regular file (links are refused)")
        _check_name(rel)
        if len(out) >= max_objects:
            raise OutputViolation(f"the run produced more than {max_objects} output objects")
        fd, size = open_regular(dir_fd, entry.name)
        out.append(OutputFile(rel, fd, size))


def _check_name(rel: str) -> None:
    try:
        check_object_name(rel)
    except ValueError as exc:
        raise OutputViolation(f"output name {rel[:200]!r} is invalid: {exc}") from None
    if rel == MANIFEST_OBJECT_NAME:
        raise OutputViolation(f"{MANIFEST_OBJECT_NAME!r} is reserved for the runner manifest")
    if rel.startswith(CHECKPOINT_DIR):
        raise OutputViolation(f"{CHECKPOINT_DIR!r} is reserved for checkpoints")
