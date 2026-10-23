"""The supervisor's overlay mounting (v0.19, D17): fetch + sha256 before any write, then the
whitelist extractor (regular files and directories only, read-only modes).

The wheels here are built with :mod:`zipfile` in a tmp directory; nothing is ever imported.
Fetches go through the real GCS shim with signed GETs, like a run.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from mapi_runner.overlay import OverlayError, extract_wheel, fetch_overlay, mount_overlays
from mapi_spec.plugins import ArchiveLimits, OverlayPin
from mapi_spec.run import OverlayMount
from shimgcs import ShimGcs

NS = "t_k3vq7m_acme"
PVID = "plv_01jbz3q2x8m9v7k5h4g3f2e1d1"
PVID2 = "plv_01jbz3q2x8m9v7k5h4g3f2e1d2"

GOOD: Mapping[str, bytes] = {
    f"{NS}/__init__.py": b'"""Acme."""\n',
    f"{NS}/model.py": b"MODELS = []\n",
    f"{NS}-0.1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: t-k3vq7m-acme\n",
}

Tweak = Callable[[zipfile.ZipInfo], None]


def zip_bytes(
    members: Mapping[str, bytes],
    *,
    tweak: Mapping[str, Tweak] | None = None,
    method: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = (0o040755 if name.endswith("/") else 0o100644) << 16
            info.compress_type = method
            if tweak and name in tweak:
                tweak[name](info)
            zf.writestr(info, data)
    return buf.getvalue()


def write(tmp: Path, data: bytes, name: str = "w.whl") -> Path:
    path = tmp / name
    path.write_bytes(data)
    return path


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def files_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


# ================================================================================ extractor


def test_a_good_wheel_is_extracted_read_only(tmp_path: Path) -> None:
    dest = tmp_path / "ov"
    extract_wheel(write(tmp_path, zip_bytes(GOOD)), dest, limits=ArchiveLimits())
    assert (dest / NS / "model.py").read_bytes() == b"MODELS = []\n"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o555
    for p in dest.rglob("*"):
        assert stat.S_IMODE(p.stat().st_mode) == (0o555 if p.is_dir() else 0o444)


def _symlink(info: zipfile.ZipInfo) -> None:
    info.external_attr = (stat.S_IFLNK | 0o777) << 16


def _fifo(info: zipfile.ZipInfo) -> None:
    info.external_attr = (stat.S_IFIFO | 0o644) << 16


@pytest.mark.parametrize(
    ("members", "tweak", "why"),
    [
        ({**GOOD, f"{NS}/link.py": b"/etc/passwd"}, {f"{NS}/link.py": _symlink}, "link"),
        ({**GOOD, f"{NS}/pipe": b""}, {f"{NS}/pipe": _fifo}, "link or a special"),
        ({**GOOD, "/etc/evil.py": b"x"}, None, "absolute"),
        ({**GOOD, f"{NS}/../../evil.py": b"x"}, None, "'..'"),
        ({**GOOD, f"{NS}\\evil.py": b"x"}, None, "backslash"),
        ({**GOOD, f"{NS}/MODEL.py": b"x"}, None, "case"),
        ({**GOOD, f"{NS}/model.py/inner.py": b"x"}, None, "is a file"),
        ({**GOOD, "C:/evil.py": b"x"}, None, "absolute"),
        ({**GOOD, f"{NS}/./x.py": b"x"}, None, "'.'"),
    ],
)
def test_unsafe_members_are_refused(
    tmp_path: Path, members: Mapping[str, bytes], tweak: Mapping[str, Tweak] | None, why: str
) -> None:
    dest = tmp_path / "ov"
    with pytest.raises(OverlayError) as err:
        extract_wheel(
            write(tmp_path, zip_bytes(members, tweak=tweak)), dest, limits=ArchiveLimits()
        )
    assert err.value.code == "SCHEMA_INVALID"
    assert why in str(err.value)
    assert not dest.exists()  # nothing is left behind


def test_duplicate_names_are_refused(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{NS}/a.py", b"1")
        with pytest.warns(UserWarning):
            zf.writestr(f"{NS}/a.py", b"2")
    with pytest.raises(OverlayError, match="same name"):
        extract_wheel(write(tmp_path, buf.getvalue()), tmp_path / "ov", limits=ArchiveLimits())


def test_encrypted_members_are_refused(tmp_path: Path) -> None:
    data = bytearray(zip_bytes(GOOD, method=zipfile.ZIP_STORED))
    # zipfile never writes the encryption flag: set bit 0 in every local and central header
    for sig, flag_at in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        at = data.find(sig)
        while at >= 0:
            data[at + flag_at] |= 0x1
            at = data.find(sig, at + 4)
    with pytest.raises(OverlayError, match="encrypted"):
        extract_wheel(write(tmp_path, bytes(data)), tmp_path / "ov", limits=ArchiveLimits())


def test_other_compression_methods_are_refused(tmp_path: Path) -> None:
    data = zip_bytes(GOOD, method=zipfile.ZIP_BZIP2)
    with pytest.raises(OverlayError, match="compression method"):
        extract_wheel(write(tmp_path, data), tmp_path / "ov", limits=ArchiveLimits())


@pytest.mark.parametrize(
    ("members", "limits", "why"),
    [
        ({f"{NS}/{i}.py": b"x" for i in range(12)}, {"max_members": 10}, "members"),
        ({f"{NS}/big.py": b"0" * 5000}, {"max_member": 4000}, "larger than"),
        ({f"{NS}/bomb.py": b"0" * 200_000}, {"max_ratio": 100}, "compressed more"),
        (
            {f"{NS}/a.py": os.urandom(3000), f"{NS}/b.py": os.urandom(3000)},
            {"max_uncompressed": 5000},
            "expands",
        ),
        ({"/".join(["d"] * 20) + "/x.py": b"x"}, {"max_depth": 16}, "deeper"),
        ({f"{NS}/" + "a" * 300 + ".py": b"x"}, {"max_path": 255}, "longer"),
    ],
)
def test_archive_limits_are_enforced(
    tmp_path: Path, members: Mapping[str, bytes], limits: Mapping[str, int], why: str
) -> None:
    with pytest.raises(OverlayError, match=why):
        extract_wheel(
            write(tmp_path, zip_bytes(members)), tmp_path / "ov", limits=ArchiveLimits(**limits)
        )
    assert not (tmp_path / "ov").exists()


def test_a_member_longer_than_declared_is_refused(tmp_path: Path) -> None:
    data = zip_bytes({f"{NS}/a.py": b"A" * 64}, method=zipfile.ZIP_STORED)
    # lie about the size in the central directory (the local header keeps the truth)
    lied = data.replace((64).to_bytes(4, "little") * 2, (8).to_bytes(4, "little") * 2)
    with pytest.raises(OverlayError):
        extract_wheel(write(tmp_path, lied), tmp_path / "ov", limits=ArchiveLimits())
    assert not (tmp_path / "ov").exists()


def test_not_a_zip_is_refused(tmp_path: Path) -> None:
    with pytest.raises(OverlayError, match="not a valid zip"):
        extract_wheel(write(tmp_path, b"PK not really"), tmp_path / "ov", limits=ArchiveLimits())


def test_the_destination_must_not_exist(tmp_path: Path) -> None:
    (tmp_path / "ov").mkdir()
    with pytest.raises(FileExistsError):
        extract_wheel(write(tmp_path, zip_bytes(GOOD)), tmp_path / "ov", limits=ArchiveLimits())


# ==================================================================================== fetch


def _mount(
    gcs: ShimGcs,
    data: bytes,
    *,
    pin_sha: str | None = None,
    pvid: str = PVID,
    ns: str = NS,
    size: int | None = None,
) -> OverlayMount:
    name = f"t/default/plugins/plg_x/{pvid}.whl"
    gcs.put(gcs.buckets.data, name, data, "application/zip")
    return OverlayMount(
        pin=OverlayPin(plugin_version_id=pvid, namespace=ns, sha256=pin_sha or sha(data)),
        get=gcs.sign_get(gcs.buckets.data, name),
        size=len(data) if size is None else size,
    )


def test_fetch_verifies_the_sha256_before_writing(gcs: ShimGcs, tmp_path: Path) -> None:
    data = zip_bytes(GOOD)
    mount = _mount(gcs, data, pin_sha="sha256:" + "ab" * 32)
    dest = tmp_path / "dl"
    with pytest.raises(OverlayError) as err:
        fetch_overlay(mount, dest, timeout_s=30)
    assert err.value.code == "INTEGRITY_FAILURE"
    assert files_under(dest) == []


def test_fetch_refuses_a_size_mismatch(gcs: ShimGcs, tmp_path: Path) -> None:
    data = zip_bytes(GOOD)
    mount = _mount(gcs, data, size=len(data) - 1)
    with pytest.raises(OverlayError) as err:
        fetch_overlay(mount, tmp_path / "dl", timeout_s=30)
    assert err.value.code == "INTEGRITY_FAILURE"
    assert files_under(tmp_path / "dl") == []


def test_fetch_refuses_a_declared_size_over_the_cap(gcs: ShimGcs, tmp_path: Path) -> None:
    mount = _mount(gcs, zip_bytes(GOOD))
    with pytest.raises(OverlayError, match="limit"):
        fetch_overlay(mount, tmp_path / "dl", timeout_s=30, max_bytes=10)


def test_fetch_writes_the_verified_wheel(gcs: ShimGcs, tmp_path: Path) -> None:
    data = zip_bytes(GOOD)
    path = fetch_overlay(_mount(gcs, data), tmp_path / "dl", timeout_s=30)
    assert path.name == f"{PVID}.whl" and path.read_bytes() == data


def test_mount_overlays_extracts_each_pin_and_drops_the_wheels(
    gcs: ShimGcs, tmp_path: Path
) -> None:
    other = "t_k3vq7m_beta"
    beta = {f"{other}/__init__.py": b"", f"{other}-1.0.dist-info/METADATA": b"Name: b\n"}
    mounts = [_mount(gcs, zip_bytes(GOOD)), _mount(gcs, zip_bytes(beta), pvid=PVID2, ns=other)]
    root = tmp_path / "overlays"
    got = mount_overlays(mounts, root, timeout_s=60)
    assert [(o.namespace, o.pvid) for o in got] == [(NS, PVID), (other, PVID2)]
    assert [o.sha256 for o in got] == [m.pin.sha256 for m in mounts]
    assert sorted(p.name for p in root.iterdir()) == [PVID, PVID2]  # no .whl left
    assert stat.S_IMODE(root.stat().st_mode) == 0o555
    assert got[0].argv == ["--overlay", f"{root / PVID}={NS}={PVID}"]


def test_mount_overlays_removes_everything_when_one_fails(gcs: ShimGcs, tmp_path: Path) -> None:
    mounts = [
        _mount(gcs, zip_bytes(GOOD)),
        _mount(gcs, zip_bytes(GOOD), pvid=PVID2, ns="t_k3vq7m_beta", pin_sha=sha(b"other")),
    ]
    root = tmp_path / "overlays"
    with pytest.raises(OverlayError) as err:
        mount_overlays(mounts, root, timeout_s=60)
    assert err.value.code == "INTEGRITY_FAILURE"
    assert not root.exists()


def test_a_transport_error_is_an_os_error(gcs: ShimGcs, tmp_path: Path) -> None:
    mount = _mount(gcs, zip_bytes(GOOD))
    gone = mount.model_copy(
        update={"get": mount.get.model_copy(update={"url": mount.get.url + "tampered"})}
    )
    with pytest.raises(OSError):
        fetch_overlay(gone, tmp_path / "dl", timeout_s=30)
    assert files_under(tmp_path / "dl") == []


def test_a_wheel_gone_from_storage_is_an_integrity_failure(gcs: ShimGcs, tmp_path: Path) -> None:
    """F2: the pinned generation was replaced or deleted after the check. The signed GET
    answers 404; that is not a transport error the platform may retry but an integrity
    failure (the approved bytes are gone), and nothing is written."""
    mount = _mount(gcs, zip_bytes(GOOD))
    gcs.delete(gcs.buckets.data, f"t/default/plugins/plg_x/{PVID}.whl")
    with pytest.raises(OverlayError) as err:
        fetch_overlay(mount, tmp_path / "dl", timeout_s=30)
    assert err.value.code == "INTEGRITY_FAILURE"
    assert "gone or was replaced" in str(err.value)
    assert files_under(tmp_path / "dl") == []
    # replaced through the storage API: a new generation, the pinned one is gone
    mount = _mount(gcs, zip_bytes(GOOD))
    gcs.put(gcs.buckets.data, f"t/default/plugins/plg_x/{PVID}.whl", zip_bytes(GOOD), "x/y")
    with pytest.raises(OverlayError) as err:
        fetch_overlay(mount, tmp_path / "dl2", timeout_s=30)
    assert err.value.code == "INTEGRITY_FAILURE"


def test_the_supervisor_and_the_overlay_module_import_no_plugin_code() -> None:
    """Mounting uploads adds nothing to the supervisor's imports: no SDK, no workflows
    runtime, no array library, no plugin package."""
    import subprocess
    import sys

    script = (
        "import sys, mapi_runner.supervisor, mapi_runner.overlay, mapi_runner.__main__; "
        "bad = ('mapi_sdk', 'mapi_workflows', 'mapi_plots', 'mapi_steps', 'mapi_plugin', "
        "'numpy', 'xarray', 'pandas', 't_'); "
        "print(sorted(m for m in sys.modules if m.startswith(bad)))"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", script], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]", out.stdout
