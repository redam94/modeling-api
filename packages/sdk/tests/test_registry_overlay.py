"""The SDK registry's overlay rule (v0.19, D17, WP0 probe P2).

An overlay is a directory the supervisor extracted an uploaded wheel into. The registry
reads its entry points from that directory only and decides "uploaded" by **path**:
everything it exports gets ``provider="upload:<pvid>"`` and must use the namespace the
platform assigned, whatever the distribution is called.

The overlays here are written by the test (a tiny step re-using a toy step class) and loaded
only in a fresh ``python -I`` child, never in this process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mapi_sdk.registry import (
    UploadOverlay,
    as_overlays,
    entry_point_origin,
    overlay_of_distribution,
    plugin_entry_points,
)

NS = "t_k3vq7m_acme"
PVID = "plv_01jbz3q2x8m9v7k5h4g3f2e1d1"


def make_overlay(
    root: Path,
    *,
    dist: str = "t-k3vq7m-acme",
    module: str = NS,
    step_id: str = f"{NS}.log1p",
    groups: Mapping[str, Mapping[str, str]] | None = None,
) -> Path:
    """An extracted wheel: ``<module>/__init__.py`` exporting one toy step under ``step_id``
    and a ``.dist-info`` naming ``dist`` with ``groups`` as entry points."""
    root.mkdir(parents=True)
    pkg = root / module
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        textwrap.dedent(
            f"""
            from mapi_sdk.testing.toy import Log1pStep

            class UploadedStep(Log1pStep):
                id = {step_id!r}

            STEPS = [UploadedStep]
            """
        )
    )
    info = root / f"{module}-0.1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 0.1.0\n")
    eps = groups if groups is not None else {"mapi.steps": {"acme": f"{module}:STEPS"}}
    text = "".join(
        f"[{g}]\n" + "".join(f"{k} = {v}\n" for k, v in t.items()) + "\n" for g, t in eps.items()
    )
    (info / "entry_points.txt").write_text(text)
    return root


_CHILD = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    overlays = [tuple(o) for o in json.loads(sys.argv[1])]
    for d, _ns, _pvid in overlays:
        sys.path.append(d)
    from mapi_sdk.registry import Registry
    reg = Registry(overlays=[(Path(d), ns, pvid) for d, ns, pvid in overlays])
    reg.load_entry_points()
    out = {
        "steps": {s.id: reg.provider_of("step", s.id) for s in reg.steps()},
        "origins": {s.id: reg.origin_of("step", s.id, s.version) for s in reg.steps()},
        "catalog": {s.id: s.provider for s in reg.catalog().steps},
        "diagnostics": [d.model_dump(mode="json") for d in reg.diagnostics],
        "of_upload": [
            d.model_dump(mode="json") for o in reg.overlays for d in reg.diagnostics_of(o.provider)
        ],
    }
    print("RESULT " + json.dumps(out))
    """
)


def load_in_child(*overlays: tuple[Path, str, str]) -> dict[str, Any]:
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    arg = json.dumps([[str(d), ns, pvid] for d, ns, pvid in overlays])
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _CHILD, arg],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, proc.stderr[-4000:]
    result: dict[str, Any] = json.loads(lines[-1].removeprefix("RESULT "))
    return result


# ================================================================================ pure rules


def test_overlays_need_an_uploaded_namespace_and_an_absolute_dir(tmp_path: Path) -> None:
    assert as_overlays([(tmp_path, NS, PVID)]) == (UploadOverlay(tmp_path, NS, PVID),)
    with pytest.raises(ValueError, match="namespace"):
        as_overlays([(tmp_path, "acme", PVID)])
    with pytest.raises(ValueError, match="absolute"):
        as_overlays([(Path("rel"), NS, PVID)])
    with pytest.raises(ValueError, match="one version"):
        as_overlays([(tmp_path, NS, PVID), (tmp_path / "b", NS, "plv_other")])


def test_the_path_decides_not_the_name(tmp_path: Path) -> None:
    ov = UploadOverlay(tmp_path / "ov", NS, PVID)
    inside = SimpleNamespace(name="mapi-steps", locate_file=lambda _p: tmp_path / "ov")
    outside = SimpleNamespace(name="t-k3vq7m-acme", locate_file=lambda _p: tmp_path / "site")
    assert overlay_of_distribution(inside, [ov]) is ov
    assert overlay_of_distribution(outside, [ov]) is None
    assert overlay_of_distribution(SimpleNamespace(name="x"), [ov]) is None  # no location
    assert overlay_of_distribution(inside, []) is None


def test_origins_name_the_upload_or_the_normalized_distribution(tmp_path: Path) -> None:
    ov = UploadOverlay(tmp_path, NS, PVID)
    assert entry_point_origin("Mapi_Steps", None) == "mapi-steps"
    assert entry_point_origin("mapi-steps", ov) == f"upload:{PVID}"
    assert entry_point_origin(None, None) is None


def test_overlay_entry_points_are_read_from_their_directory(tmp_path: Path) -> None:
    """Reading entry points imports nothing: only ``entry_points.txt`` is parsed."""
    d = make_overlay(tmp_path / "ov", dist="mapi-steps")  # named like an installed dist
    ov = UploadOverlay(d, NS, PVID)
    found = [f for f in plugin_entry_points("mapi.steps", [ov]) if f.overlay is not None]
    assert [(f.entry_point.value, f.distribution, f.origin) for f in found] == [
        (f"{NS}:STEPS", "mapi-steps", f"upload:{PVID}")
    ]
    assert NS not in sys.modules
    # the installed distribution of the same name is still listed, as installed code
    installed = [f for f in plugin_entry_points("mapi.steps", [ov]) if f.overlay is None]
    assert any(f.distribution and f.distribution.lower() == "mapi-steps" for f in installed)


# ======================================================================== loading (a child)


def test_an_overlay_step_gets_the_upload_provider(tmp_path: Path) -> None:
    d = make_overlay(tmp_path / "ov")
    r = load_in_child((d, NS, PVID))
    assert r["steps"][f"{NS}.log1p"] == f"upload:{PVID}"
    assert r["origins"][f"{NS}.log1p"] == f"upload:{PVID}"
    assert r["catalog"][f"{NS}.log1p"] == f"upload:{PVID}"
    assert r["of_upload"] == []
    # built-ins are untouched: no provider, origin = their distribution
    assert r["steps"]["core.calendar"] is None


def test_a_renamed_distribution_is_still_an_upload(tmp_path: Path) -> None:
    d = make_overlay(tmp_path / "ov", dist="mapi-steps")
    r = load_in_child((d, NS, PVID))
    assert r["steps"][f"{NS}.log1p"] == f"upload:{PVID}"
    assert r["steps"]["core.calendar"] is None  # the real mapi-steps is not hidden


@pytest.mark.parametrize("step_id", ["acme.log1p", "core.zz_uploaded", "t_k3vq7m_beta.log1p"])
def test_an_id_outside_the_claimed_namespace_is_refused(tmp_path: Path, step_id: str) -> None:
    d = make_overlay(tmp_path / "ov", step_id=step_id)
    r = load_in_child((d, NS, PVID))
    assert step_id not in r["steps"]
    refused = [x for x in r["of_upload"] if x["id"] == step_id]
    assert refused and refused[0]["code"] == "namespace_reserved"
    assert NS in refused[0]["message"]


def test_other_groups_of_an_overlay_are_load_errors(tmp_path: Path) -> None:
    d = make_overlay(
        tmp_path / "ov",
        groups={
            "mapi.steps": {"acme": f"{NS}:STEPS"},
            "console_scripts": {"acme": f"{NS}:STEPS"},
            "mapi.governance_checks": {"acme": f"{NS}:STEPS"},
        },
    )
    r = load_in_child((d, NS, PVID))
    foreign = [x for x in r["of_upload"] if x["group"] == "console_scripts"]
    assert foreign and foreign[0]["code"] == "load_error"
    # governance checks are reported by their own loader (mapi_workflows), not here
    assert not [x for x in r["of_upload"] if x["group"] == "mapi.governance_checks"]
    assert r["steps"][f"{NS}.log1p"] == f"upload:{PVID}"


def test_an_upload_not_mounted_as_an_overlay_is_refused(tmp_path: Path) -> None:
    """On ``sys.path`` but not declared as an overlay: the generic rule refuses ``t_``."""
    d = make_overlay(tmp_path / "ov")
    script = textwrap.dedent(
        f"""
        import json, sys
        sys.path.append({str(d)!r})
        from mapi_sdk.registry import Registry
        reg = Registry(); reg.load_entry_points()
        print("RESULT " + json.dumps({{
            "ids": [s.id for s in reg.steps()],
            "codes": [x.code for x in reg.diagnostics if x.id == {NS + ".log1p"!r}],
        }}))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script], capture_output=True, text=True, check=False
    )
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT "))
    r = json.loads(line.removeprefix("RESULT "))
    assert f"{NS}.log1p" not in r["ids"]
    assert r["codes"] == ["namespace_reserved"]
