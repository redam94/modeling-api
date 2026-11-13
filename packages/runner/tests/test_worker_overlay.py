"""The worker side of uploaded code (v0.19, D17): ``--overlay`` mounting and the path rule.

Each case extracts a wheel built here (from the ``mapi plugin init`` scaffold, renamed into an
uploaded namespace) with :func:`mapi_runner.overlay.extract_wheel` into a read-only directory
and loads it in a **fresh** ``python -I`` process exactly like the worker does
(:func:`mapi_runner.worker.__main__.mount_overlays`, then the SDK registry and
:func:`mapi_runner.catalog_export.full_catalog`). The upload is never imported by this test
process: only the child interpreter loads it.

:func:`build_upload` is shared with ``test_plugin_describe.py``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from mapi_runner.overlay import extract_wheel
from mapi_spec.plugins import ArchiveLimits

NS = "t_k3vq7m_acme"
PVID = "plv_01jbz3q2x8m9v7k5h4g3f2e1d1"
OTHER_NS = "t_k3vq7m_beta"
OTHER_PVID = "plv_01jbz3q2x8m9v7k5h4g3f2e1d2"
VERSION = "0.1.0"
ALL_KINDS = ("model", "step", "recipe", "node", "template")


# =============================================================================== the wheel


def _record(members: Mapping[str, bytes], record: str) -> bytes:
    rows = []
    for name, data in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rows.append(f"{name},sha256={digest},{len(data)}")
    rows.append(f"{record},,")
    return ("\n".join(rows) + "\n").encode()


def _entry_points_txt(groups: Mapping[str, Mapping[str, str]]) -> bytes:
    out = []
    for group, table in groups.items():
        out.append(f"[{group}]")
        out += [f"{k} = {v}" for k, v in table.items()]
        out.append("")
    return "\n".join(out).encode()


def upload_members(
    tmp: Path,
    *,
    namespace: str = NS,
    dist: str | None = None,
    kinds: Sequence[str] = ALL_KINDS,
    extra_files: Mapping[str, bytes] | None = None,
    extra_entry_points: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, bytes]:
    """The members of a wheel of the ``mapi plugin init`` scaffold in ``namespace``.

    ``dist`` is the distribution name written in ``METADATA`` (default: the PEP 503 form of
    the namespace, like an upload)."""
    from mapi_cli.commands.plugin import Kind, plugin_names, render_plugin

    order = [Kind(k) for k in kinds]
    names = plugin_names("acme", "acme", order)
    module = namespace
    names.update(
        dist=dist or namespace.replace("_", "-"),
        module=module,
        namespace=namespace,
        model_id=f"{namespace}.group_mean",
        step_id=f"{namespace}.winsorize",
        recipe_id=f"{namespace}.weekly_total",
        node_id=f"test.{namespace}_zero_share",
        template_id=f"{namespace}_weekly_check",
    )
    names["plugin_id"] = names["model_id"] if "model" in kinds else names["step_id"]
    src = tmp / f"src-{namespace}-{len(list(tmp.iterdir()))}"
    render_plugin(src, names, order)
    project = tomllib.loads((src / "pyproject.toml").read_text())["project"]
    groups: dict[str, dict[str, str]] = {
        g: dict(t) for g, t in project.get("entry-points", {}).items()
    }
    for g, t in (extra_entry_points or {}).items():
        groups.setdefault(g, {}).update(t)
    members: dict[str, bytes] = {}
    pkg = src / "src" / module
    for path in sorted(pkg.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            members[f"{module}/{path.relative_to(pkg).as_posix()}"] = path.read_bytes()
    members.update(extra_files or {})
    info = f"{module}-{VERSION}.dist-info"
    metadata = (
        f"Metadata-Version: 2.1\nName: {names['dist']}\nVersion: {VERSION}\n"
        "Requires-Python: >=3.12\n"
    ).encode()
    members[f"{info}/METADATA"] = metadata
    members[f"{info}/WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: mapi-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    members[f"{info}/entry_points.txt"] = _entry_points_txt(groups)
    members[f"{info}/RECORD"] = _record(members, f"{info}/RECORD")
    return members


def write_wheel(path: Path, members: Mapping[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return path


def build_upload(tmp: Path, name: str = "upload.whl", **kw: Any) -> Path:
    """A wheel of the scaffold in an uploaded namespace (see :func:`upload_members`)."""
    return write_wheel(tmp / name, upload_members(tmp, **kw))


def extract(wheel: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    extract_wheel(wheel, dest, limits=ArchiveLimits())
    return dest


def sha256_of(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


# ========================================================================= the child process

_PROBE = textwrap.dedent(
    """
    import json, sys
    from mapi_runner.worker.__main__ import mount_overlays
    overlays = mount_overlays(sys.argv[1:])
    from mapi_sdk.registry import default_registry
    from mapi_runner.catalog_export import full_catalog
    reg = default_registry()
    cat = full_catalog(reg)
    out = {
        "sys_path": sys.path,
        "models": {m.manifest.id: m.provider for m in cat.models},
        "steps": {s.id: s.provider for s in cat.steps},
        "plots": {p.id: p.provider for p in cat.plots},
        "nodes": [n.id for n in cat.workflow_nodes],
        "templates": {t.id: t.provider for t in cat.workflow_templates},
        "diagnostics": [d.model_dump(mode="json") for d in cat.diagnostics],
        "modules": sorted(m for m in sys.modules if m.startswith("t_")),
    }
    for kind, ref in (("model", "{ns}.group_mean"), ("step", "{ns}.winsorize")):
        try:
            out[kind + "_provider"] = reg.provider_of(kind, ref)
        except Exception as exc:
            out[kind + "_provider"] = repr(exc)
    from mapi_workflows.runtime.nodes import node_registry
    out["node_fns"] = sorted(k for k in node_registry() if ".t_" in k)
    print("RESULT " + json.dumps(out))
    """
)


def probe(*overlays: tuple[Path, str, str], cwd: Path) -> dict[str, Any]:
    """Load the overlays in a fresh ``python -I`` like the worker and report what it saw."""
    script = _PROBE.replace("{ns}", NS)
    args = [f"{d}={ns}={pvid}" for d, ns, pvid in overlays]
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script, *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=300,
        check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, proc.stderr[-4000:]
    result: dict[str, Any] = json.loads(lines[-1].removeprefix("RESULT "))
    return result


# ==================================================================================== tests


def nodes_accept_uploads() -> bool:
    """``NodeImplementation`` accepts a ``t_<code>_<short>`` plugin (the v0.19 change to
    ``mapi_spec/workflow_nodes.py``, outside this work package)."""
    from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS, NodeImplementation

    base = NODE_IMPLEMENTATIONS["test.adf"].model_dump(mode="json")
    try:
        NodeImplementation.model_validate({**base, "id": f"test.{NS}_x", "plugin": NS})
    except ValueError:
        return False
    return True


needs_upload_nodes = pytest.mark.skipif(
    not nodes_accept_uploads(),
    reason="needs NodeImplementation to accept t_<code>_<short> plugins (workflow_nodes.py diff)",
)


@pytest.fixture(scope="module")
def mounted(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    tmp = tmp_path_factory.mktemp("overlay")
    sentinel = tmp / "pth-ran"
    pth = f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text('x')\n".encode()
    wheel = build_upload(
        tmp,
        extra_files={"zz_probe.pth": pth, f"{NS}/zz_inner.pth": pth},
        extra_entry_points={
            "mapi.governance_checks": {"acme": f"{NS}.model:MODELS"},
            "console_scripts": {"acme": f"{NS}.model:MODELS"},
        },
    )
    d = extract(wheel, tmp / "overlays" / PVID)
    # an extracted upload that this run does not reference
    other = extract(build_upload(tmp, "beta.whl", namespace=OTHER_NS), tmp / "beta" / OTHER_PVID)
    result = probe((d, NS, PVID), cwd=tmp)
    return {"result": result, "dir": d, "other": other, "sentinel": sentinel, "tmp": tmp}


def test_the_worker_loads_models_steps_and_recipes_with_the_upload_provider(
    mounted: dict[str, Any],
) -> None:
    r = mounted["result"]
    provider = f"upload:{PVID}"
    assert r["models"][f"{NS}.group_mean"] == provider
    assert r["steps"][f"{NS}.winsorize"] == provider
    assert r["plots"][f"{NS}.weekly_total"] == provider
    assert r["model_provider"] == provider and r["step_provider"] == provider


@needs_upload_nodes
def test_the_worker_loads_nodes_and_templates_from_the_upload(mounted: dict[str, Any]) -> None:
    r = mounted["result"]
    assert f"test.{NS}_zero_share" in r["nodes"]
    assert f"test.{NS}_zero_share" in r["node_fns"]
    assert r["templates"][f"{NS}_weekly_check"] == f"upload:{PVID}"


def test_the_overlay_is_appended_after_site_packages(mounted: dict[str, Any]) -> None:
    path = mounted["result"]["sys_path"]
    assert path[-1] == str(mounted["dir"])
    assert any("site-packages" in p for p in path[:-1])


def test_a_pth_file_in_an_overlay_never_runs(mounted: dict[str, Any]) -> None:
    assert not mounted["sentinel"].exists()


def test_an_unreferenced_overlay_is_not_on_sys_path(mounted: dict[str, Any]) -> None:
    r = mounted["result"]
    assert str(mounted["other"]) not in r["sys_path"]
    assert not any(OTHER_NS in m for m in r["modules"])
    assert not any(OTHER_NS in k for k in (*r["models"], *r["steps"], *r["plots"]))


def test_uploaded_governance_checks_are_ignored_with_a_diagnostic(
    mounted: dict[str, Any],
) -> None:
    diags = mounted["result"]["diagnostics"]
    gov = [d for d in diags if d["group"] == "mapi.governance_checks"]
    assert gov and gov[0]["code"] == "load_error"
    assert "not supported" in gov[0]["message"]


def test_other_entry_point_groups_of_an_upload_are_load_errors(mounted: dict[str, Any]) -> None:
    diags = mounted["result"]["diagnostics"]
    foreign = [d for d in diags if d["group"] == "console_scripts"]
    assert foreign and foreign[0]["code"] == "load_error"


def test_the_overlay_files_are_read_only(mounted: dict[str, Any]) -> None:
    d: Path = mounted["dir"]
    assert d.stat().st_mode & 0o777 == 0o555
    for p in d.rglob("*"):
        assert p.stat().st_mode & 0o777 == (0o555 if p.is_dir() else 0o444)


def test_a_renamed_distribution_is_still_an_upload(tmp_path: Path) -> None:
    """The path decides, not the name: a wheel calling itself like an installed built-in
    distribution is still ``upload:<pvid>`` (and does not hide the installed one)."""
    wheel = build_upload(tmp_path, dist="mapi-plugin-mmm")
    d = extract(wheel, tmp_path / "ov" / PVID)
    r = probe((d, NS, PVID), cwd=tmp_path)
    assert r["models"][f"{NS}.group_mean"] == f"upload:{PVID}"
    assert r["steps"][f"{NS}.winsorize"] == f"upload:{PVID}"
    assert any(k.startswith("mmm.") and v is None for k, v in r["models"].items())


def test_an_id_outside_the_claimed_namespace_is_a_load_error(tmp_path: Path) -> None:
    """A wheel whose code uses another namespace than the one the platform assigned."""
    wheel = build_upload(tmp_path, namespace=OTHER_NS)
    d = extract(wheel, tmp_path / "ov" / PVID)
    r = probe((d, NS, PVID), cwd=tmp_path)  # mounted as NS: its OTHER_NS ids are refused
    assert not any(OTHER_NS in k for k in (*r["models"], *r["steps"], *r["plots"]))
    assert not any(OTHER_NS in n for n in r["nodes"])
    assert not any(OTHER_NS in t for t in r["templates"])
    refused = [d for d in r["diagnostics"] if d["code"] == "namespace_reserved"]
    assert {d["group"] for d in refused} >= {"mapi.models", "mapi.steps", "mapi.plots"}
    assert all(NS in d["message"] for d in refused if d["group"] in ("mapi.models",))


def test_without_overlays_an_upload_on_sys_path_is_refused(tmp_path: Path) -> None:
    """Fail closed: code in a ``t_`` namespace that is not mounted as an overlay (here: put
    on the path by hand) is refused by the generic namespace rule."""
    wheel = build_upload(tmp_path)
    d = extract(wheel, tmp_path / "ov" / PVID)
    script = textwrap.dedent(
        f"""
        import json, sys
        sys.path.append({str(d)!r})
        from mapi_sdk.registry import Registry
        reg = Registry(); reg.load_entry_points()
        print("RESULT " + json.dumps({{
            "models": [m.id for m in reg.models()],
            "codes": [x.code for x in reg.diagnostics],
        }}))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script], capture_output=True, text=True, check=False
    )
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT "))
    r = json.loads(line.removeprefix("RESULT "))
    assert not any(m.startswith("t_") for m in r["models"])
    assert "namespace_reserved" in r["codes"]


@pytest.mark.parametrize(
    "value",
    [
        "relative/dir=t_k3vq7m_acme=plv_01jbz3q2x8m9v7k5h4g3f2e1d1",
        "/nonexistent-dir=t_k3vq7m_acme=plv_01jbz3q2x8m9v7k5h4g3f2e1d1",
        "/tmp=acme=plv_01jbz3q2x8m9v7k5h4g3f2e1d1",
        "/tmp=t_k3vq7m_acme=run_1",
        "/tmp",
    ],
)
def test_bad_overlay_arguments_are_refused(value: str) -> None:
    from mapi_runner.worker.__main__ import parse_overlay

    with pytest.raises(ValueError):
        parse_overlay(value)


def test_an_overlay_directory_may_contain_equals(tmp_path: Path) -> None:
    from mapi_runner.worker.__main__ import parse_overlay

    d = tmp_path / "a=b"
    d.mkdir()
    assert parse_overlay(f"{d}={NS}={PVID}") == (d, NS, PVID)


def test_the_worker_refuses_an_invalid_overlay_before_reading_the_channel() -> None:
    from mapi_runner.worker.__main__ import main

    assert main(["--channel-fd", "999", "--overlay", "nope"]) == 3
