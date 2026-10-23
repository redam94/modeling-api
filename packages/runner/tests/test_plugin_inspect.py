"""``plugin_inspect`` (v0.19, WP2): every T19.2-T19.9 / T19.14 check refuses a hostile wheel
and passes the well-formed one. Every wheel is built in tmp by ``_wheels``; nothing is ever
installed, extracted or imported."""

from __future__ import annotations

import builtins
import hashlib
import itertools
import json
import os
import stat
import struct
import sys
import time
import zipfile
import zlib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from _wheels import (
    DIST,
    DIST_INFO,
    ENTRY_POINTS,
    NS,
    PACKAGE,
    VERSION,
    RawEntry,
    bzip2,
    fifo,
    index,
    job_for,
    metadata,
    patch_bytes,
    raw_zip,
    symlink,
    wheel_file,
    wheel_members,
    write_wheel,
)
from mapi_runner.plugin_inspect import (
    InvalidVersion,
    Version,
    inspect_wheel,
    marker_env,
    marker_matches,
    parse_requirement,
    specifier_contains,
)
from mapi_spec.jobs import PLUGIN_INPUT, PLUGIN_INSPECTION_OUTPUT
from mapi_spec.plugins import INSPECTION_MAX_BYTES, PluginInspection

# ------------------------------------------------------------------------------ helpers


def run(path: Path, **limits: int) -> PluginInspection:
    return inspect_wheel(path, job=job_for(path, **limits), index=index())


def build(tmp_path: Path, name: str = "w.whl", **kw: Any) -> Path:
    compression = kw.pop("compression", zipfile.ZIP_DEFLATED)
    tweaks = kw.pop("tweaks", None)
    return write_wheel(tmp_path / name, wheel_members(**kw), compression=compression, tweaks=tweaks)


def checks(insp: PluginInspection) -> set[str]:
    return {p.check for p in insp.problems}


def refused(insp: PluginInspection, check: str, path: str | None = None) -> None:
    assert not insp.ok
    hits = [p for p in insp.problems if p.check == check]
    assert hits, f"expected {check!r}, got {[(p.check, p.path) for p in insp.problems]}"
    if path is not None:
        assert any(p.path == path for p in hits), [(p.check, p.path) for p in hits]
    # every message is platform-authored plain text within the cap
    assert all(0 < len(p.message) <= 500 for p in insp.problems)


# ---------------------------------------------------------------------------- the good one


def test_the_well_formed_wheel_passes_with_facts_and_sbom(tmp_path: Path) -> None:
    path = build(tmp_path)
    insp = run(path)
    assert insp.ok, [(p.check, p.path, p.message) for p in insp.problems]
    assert insp.archive_sha256 == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert (insp.dist, insp.version) == (DIST, VERSION)
    assert insp.wheel_tags == ["py3-none-any"]
    assert insp.top_level == [NS]
    assert insp.requires_python == ">=3.11"
    assert {(e.group, e.name, e.value) for e in insp.entry_points} == {
        ("mapi.models", "store_mean", f"{NS}.model:MODELS"),
        ("mapi.steps", "winsorize", f"{NS}.steps:STEPS"),
    }
    sbom = insp.sbom
    assert sbom is not None and sbom.base_image_digest == "sha256:" + "ab" * 32
    assert sbom.license == "MIT"
    # the SBOM file list is exactly the archive members, with their hashes and sizes
    with zipfile.ZipFile(path) as zf:
        expected = {
            i.filename: ("sha256:" + hashlib.sha256(zf.read(i)).hexdigest(), i.file_size)
            for i in zf.infolist()
        }
    assert {f.path: (f.sha256, f.size) for f in sbom.files} == expected
    assert insp.members == len(expected)
    assert insp.uncompressed_bytes == sum(s for _, s in expected.values())
    reqs = {r.name: r for r in sbom.requires}
    assert reqs["mapi-sdk"].locked_version == "0.16.0" and reqs["mapi-sdk"].specifier == ">=0.16"
    assert reqs["pytest"].marker == "extra == 'test'" and reqs["pytest"].locked_version is None
    # the document round-trips as the control plane reads it, and fits its cap
    raw = insp.model_dump_json().encode()
    assert len(raw) <= INSPECTION_MAX_BYTES
    assert PluginInspection.model_validate_json(raw) == insp


def test_directory_entries_and_stored_members_are_fine(tmp_path: Path) -> None:
    members = wheel_members(extra={f"{NS}/sub/": b"", f"{NS}/sub/__init__.py": b"X = 1\n"})
    insp = run(write_wheel(tmp_path / "w.whl", members, compression=zipfile.ZIP_STORED))
    assert insp.ok, [(p.check, p.path) for p in insp.problems]
    assert insp.sbom is not None
    assert f"{NS}/sub/" not in {f.path for f in insp.sbom.files}


# ------------------------------------------------------------------------ archive (T19.4/5)


def test_sha_mismatch_stops_before_anything_else(tmp_path: Path) -> None:
    path = build(tmp_path)
    job = job_for(path).model_copy(update={"expected_sha256": "sha256:" + "0" * 64})
    insp = inspect_wheel(path, job=job, index=index())
    assert checks(insp) == {"sha_mismatch"} and insp.dist is None and insp.sbom is None


def test_archive_size_cap(tmp_path: Path) -> None:
    path = build(tmp_path)
    insp = run(path, max_bytes=100)
    assert checks(insp) == {"archive_size"}


def test_not_a_zip(tmp_path: Path) -> None:
    path = tmp_path / "w.whl"
    path.write_bytes(b"not a zip at all" * 10)
    refused(run(path), "zip_structure")


def test_member_count_cap(tmp_path: Path) -> None:
    extra = {f"{NS}/m{i:04d}.py": b"" for i in range(2001)}
    insp = run(build(tmp_path, extra=extra))
    refused(insp, "member_count")
    assert insp.sbom is None  # nothing streamed once a cap is broken


def test_member_size_and_uncompressed_caps(tmp_path: Path) -> None:
    path = build(tmp_path, extra={f"{NS}/big.py": b"#" * 5000})
    refused(run(path, max_member=4000), "member_size", f"{NS}/big.py")
    refused(run(path, max_uncompressed=4000), "archive_size")


def test_compression_ratio(tmp_path: Path) -> None:
    path = build(tmp_path, extra={f"{NS}/zeros.py": b"\x00" * (5 << 20)})
    insp = run(path)
    refused(insp, "ratio", f"{NS}/zeros.py")
    assert any(p.check == "ratio" and p.path is None for p in insp.problems)  # whole archive


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("../evil.py", "'..'"),
        ("/etc/evil.py", "absolute"),
        (f"{NS}\\evil.py", "backslash"),
        (f"{NS}//evil.py", "empty"),
        ("C:/evil.py", "absolute"),
    ],
)
def test_unsafe_paths(tmp_path: Path, name: str, why: str) -> None:
    insp = run(build(tmp_path, extra={name: b"x = 1\n"}))
    refused(insp, "path_unsafe", name)
    assert any(why in p.message for p in insp.problems if p.check == "path_unsafe")


def test_nul_in_a_name(tmp_path: Path) -> None:
    path = build(tmp_path, extra={f"{NS}/aXb.py": b""}, compression=zipfile.ZIP_STORED)
    patch_bytes(path, b"aXb.py", b"a\x00b.py")
    refused(run(path), "path_unsafe")


def test_path_too_long_and_too_deep(tmp_path: Path) -> None:
    deep = f"{NS}/" + "/".join("d" for _ in range(20)) + "/x.py"
    long = f"{NS}/" + "x" * 300 + ".py"
    insp = run(build(tmp_path, extra={deep: b"", long: b""}))
    refused(insp, "path_unsafe", deep)
    assert any(p.path == long[:255] for p in insp.problems if p.check == "path_unsafe")


def test_symlink_and_special_members(tmp_path: Path) -> None:
    link, pipe = f"{NS}/link.py", f"{NS}/pipe"
    insp = run(
        build(tmp_path, extra={link: b"/etc/passwd", pipe: b""}, tweaks={link: symlink, pipe: fifo})
    )
    refused(insp, "link_member", link)
    refused(insp, "special_member", pipe)
    assert insp.sbom is None


def test_duplicate_names(tmp_path: Path) -> None:
    members = [*list(wheel_members().items()), (f"{NS}/model.py", b"import os\n")]
    refused(run(write_wheel(tmp_path / "w.whl", members)), "duplicate_name", f"{NS}/model.py")


def test_case_fold_collision(tmp_path: Path) -> None:
    insp = run(build(tmp_path, extra={f"{NS}/Model.py": b""}))
    refused(insp, "case_collision")
    # a collision through an implied directory is caught too
    insp = run(build(tmp_path, "w2.whl", extra={f"{NS}/Sub/a.py": b"", f"{NS}/sub/b.py": b""}))
    refused(insp, "case_collision", f"{NS}/sub")


def test_encrypted_member(tmp_path: Path) -> None:
    # zipfile clears the flag when it writes, so the archive is written by hand
    entries = _raw_good()
    entries[1].flags = 0x1
    insp = run(raw_zip(tmp_path / "w.whl", entries))
    refused(insp, "encrypted", entries[1].name.decode())
    assert insp.sbom is None


def test_bzip2_method(tmp_path: Path) -> None:
    path = build(tmp_path, tweaks={f"{NS}/model.py": bzip2})
    refused(run(path), "compression_method", f"{NS}/model.py")


def test_central_directory_disagrees_with_local_header(tmp_path: Path) -> None:
    path = build(tmp_path, extra={f"{NS}/good.py": b""}, compression=zipfile.ZIP_STORED)
    patch_bytes(path, b"good.py", b"evil.py", count=1)  # the local header comes first
    refused(run(path), "zip_structure", f"{NS}/good.py")


def _raw_good(**over: Any) -> list[RawEntry]:
    return [RawEntry(name.encode(), data) for name, data in wheel_members(**over).items()]


def test_overlapping_entries_fifield(tmp_path: Path) -> None:
    entries = _raw_good()
    # a second directory entry that reuses the first entry's local header and data
    bomb = RawEntry(f"{NS}/__init__.py".encode(), PACKAGE[f"{NS}/__init__.py"], cd_offset=0)
    bomb.write_local = False
    bomb.name = f"{NS}/again.py".encode()
    insp = run(raw_zip(tmp_path / "w.whl", [*entries, bomb]))
    refused(insp, "zip_structure")
    assert insp.sbom is None


def test_overlapping_entries_with_consistent_headers(tmp_path: Path) -> None:
    """The local header of the second entry sits inside the first entry's data (every
    header agrees with the directory; only the overlap gives it away)."""
    name_b = f"{NS}/b.py".encode()
    body_b = b"B = 1\n"
    header_b = struct.pack(
        "<IHHHHHIIIHH", 0x04034B50, 20, 0, 0, 0, 0x5C21, zlib.crc32(body_b),
        len(body_b), len(body_b), len(name_b), 0,
    ) + name_b + body_b  # fmt: skip
    pad = b"# "
    a = RawEntry(f"{NS}/a.py".encode(), pad + header_b)
    local_a = 30 + len(a.name)
    b = RawEntry(name_b, body_b, cd_offset=local_a + len(pad), write_local=False)
    path = raw_zip(tmp_path / "w.whl", [a, b])
    insp = run(path)
    hits = [p for p in insp.problems if p.check == "zip_structure"]
    assert hits and all("overlap" in p.message for p in hits), insp.problems


def test_zip64_size_lie(tmp_path: Path) -> None:
    entries = _raw_good()
    entries[1].zip64_size = 1 << 40
    insp = run(raw_zip(tmp_path / "w.whl", entries))
    refused(insp, "zip_structure")
    refused(insp, "member_size")


def test_data_before_the_first_entry(tmp_path: Path) -> None:
    insp = run(raw_zip(tmp_path / "w.whl", _raw_good(), prefix=b"#!/bin/sh\nexit 0\n"))
    refused(insp, "zip_structure")


def test_nested_archives_by_extension_and_magic(tmp_path: Path) -> None:
    inner = tmp_path / "inner.zip"
    write_wheel(inner, {"a.txt": b"a"})
    extra = {
        f"{NS}/vendor.whl": b"just text",
        f"{NS}/blob.bin": inner.read_bytes(),
        f"{NS}/data.dat": b"\x1f\x8b\x08\x00" + b"\x00" * 20,
    }
    insp = run(build(tmp_path, extra=extra))
    for name in extra:
        refused(insp, "nested_archive", name)


# ------------------------------------------------------------------------ content (T19.2/3/6)


@pytest.mark.parametrize(
    "name",
    ["evil.pth", f"{NS}/evil.pth", f"{NS}-{VERSION}.data/purelib/evil.pth"],
)
def test_pth_files_anywhere(tmp_path: Path, name: str) -> None:
    refused(run(build(tmp_path, extra={name: b"import os; os.system('x')\n"})), "pth_file", name)


@pytest.mark.parametrize(
    "name",
    ["sitecustomize.py", f"{NS}/usercustomize.py", "__main__.py", f"{NS}/dev.egg-link"],
)
def test_site_hooks(tmp_path: Path, name: str) -> None:
    refused(run(build(tmp_path, extra={name: b"x = 1\n"})), "site_hook", name)


@pytest.mark.parametrize(
    ("name", "says"),
    [
        ("json.py", "standard module"),
        ("numpy/__init__.py", "base image"),
        ("typing_extensions.py", "base image"),
        ("mapi_runner/x.py", "reserved"),
        ("helpers/__init__.py", "extra top-level"),
    ],
)
def test_shadowing_and_extra_top_level_names(tmp_path: Path, name: str, says: str) -> None:
    insp = run(build(tmp_path, extra={name: b"x = 1\n"}))
    refused(insp, "top_level_name", name.split("/")[0])
    assert any(says in p.message for p in insp.problems if p.check == "top_level_name")


def test_the_namespace_must_be_a_regular_package(tmp_path: Path) -> None:
    insp = run(build(tmp_path, drop=[f"{NS}/__init__.py"]))
    refused(insp, "namespace_package", NS)
    files = {f"{NS}.py": b"x = 1\n"}
    insp = run(build(tmp_path, "w2.whl", files=files, entry_points=None))
    refused(insp, "top_level_name", f"{NS}.py")


def test_native_code_by_extension(tmp_path: Path) -> None:
    for i, name in enumerate([f"{NS}/_speed.cpython-312-darwin.so", f"{NS}/x.pyd"]):
        insp = run(build(tmp_path, f"w{i}.whl", extra={name: b"plain text"}))
        refused(insp, "native_code", name)


@pytest.mark.parametrize(
    "head",
    [
        b"\x7fELF\x02\x01\x01" + b"\x00" * 60,
        b"\xcf\xfa\xed\xfe" + b"\x00" * 60,
        b"\xca\xfe\xba\xbe" + b"\x00" * 60,
        b"MZ" + b"\x00" * 58 + struct.pack("<I", 64) + b"PE\x00\x00" + b"\x00" * 20,
    ],
    ids=["elf", "macho", "fat", "pe"],
)
def test_native_code_by_magic_bytes(tmp_path: Path, head: bytes) -> None:
    insp = run(build(tmp_path, extra={f"{NS}/model_data.txt": head}))
    refused(insp, "native_code", f"{NS}/model_data.txt")


def test_bytecode_by_extension_directory_and_magic(tmp_path: Path) -> None:
    pyc = (3531).to_bytes(2, "little") + b"\r\n" + b"\x00" * 12
    extra = {
        f"{NS}/__pycache__/model.cpython-312.pyc": pyc,
        f"{NS}/cached.pyo": b"x",
        f"{NS}/state.dat": pyc,
    }
    insp = run(build(tmp_path, extra=extra))
    for name in extra:
        refused(insp, "bytecode", name)


def test_python_source_is_not_mistaken_for_binaries(tmp_path: Path) -> None:
    # text that starts like a PE or a pyc header without being one
    extra = {f"{NS}/mz.py": b"MZ = 1\n" * 20, f"{NS}/notes.txt": b"ab\r\nplain"}
    assert run(build(tmp_path, extra=extra)).ok


@pytest.mark.parametrize(
    "name",
    [f"{NS}-{VERSION}.data/platlib/fast.py", f"{NS}-{VERSION}.data/scripts/run"],
)
def test_data_directories(tmp_path: Path, name: str) -> None:
    refused(run(build(tmp_path, extra={name: b"x"})), "data_dir", f"{NS}-{VERSION}.data")


def test_hidden_characters_in_names(tmp_path: Path) -> None:
    name = f"{NS}/model\u202eyp.py"
    insp = run(build(tmp_path, extra={name: b""}))
    # the reported path is normalized like every untrusted string
    refused(insp, "text_unsafe", f"{NS}/modelyp.py")


# ------------------------------------------------------------------------ metadata (T19.7-9)


def test_wheel_tag_and_purelib(tmp_path: Path) -> None:
    wheel = wheel_file(tags=["cp312-cp312-macosx_11_0_arm64"], purelib="false")
    insp = run(build(tmp_path, wheel=wheel))
    refused(insp, "wheel_tag")
    assert len([p for p in insp.problems if p.check == "wheel_tag"]) == 2
    assert run(build(tmp_path, "w2.whl", wheel=wheel_file(tags=["py2.py3-none-any"]))).ok


def test_two_dist_infos(tmp_path: Path) -> None:
    extra = {"other-1.0.dist-info/METADATA": metadata(name="other", version="1.0")}
    refused(run(build(tmp_path, extra=extra)), "dist_info")


def test_egg_info_is_a_second_distribution(tmp_path: Path) -> None:
    extra = {f"{NS}/x.egg-info/PKG-INFO": b"Name: x\n"}
    refused(run(build(tmp_path, extra=extra)), "dist_info", f"{NS}/x.egg-info/PKG-INFO")


def test_missing_metadata_files(tmp_path: Path) -> None:
    insp = run(build(tmp_path, drop=[f"{DIST_INFO}/WHEEL", f"{DIST_INFO}/METADATA"]))
    refused(insp, "dist_info", f"{DIST_INFO}/WHEEL")
    refused(insp, "dist_info", f"{DIST_INFO}/METADATA")
    refused(run(build(tmp_path, "w2.whl", record=False)), "dist_info", f"{DIST_INFO}/RECORD")


def test_dist_name_must_be_the_namespace(tmp_path: Path) -> None:
    insp = run(build(tmp_path, meta=metadata(name="acme"), dist_info=f"acme-{VERSION}.dist-info"))
    refused(insp, "dist_name")
    refused(insp, "dist_info")


@pytest.mark.parametrize("name", ["mapi-plugin-mmm", "mapi_plugin_mmm", "numpy"])
def test_built_in_and_installed_dist_names(tmp_path: Path, name: str) -> None:
    insp = run(build(tmp_path, meta=metadata(name=name)))
    assert any(p.check == "dist_name" and "base image" in p.message for p in insp.problems), (
        insp.problems
    )


def test_version_must_match_the_upload(tmp_path: Path) -> None:
    insp = run(build(tmp_path, meta=metadata(version="0.2.0")))
    refused(insp, "metadata_invalid")
    insp = run(build(tmp_path, "w2.whl", meta=metadata(version="not-a-version")))
    refused(insp, "metadata_invalid")
    # PEP 440 normalization: "0.1" == "0.1.0" for the directory, "v0.1.0" for METADATA
    assert run(build(tmp_path, "w3.whl", meta=metadata(version="v0.1.0"))).ok


def test_record_must_match_the_files(tmp_path: Path) -> None:
    members = wheel_members()
    members[f"{NS}/model.py"] = b"import os\nos.system('id')\n"  # changed after the build
    members[f"{NS}/unlisted.py"] = b""
    insp = run(write_wheel(tmp_path / "w.whl", members))
    refused(insp, "dist_info", f"{NS}/model.py")
    refused(insp, "dist_info", f"{NS}/unlisted.py")


@pytest.mark.parametrize(
    ("ep", "check"),
    [
        ("[mapi.models]\nx = os:system\n", "entry_point_target"),
        ("[mapi.models]\nx = mapi_runner.supervisor:main\n", "entry_point_target"),
        ("[mapi.models]\nx = requests:get\n", "entry_point_target"),
        (f"[mapi.models]\nx = {NS}.missing:MODELS\n", "entry_point_target"),
        (f"[mapi.models]\nx = {NS}.model:MODELS [extra]\n", "entry_point_target"),
        (f"[mapi.governance_checks]\nx = {NS}.model:CHECK\n", "entry_point_group"),
        (f"[console_scripts]\nacme = {NS}.model:main\n", "entry_point_group"),
        (f"[pytest11]\nacme = {NS}.model\n", "entry_point_group"),
        ("", "entry_point_group"),
        ("[mapi.models\nbroken", "metadata_invalid"),
        (f"[mapi.models]\nx = {NS}.model:A\nx = {NS}.model:B\n", "metadata_invalid"),
    ],
)
def test_entry_points(tmp_path: Path, ep: str, check: str) -> None:
    refused(run(build(tmp_path, entry_points=ep)), check)


def test_no_entry_points_file(tmp_path: Path) -> None:
    refused(run(build(tmp_path, entry_points=None)), "entry_point_group")


def test_entry_point_to_a_package_init_is_fine(tmp_path: Path) -> None:
    ep = ENTRY_POINTS + f"\n[mapi.plots]\nchart = {NS}:PLOTS\n"
    assert run(build(tmp_path, entry_points=ep)).ok


@pytest.mark.parametrize(
    ("req", "check", "says"),
    [
        ("scikit-image>=0.20", "requirement_unlocked", "not installed"),
        ("numpy<2", "requirement_unlocked", "base image has 2.1.3"),
        ("mapi-plots[recipes]>=0.16", "requirement_unlocked", "plotly"),
        ("acme @ https://example.com/acme.whl", "requirement_url", "URL"),
        ("numpy >= 1 ; python_version >= '3.13' and extra = 'x'", "metadata_invalid", ""),
        ("not a requirement!", "metadata_invalid", ""),
    ],
)
def test_requirements(tmp_path: Path, req: str, check: str, says: str) -> None:
    insp = run(build(tmp_path, meta=metadata(requires=[req])))
    refused(insp, check)
    assert any(says in p.message for p in insp.problems if p.check == check)


def test_requirements_that_resolve(tmp_path: Path) -> None:
    reqs = [
        "mapi-sdk>=0.16,<0.17",
        "Typing_Extensions>=4",  # PEP 503 name
        "mapi-workflows[runtime]",  # extra resolved: numpy is installed
        "numpy>=2; python_version >= '3.12'",
        "scikit-image; python_version < '3.0'",  # marker false: not needed
        "pytest; extra == 'test'",  # an extra of the upload itself: not installed
        "pandas==2.2.*",
    ]
    insp = run(build(tmp_path, meta=metadata(requires=reqs)))
    assert insp.ok, [(p.check, p.message) for p in insp.problems]
    assert insp.sbom is not None
    facts = {r.name: r for r in insp.sbom.requires}
    assert facts["typing-extensions"].locked_version == "4.12.2"
    assert facts["mapi-workflows[runtime]"].locked_version == "0.16.0"
    assert facts["scikit-image"].locked_version is None


def test_requires_python(tmp_path: Path) -> None:
    refused(run(build(tmp_path, meta=metadata(requires_python=">=3.13"))), "requires_python")
    insp = run(build(tmp_path, "w2.whl", meta=metadata(requires_python="three")))
    refused(insp, "requires_python")


def test_metadata_must_be_utf8_and_small(tmp_path: Path) -> None:
    refused(run(build(tmp_path, meta=b"Name: \xff\xfe\n")), "metadata_invalid")
    big = metadata(extra="Description: " + "x" * (1 << 20))
    path = build(tmp_path, "w2.whl", meta=big, compression=zipfile.ZIP_STORED)
    refused(run(path), "metadata_invalid")


# ---------------------------------------------------------------------------- output caps


def test_problem_list_is_capped(tmp_path: Path) -> None:
    extra = {f"{NS}/m{i}.pth": b"" for i in range(400)}
    insp = run(build(tmp_path, extra=extra))
    assert len(insp.problems) <= 200
    assert "more problems" in insp.problems[-1].message


def test_large_but_valid_wheel_is_fast_and_fits(tmp_path: Path) -> None:
    """2000 members, about 20 MiB: inspected in under 2 s, and the report fits its cap."""
    extra = {
        f"{NS}/data/m{i:04d}.py": b"# " + os.urandom(10_000).hex().encode()[:10_400]
        for i in range(1990)
    }
    members = wheel_members(extra=extra)
    path = write_wheel(tmp_path / "w.whl", members, compression=zipfile.ZIP_STORED)
    assert path.stat().st_size > 20_000_000 and len(members) <= 2000
    job = job_for(path, max_bytes=40_000_000)
    idx = index()
    started = time.perf_counter()
    insp = inspect_wheel(path, job=job, index=idx)
    elapsed = time.perf_counter() - started
    assert insp.ok, insp.problems[:3]
    assert insp.sbom is not None and len(insp.sbom.files) == len(members)
    assert len(insp.model_dump_json().encode()) <= INSPECTION_MAX_BYTES
    assert elapsed < 2.0, elapsed


# ----------------------------------------------------------------- never extract, never import


@pytest.fixture
def sentinel(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Fail on any import of a name the hostile wheels use, and on any extraction."""
    hits: list[str] = []
    real_import = builtins.__import__
    hostile = {NS, "sitecustomize", "usercustomize", "evil", "helpers", "acme"}

    def guarded(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.split(".")[0] in hostile:
            hits.append(name)
            raise AssertionError(f"imported {name} from an upload")
        return real_import(name, *args, **kwargs)

    def no_extract(*_a: Any, **_k: Any) -> Any:
        hits.append("extract")
        raise AssertionError("extracted a member")

    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setattr(zipfile.ZipFile, "extract", no_extract)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", no_extract)
    yield hits
    assert hits == []


def test_never_extracts_or_imports(tmp_path: Path, sentinel: list[str]) -> None:
    work = tmp_path / "work"
    work.mkdir()
    extra = {
        "sitecustomize.py": b"raise SystemExit('ran')\n",
        "evil.pth": b"import evil\n",
        f"{NS}/__init__.py": b"raise SystemExit('ran')\n",
        "helpers/__init__.py": b"raise SystemExit('ran')\n",
    }
    path = build(work, extra=extra)
    before = sorted(p.name for p in work.iterdir())
    cwd = os.getcwd()
    os.chdir(work)
    try:
        insp = run(path)
    finally:
        os.chdir(cwd)
    assert not insp.ok
    assert sorted(p.name for p in work.iterdir()) == before  # nothing written next to it
    assert NS not in sys.modules and "helpers" not in sys.modules


# ---------------------------------------------------------------------------- worker run


class _Ctx:
    def __init__(self, inputs: Mapping[str, Path]) -> None:
        self.inputs = dict(inputs)
        self.outputs: dict[str, bytes] = {}
        self.states: list[str] = []

    def state(self, phase: Any, message: str | None = None) -> None:
        self.states.append(str(phase))

    def progress(self, fraction: float | None, message: str = "", *, force: bool = False) -> None:
        pass

    def download(self, name: str, *, required: bool = True) -> Path | None:
        return self.inputs[name]

    def write_output(self, name: str, data: bytes) -> Path:
        self.outputs[name] = data
        return Path(name)


def test_run_plugin_inspect_writes_the_inspection(tmp_path: Path) -> None:
    from mapi_runner.worker.plugin_inspect import run_plugin_inspect

    path = build(tmp_path, extra={"evil.pth": b""})
    ctx = _Ctx({PLUGIN_INPUT: path})
    run_plugin_inspect(ctx, job_for(path))  # type: ignore[arg-type]
    assert list(ctx.outputs) == [PLUGIN_INSPECTION_OUTPUT]
    doc = PluginInspection.model_validate_json(ctx.outputs[PLUGIN_INSPECTION_OUTPUT])
    assert not doc.ok and "pth_file" in checks(doc)
    assert json.loads(ctx.outputs[PLUGIN_INSPECTION_OUTPUT])["archive_sha256"].startswith("sha256:")


# ------------------------------------------------------------------ PEP 440 / 508 subset


@pytest.mark.parametrize(
    ("spec", "version", "expected"),
    [
        (">=1.0", "1.0", True),
        (">=1.0", "0.9", False),
        ("<2", "2.0rc1", False),
        ("<2", "1.9", True),
        ("<2rc2", "2.0rc1", True),
        (">1.0", "1.0.post1", False),
        (">1.0", "1.0+local", False),
        (">1.0", "1.1", True),
        ("==1.0", "1.0+local", True),
        ("==1.0+local", "1.0", False),
        ("==1.2.*", "1.2.5", True),
        ("==1.2.*", "1.3", False),
        ("!=1.2.*", "1.3", True),
        ("~=1.4.2", "1.4.9", True),
        ("~=1.4.2", "1.5", False),
        ("~=1.4", "1.9", True),
        ("~=1.4", "2.0", False),
        ("==1", "1.0.0", True),
        ("===1.0", "1.0", True),
        ("===1.0", "1.0.0", False),
        (">=2,<3", "2.1.3", True),
        ("<=1.0", "1.0+abc", True),
        (">=1.0.dev0", "1.0a1", True),
        ("==1!2.0", "2.0", False),
        ("", "anything", True),
    ],
)
def test_specifier_contains(spec: str, version: str, expected: bool) -> None:
    assert specifier_contains(spec, version) is expected


@pytest.mark.parametrize("spec", ["1.0", "=>1", "~=1", "<1.0+x", ">=1.*", "==1.*.2"])
def test_malformed_specifiers(spec: str) -> None:
    with pytest.raises(InvalidVersion):
        specifier_contains(spec, "1.0")


def test_version_ordering_follows_pep_440() -> None:
    order = ["1.0.dev1", "1.0a1", "1.0a2.dev1", "1.0b1", "1.0rc1", "1.0", "1.0+a", "1.0.post1",
             "1.1", "1!0.1"]  # fmt: skip
    parsed = [Version.parse(v) for v in order]
    assert all(a < b for a, b in itertools.pairwise(parsed))
    assert str(Version.parse("1.0-ALPHA.2")) == "1.0a2"
    assert Version.parse("1.0").same(Version.parse("1.0.0"))


def test_agrees_with_packaging_where_available() -> None:
    packaging = pytest.importorskip("packaging.specifiers")
    cases = [
        (s, v)
        for s in [">=1.0", "<2", ">1.0", "~=1.4.2", "==1.2.*", "!=1.0", "<=1.0", "~=0.9"]
        for v in ["0.9", "1.0", "1.0.post1", "1.0+x", "1.2.5", "1.4.3", "2.0rc1", "2.0", "1.0a1"]
    ]
    for spec, version in cases:
        theirs = packaging.SpecifierSet(spec).contains(version, prereleases=True)
        assert specifier_contains(spec, version) is theirs, (spec, version)


def test_markers() -> None:
    env = marker_env("3.12.15")
    assert marker_matches("python_version >= '3.12'", env)
    assert not marker_matches("python_version < '3.12'", env)
    assert marker_matches("python_full_version == '3.12.15'", env)
    assert not marker_matches("extra == 'test'", env)
    assert marker_matches("extra == 'Te_st'", marker_env("3.12.15", extra="te-st"))
    assert marker_matches(
        "(python_version >= '3' and os_name == 'nope') or 'linux' not in 'win32'", env
    )
    assert marker_matches("'3.12' in python_version", env)
    for bad in ["python_version >=", "foo == 'x'", "(python_version > '3'", "x ~ y"]:
        with pytest.raises(InvalidVersion):
            marker_matches(bad, env)


def test_parse_requirement() -> None:
    r = parse_requirement("Mapi_Plots[Recipes, extra2] (>=0.16,<1) ; python_version >= '3.11'")
    assert (r.name, r.extras, r.specifier) == ("mapi-plots", ("recipes", "extra2"), ">=0.16,<1")
    assert r.marker == "python_version >= '3.11'" and r.url is None
    u = parse_requirement("acme @ https://x.test/a;b.whl ; os_name == 'posix'")
    assert u.url == "https://x.test/a;b.whl" and u.marker == "os_name == 'posix'"


def test_hostile_inputs_stay_cheap(tmp_path: Path) -> None:
    """Deeply nested markers, very long requirements and names fail fast, not slowly."""
    nested = "numpy; " + "(" * 5000 + "python_version > '3'" + ")" * 5000
    with pytest.raises(InvalidVersion):
        parse_requirement(nested)
    with pytest.raises(InvalidVersion):
        marker_matches("(" * 5000 + "os_name == 'x'" + ")" * 5000, marker_env("3.12.15"))
    insp = run(build(tmp_path, meta=metadata(requires=["x" * 2000])))
    refused(insp, "metadata_invalid")
    deep = "/".join(["d"] * 20000) + "/x.py"
    started = time.perf_counter()
    insp = run(build(tmp_path, "w2.whl", extra={deep: b""}))
    assert time.perf_counter() - started < 2.0
    refused(insp, "path_unsafe")


# ------------------------------------------------------------- review fixes (inspect-1..6)


@pytest.mark.parametrize(
    "text",
    [
        f"[mapi.models]\nok = {NS}.model:MODELS\n;evil = os:getcwd\n",
        f"[mapi.models]\nok = {NS}.model:MODELS\n  ;evil = os:getcwd\n",
        f"[mapi.models]\nok = {NS}.model:MODELS\n  evil = os:getcwd\n",
        f"stray = os:getcwd\n[mapi.models]\nok = {NS}.model:MODELS\n",
        f"[mapi.models]\nok = {NS}.model:MODELS\nno equals sign\n",
        f"[mapi.models]\nok = {NS}.model:MODELS\nok = {NS}.steps:STEPS\n",
    ],
)
def test_entry_points_the_loader_reads_differently_are_refused(tmp_path: Path, text: str) -> None:
    """inspect-1: a ';' line is a comment for configparser but an entry point for
    importlib.metadata; an indented line is a continuation for one and a new line for the
    other. Such files are refused, so the reviewed list is the loaded list."""
    insp = run(build(tmp_path, entry_points=text))
    refused(insp, "metadata_invalid", f"{DIST_INFO}/entry_points.txt")


def test_a_default_section_is_an_ordinary_group(tmp_path: Path) -> None:
    """inspect-1: ``[DEFAULT]`` is an ordinary group for the loader, not defaults folded into
    every group: it is refused as a group, and no phantom mapi.models entry appears."""
    text = f"[DEFAULT]\nx = {NS}.model:MODELS\n[mapi.models]\nok = {NS}.model:MODELS\n"
    insp = run(build(tmp_path, entry_points=text))
    refused(insp, "entry_point_group")
    assert {(e.group, e.name) for e in insp.entry_points} == {
        ("DEFAULT", "x"),
        ("mapi.models", "ok"),
    }


def test_the_reviewed_entry_points_are_the_loaded_ones(tmp_path: Path) -> None:
    """inspect-1: the inspector reads entry_points.txt exactly as importlib.metadata does."""
    import importlib.metadata as im

    insp = run(build(tmp_path))
    assert insp.ok
    loaded = {(e.group, e.name, e.value) for e in im.EntryPoints._from_text(ENTRY_POINTS)}
    assert {(e.group, e.name, e.value) for e in insp.entry_points} == loaded


def _dir_mode(info: zipfile.ZipInfo) -> None:
    info.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10


def test_a_directory_mode_on_a_file_name_is_refused_and_never_skipped(tmp_path: Path) -> None:
    """inspect-2: an entry whose mode says directory but whose name has no '/' is a file
    for name-based tools (zipfile, pip): it is refused, never skipped unread."""
    so, pth = f"{NS}/evil.so", f"{NS}/x.pth"
    members = wheel_members(extra={so: b"\x7fELF\x02\x01\x01" + b"\x00" * 60, pth: b"x\n"})
    path = write_wheel(tmp_path / "w.whl", members, tweaks={so: _dir_mode, pth: _dir_mode})
    insp = run(path)
    refused(insp, "special_member", so)
    refused(insp, "special_member", pth)


def test_a_file_mode_on_a_directory_name_and_a_directory_with_data_are_refused(
    tmp_path: Path,
) -> None:
    """inspect-2/5: the mount extractor refuses a file mode on a name ending in '/'; a
    directory entry that holds bytes hides them from every check."""

    def reg(info: zipfile.ZipInfo) -> None:
        info.external_attr = (stat.S_IFREG | 0o644) << 16

    path = build(tmp_path, extra={f"{NS}/sub/": b""}, tweaks={f"{NS}/sub/": reg})
    refused(run(path), "special_member", f"{NS}/sub/")
    path = build(tmp_path, "d.whl", extra={f"{NS}/data/": b"hidden bytes"})
    refused(run(path), "zip_structure", f"{NS}/data/")


@pytest.mark.parametrize(
    "extra",
    [
        {f"{NS}/x": b"x = 1\n", f"{NS}/x/": b""},
        {f"{NS}/x": b"x = 1\n", f"{NS}/x/y.py": b"y = 1\n"},
    ],
)
def test_a_path_that_is_a_file_and_a_directory_is_refused(
    tmp_path: Path, extra: dict[str, bytes]
) -> None:
    """inspect-5: what the inspector passes, the mount extractor can extract."""
    insp = run(build(tmp_path, extra=extra))
    assert not insp.ok
    assert checks(insp) & {"duplicate_name", "path_unsafe"}


def test_padding_the_problem_list_never_hides_a_bomb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """inspect-3: 200 cheap problems first, then a high-ratio member: the report still names
    the bomb, and no member data is inflated."""
    import mapi_runner.plugin_inspect as pi

    streamed: list[int] = []
    real = pi._stream

    def spy(*args: Any, **kw: Any) -> Any:
        streamed.append(1)
        return real(*args, **kw)

    monkeypatch.setattr(pi, "_stream", spy)
    pad = {}
    for i in range(200):
        pad[f"{NS}/c{i}.py"] = b""
        pad[f"{NS}/C{i}.py"] = b""
    pad[f"{NS}/zeros.py"] = b"\x00" * (5 * 1024 * 1024)
    insp = run(build(tmp_path, extra=pad))
    assert len(insp.problems) <= 200
    assert {"case_collision", "ratio"} <= checks(insp)
    assert streamed == []


@pytest.mark.parametrize(
    "meta",
    [
        metadata(version="1." + "9" * 5000),
        metadata(requires_python=">=" + "9" * 5000),
        metadata(requires_python=">=3." + "9" * 200),
    ],
)
def test_huge_version_numbers_are_refused_not_a_crash(tmp_path: Path, meta: bytes) -> None:
    """inspect-4: int() of thousands of digits raises ValueError; the inspector refuses the
    wheel with a metadata problem instead of failing the run."""
    insp = run(build(tmp_path, meta=meta))
    assert not insp.ok
    assert checks(insp) & {"metadata_invalid", "dist_info", "requires_python"}


def test_huge_versions_raise_invalid_version() -> None:
    with pytest.raises(InvalidVersion):
        Version.parse("1." + "9" * 5000)
    with pytest.raises(InvalidVersion):
        specifier_contains(">=" + "9" * 5000, "3.12.0")


def test_only_the_dist_info_record_signatures_skip_the_hash(tmp_path: Path) -> None:
    """inspect-6: a package file called RECORD.jws is hashed like any other file."""
    members = wheel_members(extra={f"{NS}/RECORD.jws": b"x = 1\n"})
    record = members[f"{DIST_INFO}/RECORD"].replace(
        f"{NS}/RECORD.jws,sha256=".encode(), f"{NS}/RECORD.jws,bogus=".encode()
    )
    members[f"{DIST_INFO}/RECORD"] = record
    refused(run(write_wheel(tmp_path / "w.whl", members)), "dist_info", f"{NS}/RECORD.jws")
