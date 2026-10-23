"""Build the v0.19 malicious upload corpus (``cases.toml``) into a directory.

Each case becomes one artifact:

* ``wheel`` - a ``.whl`` file for the static inspect stage (``archive``, ``content``,
  ``metadata`` groups and the ``sbom_name_html`` text case). The member *contents* are inert
  (``x = 1``, zeros, magic-byte headers): every case is refused by its name, structure or
  metadata, never by what its code would do;
* ``describe`` - a ``describe.json`` (:class:`mapi_spec.plugins.PluginDescribe` as JSON) for the
  control-plane re-check (``text`` group);
* ``ticket`` - an upload ticket request body (``upload`` stage);
* ``scenario`` - nothing to build: the review cases are API scenarios.

The ``runtime`` group (docker) is declared in ``cases.toml`` but not built here yet; see
``README.md``. :func:`build_case` raises :class:`NotBuilt` for those cases.

Everything is written below the output directory (a tmp dir in the tests). Nothing is ever
installed, extracted or imported: the tests hand the files to
:func:`mapi_runner.plugin_inspect.inspect_wheel` (which reads the zip directory and streams
member bytes) and to :func:`mapi_spec.plugins.check_describe`.

Usage::

    python security/plugin-uploads/build_corpus.py OUT_DIR [--case NAME ...]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import stat
import struct
import sys
import tomllib
import warnings
import zipfile
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CASES_TOML = HERE / "cases.toml"

ARTIFACTS = ("wheel", "describe", "ticket", "scenario")
GROUPS = ("archive", "content", "metadata", "runtime", "text", "review")
MIB = 1024 * 1024


class NotBuilt(Exception):
    """The case is declared but this builder does not produce it (the runtime group)."""


# ======================================================================================= cases


@dataclass(frozen=True)
class Case:
    name: str
    group: str
    threat: str
    stage: str
    expect: str
    artifact: str
    summary: str
    path: str | None = None
    says: str | None = None
    live: bool = False


@dataclass(frozen=True)
class Corpus:
    namespace: str
    probe_namespace: str
    version: str
    plugin_version_id: str
    builtin_titles: tuple[str, ...]
    base_image: Mapping[str, Any]
    cases: tuple[Case, ...]

    @property
    def dist(self) -> str:
        return self.namespace.replace("_", "-")

    @property
    def dist_info(self) -> str:
        return f"{self.namespace}-{self.version}.dist-info"

    @property
    def data(self) -> str:
        return f"{self.namespace}-{self.version}.data"

    def by_name(self) -> dict[str, Case]:
        return {c.name: c for c in self.cases}

    def fill(self, text: str) -> str:
        """``text`` with the ``{ns}``, ``{dist_info}`` and ``{data}`` placeholders filled."""
        return (
            text.replace("{ns}", self.namespace)
            .replace("{dist_info}", self.dist_info)
            .replace("{data}", self.data)
        )


def load_corpus(path: Path = CASES_TOML) -> Corpus:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    meta = raw["corpus"]
    cases = tuple(Case(**c) for c in raw["case"])
    names = [c.name for c in cases]
    if len(names) != len(set(names)):
        raise ValueError("duplicate case names in cases.toml")
    for c in cases:
        if c.group not in GROUPS or c.artifact not in ARTIFACTS:
            raise ValueError(f"case {c.name}: unknown group or artifact")
    return Corpus(
        namespace=meta["namespace"],
        probe_namespace=meta["probe_namespace"],
        version=meta["version"],
        plugin_version_id=meta["plugin_version_id"],
        builtin_titles=tuple(meta["builtin_titles"]),
        base_image=raw["base_image"],
        cases=cases,
    )


# ================================================================================ wheel bytes


def _record(members: Mapping[str, bytes], record_name: str) -> bytes:
    rows = []
    for name, data in members.items():
        if name.endswith("/"):
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rows.append(f"{name},sha256={digest},{len(data)}")
    rows.append(f"{record_name},,")
    return ("\n".join(rows) + "\n").encode()


def _metadata(
    c: Corpus,
    *,
    name: str | None = None,
    version: str | None = None,
    requires: Iterable[str] = ("mapi-sdk>=0.16",),
    requires_python: str | None = ">=3.11",
) -> bytes:
    lines = [
        "Metadata-Version: 2.3",
        f"Name: {name or c.dist}",
        f"Version: {version or c.version}",
    ]
    if requires_python is not None:
        lines.append(f"Requires-Python: {requires_python}")
    lines += [f"Requires-Dist: {r}" for r in requires]
    lines.append("License-Expression: MIT")
    return ("\n".join(lines) + "\n").encode()


def _wheel_file(*, tags: Iterable[str] = ("py3-none-any",), purelib: str = "true") -> bytes:
    lines = ["Wheel-Version: 1.0", "Generator: mapi-corpus", f"Root-Is-Purelib: {purelib}"]
    lines += [f"Tag: {t}" for t in tags]
    return ("\n".join(lines) + "\n").encode()


def _entry_points(c: Corpus) -> str:
    return f"[mapi.steps]\nwinsorize = {c.namespace}.steps:STEPS\n"


def members(
    c: Corpus,
    *,
    extra: Mapping[str, bytes] | None = None,
    drop: Iterable[str] = (),
    dist_info: str | None = None,
    meta: bytes | None = None,
    wheel: bytes | None = None,
    entry_points: str | None = "",
    record: bool = True,
) -> dict[str, bytes]:
    """The members of a well-formed, inert upload with overrides (``RECORD`` last)."""
    ns = c.namespace
    di = dist_info or c.dist_info
    out: dict[str, bytes] = {
        f"{ns}/__init__.py": b'"""Corpus package (inert)."""\n',
        f"{ns}/model.py": b"MODELS = []\n",
        f"{ns}/steps.py": b"STEPS = []\n",
    }
    out.update(extra or {})
    out[f"{di}/METADATA"] = _metadata(c) if meta is None else meta
    out[f"{di}/WHEEL"] = _wheel_file() if wheel is None else wheel
    eps = _entry_points(c) if entry_points == "" else entry_points
    if eps is not None:
        out[f"{di}/entry_points.txt"] = eps.encode()
    for name in drop:
        out.pop(name, None)
    if record:
        out[f"{di}/RECORD"] = _record(out, f"{di}/RECORD")
    return out


ZipTweak = Callable[[zipfile.ZipInfo], None]


def write_wheel(
    dest: Path,
    items: Mapping[str, bytes] | Iterable[tuple[str, bytes]],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
    tweaks: Mapping[str, ZipTweak] | None = None,
) -> Path:
    rows = list(items.items()) if isinstance(items, Mapping) else list(items)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate names on purpose
        with zipfile.ZipFile(dest, "w", compression=compression) as zf:
            for name, data in rows:
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = compression
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                if tweaks and name in tweaks:
                    tweaks[name](info)
                zf.writestr(info, data)
    return dest


@dataclass
class RawEntry:
    name: bytes
    data: bytes
    flags: int = 0
    external_attr: int = (stat.S_IFREG | 0o644) << 16
    cd_offset: int | None = None
    zip64_size: int | None = None
    write_local: bool = True


def raw_zip(dest: Path, entries: Iterable[RawEntry], *, prefix: bytes = b"") -> Path:
    """A minimal stored-only zip writer for structures :mod:`zipfile` will not write."""
    out = bytearray(prefix)
    directory = bytearray()
    rows = list(entries)
    offsets: list[int] = []
    for e in rows:
        offsets.append(len(out))
        if e.write_local:
            out += struct.pack(
                "<IHHHHHIIIHH", 0x04034B50, 20, e.flags, 0, 0, 0x5C21, zlib.crc32(e.data),
                len(e.data), len(e.data), len(e.name), 0,
            )  # fmt: skip
            out += e.name + e.data
    for i, e in enumerate(rows):
        usize = len(e.data)
        extra = b""
        if e.zip64_size is not None:
            usize = 0xFFFFFFFF
            extra = struct.pack("<HHQ", 0x0001, 8, e.zip64_size)
        offset = offsets[i] if e.cd_offset is None else e.cd_offset
        directory += struct.pack(
            "<IHHHHHHIIIHHHHHII", 0x02014B50, 0x031E, 20, e.flags, 0, 0, 0x5C21,
            zlib.crc32(e.data), len(e.data), usize, len(e.name), len(extra), 0, 0, 0,
            e.external_attr, offset,
        )  # fmt: skip
        directory += e.name + extra
    cd_start = len(out)
    out += directory
    out += struct.pack(
        "<IHHHHIIH", 0x06054B50, 0, 0, len(rows), len(rows), len(directory), cd_start, 0
    )
    dest.write_bytes(bytes(out))
    return dest


def _raw_good(c: Corpus) -> list[RawEntry]:
    return [RawEntry(n.encode(), d) for n, d in members(c).items()]


def _patch(path: Path, old: bytes, new: bytes, *, count: int = -1) -> Path:
    assert len(old) == len(new)
    data = path.read_bytes()
    assert old in data
    path.write_bytes(data.replace(old, new, count))
    return path


def _symlink(info: zipfile.ZipInfo) -> None:
    info.external_attr = (stat.S_IFLNK | 0o777) << 16


def _fifo(info: zipfile.ZipInfo) -> None:
    info.external_attr = (stat.S_IFIFO | 0o644) << 16


def _bzip2(info: zipfile.ZipInfo) -> None:
    info.compress_type = zipfile.ZIP_BZIP2


def _small_zip(dest: Path) -> bytes:
    write_wheel(dest, {"a.txt": b"a"})
    data = dest.read_bytes()
    dest.unlink()
    return data


# Magic-byte headers only (padding after them); no executable code.
_ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 60
_MACHO = b"\xcf\xfa\xed\xfe" + b"\x00" * 60
_PE = b"MZ" + b"\x00" * 58 + struct.pack("<I", 64) + b"PE\x00\x00" + b"\x00" * 20
_PYC = (3531).to_bytes(2, "little") + b"\r\n" + b"\x00" * 12
_INERT = b"x = 1\n"


# ============================================================================== wheel builders


@dataclass
class Artifact:
    """A built case: the file, and what the inspect job must say about it."""

    case: Case
    path: Path | None
    expected_sha256: str | None = None
    """Overrides the job's expected sha256 (``sha_mismatch``); default: the file's sha256."""
    limits: dict[str, int] = field(default_factory=dict)


def _wheel_case(c: Corpus, case: Case, out: Path) -> Artifact:
    ns, di, data = c.namespace, c.dist_info, c.data
    dest = out / f"{case.name}.whl"
    n = case.name
    expected: str | None = None

    def w(**kw: Any) -> Path:
        compression = kw.pop("compression", zipfile.ZIP_DEFLATED)
        tweaks = kw.pop("tweaks", None)
        return write_wheel(dest, members(c, **kw), compression=compression, tweaks=tweaks)

    # ------------------------------------------------------------------ archive
    if n == "sha_mismatch":
        w()
        expected = "sha256:" + "0" * 64
    elif n == "bomb_ratio":
        w(extra={f"{ns}/zeros.py": b"\x00" * (5 * MIB)})
    elif n == "bomb_overlap":
        entries = _raw_good(c)
        again = RawEntry(f"{ns}/again.py".encode(), entries[0].data, cd_offset=0)
        again.write_local = False
        raw_zip(dest, [*entries, again])
    elif n == "bomb_zip64_lie":
        entries = _raw_good(c)
        entries[1].zip64_size = 1 << 40
        raw_zip(dest, entries)
    elif n == "bomb_member_count":
        w(extra={f"{ns}/m{i:04d}.py": b"" for i in range(2001)})
    elif n == "bomb_member_size":
        w(extra={f"{ns}/huge.py": b"#" * (20 * MIB + 1)})
    elif n == "bomb_archive_size":
        # stored, incompressible-looking bytes: the file on disk is over 20 MiB
        block = hashlib.sha256(b"corpus").digest() * 32768  # 1 MiB
        w(extra={f"{ns}/blob{i}.bin": block for i in range(21)}, compression=zipfile.ZIP_STORED)
    elif n == "not_a_zip":
        dest.write_bytes(b"this is not a zip archive\n" * 8)
    elif n == "prefix_polyglot":
        raw_zip(dest, _raw_good(c), prefix=b"GIF89a" + b"\x00" * 26)
    elif n == "header_mismatch":
        w(extra={f"{ns}/good.py": b""}, compression=zipfile.ZIP_STORED)
        _patch(dest, b"good.py", b"evil.py", count=1)  # the local header comes first
    elif n == "encrypted_member":
        entries = _raw_good(c)
        entries[1].flags = 0x1
        raw_zip(dest, entries)
    elif n == "bzip2_member":
        w(tweaks={f"{ns}/model.py": _bzip2})
    elif n in ("path_dotdot", "path_absolute"):
        assert case.path is not None
        w(extra={case.path: _INERT})
    elif n == "path_backslash":
        w(extra={f"{ns}\\evil.py": _INERT})
    elif n == "path_nul":
        w(extra={f"{ns}/aXb.py": b""}, compression=zipfile.ZIP_STORED)
        _patch(dest, b"aXb.py", b"a\x00b.py")
    elif n == "path_too_deep":
        w(extra={f"{ns}/" + "/".join("d" for _ in range(20)) + "/x.py": b""})
    elif n == "symlink_member":
        name = c.fill(case.path or "")
        w(extra={name: b"/etc/passwd"}, tweaks={name: _symlink})
    elif n == "fifo_member":
        name = c.fill(case.path or "")
        w(extra={name: b""}, tweaks={name: _fifo})
    elif n == "duplicate_name":
        write_wheel(dest, [*members(c).items(), (f"{ns}/model.py", _INERT)])
    elif n == "case_collision":
        w(extra={f"{ns}/Model.py": b""})
    elif n in ("nested_wheel", "nested_zip_magic"):
        w(extra={c.fill(case.path or ""): _small_zip(out / f"{n}.inner.zip")})
    # ------------------------------------------------------------------ content
    elif n in (
        "pth_top",
        "pth_nested",
        "pth_data_purelib",
        "sitecustomize_top",
        "usercustomize_nested",
        "main_top",
        "egg_link",
        "native_so",
        "native_pyd",
    ):
        body = b"# inert\n" if n.startswith("pth") else _INERT
        w(extra={c.fill(case.path or ""): body})
    elif n == "shadow_json":
        w(extra={"json.py": _INERT})
    elif n == "shadow_numpy":
        w(extra={"numpy/__init__.py": _INERT})
    elif n == "shadow_typing_extensions":
        w(extra={"typing_extensions.py": _INERT})
    elif n == "shadow_mapi_runner":
        w(extra={"mapi_runner/x.py": _INERT})
    elif n == "extra_top_level":
        w(extra={"helpers/__init__.py": _INERT})
    elif n == "namespace_package":
        w(drop=[f"{ns}/__init__.py"])
    elif n == "native_elf_magic":
        w(extra={c.fill(case.path or ""): _ELF})
    elif n == "native_macho_magic":
        w(extra={c.fill(case.path or ""): _MACHO})
    elif n == "native_pe_magic":
        w(extra={c.fill(case.path or ""): _PE})
    elif n in ("bytecode_pyc", "bytecode_pycache", "bytecode_magic"):
        w(extra={c.fill(case.path or ""): _PYC})
    elif n == "data_scripts":
        w(extra={f"{data}/scripts/run": _INERT})
    elif n == "data_platlib":
        w(extra={f"{data}/platlib/fast.py": _INERT})
    elif n == "name_bidi":
        w(extra={f"{ns}/model‮yp.py": b""})
    # ----------------------------------------------------------------- metadata
    elif n == "wheel_tag_native":
        w(wheel=_wheel_file(tags=["cp312-cp312-manylinux_2_17_x86_64"]))
    elif n == "purelib_false":
        w(wheel=_wheel_file(purelib="false"))
    elif n == "two_dist_infos":
        w(extra={"other-1.0.dist-info/METADATA": _metadata(c, name="other", version="1.0")})
    elif n == "egg_info":
        w(extra={f"{ns}/x.egg-info/PKG-INFO": b"Name: x\n"})
    elif n == "missing_metadata":
        w(drop=[f"{di}/METADATA"])
    elif n == "record_tampered":
        m = members(c)
        m[f"{ns}/model.py"] = b"MODELS = [1]\n"  # changed after RECORD was written
        write_wheel(dest, m)
    elif n == "dist_name_other":
        w(meta=_metadata(c, name="acme"), dist_info=f"acme-{c.version}.dist-info")
    elif n == "dist_name_mapi_plugin_mmm":
        w(meta=_metadata(c, name="mapi-plugin-mmm"))
    elif n == "dist_name_numpy":
        w(meta=_metadata(c, name="numpy"))
    elif n == "version_mismatch":
        w(meta=_metadata(c, version="0.2.0"))
    elif n == "metadata_not_utf8":
        w(meta=b"Name: \xff\xfe\n")
    elif n == "ep_os_system":
        w(entry_points="[mapi.models]\nx = os:system\n")
    elif n == "ep_mapi_runner":
        w(entry_points="[mapi.models]\nx = mapi_runner.supervisor:main\n")
    elif n == "ep_foreign_package":
        w(entry_points="[mapi.models]\nx = requests:get\n")
    elif n == "ep_governance_checks":
        w(entry_points=f"[mapi.governance_checks]\nx = {ns}.model:MODELS\n")
    elif n == "ep_console_scripts":
        w(entry_points=f"[console_scripts]\nacme = {ns}.model:MODELS\n")
    elif n == "ep_pytest_plugin":
        w(entry_points=f"[pytest11]\nacme = {ns}.model\n")
    elif n == "ep_none":
        w(entry_points=None)
    elif n == "req_not_installed":
        w(meta=_metadata(c, requires=["scikit-image>=0.20"]))
    elif n == "req_out_of_range":
        w(meta=_metadata(c, requires=["numpy<2"]))
    elif n == "req_extra_missing":
        w(meta=_metadata(c, requires=["mapi-plots[recipes]>=0.16"]))
    elif n == "req_url":
        w(meta=_metadata(c, requires=["acme @ https://example.com/acme.whl"]))
    elif n == "requires_python_future":
        w(meta=_metadata(c, requires_python=">=3.13"))
    # --------------------------------------------------------------------- text
    elif n == "sbom_name_html":
        w(extra={sbom_html_name(c): b""})
    else:
        raise NotBuilt(f"no wheel builder for case {n!r}")
    return Artifact(case, dest, expected_sha256=expected)


def sbom_html_name(c: Corpus) -> str:
    """The member name of ``sbom_name_html`` (markup in a file name, no ``/`` inside)."""
    return f"{c.namespace}/<b onmouseover=x>bold<b>.py"


# =========================================================================== describe builders

_ABOUT: dict[str, Any] = {
    "title": "Store mean",
    "summary_plain": "Replaces extreme values with a cap so that one odd week does not dominate.",
    "use_when": ["A few weeks have very large values"],
    "avoid_when": ["The large values are real and matter"],
    "data_needs": ["A numeric column"],
    "assumptions": ["Extreme values are errors or noise"],
    "outputs": ["The same columns with capped values"],
    "difficulty": "beginner",
    "typical_runtime": "instant",
    "example": "A week of 10,000 among weeks of 100 becomes 300.",
}

#: Hostile text per describe case: (where, value). ``where`` is a dotted path in the
#: describe document built by :func:`base_describe`.
TEXT_VALUES: dict[str, tuple[str, Any]] = {
    "title_script_tag": ("exports.steps.0.about.title", "<script>alert(1)</script> Store mean"),
    "title_bidi_override": ("exports.steps.0.about.title", "Store ‮naem"),
    # Cyrillic "о" in a built-in title
    "title_homoglyph_builtin": ("exports.steps.0.about.title", "Weekly tоtals"),
    "about_zero_width": (
        "exports.plots.0.about.summary_plain",
        "Shows the weekly​ total of a column.",
    ),
    "ref_userinfo_url": (
        "exports.steps.0.about.references",
        [{"title": "Docs", "url": "https://docs.example@evil.example/winsorize"}],
    ),
    # Cyrillic "е", "х", "а" in the host
    "ref_idn_homograph": (
        "exports.steps.0.about.references",
        [{"title": "Docs", "url": "https://ехаmple.com/winsorize"}],
    ),
    "csv_formula_title": ("exports.steps.0.about.title", "=SUM(A1:A9) Store mean"),
    "mail_html_title": ("exports.steps.0.about.title", "<b>Store</b> mean <a href=x>link</a>"),
}


def base_describe(c: Corpus) -> dict[str, Any]:
    """A clean describe document of the corpus namespace (one step, one recipe)."""
    ns = c.namespace
    sha = "sha256:" + "ab" * 32
    return {
        "plugin_version_id": c.plugin_version_id,
        "overlay_sha256": sha,
        "base_image_digest": c.base_image["image_digest"],
        "exports": {
            "steps": [
                {
                    "id": f"{ns}.winsorize",
                    "version": c.version,
                    "kind": "value",
                    "stateful": True,
                    "about": dict(_ABOUT),
                }
            ],
            "plots": [
                {
                    "id": f"{ns}.weekly_chart",
                    "title": "Store weekly chart",
                    "about": {**_ABOUT, "title": "Store weekly chart"},
                }
            ],
        },
        "kit": {"ok": True, "checks": []},
    }


def _set(doc: dict[str, Any], where: str, value: Any) -> None:
    cur: Any = doc
    parts = where.split(".")
    for p in parts[:-1]:
        cur = cur[int(p)] if isinstance(cur, list) else cur[p]
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def describe_doc(c: Corpus, case: Case) -> dict[str, Any]:
    if case.name not in TEXT_VALUES:
        raise NotBuilt(f"no describe builder for case {case.name!r}")
    doc = base_describe(c)
    where, value = TEXT_VALUES[case.name]
    _set(doc, where, value)
    if where.startswith("exports.plots.0.about.title"):
        doc["exports"]["plots"][0]["title"] = value
    return doc


# ============================================================================= ticket builders


def ticket_body(case: Case, namespace: str) -> dict[str, Any]:
    """The upload ticket request of an ``upload`` stage case for ``namespace``."""
    if case.name == "sdist_upload":
        return {"filename": f"{namespace}-0.1.0.tar.gz", "size": 10}
    if case.name == "oversize_ticket":
        return {"filename": f"{namespace}-0.1.0-py3-none-any.whl", "size": 20 * MIB + 1}
    raise NotBuilt(f"no ticket builder for case {case.name!r}")


# ===================================================================================== inspect


def installed_index(c: Corpus) -> Any:
    """The synthetic base image of ``cases.toml`` as a ``mapi_runner`` ``InstalledIndex``."""
    from mapi_runner.plugin_inspect import InstalledIndex

    base = c.base_image
    return InstalledIndex(
        dists=dict(base["dists"]),
        top_level=frozenset(base["top_level"]),
        requires_python=base["requires_python"],
        requires={k: tuple(v) for k, v in base.get("requires", {}).items()},
        image_digest=base["image_digest"],
    )


def inspect_artifact(c: Corpus, art: Artifact) -> Any:
    """Run the static inspector on a built wheel (reads the zip; imports and extracts
    nothing). Returns the ``PluginInspection``."""
    from mapi_runner.plugin_inspect import inspect_wheel
    from mapi_spec.jobs import PluginInspectJob
    from mapi_spec.plugins import ArchiveLimits

    assert art.path is not None
    sha = "sha256:" + hashlib.sha256(art.path.read_bytes()).hexdigest()
    job = PluginInspectJob(
        plugin_version_id=c.plugin_version_id,
        namespace=c.namespace,
        dist_name=c.dist,
        version=c.version,
        expected_sha256=art.expected_sha256 or sha,
        limits=ArchiveLimits(**art.limits),
    )
    return inspect_wheel(art.path, job=job, index=installed_index(c))


# ======================================================================================= build


def build_case(c: Corpus, case: Case, out: Path) -> Artifact:
    """Build ``case`` into ``out`` (created if needed)."""
    out.mkdir(parents=True, exist_ok=True)
    if case.group == "runtime":
        raise NotBuilt(f"{case.name}: the runtime group is not built by this script yet")
    if case.artifact == "wheel":
        return _wheel_case(c, case, out)
    if case.artifact == "describe":
        dest = out / f"{case.name}.describe.json"
        dest.write_text(json.dumps(describe_doc(c, case), ensure_ascii=False), encoding="utf-8")
        return Artifact(case, dest)
    if case.artifact == "ticket":
        dest = out / f"{case.name}.ticket.json"
        dest.write_text(json.dumps(ticket_body(case, c.namespace)), encoding="utf-8")
        return Artifact(case, dest)
    return Artifact(case, None)  # scenario


def build_all(out: Path, names: Iterable[str] | None = None) -> dict[str, Artifact | str]:
    """Build every case (or ``names``); a case that is not built maps to the reason."""
    c = load_corpus()
    wanted = set(names) if names else None
    result: dict[str, Artifact | str] = {}
    for case in c.cases:
        if wanted is not None and case.name not in wanted:
            continue
        try:
            result[case.name] = build_case(c, case, out)
        except NotBuilt as exc:
            result[case.name] = str(exc)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--case", action="append", default=None)
    args = parser.parse_args(argv)
    for name, built in build_all(args.out, args.case).items():
        where = built if isinstance(built, str) else (built.path or "(scenario)")
        print(f"{name}\t{where}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
