"""Check the layout of a wheel the way the platform does (docs/extend/upload.md).

``plugin_inspect`` is the first check of every upload. It reads the wheel as a zip archive and
never extracts or imports anything from it. This snippet writes a small wheel in a temporary
directory (it is never installed) and runs the same function on your machine, then does the
same with a wheel that has a ``.pth`` file. ``make docs-test`` runs the file top to bottom.
"""

from __future__ import annotations

import base64
import hashlib
import tempfile
import zipfile
from pathlib import Path

NS = "t_k3vq7m_acme"
VERSION = "0.1.0"
DIST_INFO = f"{NS}-{VERSION}.dist-info"


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + digest.decode()


def write_wheel(folder: Path, extra: dict[str, bytes] | None = None) -> Path:
    """A pure-Python wheel for the namespace NS, as `uv build --wheel` writes it."""
    members: dict[str, bytes] = {
        f"{NS}/__init__.py": b'"""Acme steps."""\n',
        f"{NS}/steps.py": b"STEPS = []\n",
        f"{DIST_INFO}/METADATA": (
            "Metadata-Version: 2.1\n"
            "Name: t-k3vq7m-acme\n"
            f"Version: {VERSION}\n"
            "Requires-Dist: mapi-sdk>=0.16\n"
        ).encode(),
        f"{DIST_INFO}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: docs\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{DIST_INFO}/entry_points.txt": f"[mapi.steps]\nwinsorize = {NS}.steps:STEPS\n".encode(),
        **(extra or {}),
    }
    record = "".join(f"{name},{_record_hash(data)},{len(data)}\n" for name, data in members.items())
    members[f"{DIST_INFO}/RECORD"] = (record + f"{DIST_INFO}/RECORD,,\n").encode()
    path = folder / f"{NS}-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


# --8<-- [start:inspect]
from mapi_runner.plugin_inspect import inspect_wheel, installed_index
from mapi_spec.jobs import PluginInspectJob
from mapi_spec.plugins import upload_dist_name


def check(wheel: Path):  # noqa: ANN201 - a PluginInspection
    job = PluginInspectJob(
        plugin_version_id="plv_01jbz3q2x8m9v7k5h4g3f2e1d1",
        namespace=NS,
        dist_name=upload_dist_name(NS),
        version=VERSION,
        expected_sha256="sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest(),
    )
    # the index is the installed packages: in the sandbox, those of the base image
    return inspect_wheel(wheel, job=job, index=installed_index())


with tempfile.TemporaryDirectory() as tmp:
    good = check(write_wheel(Path(tmp)))
    print("ok:", good.ok, "| top level:", good.top_level, "| files:", good.members)
    for ep in good.entry_points:
        print("entry point:", ep.group, ep.name, "=", ep.value)
    for req in good.sbom.requires if good.sbom else []:
        print("requires:", req.name, req.specifier, "-> base image has", req.locked_version)
# --8<-- [end:inspect]

assert good.ok, good.problems
assert good.top_level == [NS]
assert good.sbom is not None and len(good.sbom.files) == good.members

# --8<-- [start:refused]
with tempfile.TemporaryDirectory() as tmp:
    bad = check(write_wheel(Path(tmp), extra={"zz_hook.pth": b"import os\n"}))
    print("ok:", bad.ok)
    for problem in bad.problems:
        print(f"{problem.check}: {problem.message} ({problem.path})")
# --8<-- [end:refused]

assert not bad.ok
assert "pth_file" in {p.check for p in bad.problems}
