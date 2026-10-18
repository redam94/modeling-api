"""Safety checks of an uploaded HDF5 file, before xarray or h5py's high-level API opens it.

An HDF5 file can point outside itself or make a reader do unbounded work: external links
(``/etc/passwd`` as an HDF5 file), external raw storage (a dataset's bytes in another file),
virtual datasets (a mosaic of other files), dynamically loaded filter plugins (code loaded
from a path the file names), cycles of soft or hard links (a reader that walks the tree
never ends), and shapes, chunks or attributes that declare far more memory than the file
holds. :func:`check_hdf5` walks the file with h5py's **low-level** API (``h5l``/``h5o``),
never following a soft or external link, and refuses all of that with
:class:`~mapi_spec.errors.UnsafeUpload` (``INGEST_UNSAFE_FILE``):

* external links and user-defined links; soft links that dangle, chain into a cycle or
  name a group (a reader following them would walk the tree again);
* a group reachable from itself through hard links (a cycle);
* datasets with external raw storage or a virtual layout;
* filters outside :data:`ALLOWED_FILTERS` (no szip, no dynamically loaded plugins);
* a user block larger than :data:`MAX_USERBLOCK`;
* more than ``limits.max_hdf5_objects`` links or attributes, groups deeper than
  :data:`MAX_HDF5_DEPTH`, arrays with more than ``limits.max_ndim`` dims;
* an attribute value larger than ``limits.max_attr_bytes``; with ``lenient_attrs`` (a
  self-describing NetCDF/xarray file, which ingested before M4 without these caps) the cap
  is the file's size instead (attributes are stored uncompressed, so the file bounds
  them) and the file may hold :data:`NETCDF_ATTRS_PER_VARIABLE` attributes per variable
  the ingest accepts (``limits.max_columns``);
* declared array bytes (shape x item size, a missing chunk decodes to the fill value) over
  the expansion budget of the upload, or one chunk larger than it;
* data types whose size cannot be bounded: variable-length sequences, compound, array,
  reference and opaque datasets. Variable-length *strings* are accepted: their bytes live in
  the file's heap, so the file's size bounds each one. A variable-length text attribute is
  read whole by libhdf5, so before it is read its worst case (elements x file size: elements
  may share one heap object) must fit the expansion budget; its text is then capped as above.

The walk also returns what it saw (:class:`Hdf5Walk`), which the ``inspect`` run lists
without reading array data. Dynamic filter plugins are disabled for the whole process
(:func:`disable_plugins`) before any file is opened; the supervisor also points
``HDF5_PLUGIN_PATH`` at an empty directory in the worker's environment.
"""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from mapi_spec.errors import ErrorCode, Issue, UnsafeUpload

from mapi_runner.worker.tabular import IngestLimits

__all__ = [
    "ALLOWED_FILTERS",
    "MAX_HDF5_DEPTH",
    "MAX_USERBLOCK",
    "NETCDF_ATTRS_PER_VARIABLE",
    "Hdf5Array",
    "Hdf5Walk",
    "check_hdf5",
    "disable_plugins",
    "unsafe",
    "walk_hdf5",
]

MAX_HDF5_DEPTH: Final = 32
"""Groups below the root."""
MAX_USERBLOCK: Final = 64 * 1024
"""Bytes of user block before the HDF5 superblock."""
NETCDF_ATTRS_PER_VARIABLE: Final = 64
"""Attributes per variable a self-describing NetCDF file may hold (``lenient_attrs``)."""
MAX_SOFT_LINK_HOPS: Final = 16
MAX_VLEN_ATTR_ELEMENTS: Final = 4096
"""Elements of one variable-length attribute (``DIMENSION_LIST`` holds one per dim)."""
_VLEN_STRING_COST: Final = 64
"""Bytes one variable-length string element is budgeted at (its text lives in the heap)."""

# HDF5 filter ids: deflate, shuffle, fletcher32, nbit, scaleoffset (built into libhdf5)
# and lzf (built into h5py; registered in-process, never loaded as a plugin).
ALLOWED_FILTERS: Final = {
    1: "deflate",
    2: "shuffle",
    3: "fletcher32",
    5: "nbit",
    6: "scaleoffset",
    32000: "lzf",
}

_plugins_disabled = False


def unsafe(
    message: str, *, untrusted: str | None = None, action: str | None = None
) -> UnsafeUpload:
    """An ``INGEST_UNSAFE_FILE`` refusal (object names only ever in ``untrusted_text``)."""
    issue = Issue(
        code=ErrorCode.INGEST_UNSAFE_FILE,
        pointer="/upload",
        message=message[:2000],
        suggested_action=(
            action
            or "Write the file again with h5py, xarray or netCDF4 using only plain datasets, "
            "groups and the built-in compression filters (gzip, shuffle, lzf)."
        )[:2000],
        untrusted_text=untrusted[:8000] if untrusted else None,
    )
    return UnsafeUpload(
        message[:2000],
        pointer="/upload",
        untrusted_text=untrusted[:8000] if untrusted else None,
        issues=[issue],
    )


def disable_plugins() -> None:
    """Make libhdf5 unable to load filter plugins in this process.

    ``HDF5_PLUGIN_PATH`` is read when the library loads its first plugin; it is pointed at a
    fresh empty directory (the supervisor already does this for the worker), and the search
    path h5py exposes is emptied too. ``HDF5_USE_FILE_LOCKING`` is turned off (uploads are
    read-only scratch files; locking fails on some sandbox file systems).
    """
    global _plugins_disabled
    if _plugins_disabled:
        return
    empty = os.environ.get("HDF5_PLUGIN_PATH", "")
    if not empty or not Path(empty).is_dir() or any(Path(empty).iterdir()):
        empty = tempfile.mkdtemp(prefix="mapi-no-hdf5-plugins-")
        os.environ["HDF5_PLUGIN_PATH"] = empty
    os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"
    from h5py import h5pl

    while h5pl.size() > 0:
        h5pl.remove(0)
    h5pl.append(empty.encode())
    _plugins_disabled = True


# ---------------------------------------------------------------------------------- walk


@dataclass(slots=True)
class Hdf5Array:
    """One dataset of the file as its metadata declares it (no data read)."""

    path: str
    addr: int
    """The object's address in the file (the same array under two names has one)."""
    shape: tuple[int, ...]
    dtype: str
    itemsize: int
    chunks: tuple[int, ...] | None
    filters: list[str]
    attrs: dict[str, Any] = field(default_factory=dict)
    """Small scalar attributes (only collected for inspection)."""


@dataclass(slots=True)
class Hdf5Walk:
    """What :func:`walk_hdf5` saw: groups, datasets and warnings."""

    groups: list[str] = field(default_factory=list)
    arrays: list[Hdf5Array] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    declared_bytes: int = 0
    objects: int = 0
    attributes: int = 0


def _join(base: str, name: str) -> str:
    return "/" + name if base == "/" else f"{base}/{name}"


def _decode(raw: bytes | str) -> str:
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


class _Walker:
    def __init__(
        self,
        fid: Any,
        limits: IngestLimits,
        budget: int,
        collect_attrs: bool,
        *,
        attr_bytes: int,
        max_attrs: int,
        file_bytes: int = 0,
    ) -> None:
        self.fid = fid
        self.file_bytes = file_bytes
        self.limits = limits
        self.budget = budget
        self.collect_attrs = collect_attrs
        self.attr_bytes = attr_bytes
        self.max_attrs = max_attrs
        self.out = Hdf5Walk()
        self.seen_objects: dict[int, str] = {}

    # ------------------------------------------------------------------ counting
    def _count(self, n: int = 1) -> None:
        self.out.objects += n
        if self.out.objects > self.limits.max_hdf5_objects:
            raise unsafe(
                f"the file has more than {self.limits.max_hdf5_objects:,} groups, datasets "
                "and links",
                action="Split the file, or keep only the arrays the dataset needs.",
            )

    # ---------------------------------------------------------------- attributes
    def _attrs(self, obj: Any, path: str) -> dict[str, Any]:
        from h5py import h5a, h5t

        n = h5a.get_num_attrs(obj)
        self.out.attributes += n
        if self.out.attributes > self.max_attrs:
            raise unsafe(
                f"the file has more than {self.max_attrs:,} attributes",
                action="Remove the attributes the dataset does not need.",
            )
        kept: dict[str, Any] = {}
        for i in range(n):
            attr = h5a.open(obj, index=i)
            name = _decode(attr.name)
            tid = attr.get_type()
            cls = tid.get_class()
            nelem = int(math.prod(attr.shape)) if attr.shape else 1
            size = int(attr.get_storage_size())
            where = f"{path} @{name}"
            if size > self.attr_bytes:
                raise unsafe(
                    f"an attribute holds {size:,} bytes (the maximum is {self.attr_bytes:,})",
                    untrusted=where,
                )
            variable = cls == h5t.VLEN or (cls == h5t.STRING and tid.is_variable_str())
            if variable and nelem > MAX_VLEN_ATTR_ELEMENTS:
                raise unsafe(
                    f"a variable-length attribute has {nelem:,} elements (the maximum is "
                    f"{MAX_VLEN_ATTR_ELEMENTS:,})",
                    untrusted=where,
                )
            if cls == h5t.STRING and tid.is_variable_str():
                # The text lives in the heap and libhdf5 reads an attribute whole, so its
                # size is only known after the read. Bound the read first: every element's
                # text lies in the file, but elements may share one heap object, so the
                # worst case is elements x file size - it must fit the expansion budget.
                worst = nelem * max(self.file_bytes, 1)
                if nelem > 1 and worst > self.budget:
                    raise unsafe(
                        f"a variable-length text attribute has {nelem:,} elements, which could "
                        f"decode to {worst:,} bytes (the maximum is {self.budget:,})",
                        untrusted=where,
                        action="Store the text as one string, or as a fixed-length string array.",
                    )
                text = _read_attr(attr)
                total = sum(len(t) for t in text)
                if total > self.attr_bytes:
                    raise unsafe(
                        f"an attribute holds {total:,} characters of text (the maximum is "
                        f"{self.attr_bytes:,})",
                        untrusted=where,
                    )
                if self.collect_attrs and nelem == 1 and not attr.shape:
                    kept[name] = text[0] if text else ""
            elif self.collect_attrs and nelem == 1 and cls in (h5t.INTEGER, h5t.FLOAT, h5t.STRING):
                kept[name] = _read_attr(attr)[0]
        return kept

    # ------------------------------------------------------------------- objects
    def walk(self) -> Hdf5Walk:
        from h5py import h5g, h5o

        root = h5g.open(self.fid, b"/")
        info = h5o.get_info(root)
        self.seen_objects[int(info.addr)] = "/"
        self.out.groups.append("/")
        self._count()
        self._attrs(root, "/")
        self._group(root, "/", 0, [int(info.addr)])
        return self.out

    def _group(self, gid: Any, path: str, depth: int, ancestors: list[int]) -> None:
        from h5py import h5l, h5o

        if depth > MAX_HDF5_DEPTH:
            raise unsafe(
                f"the file nests groups deeper than {MAX_HDF5_DEPTH} levels", untrusted=path
            )
        links: list[tuple[bytes, int]] = []
        room = self.limits.max_hdf5_objects - self.out.objects

        def collect(name: bytes, linfo: Any) -> int | None:
            # h5py reuses ``linfo`` between calls (keep the type, not the object); an
            # exception raised in here cannot cross libhdf5, so stop by returning non-None
            links.append((name, int(linfo.type)))
            return 1 if len(links) > room else None

        gid.links.iterate(collect, info=True)
        self._count(len(links))
        for raw, ltype in links:
            name = _decode(raw)
            child = _join(path, name)
            if ltype == h5l.TYPE_EXTERNAL:
                target = gid.links.get_val(raw)
                raise unsafe(
                    "the file has an external link (a reference to another file)",
                    untrusted=f"{child} -> {_decode(target[0])}:{_decode(target[1])}",
                )
            if ltype == h5l.TYPE_SOFT:
                self._soft(gid, raw, path, child)
                continue
            if ltype != h5l.TYPE_HARD:
                raise unsafe("the file has a user-defined link type", untrusted=child)
            oinfo = h5o.get_info(gid, raw)
            addr = int(oinfo.addr)
            if oinfo.type == h5o.TYPE_GROUP:
                if addr in ancestors:
                    raise unsafe(
                        "the file has a group that contains itself (a cycle of hard links)",
                        untrusted=child,
                    )
                if addr in self.seen_objects:
                    self.out.warnings.append(
                        f"group {child[:200]} is a second name of {self.seen_objects[addr][:200]}"
                    )
                    continue
                self.seen_objects[addr] = child
                sub = h5o.open(gid, raw)
                self.out.groups.append(child)
                self._attrs(sub, child)
                self._group(sub, child, depth + 1, [*ancestors, addr])
            elif oinfo.type == h5o.TYPE_DATASET:
                if addr in self.seen_objects:
                    self.out.warnings.append(
                        f"dataset {child[:200]} is a second name of {self.seen_objects[addr][:200]}"
                    )
                    continue
                self.seen_objects[addr] = child
                self._dataset(h5o.open(gid, raw), child, addr)
            else:  # a named datatype: metadata only
                self._attrs(h5o.open(gid, raw), child)

    def _soft(self, gid: Any, raw: bytes, base: str, child: str) -> None:
        """A soft link must resolve, without a cycle, to a dataset of this file."""
        from h5py import h5g, h5l, h5o

        target = _decode(gid.links.get_val(raw))
        seen = {child}
        current = target if target.startswith("/") else _join(base, target)
        for _ in range(MAX_SOFT_LINK_HOPS):
            parts = [p for p in current.split("/") if p not in ("", ".")]
            if any(p == ".." for p in parts):
                raise unsafe("a soft link uses '..'", untrusted=f"{child} -> {target}")
            loc = h5g.open(self.fid, b"/")
            resolved = "/"
            redirect: str | None = None
            for i, part in enumerate(parts):
                bpart = part.encode()
                if not loc.links.exists(bpart):
                    raise unsafe(
                        "the file has a soft link to an object that does not exist",
                        untrusted=f"{child} -> {target}",
                    )
                linfo = loc.links.get_info(bpart)
                here = _join(resolved, part)
                if linfo.type == h5l.TYPE_SOFT:
                    rest = "/".join(parts[i + 1 :])
                    nxt = _decode(loc.links.get_val(bpart))
                    nxt = nxt if nxt.startswith("/") else _join(resolved, nxt)
                    redirect = nxt + ("/" + rest if rest else "")
                    break
                if linfo.type != h5l.TYPE_HARD:
                    raise unsafe(
                        "a soft link leads through an external or user-defined link",
                        untrusted=f"{child} -> {target}",
                    )
                if i < len(parts) - 1:
                    if h5o.get_info(loc, bpart).type != h5o.TYPE_GROUP:
                        raise unsafe(
                            "the file has a soft link to an object that does not exist",
                            untrusted=f"{child} -> {target}",
                        )
                    loc = h5o.open(loc, bpart)
                resolved = here
            if redirect is None:
                last = parts[-1].encode() if parts else None
                kind = h5o.TYPE_GROUP if last is None else h5o.get_info(loc, last).type
                if kind == h5o.TYPE_GROUP:
                    raise unsafe(
                        "the file has a soft link to a group (readers would walk the group "
                        "twice, or forever)",
                        untrusted=f"{child} -> {target}",
                    )
                return
            if redirect in seen:
                raise unsafe("the file has a cycle of soft links", untrusted=f"{child} -> {target}")
            seen.add(redirect)
            current = redirect
        raise unsafe(
            f"a soft link takes more than {MAX_SOFT_LINK_HOPS} hops to resolve",
            untrusted=f"{child} -> {target}",
        )

    def _dataset(self, did: Any, path: str, addr: int) -> None:
        from h5py import h5d, h5s, h5t

        dcpl = did.get_create_plist()
        if dcpl.get_layout() == h5d.VIRTUAL:
            raise unsafe(
                "the file has a virtual dataset (its data lives in other files)", untrusted=path
            )
        if dcpl.get_external_count() > 0:
            raise unsafe(
                "the file has a dataset whose bytes are stored in another file", untrusted=path
            )
        filters: list[str] = []
        for i in range(dcpl.get_nfilters()):
            code, _flags, _values, fname = dcpl.get_filter(i)
            if int(code) not in ALLOWED_FILTERS:
                raise unsafe(
                    f"the file uses compression filter {int(code)}, which the server does not "
                    f"read (allowed: {', '.join(sorted(ALLOWED_FILTERS.values()))})",
                    untrusted=f"{path}: {_decode(fname)}",
                )
            filters.append(ALLOWED_FILTERS[int(code)])
        space = did.get_space()
        stype = space.get_simple_extent_type()
        if stype == h5s.NULL:
            shape: tuple[int, ...] = (0,)
        elif stype == h5s.SCALAR:
            shape = ()
        else:
            shape = tuple(int(s) for s in space.shape)
        if len(shape) > self.limits.max_ndim:
            raise unsafe(
                f"an array has {len(shape)} dims (the maximum is {self.limits.max_ndim})",
                untrusted=path,
            )
        tid = did.get_type()
        cls = tid.get_class()
        itemsize = int(tid.get_size())
        if cls == h5t.STRING and tid.is_variable_str():
            itemsize = _VLEN_STRING_COST
        elif cls not in (h5t.INTEGER, h5t.FLOAT, h5t.STRING, h5t.ENUM, h5t.BITFIELD):
            kind = {
                h5t.VLEN: "variable-length sequences",
                h5t.COMPOUND: "compound records",
                h5t.ARRAY: "array elements",
                h5t.REFERENCE: "object references",
                h5t.OPAQUE: "opaque bytes",
            }.get(cls, f"type class {cls}")
            raise unsafe(
                f"an array holds {kind}, which the server does not read",
                untrusted=path,
                action="Store numbers, booleans or text in plain arrays.",
            )
        nbytes = int(math.prod(shape)) * itemsize
        self.out.declared_bytes += nbytes
        if self.out.declared_bytes > self.budget:
            raise unsafe(
                f"the arrays of the file declare {self.out.declared_bytes:,} bytes, more than "
                f"the {self.budget:,} the upload may expand to",
                untrusted=path,
                action="Split the file, or store the arrays with less compression.",
            )
        chunks: tuple[int, ...] | None = None
        if dcpl.get_layout() == h5d.CHUNKED:
            chunks = tuple(int(c) for c in dcpl.get_chunk())
            chunk_bytes = int(math.prod(chunks)) * itemsize
            if chunk_bytes > self.budget:
                raise unsafe(
                    f"an array has chunks of {chunk_bytes:,} bytes, more than the "
                    f"{self.budget:,} the upload may expand to",
                    untrusted=path,
                )
        attrs = self._attrs(did, path)
        self.out.arrays.append(
            Hdf5Array(
                path=path,
                addr=addr,
                shape=shape,
                dtype=_dtype_name(did),
                itemsize=itemsize,
                chunks=chunks,
                filters=filters,
                attrs=attrs,
            )
        )


def _read_attr(attr: Any) -> list[Any]:
    """An attribute's values as a flat list (``str`` for text)."""
    import numpy as np

    buf = np.empty(attr.shape, dtype=attr.dtype)
    attr.read(buf)
    out: list[Any] = []
    for v in np.asarray(buf, dtype=object).reshape(-1).tolist():
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        out.append(v)
    return out


def _dtype_name(did: Any) -> str:
    try:
        dt = did.dtype
    except (TypeError, ValueError):
        return "unknown"
    from h5py import check_string_dtype

    if check_string_dtype(dt) is not None:
        return "str"
    return str(dt)[:64]


@contextmanager
def _open(path: Path) -> Iterator[Any]:
    from h5py import h5f

    try:
        fid = h5f.open(os.fsencode(path), h5f.ACC_RDONLY)
    except (OSError, ValueError) as exc:
        from mapi_runner.worker.tabular import problem

        raise problem(
            ErrorCode.SCHEMA_INVALID,
            "the upload is not a readable HDF5 file",
            pointer="/upload",
            action="Upload the file as h5py, xarray or netCDF4 wrote it.",
            untrusted=str(exc)[:2000],
        ) from None
    try:
        yield fid
    finally:
        fid.close()


def walk_hdf5(
    path: Path,
    limits: IngestLimits,
    *,
    collect_attrs: bool = False,
    lenient_attrs: bool = False,
) -> Hdf5Walk:
    """Check ``path`` (see the module docstring) and describe its groups and datasets.

    ``lenient_attrs`` relaxes the attribute caps for self-describing NetCDF/xarray files
    (format ``netcdf`` and the ``inspect`` listing): an attribute may hold up to the file's
    size, and the file up to :data:`NETCDF_ATTRS_PER_VARIABLE` attributes per variable.

    Raises :class:`~mapi_spec.errors.UnsafeUpload` for anything unsafe and a
    ``SCHEMA_INVALID`` error when the file is not HDF5 at all.
    """
    disable_plugins()
    size = path.stat().st_size
    budget = limits.expansion_budget(size)
    attr_bytes = limits.max_attr_bytes
    max_attrs = limits.max_hdf5_objects
    if lenient_attrs:
        attr_bytes = max(attr_bytes, size)
        max_attrs = max(max_attrs, NETCDF_ATTRS_PER_VARIABLE * limits.max_columns)
    with _open(path) as fid:
        userblock = int(fid.get_create_plist().get_userblock())
        if userblock > MAX_USERBLOCK:
            raise unsafe(
                f"the file starts with a {userblock:,}-byte user block (the maximum is "
                f"{MAX_USERBLOCK:,})"
            )
        try:
            walker = _Walker(
                fid,
                limits,
                budget,
                collect_attrs,
                attr_bytes=attr_bytes,
                max_attrs=max_attrs,
                file_bytes=size,
            )
            return walker.walk()
        except RecursionError:
            raise unsafe(f"the file nests groups deeper than {MAX_HDF5_DEPTH} levels") from None


def check_hdf5(path: Path, limits: IngestLimits, *, lenient_attrs: bool = False) -> list[str]:
    """Refuse an unsafe HDF5 file (:class:`~mapi_spec.errors.UnsafeUpload`); returns the
    walk's warnings (second names of objects). Call it BEFORE any xarray or h5py high-level
    open of an upload."""
    return walk_hdf5(path, limits, lenient_attrs=lenient_attrs).warnings
