"""``plugin_inspect`` (v0.19): check an uploaded wheel as an archive, never as code.

The control plane never opens an upload. This module runs in the sandbox as the
``plugin_inspect`` run (:mod:`mapi_runner.worker.plugin_inspect`) and reads the wheel with
the stdlib :mod:`zipfile` only: the central directory, the local headers and each member's
bytes, streamed into ``sha256``. It **never extracts a member to disk and never imports
anything from the archive** (no ``importlib.metadata`` call on the wheel either: its
``METADATA``, ``WHEEL``, ``entry_points.txt`` and ``RECORD`` are parsed here as text).

The result is a :class:`~mapi_spec.plugins.PluginInspection`: the facts the reviewer sees
(distribution, version, top-level package, entry points, requirements resolved against the
base image, the SBOM) and one :class:`~mapi_spec.plugins.InspectProblem` per refusal reason.
The checks (threats T19.2-T19.9, T19.14), by :data:`~mapi_spec.plugins.InspectCheck` name:

* archive (T19.4, T19.5): ``sha_mismatch``, ``archive_size``, ``member_count``,
  ``member_size``, ``ratio``, ``zip_structure`` (unreadable directory, local header that
  disagrees with the central directory, overlapping entries, ZIP64 records, data before the
  first entry), ``encrypted``, ``compression_method`` (stored and deflate only),
  ``path_unsafe``, ``link_member``, ``special_member``, ``duplicate_name``,
  ``case_collision``, ``nested_archive``;
* content (T19.2, T19.3, T19.6): ``native_code`` and ``bytecode`` (by extension and by magic
  bytes), ``pth_file``, ``site_hook`` (``sitecustomize``/``usercustomize``, a top-level
  ``__main__.py``, ``.egg-link``), ``top_level_name`` (exactly one top-level package, named
  the namespace, shadowing nothing in the stdlib or the image), ``namespace_package``,
  ``data_dir``, ``text_unsafe``;
* metadata (T19.7-T19.9): ``dist_name``, ``dist_info`` (one ``.dist-info``, matching names,
  a ``RECORD`` that lists every file with its hash), ``wheel_tag`` (``py3-none-any``, purelib),
  ``metadata_invalid``, ``entry_point_group`` (the five ``mapi.*`` groups only),
  ``entry_point_target`` (inside the namespace package), ``requirement_url``,
  ``requirement_unlocked`` (every ``Requires-Dist`` already satisfied by the base image,
  extras resolved recursively), ``requires_python``.

Requirements are resolved against :class:`InstalledIndex`, the distributions of the image
the run executes in (:func:`installed_index`). PEP 440 versions, specifiers and PEP 508
markers are implemented here (a subset sufficient for ``Requires-Dist``): this module is
stdlib plus :mod:`mapi_spec` only (import-linter contract), and ``packaging`` is not a
declared dependency of the runner.
"""

from __future__ import annotations

import base64
import configparser
import csv
import hashlib
import io
import itertools
import os
import platform
import re
import stat
import struct
import sys
import unicodedata
import zipfile
import zlib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from email.parser import HeaderParser
from pathlib import Path, PurePosixPath
from typing import Final

from mapi_spec.jobs import PluginInspectJob
from mapi_spec.namespaces import is_builtin_distribution
from mapi_spec.plugins import (
    INSPECTION_MAX_BYTES,
    PLUGIN_ENTRY_POINT_GROUPS,
    EntryPointDecl,
    InspectCheck,
    InspectProblem,
    PluginInspection,
    PluginSbom,
    RequirementFact,
    SbomFile,
)
from mapi_spec.text_safety import has_unsafe_text, normalize_untrusted_text

__all__ = [
    "InstalledIndex",
    "InvalidVersion",
    "Version",
    "inspect_wheel",
    "installed_index",
    "iter_problem_checks",
    "marker_matches",
    "parse_requirement",
    "specifier_contains",
]

_CHUNK: Final = 1 << 16
_SNIFF: Final = 512
_PROBLEMS_MAX: Final = 200
_ENTRY_POINTS_MAX: Final = 500
_REQUIRES_MAX: Final = 200
_TAGS_MAX: Final = 8
_TOP_LEVEL_MAX: Final = 16
_RATIO_MEMBER_FLOOR: Final = 1 << 20
"""A single member is held to the ratio cap only from this size up (small text compresses
well; the archive as a whole is always held to it)."""
_EXTRAS_DEPTH: Final = 8
_ZIP64_FIELD: Final = 0xFFFFFFFF
_IMAGE_DIGEST_FILE: Final = Path("/opt/mapi/catalog.digest")
_SHA256_REF: Final = re.compile(r"^sha256:[0-9a-f]{64}$")

# metadata files are read into memory, so each has a cap of its own
_META_CAPS: Final[Mapping[str, int]] = {
    "METADATA": 1 << 20,
    "WHEEL": 64 << 10,
    "entry_points.txt": 256 << 10,
    "RECORD": 2 << 20,
}
_RECORD_SIGNATURES: Final = frozenset({"RECORD.jws", "RECORD.p7s"})

_NATIVE_SUFFIXES: Final = frozenset(
    {".so", ".pyd", ".dll", ".dylib", ".exe", ".o", ".a", ".lib", ".obj", ".bundle", ".sl"}
)
_BYTECODE_SUFFIXES: Final = frozenset({".pyc", ".pyo"})
_ARCHIVE_SUFFIXES: Final = frozenset(
    {
        ".zip", ".whl", ".egg", ".jar", ".tar", ".gz", ".tgz", ".bz2", ".tbz", ".xz", ".txz",
        ".7z", ".rar", ".zst", ".lzma", ".pex", ".pyz",
    }
)  # fmt: skip
_SITE_HOOKS: Final = frozenset({"sitecustomize", "usercustomize"})

# ELF, Mach-O (32/64, both byte orders, fat) and the archive formats, by magic bytes
_NATIVE_MAGIC: Final = (
    b"\x7fELF",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
)
_ARCHIVE_MAGIC: Final = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"\x1f\x8b",
    b"\xfd7zXZ\x00",
    b"7z\xbc\xaf\x27\x1c",
    b"Rar!\x1a\x07",
    b"\x28\xb5\x2f\xfd",
)
_BZIP2_MAGIC: Final = re.compile(rb"^BZh[1-9]1AY&SY")

_LOCAL_HEADER: Final = struct.Struct("<IHHHHHIIIHH")
_LOCAL_SIGNATURE: Final = 0x04034B50
_FLAG_ENCRYPTED: Final = 0x1
_FLAG_DESCRIPTOR: Final = 0x8
_FLAG_UTF8: Final = 0x800

_IDENTIFIER: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EP_NAME: Final = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}$")
_EP_VALUE: Final = re.compile(
    r"^(?P<module>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
    r"(?::(?P<attr>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*))?$"
)


# ================================================================================ versions


class InvalidVersion(ValueError):
    """A string that is not a PEP 440 version, specifier, requirement or marker."""


_VERSION_RE: Final = re.compile(
    r"""
    ^\s*v?
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?P<pre>[-_.]?(?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)[-_.]?(?P<pre_n>[0-9]+)?)?
    (?P<post>(?:-(?P<post_n1>[0-9]+))|(?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?))?
    (?P<dev>[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)
_PRE_LETTER: Final[Mapping[str, str]] = {
    "a": "a",
    "alpha": "a",
    "b": "b",
    "beta": "b",
    "c": "rc",
    "pre": "rc",
    "preview": "rc",
    "rc": "rc",
}

_Key = tuple[object, ...]


@dataclass(frozen=True, slots=True)
class Version:
    """A PEP 440 version, compared like ``packaging.version.Version``."""

    epoch: int
    release: tuple[int, ...]
    pre: tuple[str, int] | None = None
    post: int | None = None
    dev: int | None = None
    local: tuple[int | str, ...] | None = None

    @classmethod
    def parse(cls, text: str) -> Version:
        m = _VERSION_RE.match(text)
        if m is None:
            raise InvalidVersion(f"{text!r} is not a PEP 440 version")
        pre = None
        if m.group("pre"):
            pre = (_PRE_LETTER[m.group("pre_l").lower()], int(m.group("pre_n") or 0))
        post = None
        if m.group("post"):
            post = int(m.group("post_n1") or m.group("post_n2") or 0)
        dev = int(m.group("dev_n") or 0) if m.group("dev") else None
        local = None
        if m.group("local"):
            local = tuple(
                int(p) if p.isdigit() else p.lower() for p in re.split(r"[-_.]", m.group("local"))
            )
        return cls(
            epoch=int(m.group("epoch") or 0),
            release=tuple(int(p) for p in m.group("release").split(".")),
            pre=pre,
            post=post,
            dev=dev,
            local=local,
        )

    def __str__(self) -> str:
        out = f"{self.epoch}!" if self.epoch else ""
        out += ".".join(str(p) for p in self.release)
        if self.pre is not None:
            out += f"{self.pre[0]}{self.pre[1]}"
        if self.post is not None:
            out += f".post{self.post}"
        if self.dev is not None:
            out += f".dev{self.dev}"
        if self.local is not None:
            out += "+" + ".".join(str(p) for p in self.local)
        return out

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None or self.dev is not None

    @property
    def is_postrelease(self) -> bool:
        return self.post is not None

    @property
    def public(self) -> Version:
        """This version without its local label."""
        return Version(self.epoch, self.release, self.pre, self.post, self.dev, None)

    @property
    def base(self) -> Version:
        """Epoch and release only."""
        return Version(self.epoch, self.release)

    def key(self) -> _Key:
        release = list(self.release)
        while len(release) > 1 and release[-1] == 0:
            release.pop()
        # the sort rules of PEP 440: a dev release sorts before its pre-releases, a release
        # without a pre-release after them; no post release before any; local labels last
        pre: _Key
        if self.pre is None and self.post is None and self.dev is not None:
            pre = (0,)
        elif self.pre is None:
            pre = (2,)
        else:
            pre = (1, self.pre[0], self.pre[1])
        post: _Key = (0,) if self.post is None else (1, self.post)
        dev: _Key = (1,) if self.dev is None else (0, self.dev)
        local: _Key
        if self.local is None:
            local = (0,)
        else:
            local = (
                1,
                tuple((1, p, "") if isinstance(p, int) else (0, 0, p) for p in self.local),
            )
        return (self.epoch, tuple(release), pre, post, dev, local)

    def __lt__(self, other: Version) -> bool:
        return self.key() < other.key()

    def __le__(self, other: Version) -> bool:
        return self.key() <= other.key()

    def __gt__(self, other: Version) -> bool:
        return self.key() > other.key()

    def __ge__(self, other: Version) -> bool:
        return self.key() >= other.key()

    def same(self, other: Version) -> bool:
        return self.key() == other.key()


_CLAUSE_RE: Final = re.compile(r"^\s*(~=|===|==|!=|<=|>=|<|>)\s*([^\s,;]+)\s*$")
_PREFIX_RE: Final = re.compile(r"^(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*\.\*$")


def _parse_clauses(spec: str) -> list[tuple[str, str]]:
    """``[(op, version), ...]`` of a comma-separated specifier (``""`` -> ``[]``)."""
    spec = spec.strip()
    if not spec:
        return []
    out = []
    for part in spec.split(","):
        m = _CLAUSE_RE.match(part)
        if m is None:
            raise InvalidVersion(f"{part.strip()!r} is not a version specifier")
        op, ver = m.group(1), m.group(2)
        if ver.endswith(".*"):
            if op not in ("==", "!=") or not _PREFIX_RE.match(ver):
                raise InvalidVersion(f"{part.strip()!r} is not a version specifier")
        elif op != "===":
            parsed = Version.parse(ver)
            if op == "~=" and len(parsed.release) < 2:
                raise InvalidVersion(f"{part.strip()!r} needs at least two release numbers")
            if parsed.local is not None and op not in ("==", "!="):
                raise InvalidVersion(f"{part.strip()!r} cannot use a local version")
        out.append((op, ver))
    return out


def _prefix_match(v: Version, prefix: str) -> bool:
    p = Version.parse(prefix)
    if v.epoch != p.epoch:
        return False
    n = len(p.release)
    release = v.release + (0,) * max(0, n - len(v.release))
    return release[:n] == p.release


def _clause_ok(op: str, spec: str, v: Version, raw: str) -> bool:
    if op == "===":
        return raw.strip().lower() == spec.lower()
    if spec.endswith(".*"):
        match = _prefix_match(v.public, spec[:-2])
        return match if op == "==" else not match
    s = Version.parse(spec)
    if op in ("==", "!="):
        equal = (v.public if s.local is None else v).same(s)
        return equal if op == "==" else not equal
    if op == "<=":
        return v.public <= s
    if op == ">=":
        return v.public >= s
    if op == "<":
        if not v < s:
            return False
        return s.is_prerelease or not v.is_prerelease or not v.base.same(s.base)
    if op == ">":
        if not v > s:
            return False
        if not s.is_postrelease and v.is_postrelease and v.base.same(s.base):
            return False
        return v.local is None or not v.base.same(s.base)
    # ~=: at least this version, and the same release prefix without its last number
    prefix = ".".join(str(p) for p in s.release[:-1])
    if s.epoch:
        prefix = f"{s.epoch}!{prefix}"
    return v.public >= s and _prefix_match(v.public, prefix)


def specifier_contains(spec: str, version: str) -> bool:
    """True if ``version`` satisfies every clause of ``spec`` (PEP 440; pre-releases are
    accepted, since the version is one the image already has). Raises
    :class:`InvalidVersion` for a malformed specifier; an unparsable ``version`` is never
    contained."""
    clauses = _parse_clauses(spec)
    try:
        v = Version.parse(version)
    except InvalidVersion:
        return all(op == "===" and version.strip().lower() == s.lower() for op, s in clauses)
    return all(_clause_ok(op, s, v, version) for op, s in clauses)


# ================================================================================= markers

_MARKER_TOKEN: Final = re.compile(
    r"""\s*(?:
        (?P<str>'[^']*'|"[^"]*")
      | (?P<op>===|==|!=|<=|>=|~=|<|>)
      | (?P<lp>\()
      | (?P<rp>\))
      | (?P<word>[A-Za-z_][A-Za-z0-9_.]*)
    )""",
    re.VERBOSE,
)
_MARKER_VARIABLES: Final = frozenset(
    {
        "python_version",
        "python_full_version",
        "os_name",
        "sys_platform",
        "platform_release",
        "platform_system",
        "platform_version",
        "platform_machine",
        "platform_python_implementation",
        "implementation_name",
        "implementation_version",
        "extra",
    }
)
_MARKER_ALIASES: Final[Mapping[str, str]] = {
    "os.name": "os_name",
    "sys.platform": "sys_platform",
    "platform.version": "platform_version",
    "platform.machine": "platform_machine",
    "platform.python_implementation": "platform_python_implementation",
    "python_implementation": "platform_python_implementation",
}
_VERSION_OPS: Final = frozenset({"===", "==", "!=", "<=", ">=", "~=", "<", ">"})


def canonical_name(name: str) -> str:
    """The PEP 503 form of a distribution or extra name."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _tokens(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = _MARKER_TOKEN.match(text, pos)
        if m is None or m.end() == pos:
            raise InvalidVersion(f"{text!r} is not an environment marker")
        kind = m.lastgroup
        assert kind is not None
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


class _MarkerParser:
    """A recursive-descent evaluator for PEP 508 markers against one environment."""

    def __init__(self, text: str, env: Mapping[str, str]) -> None:
        self.toks = _tokens(text)
        self.i = 0
        self.env = env

    def peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise InvalidVersion("the environment marker ends too early")
        self.i += 1
        return tok

    def run(self) -> bool:
        try:
            result = self.expr()
        except RecursionError:
            raise InvalidVersion("the environment marker is nested too deeply") from None
        if self.peek() is not None:
            raise InvalidVersion("the environment marker has extra text")
        return result

    def expr(self) -> bool:
        result = self.conj()
        while self.peek() == ("word", "or"):
            self.take()
            rhs = self.conj()
            result = result or rhs
        return result

    def conj(self) -> bool:
        result = self.atom()
        while self.peek() == ("word", "and"):
            self.take()
            rhs = self.atom()
            result = result and rhs
        return result

    def atom(self) -> bool:
        if self.peek() == ("lp", "("):
            self.take()
            result = self.expr()
            if self.take() != ("rp", ")"):
                raise InvalidVersion("the environment marker has an unclosed bracket")
            return result
        lhs, lvar = self.value()
        op = self.operator()
        rhs, rvar = self.value()
        if lvar == "extra" or rvar == "extra":
            lhs, rhs = canonical_name(lhs), canonical_name(rhs)
        return _compare(lhs, op, rhs)

    def value(self) -> tuple[str, str | None]:
        kind, text = self.take()
        if kind == "str":
            return text[1:-1], None
        if kind == "word":
            name = _MARKER_ALIASES.get(text, text)
            if name in _MARKER_VARIABLES:
                return self.env.get(name, ""), name
        raise InvalidVersion(f"{text!r} is not a marker value")

    def operator(self) -> str:
        kind, text = self.take()
        if kind == "op":
            return text
        if (kind, text) == ("word", "in"):
            return "in"
        if (kind, text) == ("word", "not") and self.take() == ("word", "in"):
            return "not in"
        raise InvalidVersion(f"{text!r} is not a marker operator")


def _compare(lhs: str, op: str, rhs: str) -> bool:
    if op in _VERSION_OPS:
        try:
            return specifier_contains(f"{op}{rhs}", lhs)
        except InvalidVersion:
            pass  # not versions: compare as strings below
    if op == "in":
        return lhs in rhs
    if op == "not in":
        return lhs not in rhs
    if op == "==":
        return lhs == rhs
    if op == "!=":
        return lhs != rhs
    if op == "<":
        return lhs < rhs
    if op == "<=":
        return lhs <= rhs
    if op == ">":
        return lhs > rhs
    if op == ">=":
        return lhs >= rhs
    raise InvalidVersion(f"{op!r} cannot compare {lhs!r} and {rhs!r}")


def marker_env(python_version: str, *, extra: str = "") -> dict[str, str]:
    """The PEP 508 environment of this interpreter, with ``python_full_version`` taken from
    ``python_version`` (the image's Python, from :class:`InstalledIndex`)."""
    impl = sys.implementation
    iv = impl.version
    impl_version = f"{iv.major}.{iv.minor}.{iv.micro}"
    if iv.releaselevel != "final":
        impl_version += iv.releaselevel[0] + str(iv.serial)
    return {
        "python_version": ".".join(python_version.split(".")[:2]),
        "python_full_version": python_version,
        "os_name": os.name,
        "sys_platform": sys.platform,
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "platform_machine": platform.machine(),
        "platform_python_implementation": platform.python_implementation(),
        "implementation_name": impl.name,
        "implementation_version": impl_version,
        "extra": extra,
    }


def marker_matches(marker: str, env: Mapping[str, str]) -> bool:
    """Evaluate a PEP 508 marker. Raises :class:`InvalidVersion` when it is malformed."""
    return _MarkerParser(marker, env).run()


# ============================================================================ requirements

_REQUIREMENT_RE: Final = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*"
    r"(?:\[(?P<extras>[^\]]*)\])?\s*(?P<rest>.*)$",
    re.DOTALL,
)


_REQUIREMENT_MAX_LEN: Final = 1024


@dataclass(frozen=True, slots=True)
class Requirement:
    """One parsed ``Requires-Dist`` line (PEP 508)."""

    name: str
    """Canonical (PEP 503) name."""
    extras: tuple[str, ...]
    specifier: str
    marker: str | None
    url: str | None


def parse_requirement(line: str) -> Requirement:
    """Parse a PEP 508 requirement. Raises :class:`InvalidVersion` when it is malformed
    or longer than 1024 characters."""
    if len(line) > _REQUIREMENT_MAX_LEN:
        raise InvalidVersion("the requirement is longer than 1024 characters")
    m = _REQUIREMENT_RE.match(line)
    if m is None:
        raise InvalidVersion(f"{line!r} is not a requirement")
    extras: tuple[str, ...] = ()
    if m.group("extras") is not None:
        parts = [p.strip() for p in m.group("extras").split(",") if p.strip()]
        if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", p) for p in parts):
            raise InvalidVersion(f"{line!r} has a malformed extra")
        extras = tuple(canonical_name(p) for p in parts)
    rest = m.group("rest").strip()
    name = canonical_name(m.group("name"))
    if rest.startswith("@"):
        # PEP 508: the marker of a URL requirement follows " ;" (the URL may hold ";")
        url, _, marker = rest[1:].partition(" ;")
        return Requirement(name, extras, "", marker.strip() or None, url.strip())
    spec, _, marker = rest.partition(";")
    spec = spec.strip()
    if spec.startswith("(") and spec.endswith(")"):
        spec = spec[1:-1].strip()
    _parse_clauses(spec)
    clean_marker = marker.strip() or None
    if clean_marker is not None:
        _tokens(clean_marker)
    return Requirement(name, extras, spec, clean_marker, None)


# ========================================================================= installed index


@dataclass(frozen=True, slots=True)
class InstalledIndex:
    """The distributions of the image the inspect run executes in."""

    dists: Mapping[str, str]
    """Canonical distribution name -> installed version."""
    top_level: frozenset[str]
    """Every top-level module or package those distributions install."""
    requires_python: str
    """The image's Python version (``"3.12.15"``)."""
    requires: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Canonical name -> its ``Requires-Dist`` lines (to resolve extras such as
    ``mapi-plots[recipes]``)."""
    image_digest: str | None = None
    """``sha256:…`` of the image (``/opt/mapi/catalog.digest``); :meth:`digest` falls back to
    a digest of the installed set."""

    def digest(self) -> str:
        """The base image digest recorded in the SBOM."""
        if self.image_digest is not None and _SHA256_REF.match(self.image_digest):
            return self.image_digest
        text = "\n".join(
            [f"python=={self.requires_python}"]
            + [f"{n}=={v}" for n, v in sorted(self.dists.items())]
        )
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _top_level_of(files: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for f in files:
        head = PurePosixPath(f).parts[0] if f else ""
        if not head or head.startswith(".") or head.endswith((".dist-info", ".egg-info", ".data")):
            continue
        if head in ("__pycache__", "..") or head.endswith(".pth"):
            continue
        name = head.split(".", 1)[0]
        if _IDENTIFIER.match(name):
            out.add(name)
    return out


def _editable_top_level(pth: Path) -> set[str]:
    """Top-level names under the directories an installed ``.pth`` adds (editable installs
    list no package files). This is the image's own trusted ``.pth``, read as text; lines
    that ``import`` are skipped, never run."""
    out: set[str] = set()
    try:
        lines = pth.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith(("#", "import ", "import\t")):
            continue
        root = Path(line)
        if not root.is_absolute():
            root = pth.parent / root
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name.split(".", 1)[0]
            is_module = entry.suffix in (".py", ".so", ".pyd") and entry.is_file()
            if _IDENTIFIER.match(name) and (is_module or (entry / "__init__.py").is_file()):
                out.add(name)
    return out


def installed_index() -> InstalledIndex:
    """Index the distributions of the running interpreter (``importlib.metadata``): only the
    image's own site-packages are on the path of an inspect run, never the upload."""
    from importlib.metadata import distributions

    dists: dict[str, str] = {}
    requires: dict[str, tuple[str, ...]] = {}
    top: set[str] = set()
    for dist in distributions():
        raw_name = dist.metadata["Name"] if dist.metadata else None
        if not raw_name:
            continue
        name = canonical_name(str(raw_name))
        if name in dists:
            continue  # first on the path wins, as for the import system
        dists[name] = str(dist.version)
        requires[name] = tuple(dist.requires or ())
        listed = dist.read_text("top_level.txt")
        if listed:
            top.update(n.strip() for n in listed.splitlines() if _IDENTIFIER.match(n.strip()))
            continue
        files = list(dist.files or ())
        top.update(_top_level_of(str(f) for f in files))
        for f in files:
            if str(f).endswith(".pth"):
                top.update(_editable_top_level(Path(str(dist.locate_file(f)))))
    digest = None
    try:
        text = _IMAGE_DIGEST_FILE.read_text(encoding="utf-8").strip()
        digest = text if _SHA256_REF.match(text) else None
    except OSError:
        pass
    return InstalledIndex(
        dists=dists,
        top_level=frozenset(top),
        requires_python=platform.python_version(),
        requires=requires,
        image_digest=digest,
    )


# ================================================================================ problems


class _Problems:
    """The problems found so far: one per (check, path, message), at most 200."""

    def __init__(self) -> None:
        self.items: list[InspectProblem] = []
        self._seen: set[tuple[str, str | None, str]] = set()
        self.dropped = 0

    def add(self, check: InspectCheck, message: str, path: str | None = None) -> None:
        key = (check, path, message)
        if key in self._seen:
            return
        self._seen.add(key)
        if len(self.items) >= _PROBLEMS_MAX - 1:
            self.dropped += 1
            if len(self.items) == _PROBLEMS_MAX - 1:
                self.items.append(
                    InspectProblem(
                        check=check,
                        path=None,
                        message="The wheel has more problems than this list shows. "
                        "Correct the problems above and upload it again.",
                    )
                )
            return
        self.items.append(
            InspectProblem(
                check=check,
                path=_clip(path, 255) if path is not None else None,
                message=normalize_untrusted_text(message, max_len=500),
            )
        )

    def has(self, *checks: InspectCheck) -> bool:
        return any(p.check in checks for p in self.items)

    def __bool__(self) -> bool:
        return bool(self.items)


def _clip(text: str, n: int) -> str:
    return normalize_untrusted_text(text, max_len=n)


def _show(text: str, n: int = 80) -> str:
    """Untrusted text quoted in a platform message (normalized, short)."""
    clean = normalize_untrusted_text(text, max_len=n)
    return repr(clean)


# ================================================================================= archive


@dataclass(slots=True)
class _Member:
    info: zipfile.ZipInfo
    name: str
    """The name without a trailing ``/``."""
    is_dir: bool
    sha256: str | None = None
    head: bytes = b""


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _has_zip64(info: zipfile.ZipInfo) -> bool:
    if max(info.file_size, info.compress_size, info.header_offset) >= _ZIP64_FIELD:
        return True
    extra = info.extra
    pos = 0
    while pos + 4 <= len(extra):
        tag, size = struct.unpack_from("<HH", extra, pos)
        if tag == 0x0001:
            return True
        pos += 4 + size
    return False


def _raw_name(info: zipfile.ZipInfo) -> bytes:
    encoding = "utf-8" if info.flag_bits & _FLAG_UTF8 else "cp437"
    try:
        return info.orig_filename.encode(encoding)
    except UnicodeEncodeError:
        return info.orig_filename.encode("utf-8")


def _check_layout_on_disk(
    path: Path, zf: zipfile.ZipFile, infos: Sequence[zipfile.ZipInfo], problems: _Problems
) -> None:
    """Local headers against the central directory; entries inside the file, in order, not
    overlapping (overlapping entries are how a small zip expands to gigabytes)."""
    size = path.stat().st_size
    cd_start = int(getattr(zf, "start_dir", size))
    spans: list[tuple[int, int, str]] = []
    with path.open("rb") as raw:
        for info in infos:
            name = info.orig_filename
            off = info.header_offset
            if off < 0 or off + _LOCAL_HEADER.size > cd_start:
                problems.add(
                    "zip_structure",
                    "An entry points outside the file data of the archive. Build the wheel "
                    "again with a standard tool.",
                    name,
                )
                continue
            raw.seek(off)
            head = raw.read(_LOCAL_HEADER.size)
            if len(head) < _LOCAL_HEADER.size:
                problems.add("zip_structure", "An entry header is cut off.", name)
                continue
            (sig, _ver, flags, method, _t, _d, crc, csize, usize, nlen, xlen) = (
                _LOCAL_HEADER.unpack(head)
            )
            local_name = raw.read(nlen)
            if sig != _LOCAL_SIGNATURE:
                problems.add("zip_structure", "An entry has no valid local header.", name)
                continue
            mismatch = local_name != _raw_name(info) or method != info.compress_type
            if not (flags & _FLAG_DESCRIPTOR) and not mismatch:
                mismatch = (crc, csize, usize) != (info.CRC, info.compress_size, info.file_size)
            if mismatch:
                problems.add(
                    "zip_structure",
                    "The local header of an entry does not agree with the central directory "
                    "(name, method or sizes differ). Tools could read different files from "
                    "this archive. Build the wheel again with a standard tool.",
                    name,
                )
            start = off + _LOCAL_HEADER.size + nlen + xlen
            end = start + info.compress_size
            if end > cd_start:
                problems.add(
                    "zip_structure",
                    "The data of an entry runs past the end of the file data. The declared "
                    "sizes are not true.",
                    name,
                )
            spans.append((off, end, name))
    spans.sort()
    if spans and spans[0][0] != 0:
        problems.add(
            "zip_structure",
            "The archive has data before its first entry. Upload the wheel that the build "
            "tool wrote, without changes.",
        )
    for (_a0, a_end, a_name), (b_off, _b_end, b_name) in itertools.pairwise(spans):
        if b_off < a_end:
            problems.add(
                "zip_structure",
                f"The entries {_show(a_name)} and {_show(b_name)} overlap. Overlapping "
                "entries can expand a small file into a very large one.",
                b_name,
            )


def _unsafe_path(name: str, limits_path: int, limits_depth: int) -> str | None:
    """Why a member name is unsafe to extract (plain text), else ``None``."""
    if "\x00" in name:
        return "The file name contains a NUL character."
    if "\\" in name:
        return "The file name contains a backslash. Use / between directories."
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return "The file name is an absolute path."
    bare = name[:-1] if name.endswith("/") else name
    parts = bare.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return "The file name contains an empty, '.' or '..' part."
    if len(name.encode("utf-8", "surrogateescape")) > limits_path:
        return f"The file name is longer than {limits_path} bytes."
    if len(parts) > limits_depth:
        return f"The file is more than {limits_depth} directories deep."
    return None


def _file_kind(info: zipfile.ZipInfo) -> str:
    """``"file"``, ``"dir"``, ``"link"`` or ``"special"`` from the Unix mode bits."""
    mode = info.external_attr >> 16
    fmt = stat.S_IFMT(mode)
    if fmt == stat.S_IFLNK:
        return "link"
    if fmt in (0, stat.S_IFREG):
        return "dir" if info.orig_filename.endswith("/") else "file"
    if fmt == stat.S_IFDIR:
        return "dir"
    return "special"


def _scan_directory(
    path: Path,
    zf: zipfile.ZipFile,
    job: PluginInspectJob,
    archive_size: int,
    problems: _Problems,
) -> tuple[list[_Member], int, bool]:
    """Every central directory check (no member data is read).

    Returns ``(members, uncompressed bytes, safe to stream)``: member data is streamed only
    when no size, structure, encryption or method problem was found.
    """
    limits = job.limits
    infos = zf.infolist()
    if len(infos) > limits.max_members:
        problems.add(
            "member_count",
            f"The wheel has {len(infos):,} entries. The maximum is {limits.max_members:,}.",
        )
    members: list[_Member] = []
    seen: dict[str, int] = {}
    folded: dict[str, str] = {}
    total = 0
    for info in infos:
        orig = info.orig_filename
        total += info.file_size
        if info.file_size > limits.max_member:
            problems.add(
                "member_size",
                f"A file expands to {info.file_size:,} bytes. The maximum is "
                f"{limits.max_member:,}.",
                orig,
            )
        if info.file_size >= _RATIO_MEMBER_FLOOR and info.file_size > limits.max_ratio * max(
            info.compress_size, 1
        ):
            problems.add(
                "ratio",
                f"A file expands more than {limits.max_ratio} times. Store large data "
                "with less compression, or leave it out of the wheel.",
                orig,
            )
        if info.flag_bits & _FLAG_ENCRYPTED:
            problems.add("encrypted", "The file is encrypted. Upload an unencrypted wheel.", orig)
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            problems.add(
                "compression_method",
                f"The file uses compression method {info.compress_type}. A wheel can use "
                "only 'stored' or 'deflate'.",
                orig,
            )
        if _has_zip64(info):
            problems.add(
                "zip_structure",
                "The entry uses ZIP64 records, which a wheel of this size does not need. "
                "Build the wheel again with a standard tool.",
                orig,
            )
        why = _unsafe_path(orig, limits.max_path, limits.max_depth)
        if why is not None:
            problems.add("path_unsafe", why, orig)
        if has_unsafe_text(orig):
            problems.add(
                "text_unsafe",
                "The file name contains invisible, control or direction-changing characters.",
                orig,
            )
        kind = _file_kind(info)
        if kind == "link":
            problems.add("link_member", "The entry is a symbolic link. Wheels hold files.", orig)
        elif kind == "special":
            problems.add(
                "special_member",
                "The entry is a device, pipe or socket, not a file or a directory.",
                orig,
            )
        seen[orig] = seen.get(orig, 0) + 1
        if seen[orig] == 2:
            problems.add(
                "duplicate_name",
                "The archive has two entries with this name. Tools could read either one.",
                orig,
            )
        name = info.filename.rstrip("/")
        is_dir = kind == "dir"
        # every path and every directory it implies, compared without case (macOS and
        # Windows file systems would merge them); an unsafe name is already refused, and
        # its prefixes could be very many
        parts = name.split("/") if why is None else []
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            fold = unicodedata.normalize("NFC", prefix).casefold()
            other = folded.setdefault(fold, prefix)
            if other != prefix:
                problems.add(
                    "case_collision",
                    f"The names {_show(other)} and {_show(prefix)} differ only in case. "
                    "Rename one of them.",
                    prefix,
                )
        if seen[orig] == 1:
            members.append(_Member(info=info, name=name, is_dir=is_dir))
    if total > limits.max_uncompressed:
        problems.add(
            "archive_size",
            f"The wheel expands to {total:,} bytes. The maximum is {limits.max_uncompressed:,}.",
        )
    if archive_size > 0 and total > limits.max_ratio * archive_size:
        problems.add(
            "ratio",
            f"The wheel expands more than {limits.max_ratio} times its size. Store large "
            "data with less compression, or leave it out of the wheel.",
        )
    _check_layout_on_disk(path, zf, infos, problems)
    safe = not problems.has(
        "member_count",
        "member_size",
        "archive_size",
        "ratio",
        "zip_structure",
        "encrypted",
        "compression_method",
        "link_member",
        "special_member",
        "duplicate_name",
    )
    return members, total, safe


def _suffix(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return "." + base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _check_names(
    members: Sequence[_Member], job: PluginInspectJob, index: InstalledIndex, problems: _Problems
) -> tuple[list[str], list[str]]:
    """Content checks by name and the top-level layout. Returns ``(top-level names,
    .dist-info directory names)``."""
    ns = job.namespace
    tops: dict[str, bool] = {}  # top-level name -> is a directory
    dist_infos: list[str] = []
    for m in members:
        parts = m.name.split("/")
        head = parts[0]
        under = len(parts) > 1 or m.is_dir
        tops[head] = tops.get(head, False) or under
        base = parts[-1]
        suffix = _suffix(m.name)
        if m.is_dir:
            if base == "__pycache__":
                problems.add("bytecode", "The wheel contains a __pycache__ directory.", m.name)
            if base.endswith(".egg-info") or (base.endswith(".dist-info") and len(parts) > 1):
                problems.add(
                    "dist_info",
                    "The wheel contains a second metadata directory. A wheel has one "
                    ".dist-info directory at the top.",
                    m.name,
                )
            continue
        if any(p.endswith(".egg-info") for p in parts[:-1]) or any(
            p.endswith(".dist-info") for p in parts[1:-1]
        ):
            problems.add(
                "dist_info",
                "The file is in a second metadata directory. A wheel has one .dist-info "
                "directory at the top.",
                m.name,
            )
        if suffix == ".pth":
            problems.add(
                "pth_file",
                "The wheel contains a .pth file. Python runs .pth files when it starts. "
                "Remove the file.",
                m.name,
            )
        stem = base.split(".", 1)[0]
        if stem in _SITE_HOOKS:
            problems.add(
                "site_hook",
                f"The wheel contains {stem}, which Python runs when it starts. Remove it.",
                m.name,
            )
        if suffix == ".egg-link":
            problems.add("site_hook", "The wheel contains an .egg-link file. Remove it.", m.name)
        if len(parts) == 1 and base == "__main__.py":
            problems.add(
                "site_hook",
                "The wheel has a __main__.py at the top. Put code inside the package.",
                m.name,
            )
        if suffix in _NATIVE_SUFFIXES:
            problems.add(
                "native_code",
                "The wheel contains compiled native code. Uploads must be pure Python.",
                m.name,
            )
        if suffix in _BYTECODE_SUFFIXES or "__pycache__" in parts:
            problems.add(
                "bytecode",
                "The wheel contains compiled Python bytecode. Upload the .py source only.",
                m.name,
            )
        if suffix in _ARCHIVE_SUFFIXES:
            problems.add(
                "nested_archive",
                "The wheel contains another archive. Uploads cannot hold archives.",
                m.name,
            )
    stdlib = sys.stdlib_module_names
    for head, is_dir in sorted(tops.items()):
        if head.endswith(".dist-info"):
            dist_infos.append(head)
            continue
        if head.endswith(".data"):
            problems.add(
                "data_dir",
                "The wheel has a .data directory (scripts, headers or platform files). "
                "Uploads install only the package.",
                head,
            )
            continue
        if head == ns:
            if not is_dir:
                problems.add(
                    "top_level_name",
                    f"{ns} must be a package directory, not a single module.",
                    head,
                )
            elif ns in index.top_level:
                problems.add(
                    "top_level_name",
                    f"The package {ns} would hide a package of the base image.",
                    head,
                )
            continue
        module = head.split(".", 1)[0]
        if module in stdlib:
            why = f"The top-level name {_show(module)} would hide a Python standard module."
        elif module in index.top_level:
            why = f"The top-level name {_show(module)} would hide a package of the base image."
        elif module.startswith("mapi"):
            why = f"The top-level name {_show(module)} is reserved for the platform."
        else:
            why = f"The wheel has the extra top-level name {_show(module)}."
        problems.add(
            "top_level_name",
            f"{why} A wheel can hold only the package {ns} and its .dist-info directory.",
            head,
        )
    if ns not in tops:
        problems.add(
            "top_level_name",
            f"The wheel has no package named {ns}. Put the code in that package.",
        )
    elif tops[ns] and not any(m.name == f"{ns}/__init__.py" for m in members):
        problems.add(
            "namespace_package",
            f"The package {ns} has no __init__.py. Add one: namespace packages can merge "
            "with other packages.",
            ns,
        )
    if not dist_infos:
        problems.add("dist_info", "The wheel has no .dist-info directory.")
    elif len(dist_infos) > 1:
        problems.add(
            "dist_info",
            f"The wheel has {len(dist_infos)} .dist-info directories. A wheel describes one "
            "distribution.",
        )
    top_level = sorted(h for h in tops if not h.endswith((".dist-info", ".data")))
    return top_level, dist_infos


def _sniff(head: bytes, name: str, problems: _Problems) -> None:
    """Content checks by magic bytes (a renamed binary is still refused)."""
    if head.startswith(_NATIVE_MAGIC) or _is_pe(head):
        problems.add(
            "native_code",
            "The file is a compiled native binary. Uploads must be pure Python.",
            name,
        )
    elif head.startswith(_ARCHIVE_MAGIC) or _BZIP2_MAGIC.match(head) or head[257:262] == b"ustar":
        problems.add(
            "nested_archive",
            "The file is an archive. Uploads cannot hold archives.",
            name,
        )
    elif not name.endswith(".py") and _is_pyc(head):
        problems.add(
            "bytecode",
            "The file is compiled Python bytecode. Upload the .py source only.",
            name,
        )


def _is_pe(head: bytes) -> bool:
    if len(head) < 64 or not head.startswith(b"MZ"):
        return False
    (pe,) = struct.unpack_from("<I", head, 0x3C)
    return pe + 4 <= len(head) and head[pe : pe + 4] == b"PE\x00\x00"


def _is_pyc(head: bytes) -> bool:
    # a pyc starts with a 2-byte little-endian magic number then b"\r\n" (3.x: 2900-3999)
    if len(head) < 4 or head[2:4] != b"\r\n":
        return False
    return 2900 <= int.from_bytes(head[:2], "little") < 4000


def _stream(
    zf: zipfile.ZipFile,
    members: Sequence[_Member],
    wanted: Mapping[str, int],
    problems: _Problems,
) -> tuple[dict[str, bytes], bool]:
    """Hash every file by streaming it (never to disk); keep the first bytes for the magic
    checks and the metadata files named in ``wanted`` (path -> cap). Returns ``(metadata
    bytes, every file read)``."""
    kept: dict[str, bytes] = {}
    complete = True
    for m in members:
        if m.is_dir:
            continue
        h = hashlib.sha256()
        cap = wanted.get(m.name)
        buf = io.BytesIO() if cap is not None else None
        head = b""
        n = 0
        try:
            with zf.open(m.info) as src:
                while chunk := src.read(_CHUNK):
                    n += len(chunk)
                    if n > m.info.file_size:
                        raise zipfile.BadZipFile("more data than declared")
                    h.update(chunk)
                    if len(head) < _SNIFF:
                        head += chunk[: _SNIFF - len(head)]
                    if buf is not None and cap is not None and buf.tell() <= cap:
                        buf.write(chunk[: cap + 1 - buf.tell()])
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, RuntimeError,
                NotImplementedError, ValueError) as exc:  # fmt: skip
            problems.add(
                "zip_structure",
                "The file cannot be read back from the archive "
                f"({type(exc).__name__}). Build the wheel again with a standard tool.",
                m.name,
            )
            complete = False
            continue
        if n != m.info.file_size:
            problems.add(
                "zip_structure",
                "The file is shorter than its declared size.",
                m.name,
            )
            complete = False
            continue
        m.sha256 = "sha256:" + h.hexdigest()
        m.head = head
        _sniff(head, m.name, problems)
        if buf is not None:
            kept[m.name] = buf.getvalue()
    return kept, complete


# ================================================================================ metadata


def _text(data: bytes, what: str, cap: int, check: InspectCheck, problems: _Problems) -> str | None:
    if len(data) > cap:
        problems.add(check, f"{what} is larger than {cap:,} bytes.")
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        problems.add(check, f"{what} is not UTF-8 text.")
        return None


def _headers(text: str) -> dict[str, list[str]]:
    """The headers of an email-style metadata file (repeated headers kept in order)."""
    msg = HeaderParser().parsestr(text)
    out: dict[str, list[str]] = {}
    for key, value in msg.items():
        out.setdefault(key.lower(), []).append(str(value).strip())
    return out


def _check_wheel_file(text: str | None, problems: _Problems, dist_info: str) -> list[str]:
    if text is None:
        problems.add("dist_info", "The wheel has no WHEEL file.", f"{dist_info}/WHEEL")
        return []
    h = _headers(text)
    version = (h.get("wheel-version") or [""])[0]
    if not re.fullmatch(r"1\.[0-9]+", version):
        problems.add(
            "wheel_tag",
            "The WHEEL file needs 'Wheel-Version: 1.x'. Build the wheel with a current tool.",
            f"{dist_info}/WHEEL",
        )
    if (h.get("root-is-purelib") or [""])[0].lower() != "true":
        problems.add(
            "wheel_tag",
            "The WHEEL file needs 'Root-Is-Purelib: true'. Uploads must be pure Python.",
            f"{dist_info}/WHEEL",
        )
    tags = h.get("tag") or []
    if not tags:
        problems.add("wheel_tag", "The WHEEL file has no Tag.", f"{dist_info}/WHEEL")
    if len(tags) > _TAGS_MAX:
        problems.add("wheel_tag", f"The WHEEL file has more than {_TAGS_MAX} tags.")
    for tag in tags:
        py, _, rest = tag.partition("-")
        abi, _, plat = rest.partition("-")
        if "py3" not in py.split(".") or abi != "none" or plat != "any":
            problems.add(
                "wheel_tag",
                f"The wheel tag {_show(tag, 60)} is not for pure Python 3. Build a "
                "py3-none-any wheel.",
                f"{dist_info}/WHEEL",
            )
    return [_clip(t, 128) for t in tags[:_TAGS_MAX]]


def _norm_version(text: str) -> str | None:
    try:
        return str(Version.parse(text))
    except InvalidVersion:
        return None


@dataclass(slots=True)
class _Metadata:
    dist: str | None = None
    version: str | None = None
    requires: list[str] = field(default_factory=list)
    requires_python: str | None = None
    license: str | None = None


def _check_metadata(
    text: str | None,
    job: PluginInspectJob,
    index: InstalledIndex,
    dist_info: str,
    problems: _Problems,
) -> _Metadata:
    out = _Metadata()
    where = f"{dist_info}/METADATA"
    if text is None:
        problems.add("dist_info", "The wheel has no METADATA file.", where)
        return out
    h = _headers(text)
    mv = (h.get("metadata-version") or [""])[0]
    if not re.fullmatch(r"[12]\.[0-9]+", mv):
        problems.add("metadata_invalid", "METADATA has no valid Metadata-Version.", where)
    name = (h.get("name") or [""])[0]
    version = (h.get("version") or [""])[0]
    if not name or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", name):
        problems.add("metadata_invalid", "METADATA has no valid Name.", where)
    else:
        out.dist = name
        if has_unsafe_text(name):
            problems.add("text_unsafe", "The distribution name has hidden characters.", where)
        canon = canonical_name(name)
        if is_builtin_distribution(canon) or canon in index.dists:
            problems.add(
                "dist_name",
                f"The distribution name {_show(name)} is the name of a package in the base "
                "image. An upload cannot replace it.",
                where,
            )
        if canon != job.dist_name:
            problems.add(
                "dist_name",
                f"The distribution name is {_show(name)}. For this plugin it must be "
                f"{job.dist_name!r}.",
                where,
            )
    normalized = _norm_version(version) if version else None
    if normalized is None:
        problems.add("metadata_invalid", "METADATA has no valid PEP 440 Version.", where)
    else:
        out.version = normalized
        expected = _norm_version(job.version)
        if expected != normalized:
            problems.add(
                "metadata_invalid",
                f"The version in METADATA is {normalized!r}. The upload is for version "
                f"{job.version!r}.",
                where,
            )
    # dist-info directory name: {escaped name}-{version}.dist-info
    stem = dist_info[: -len(".dist-info")]
    d_name, _, d_version = stem.rpartition("-")
    if canonical_name(d_name) != job.dist_name or (
        normalized is not None and _norm_version(d_version) != normalized
    ):
        problems.add(
            "dist_info",
            f"The metadata directory {_show(dist_info)} does not match the distribution "
            "name and version.",
            dist_info,
        )
    out.requires = h.get("requires-dist", [])
    rp = h.get("requires-python")
    out.requires_python = rp[0] if rp else None
    lic = (h.get("license-expression") or h.get("license") or [""])[0]
    if lic.strip():
        out.license = _clip(lic.strip().splitlines()[0], 200) or None
    return out


def _parse_entry_points(
    text: str | None,
    job: PluginInspectJob,
    files: frozenset[str],
    dist_info: str,
    problems: _Problems,
) -> list[EntryPointDecl]:
    where = f"{dist_info}/entry_points.txt"
    allowed = ", ".join(PLUGIN_ENTRY_POINT_GROUPS)
    if text is None:
        problems.add(
            "entry_point_group",
            f"The wheel declares no entry points. Declare at least one in {allowed}.",
            where,
        )
        return []
    parser = configparser.ConfigParser(
        interpolation=None, delimiters=("=",), strict=True, comment_prefixes=("#", ";")
    )
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        problems.add(
            "metadata_invalid",
            f"entry_points.txt cannot be read ({type(exc).__name__}).",
            where,
        )
        return []
    ns = job.namespace
    out: list[EntryPointDecl] = []
    total = 0
    for group in parser.sections():
        for name, value in parser.items(group):
            total += 1
            value = value.strip()
            if group not in PLUGIN_ENTRY_POINT_GROUPS:
                if group == "mapi.governance_checks":
                    why = "Uploads cannot add governance checks."
                elif group in ("console_scripts", "gui_scripts"):
                    why = "Uploads cannot install commands."
                else:
                    why = f"Uploads can use only {allowed}."
                problems.add(
                    "entry_point_group",
                    f"The entry point group {_show(group, 64)} is not allowed. {why}",
                    where,
                )
            if not _EP_NAME.match(name) or has_unsafe_text(name):
                problems.add(
                    "entry_point_target",
                    f"The entry point name {_show(name, 60)} is not valid.",
                    where,
                )
            m = _EP_VALUE.match(value) if len(value) <= 256 else None
            if m is None:
                problems.add(
                    "entry_point_target",
                    f"The entry point {_show(name, 60)} has the target {_show(value, 80)}. "
                    "Use 'module:attribute' without extras.",
                    where,
                )
            else:
                module = m.group("module")
                if module != ns and not module.startswith(ns + "."):
                    problems.add(
                        "entry_point_target",
                        f"The entry point {_show(name, 60)} points to {_show(module, 80)}, "
                        f"outside the package {ns}. Entry points must point into it.",
                        where,
                    )
                else:
                    rel = module.replace(".", "/")
                    if f"{rel}.py" not in files and f"{rel}/__init__.py" not in files:
                        problems.add(
                            "entry_point_target",
                            f"The entry point {_show(name, 60)} points to the module "
                            f"{_show(module, 80)}, which is not in the wheel.",
                            where,
                        )
            if len(out) < _ENTRY_POINTS_MAX:
                out.append(EntryPointDecl(group=group, name=name, value=value))
    if total > _ENTRY_POINTS_MAX:
        problems.add(
            "metadata_invalid",
            f"The wheel declares {total:,} entry points. The maximum is {_ENTRY_POINTS_MAX}.",
            where,
        )
    if not any(e.group in PLUGIN_ENTRY_POINT_GROUPS for e in out):
        problems.add(
            "entry_point_group",
            f"The wheel declares no entry point in {allowed}. Declare at least one.",
            where,
        )
    return out


def _check_record(
    text: str | None, members: Sequence[_Member], dist_info: str, problems: _Problems
) -> None:
    """``RECORD`` must list every file with its sha256 and size, and nothing else."""
    where = f"{dist_info}/RECORD"
    if text is None:
        problems.add("dist_info", "The wheel has no RECORD file.", where)
        return
    files = {m.name: m for m in members if not m.is_dir}
    listed: set[str] = set()
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        problems.add("dist_info", "RECORD is not a valid CSV file.", where)
        return
    for row in rows:
        if not row:
            continue
        if len(row) != 3:
            problems.add("dist_info", "A RECORD row does not have 3 fields.", where)
            continue
        path, digest, size = row
        listed.add(path)
        m = files.get(path)
        if m is None:
            problems.add("dist_info", "RECORD lists a file that the wheel does not have.", path)
            continue
        if path == where or path.rsplit("/", 1)[-1] in _RECORD_SIGNATURES:
            continue
        algo, _, value = digest.partition("=")
        if algo != "sha256" or m.sha256 is None:
            problems.add("dist_info", "RECORD needs a sha256 hash for each file.", path)
            continue
        actual = base64.urlsafe_b64encode(bytes.fromhex(m.sha256[7:])).rstrip(b"=").decode()
        if value != actual or size != str(m.info.file_size):
            problems.add(
                "dist_info",
                "The file does not match its RECORD hash or size. The wheel was changed "
                "after the build.",
                path,
            )
    for name in sorted(set(files) - listed):
        problems.add("dist_info", "RECORD does not list this file.", name)


# ============================================================================ requirements


def _check_installed(
    req: Requirement,
    index: InstalledIndex,
    problems: _Problems,
    where: str,
    via: str,
    seen: set[tuple[str, str]],
    depth: int,
) -> None:
    """``req`` (whose marker holds) must be installed in a matching version; its extras are
    resolved through the installed distribution's own requirements."""
    installed = index.dists.get(req.name)
    shown = _show(req.name, 64)
    if installed is None:
        problems.add(
            "requirement_unlocked",
            f"{shown}{via} is not installed in the base image. Uploads can use only "
            "packages that the base image has.",
            where,
        )
        return
    if req.specifier and not specifier_contains(req.specifier, installed):
        problems.add(
            "requirement_unlocked",
            f"{shown}{via} needs {_show(req.specifier, 80)}. The base image has {installed}.",
            where,
        )
    if depth >= _EXTRAS_DEPTH:
        return
    for extra in req.extras:
        if (req.name, extra) in seen:
            continue
        seen.add((req.name, extra))
        env = marker_env(index.requires_python, extra=extra)
        for line in index.requires.get(req.name, ()):
            try:
                sub = parse_requirement(line)
                if sub.marker is not None and not marker_matches(sub.marker, env):
                    continue
            except InvalidVersion:
                continue  # the image's own metadata: not the upload's problem
            if sub.url is not None:
                continue
            _check_installed(
                sub, index, problems, where, f" (needed by {req.name}[{extra}])", seen, depth + 1
            )


def _check_requirements(
    lines: Sequence[str], index: InstalledIndex, dist_info: str, problems: _Problems
) -> list[RequirementFact]:
    where = f"{dist_info}/METADATA"
    if len(lines) > _REQUIRES_MAX:
        problems.add(
            "metadata_invalid",
            f"The wheel has {len(lines)} requirements. The maximum is {_REQUIRES_MAX}.",
            where,
        )
    env = marker_env(index.requires_python)
    facts: list[RequirementFact] = []
    seen: set[tuple[str, str]] = set()
    for line in lines[:_REQUIRES_MAX]:
        try:
            req = parse_requirement(line)
        except InvalidVersion:
            problems.add(
                "metadata_invalid",
                f"The requirement {_show(line, 120)} is not valid (PEP 508).",
                where,
            )
            continue
        name = req.name + (f"[{','.join(req.extras)}]" if req.extras else "")
        facts.append(
            RequirementFact(
                name=name,
                specifier=req.specifier if req.url is None else "",
                marker=req.marker,
                locked_version=index.dists.get(req.name),
            )
        )
        if req.url is not None:
            problems.add(
                "requirement_url",
                f"{_show(req.name, 64)} comes from a URL. Uploads can use only packages "
                "that the base image has.",
                where,
            )
            continue
        try:
            applies = req.marker is None or marker_matches(req.marker, env)
        except InvalidVersion:
            problems.add(
                "metadata_invalid",
                f"The marker of the requirement {_show(req.name, 64)} is not valid.",
                where,
            )
            continue
        if applies:
            _check_installed(req, index, problems, where, "", seen, 0)
    return facts


def _check_requires_python(
    spec: str | None, index: InstalledIndex, dist_info: str, problems: _Problems
) -> None:
    if spec is None:
        return
    try:
        ok = specifier_contains(spec, index.requires_python)
    except InvalidVersion:
        problems.add(
            "requires_python",
            f"Requires-Python {_show(spec, 80)} is not a valid specifier.",
            f"{dist_info}/METADATA",
        )
        return
    if not ok:
        problems.add(
            "requires_python",
            f"The wheel needs Python {_show(spec, 80)}. The base image has Python "
            f"{index.requires_python}.",
            f"{dist_info}/METADATA",
        )


# ================================================================================== entry


def inspect_wheel(path: Path, *, job: PluginInspectJob, index: InstalledIndex) -> PluginInspection:
    """Check the wheel at ``path`` (T19.2-T19.9, T19.14) and return the inspection.

    Never extracts a member and never imports anything from the archive. A wheel with any
    problem has ``ok=False``; the SBOM is present only when every file was read back.
    """
    problems = _Problems()
    archive_sha = _file_sha256(path)
    if archive_sha != job.expected_sha256:
        problems.add(
            "sha_mismatch",
            "The file is not the file that was uploaded (the sha256 differs). Upload it again.",
        )
        return _result(archive_sha, problems)
    size = path.stat().st_size
    if size > job.limits.max_bytes:
        problems.add(
            "archive_size",
            f"The wheel is {size:,} bytes. The maximum is {job.limits.max_bytes:,}.",
        )
        return _result(archive_sha, problems)
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError, EOFError) as exc:
        problems.add(
            "zip_structure",
            f"The file is not a readable zip archive ({type(exc).__name__}). Upload the .whl "
            "file that the build tool wrote.",
        )
        return _result(archive_sha, problems)
    with zf:
        return _inspect_open(path, zf, job, index, archive_sha, size, problems)


def _inspect_open(
    path: Path,
    zf: zipfile.ZipFile,
    job: PluginInspectJob,
    index: InstalledIndex,
    archive_sha: str,
    size: int,
    problems: _Problems,
) -> PluginInspection:
    members, total, safe = _scan_directory(path, zf, job, size, problems)
    top_level, dist_infos = _check_names(members, job, index, problems)
    facts = dict(
        members=len(zf.infolist()),
        uncompressed_bytes=total,
        top_level=[_clip(t, 128) for t in top_level[:_TOP_LEVEL_MAX]],
    )
    if not safe:
        return _result(archive_sha, problems, **facts)
    dist_info = dist_infos[0] if len(dist_infos) == 1 else None
    wanted = {f"{dist_info}/{n}": cap for n, cap in _META_CAPS.items()} if dist_info else {}
    kept, complete = _stream(zf, members, wanted, problems)
    if dist_info is None:
        return _result(archive_sha, problems, **facts)

    def text(n: str, check: InspectCheck) -> str | None:
        data = kept.get(f"{dist_info}/{n}")
        return None if data is None else _text(data, n, _META_CAPS[n], check, problems)

    tags = _check_wheel_file(text("WHEEL", "dist_info"), problems, dist_info)
    meta = _check_metadata(text("METADATA", "metadata_invalid"), job, index, dist_info, problems)
    files = frozenset(m.name for m in members if not m.is_dir)
    entry_points = _parse_entry_points(
        text("entry_points.txt", "metadata_invalid"), job, files, dist_info, problems
    )
    if complete:
        _check_record(text("RECORD", "dist_info"), members, dist_info, problems)
    requires = _check_requirements(meta.requires, index, dist_info, problems)
    _check_requires_python(meta.requires_python, index, dist_info, problems)
    sbom = None
    if complete and len(files) <= 2000 and meta.dist and meta.version:
        sbom = PluginSbom(
            dist=meta.dist,
            version=meta.version,
            files=[
                SbomFile(path=m.name, sha256=m.sha256, size=m.info.file_size)
                for m in members
                if not m.is_dir and m.sha256 is not None
            ],
            requires=requires,
            license=meta.license,
            base_image_digest=index.digest(),
        )
    return _result(
        archive_sha,
        problems,
        dist=meta.dist,
        version=meta.version,
        wheel_tags=tags,
        entry_points=entry_points,
        requires_python=meta.requires_python,
        sbom=sbom,
        **facts,
    )


def _result(archive_sha: str, problems: _Problems, **facts: object) -> PluginInspection:
    """Build the inspection; a document over :data:`INSPECTION_MAX_BYTES` loses its SBOM
    and entry point lists and gets an ``archive_size`` problem instead."""
    doc = PluginInspection.model_validate(
        {"ok": not problems, "archive_sha256": archive_sha, "problems": problems.items, **facts}
    )
    if len(doc.model_dump_json().encode("utf-8")) <= INSPECTION_MAX_BYTES:
        return doc
    problems.add(
        "archive_size",
        "The file list and metadata of the wheel are too large to report. Upload a wheel "
        "with fewer files.",
    )
    slim = {**facts, "sbom": None, "entry_points": []}
    return PluginInspection.model_validate(
        {"ok": False, "archive_sha256": archive_sha, "problems": problems.items, **slim}
    )


def iter_problem_checks(inspection: PluginInspection) -> Iterator[str]:
    """The distinct check names of an inspection, in order (for logs and tests)."""
    seen: set[str] = set()
    for p in inspection.problems:
        if p.check not in seen:
            seen.add(p.check)
            yield p.check
