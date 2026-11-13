"""The security suite's probes, pushed as an uploaded plugin, on the docker executor (v0.19 WP12).

``make security-suite`` runs the isolation probes of :mod:`mapi_security` in its own hardened
``docker run``. This test takes the *same* probe code and the *same* malicious model
(``security/plugin``: :class:`mapi_security_plugin.EvilModel`, whose ``fit`` runs every
isolation probe), re-packages it as an upload and sends it through the real v0.19 pipeline:
claim, upload ticket, ``plugin_inspect`` run, ``plugin_describe`` run with the kits, a fit run
on the docker executor (supervisor + uid 10001 worker, the executor's own container settings),
review by a second admin. Then it checks that

* every probe ends with the outcome ``make security-suite`` gives it on docker, except the
  differences listed in :data:`EXECUTOR_DELTAS` (the executor's container is not the suite's
  container; each delta says why);
* the run container has the executor's hardening (read-only root, no capabilities beyond the
  supervisor's, ``no-new-privileges``, pids limit, ``/tmp`` ``noexec``);
* a run mounts only the overlays pinned to it: two versions of the upload are mountable, the
  run pins one, and the finalizer accepted the run (the supervisor's verified overlay hashes
  equal the pins) - the run's manifest lists exactly that hash;
* swapping the stored wheel of an *approved* version is refused before any uploaded code runs,
  both ways it can happen: (a) other bytes under the same object generation (storage-side
  substitution) - the supervisor's sha256 check refuses the overlay (``INTEGRITY_FAILURE``)
  and the original bytes make the same run succeed again; (b) a replacement through the
  storage API (a new generation) - the generation-pinned signed GET no longer resolves, the
  run fails before a container starts with ``INTEGRITY_FAILURE`` (class ``integrity``), the
  platform withdraws the version (reason ``security``) and the uploader and tenant admins are
  mailed; a later fit of it is refused even with the same bytes put back.

**Re-packaging is a rename only.** The wheel is built from the files in ``security/plugin/src``
and ``security/src/mapi_security`` (outcomes and probes): the import paths become
``<namespace>`` and ``<namespace>._security``, the ids ``security.evil``/``security.evil_step``
become ``<namespace>.evil``/``<namespace>.evil_step`` and the model version becomes the wheel
version. Two additions only make the package acceptable as an upload, without touching any
probe: an About card on the model (the upload kit requires one; ``fits.data_shape = table``
makes the kit skip its toy-data fit, so the probes run only in the fit run below) and the
``mapi.models`` entry point naming the plugin's existing ``PLUGINS`` tuple. No payload is
added or changed.

The wheel is built as bytes and never imported, installed or extracted here (the
``conftest.py`` sentinel stays armed). Results are PROCESS-level isolation (runc), not a gVisor
claim.

**Needs a running stack B** (``plans/v019-probe.md`` (c): the GCS shim in a container, run
containers in its network namespace, ``MAPI_EXECUTOR=docker``, no dev flag) with a second
tenant admin the uploader did not add: ``MAPI_DEVIDP_USERS=admin@dev.local,analyst@dev.local,
viewer@dev.local,reviewer@dev.local`` and ``MAPI_DEFAULT_TENANT_ADMINS=reviewer@dev.local``.
Environment (defaults are the isolated v0.19 ports)::

    MAPI_PROBES_API_URL=http://127.0.0.1:18000
    MAPI_PROBES_GCS_ROOT=$PWD/.local-v019b/gcs     # the shim root (for the swap)
    MAPI_PROBES_GCS_ENDPOINT=http://127.0.0.1:14443
    MAPI_PROBES_IMAGE=mapi/base-bayes:0.16.0       # the suite's baseline image

    .venv/bin/pytest tests/security/test_probes_as_upload.py -m docker -q

The test skips when Docker, the image or the stack is missing.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import pytest

pytestmark = [pytest.mark.docker, pytest.mark.sandbox]

ROOT: Final = Path(__file__).resolve().parents[2]
PLUGIN_SRC: Final = ROOT / "security" / "plugin" / "src" / "mapi_security_plugin"
SUITE_SRC: Final = ROOT / "security" / "src"
API_URL: Final = os.environ.get("MAPI_PROBES_API_URL", "http://127.0.0.1:18000").rstrip("/")
GCS_ROOT: Final = Path(os.environ.get("MAPI_PROBES_GCS_ROOT", str(ROOT / ".local-v019b" / "gcs")))
GCS_ENDPOINT: Final = os.environ.get("MAPI_PROBES_GCS_ENDPOINT", "http://127.0.0.1:14443")
IMAGE: Final = os.environ.get("MAPI_PROBES_IMAGE", "mapi/base-bayes:0.16.0")
PROJECT: Final = "default/default"
UPLOADER: Final = "admin@dev.local"
REVIEWER: Final = os.environ.get("MAPI_PROBES_REVIEWER", "reviewer@dev.local")
SHORT: Final = "probe"
PLUGINS_BUCKET: Final = os.environ.get("MAPI_PROBES_PLUGINS_BUCKET", "mapi-local-plugins")
RUNS_BUCKET: Final = os.environ.get("MAPI_PROBES_RUNS_BUCKET", "mapi-local-runs")

ISOLATION_PROBES: Final = (
    "metadata_token",
    "http_egress",
    "dns_egress",
    "write_outside_scratch",
    "reuse_foreign_url",
    "read_proc1_environ",
    "read_supervisor_mem",
    "pth_injection",
    "fork_bomb",
    "memory_bomb",
    "disk_bomb",
    "log_flood",
)
"""The probes the malicious model runs (``Category.ISOLATION``; the platform probes are
control-plane checks that never run as tenant code)."""

EXECUTOR_DELTAS: Final[Mapping[str, tuple[str, str, str]]] = {
    "write_outside_scratch": (
        "blocked",
        "allowed",
        "not an escape: the supervisor sets the worker's HOME to the run's scratch work dir "
        "(supervisor.py, worker env), so the probe's target ~/.mapi-security-probe is inside "
        "the /tmp scratch tmpfs; the suite's container has HOME=/tmp/mapi-home on a read-only "
        "root with no /tmp mount",
    ),
    "reuse_foreign_url": (
        "blocked",
        "allowed",
        "looser, declared: run containers share the GCS shim's network namespace "
        "(MAPI_DOCKER_NETWORK=container:<shim>, plans/v019-probe.md (c)), which has the "
        "bridge's internet egress, so storage.googleapis.com:443 is reachable; the suite uses "
        "--network none. The docker executor reports egress_enforced=false on this network "
        "(PROCESS isolation); an internal network + MAPI_DOCKER_NETWORK_ISOLATED enforces it",
    ),
    "read_proc1_environ": (
        "allowed",
        "blocked",
        "stricter: in the executor's container PID 1 is the root supervisor (under tini) and "
        "the worker is uid 10001; in the suite's container the probe process is PID 1 itself",
    ),
    "disk_bomb": (
        "blocked",
        "allowed",
        "by design: the worker's TMPDIR is its scratch on the /tmp tmpfs (size-capped, "
        "noexec, nosuid) and the 16 MiB probe fits that budget; the suite's container has no "
        "writable /tmp at all",
    ),
}
"""``probe -> (suite outcome, upload outcome, why)``: where the docker *executor's* container
differs from the suite's ``docker run`` (recorded 2026-10-23 on stack B; see
``plans/v019-live.md`` › Runtime corpus). Every other probe must match the suite exactly."""

_WORKING: Final = frozenset({"uploaded", "inspecting", "describing"})
_MOUNTABLE: Final = frozenset({"kit_passed", "awaiting_approval", "approved"})
_RUN_TERMINAL: Final = frozenset({"succeeded", "partial", "failed", "cancelled", "rejected"})


# ===================================================================================== skips


def _docker_ok() -> str | None:
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"docker is not available: {exc}"
    if proc.returncode != 0:
        return (
            f'{IMAGE} is missing: make docker-build IMAGES="base-bayes gcs-shim" '
            "(plans/v019-probe.md P8)"
        )
    return None


def _stack_ok() -> str | None:
    try:
        r = httpx.get(f"{API_URL}/healthz", timeout=5)
    except httpx.HTTPError as exc:
        return f"stack B is not running at {API_URL}: {exc}"
    if r.status_code != 200:
        return f"stack B at {API_URL} is not healthy (HTTP {r.status_code})"
    return None


# ================================================================================ the wheel


def _rename(text: str, namespace: str, version: str) -> str:
    """The rename (module docs): import paths, ids and the model version, nothing else."""
    text = re.sub(r"\bmapi_security_plugin\b", namespace, text)
    text = re.sub(r"\b(from|import)(\s+)mapi_security\b", rf"\1\2{namespace}._security", text)
    text = text.replace('"security.evil"', f'"{namespace}.evil"')
    text = text.replace('"security.evil_step"', f'"{namespace}.evil_step"')
    text = text.replace('version: ClassVar[str] = "0.0.0"', f'version: ClassVar[str] = "{version}"')
    return text


_ABOUT_IMPORT: Final = "from mapi_spec.about import About\n"
_ABOUT: Final = """
    # Added for the upload kit (an About card is required); not part of the probes.
    about: ClassVar[About | None] = About(
        title="Security probe model",
        summary_plain="Tries to leave its sandbox and records what the platform allowed. "
        "Use it only to test the sandbox.",
        use_when=["You test the sandbox of uploaded code"],
        avoid_when=["You want a forecast or any real result"],
        data_needs=["Any table with one numeric column"],
        assumptions=["None: the model does not learn from the data"],
        outputs=["The outcome of each probe"],
        fits={"data_shape": ["table"]},
        difficulty="expert",
        typical_runtime="seconds",
        example="It reports that the run could not write outside its scratch folder.",
    )
"""


def _with_about(model_py: str) -> str:
    anchor = "from mapi_spec.capabilities import Capabilities, Representation\n"
    marker = '    description: ClassVar[str | None] = "MALICIOUS test-only model'
    assert anchor in model_py and marker in model_py, "security/plugin model.py changed shape"
    model_py = model_py.replace(anchor, anchor + _ABOUT_IMPORT, 1)
    head, tail = model_py.split(marker, 1)
    line_end = tail.index("\n") + 1
    return head + marker + tail[:line_end] + _ABOUT + tail[line_end:]


def probe_sources(namespace: str, version: str) -> dict[str, bytes]:
    """``member path -> bytes`` of the package: the plugin and the suite's probe modules."""
    files: dict[str, str] = {
        f"{namespace}/__init__.py": (PLUGIN_SRC / "__init__.py").read_text(),
        f"{namespace}/model.py": _with_about((PLUGIN_SRC / "model.py").read_text()),
        f"{namespace}/steps.py": (PLUGIN_SRC / "steps.py").read_text(),
    }
    suite = SUITE_SRC / "mapi_security"
    for rel in (
        "__init__.py",
        "outcomes.py",
        "probes/__init__.py",
        "probes/isolation.py",
        "probes/platform.py",
    ):
        files[f"{namespace}/_security/{rel}"] = (suite / rel).read_text()
    return {k: _rename(v, namespace, version).encode() for k, v in files.items()}


def _record(members: Mapping[str, bytes], record_name: str) -> bytes:
    rows = []
    for name, data in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rows.append(f"{name},sha256={digest},{len(data)}")
    rows.append(f"{record_name},,")
    return ("\n".join(rows) + "\n").encode()


def build_probe_wheel(namespace: str, version: str) -> tuple[str, bytes]:
    """``(file name, wheel bytes)``: a pure-Python ``py3-none-any`` wheel of the probes."""
    dist = namespace  # the upload distribution name is the namespace (t_<code>_<short>)
    di = f"{dist}-{version}.dist-info"
    members = probe_sources(namespace, version)
    members[f"{di}/METADATA"] = (
        "Metadata-Version: 2.3\n"
        f"Name: {dist.replace('_', '-')}\n"
        f"Version: {version}\n"
        "Summary: The security suite's probes as an uploaded plugin (test only)\n"
        "Requires-Python: >=3.12\n"
        "Requires-Dist: mapi-sdk>=0.16\n"
        "Requires-Dist: mapi-spec>=0.16\n"
        "Requires-Dist: mapi-plots>=0.16\n"
        "License-Expression: AGPL-3.0-only\n"
    ).encode()
    members[f"{di}/WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: mapi-probes-as-upload\nRoot-Is-Purelib: true\n"
        b"Tag: py3-none-any\n"
    )
    members[f"{di}/entry_points.txt"] = f"[mapi.models]\nevil = {namespace}:PLUGINS\n".encode()
    members[f"{di}/RECORD"] = _record(members, f"{di}/RECORD")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return f"{dist}-{version}-py3-none-any.whl", buf.getvalue()


# ======================================================================================= API


class Problem(AssertionError):
    pass


def _ok(r: httpx.Response, *codes: int) -> Any:
    if r.status_code not in codes:
        raise Problem(f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:800]}")
    return r.json() if r.content else None


def bearer_client(email: str) -> httpx.Client:
    from mapi_client.oidc import dev_idp_login

    token = dev_idp_login(API_URL, email)
    return httpx.Client(
        base_url=API_URL,
        headers={"Authorization": f"Bearer {token}", "X-Mapi-Project": PROJECT},
        timeout=60,
    )


def session_client(email: str) -> httpx.Client:
    """A web-app session (cookie + CSRF) through ``/auth/login`` and the dev IdP: approving
    uploaded code needs a fresh browser sign-in, never a token."""
    c = httpx.Client(base_url=API_URL, timeout=60, follow_redirects=False)
    r = c.get("/auth/login", params={"return_to": "/"})
    assert r.status_code == 302, r.text
    auth = urlsplit(r.headers["location"])
    q = dict(parse_qsl(auth.query))
    q["login_hint"] = email
    r = c.get(urlunsplit(auth._replace(query=urlencode(q))))
    assert r.status_code == 302, r.text
    cb = urlsplit(r.headers["location"])
    r = c.get(f"{cb.path}?{cb.query}")
    assert r.status_code == 302, f"callback {r.status_code}: {r.text[:500]}"
    r = c.get("/auth/session")
    csrf = r.headers.get("X-CSRF-Token") or c.cookies.get("mapi_csrf")
    assert csrf, "no CSRF token after sign-in"
    c.headers.update({"X-CSRF-Token": csrf, "X-Mapi-Project": PROJECT})
    return c


def claim(api: httpx.Client) -> dict[str, Any]:
    r = api.post("/v1/plugins", json={"short_name": SHORT, "title": "Security probes"})
    if r.status_code == 201:
        return dict(r.json())
    assert r.json().get("code") == "PLUGIN_NAMESPACE_TAKEN", r.text
    for p in _ok(api.get("/v1/plugins", params={"limit": 200}), 200)["items"]:
        if p["short_name"] == SHORT:
            return dict(p)
    raise Problem("the short name is taken by a plugin this account cannot see")


def push(api: httpx.Client, plugin: dict[str, Any], version: str) -> dict[str, Any]:
    """Upload a probe wheel and wait for its checks; the final version document."""
    filename, wheel = build_probe_wheel(plugin["namespace"], version)
    pid = plugin["id"]
    ticket = _ok(
        api.post(f"/v1/plugins/{pid}/uploads", json={"filename": filename, "size": len(wheel)}),
        201,
    )
    post = ticket["post"]
    fields = dict(post["fields"])
    r = httpx.post(
        post["url"],
        data=fields,
        files={"file": (filename, wheel, "application/octet-stream")},
        timeout=60,
    )
    assert r.status_code in (200, 201, 204), f"storage refused the wheel: {r.status_code}"
    v = _ok(
        api.post(
            f"/v1/plugins/{pid}/uploads/{ticket['upload_id']}:complete",
            json={"project": PROJECT.split("/")[1]},
        ),
        202,
    )
    deadline = time.monotonic() + 600
    while v["state"] in _WORKING:
        assert time.monotonic() < deadline, f"checks still running: {v['state']}"
        time.sleep(2)
        v = _ok(api.get(f"/v1/plugins/{pid}/versions/{v['id']}"), 200)
    v["_sha256"] = "sha256:" + hashlib.sha256(wheel).hexdigest()
    v["_wheel"] = wheel
    return dict(v)


def ensure_dataset(api: httpx.Client) -> str:
    """A tiny weekly series (``date`` x ``revenue``); its dataset ref."""
    import numpy as np
    import pandas as pd
    import xarray as xr
    from mapi_sdk.wire import write_zarr_zip

    name = "probes-as-upload"
    found = [
        d
        for d in _ok(api.get("/v1/datasets", params={"limit": 200}), 200)["items"]
        if d["name"] == name
    ]
    ds = found[0] if found else _ok(api.post("/v1/datasets", json={"name": name}), 201)
    dates = pd.date_range("2024-01-07", periods=26, freq="W-SUN")
    data = xr.Dataset({"revenue": ("date", np.linspace(100.0, 150.0, 26))}, coords={"date": dates})
    payload = write_zarr_zip(data)
    assert payload is not None
    up = _ok(
        api.post(
            f"/v1/datasets/{ds['id']}/uploads",
            json={
                "filename": "probe.zip",
                "content_type": "application/zip",
                "size_bytes": len(payload),
            },
        ),
        201,
    )
    put = up["upload"]
    r = httpx.request(put["method"], put["url"], content=payload, headers=put.get("headers", {}))
    assert r.status_code in (200, 201), r.text
    ver = _ok(
        api.post(
            f"/v1/datasets/{ds['id']}/versions",
            json={"upload_id": up["id"], "mapping": {"format": "zarr_zip"}, "message": "probes"},
        ),
        202,
    )
    deadline = time.monotonic() + 300
    while ver["status"] == "ingesting":
        assert time.monotonic() < deadline, "ingest still running"
        time.sleep(2)
        ver = _ok(api.get(f"/v1/datasets/{ds['id']}/versions/{ver['version']}"), 200)
    assert ver["status"] != "failed", ver
    return str(ver["ref"])


def fit(
    api: httpx.Client, dataset_ref: str, model_ref: str, *, refusable: bool = False
) -> dict[str, Any]:
    """Create a spec that fits ``model_ref`` and run it to the end; the run. With
    ``refusable``, a refusal of the spec or of the run is returned as ``{"refused": status,
    "problem": body}`` instead of raising."""
    yaml = (
        "apiVersion: mapi/v1\n"
        "kind: ModelSpec\n"
        f"metadata: {{name: probes-as-upload-{int(time.time() * 1000)}, "
        f"project: {PROJECT.split('/')[1]}}}\n"
        "data:\n"
        f"  dataset: {dataset_ref}\n"
        "  bind:\n"
        "    dims: {t: date}\n"
        "    vars: {y: revenue}\n"
        "model:\n"
        f"  use: {model_ref}\n"
        "  fit: {mode: train_only, seed: 7}\n"
    )
    made = api.post("/v1/specs", json={"yaml": yaml, "message": "probes as upload"})
    if refusable and made.status_code >= 400:
        return {"refused": made.status_code, "problem": made.json()}
    spec = _ok(made, 201)
    started = api.post(
        "/v1/runs",
        json={"kind": "fit", "spec_id": spec["id"], "revision": spec["latest_revision"]},
    )
    if refusable and started.status_code >= 400:
        return {"refused": started.status_code, "problem": started.json()}
    run = _ok(started, 201)
    deadline = time.monotonic() + 900
    while run["state"] not in _RUN_TERMINAL:
        assert time.monotonic() < deadline, f"run {run['id']} still {run['state']}"
        time.sleep(2)
        run = _ok(api.get(f"/v1/runs/{run['id']}"), 200)
    return dict(run)


def run_logs(api: httpx.Client, run_id: str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 200, "include_user": "true"}
        if cursor:
            params["cursor"] = cursor
        page = _ok(api.get(f"/v1/runs/{run_id}/logs", params=params), 200)
        lines += page["items"]
        cursor = page.get("next_cursor")
        if not cursor:
            return lines


_OUTCOMES: Final = re.compile(r"probe outcomes: (\{[^{}]*\})")


def probe_outcomes(lines: list[dict[str, Any]]) -> dict[str, str]:
    """The model's ``ctx.info`` line, parsed as data (``ast.literal_eval`` of a dict of str;
    untrusted text: only probe ids and outcome words are accepted)."""
    import ast

    for line in lines:
        m = _OUTCOMES.search(str(line.get("line", "")))
        if m is None:
            continue
        raw = ast.literal_eval(m.group(1))
        assert isinstance(raw, dict)
        out = {str(k): str(v) for k, v in raw.items()}
        assert set(out) <= set(ISOLATION_PROBES), out
        return out
    raise Problem("the fit run logged no probe outcomes")


def containers_of(run_id: str) -> list[dict[str, Any]]:
    ids = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=dev.mapi.run_id={run_id}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    if not ids:
        return []
    out = subprocess.run(["docker", "inspect", *ids], capture_output=True, text=True, check=True)
    return list(json.loads(out.stdout))


def _store() -> Any:
    from mapi_gcs_shim.client import ShimObjectStore

    return ShimObjectStore.from_root(GCS_ROOT, endpoint=GCS_ENDPOINT)


async def _replace(key: str, data: bytes) -> bytes:
    """Replace the stored wheel through the storage API (a new object generation, as anyone
    with write access to the bucket would); the old bytes."""
    async with _store() as store:
        old = await store.get(PLUGINS_BUCKET, key)
        await store.put(PLUGINS_BUCKET, key, data, content_type="application/octet-stream")
        return bytes(old)


def _live_blob(key: str) -> Path:
    """The shim's file for the live generation of ``key`` (``objects/{h[:2]}/{h}.{gen}.bin``,
    next to its ``{h}.json`` sidecar)."""
    for sidecar in (GCS_ROOT / "buckets" / PLUGINS_BUCKET / "objects").glob("*/*.json"):
        meta = json.loads(sidecar.read_text())
        if meta.get("name") == key:
            blob = sidecar.with_name(f"{sidecar.stem}.{meta['generation']}.bin")
            assert blob.is_file(), blob
            return blob
    raise Problem(f"no stored object {key!r} under {GCS_ROOT}")


def _tamper_in_place(key: str) -> tuple[Path, bytes]:
    """Change the stored wheel's bytes *without* a new generation (storage-side substitution
    that generation pinning cannot see): one byte in the middle is flipped, the size stays.
    ``(file, original bytes)``."""
    blob = _live_blob(key)
    original = blob.read_bytes()
    tampered = bytearray(original)
    tampered[len(tampered) // 2] ^= 0xFF
    blob.write_bytes(bytes(tampered))
    return blob, original


async def _run_manifest(run_id: str) -> dict[str, Any] | None:
    async with _store() as store:
        page = await store.list(RUNS_BUCKET, prefix=f"t/default/runs/{run_id}/")
        names = [o.name for o in page.items if o.name.endswith("/manifest.json")]
        if not names:
            return None
        got = await store.get(RUNS_BUCKET, names[0])
        raw = got if isinstance(got, bytes | bytearray) else got.data
        return dict(json.loads(raw))


# ================================================================================ baseline


def suite_baseline(tmp: Path) -> dict[str, str]:
    """``make security-suite`` on docker (the direct command of plans/v019-probe.md (d))."""
    out = tmp / "suite.json"
    env = {**os.environ, "PYTHONPATH": f"{SUITE_SRC}:{ROOT / 'security' / 'plugin' / 'src'}"}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mapi_security",
            "run",
            "--executor",
            "docker",
            "--image",
            IMAGE,
            "--json",
            str(out),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert out.is_file(), proc.stdout[-2000:] + proc.stderr[-2000:]
    rows = json.loads(out.read_text())["rows"]
    return {r["probe_id"]: r["outcome"] for r in rows if r["probe_id"] in ISOLATION_PROBES}


# ================================================================================ scenario


@dataclass
class Scenario:
    plugin: dict[str, Any]
    v1: dict[str, Any]
    v2: dict[str, Any]
    dataset: str
    baseline: dict[str, str]
    run: dict[str, Any] = field(default_factory=dict)
    outcomes: dict[str, str] = field(default_factory=dict)
    manifest: dict[str, Any] | None = None
    containers: list[dict[str, Any]] = field(default_factory=list)
    approved: dict[str, Any] = field(default_factory=dict)
    tamper_run: dict[str, Any] = field(default_factory=dict)
    tamper_logs: list[dict[str, Any]] = field(default_factory=list)
    restored_run: dict[str, Any] = field(default_factory=dict)
    replace_run: dict[str, Any] = field(default_factory=dict)
    replace_logs: list[dict[str, Any]] = field(default_factory=list)
    replace_containers: list[dict[str, Any]] = field(default_factory=list)
    after_replace_run: dict[str, Any] = field(default_factory=dict)
    replaced_version: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)


def _model_ref(plugin: dict[str, Any], v: dict[str, Any]) -> str:
    return f"{plugin['namespace']}.evil@{v['version']}"


@pytest.fixture(scope="module")
def scenario(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Scenario]:
    for why in (_docker_ok(), _stack_ok()):
        if why:
            pytest.skip(why)
    tmp = tmp_path_factory.mktemp("probes")
    baseline = suite_baseline(tmp)
    api = bearer_client(UPLOADER)
    plugin = claim(api)
    base = int(time.time())
    v1 = push(api, plugin, f"0.1.{base}")
    v2 = push(api, plugin, f"0.1.{base + 1}")
    for v in (v1, v2):
        assert v["state"] in _MOUNTABLE, (v["state"], v.get("issues"), v.get("kit"))
    sc = Scenario(plugin=plugin, v1=v1, v2=v2, dataset=ensure_dataset(api), baseline=baseline)

    # 1. the probes, as uploaded code, in a fit on the docker executor (v2 pinned; v1 exists)
    sc.run = fit(api, sc.dataset, _model_ref(plugin, v2))
    assert sc.run["state"] == "succeeded", sc.run
    sc.outcomes = probe_outcomes(run_logs(api, sc.run["id"]))
    sc.manifest = asyncio.run(_run_manifest(sc.run["id"]))
    sc.containers = containers_of(sc.run["id"])

    # 2. approve v2 (second admin, fresh web sign-in), then swap its stored wheel
    rev = session_client(REVIEWER)
    pid = plugin["id"]
    asked = _ok(api.post(f"/v1/plugins/{pid}/versions/{v2['id']}:request-approval", json={}), 200)
    assert asked["state"] == "awaiting_approval", asked
    sc.approved = _ok(
        rev.post(
            f"/v1/plugins/{pid}/versions/{v2['id']}:approve",
            json={
                key: asked[key]
                for key in ("overlay_sha256", "describe_digest", "base_image_digest")
            },
        ),
        200,
    )
    assert sc.approved["state"] == "approved", sc.approved
    from mapi_spec.storage import plugin_key

    key = plugin_key("default", pid, v2["id"])

    # 2a. same generation, other bytes: only the supervisor's sha256 check can catch it
    blob, original = _tamper_in_place(key)
    try:
        assert "sha256:" + hashlib.sha256(original).hexdigest() == v2["_sha256"]
        sc.tamper_run = fit(api, sc.dataset, _model_ref(plugin, v2))
        sc.tamper_logs = run_logs(api, sc.tamper_run["id"])
    finally:
        blob.write_bytes(original)
    sc.restored_run = fit(api, sc.dataset, _model_ref(plugin, v2))

    # 2b. replaced through the storage API (new generation): another valid wheel (v1's)
    old = asyncio.run(_replace(key, v1["_wheel"]))
    assert old == original
    try:
        sc.replace_run = fit(api, sc.dataset, _model_ref(plugin, v2))
        sc.replace_logs = run_logs(api, sc.replace_run["id"])
        sc.replace_containers = containers_of(sc.replace_run["id"])
    finally:
        asyncio.run(_replace(key, original))  # same bytes again, but yet another generation
    sc.replaced_version = _ok(api.get(f"/v1/plugins/{pid}/versions/{v2['id']}"), 200)
    sc.after_replace_run = fit(api, sc.dataset, _model_ref(plugin, v2), refusable=True)

    def brief(run: dict[str, Any]) -> dict[str, Any]:
        return {k: run.get(k) for k in ("id", "state", "failure_class", "problem")}

    sc.report = {
        "plugin": {k: plugin[k] for k in ("id", "namespace")},
        "versions": {
            v["version"]: {"id": v["id"], "state": v["state"], "sha256": v["_sha256"]}
            for v in (v1, v2)
        },
        "baseline": baseline,
        "upload": sc.outcomes,
        "run": sc.run["id"],
        "manifest_overlay_hashes": (sc.manifest or {}).get("overlay_hashes"),
        "approved": sc.approved["state"],
        "tamper_run": brief(sc.tamper_run),
        "restored_run": brief(sc.restored_run),
        "replace_run": brief(sc.replace_run),
        "replace_containers": len(sc.replace_containers),
        "after_replace_run": brief(sc.after_replace_run)
        | {"refused": sc.after_replace_run.get("refused")},
        "replaced_version": {
            k: sc.replaced_version.get(k) for k in ("state", "yank_reason", "yank_note")
        },
    }
    (tmp / "report.json").write_text(json.dumps(sc.report, indent=2, default=str))
    sys.stderr.write("\nprobes-as-upload report: " + json.dumps(sc.report, default=str) + "\n")
    yield sc
    api.close()
    rev.close()
    runs = [
        r.get("id")
        for r in (sc.run, sc.tamper_run, sc.restored_run, sc.replace_run, sc.after_replace_run)
    ]
    for run_id in filter(None, runs):
        ids = [c["Id"] for c in containers_of(str(run_id))]
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, check=False)


# =================================================================================== tests


def test_upload_reaches_kit_passed_with_its_checks(scenario: Scenario) -> None:
    for v in (scenario.v1, scenario.v2):
        assert v["inspection"] is not None
        assert v["kit"] is not None
        failed = [c for c in v["kit"]["checks"] if c["status"] == "fail"]
        assert failed == [], failed
        assert v["overlay_sha256"] == v["_sha256"]  # the server hashed the bytes we sent


def test_every_probe_ends_as_in_the_security_suite(scenario: Scenario) -> None:
    assert set(scenario.outcomes) == set(ISOLATION_PROBES), scenario.outcomes
    assert set(scenario.baseline) == set(ISOLATION_PROBES), scenario.baseline
    assert "error" not in scenario.outcomes.values(), scenario.outcomes
    for probe in ISOLATION_PROBES:
        suite, upload = scenario.baseline[probe], scenario.outcomes[probe]
        if probe in EXECUTOR_DELTAS:
            want_suite, want_upload, why = EXECUTOR_DELTAS[probe]
            assert (suite, upload) == (want_suite, want_upload), f"{probe}: {why}"
        else:
            assert upload == suite, f"{probe}: suite {suite}, as an upload {upload}"


def test_run_container_has_the_executor_hardening(scenario: Scenario) -> None:
    assert scenario.containers, "no run container labelled with the run id"
    for c in scenario.containers:
        hc = c["HostConfig"]
        assert hc["ReadonlyRootfs"] is True
        assert "ALL" in (hc.get("CapDrop") or [])
        assert any("no-new-privileges" in s for s in hc.get("SecurityOpt") or [])
        assert hc.get("PidsLimit") and hc["PidsLimit"] > 0
        assert hc["NetworkMode"].startswith("container:") or hc["NetworkMode"] == "none"
        tmp_opts = (hc.get("Tmpfs") or {}).get("/tmp", "")
        assert "noexec" in tmp_opts and "nosuid" in tmp_opts, tmp_opts


def test_run_mounts_only_its_pinned_overlay(scenario: Scenario) -> None:
    sc = scenario
    assert sc.v1["state"] in _MOUNTABLE  # a second mountable version of the same plugin exists
    assert sc.run["state"] == "succeeded"  # the finalizer checked overlay_hashes == pins
    assert sc.manifest is not None, "no manifest.json for the run"
    assert sc.manifest.get("overlay_hashes") == [sc.v2["_sha256"]], sc.manifest


def _ran_upload_code(lines: list[dict[str, Any]]) -> bool:
    return any(_OUTCOMES.search(str(line.get("line", ""))) for line in lines)


def test_overlay_swap_after_approval_is_an_integrity_failure(scenario: Scenario) -> None:
    """Same generation, other bytes (the T19.14 case): the supervisor refuses the overlay."""
    sc = scenario
    assert sc.approved["state"] == "approved"
    assert sc.tamper_run["state"] == "failed", sc.tamper_run
    problem = sc.tamper_run.get("problem") or {}
    assert problem.get("code") == "INTEGRITY_FAILURE", problem
    # F3: the supervisor's exit 13 is class integrity, and the detail says what happened
    assert sc.tamper_run["failure_class"] == "integrity", sc.tamper_run
    assert "did not match the hash (sha256)" in str(problem.get("detail")), problem
    assert "CRC32C" not in str(problem.get("detail")), problem
    assert not _ran_upload_code(sc.tamper_logs)  # refused before any uploaded code ran
    # operator-side evidence: the supervisor's own reason, in the run container's log
    supervisor_log = "\n".join(
        subprocess.run(
            ["docker", "logs", c["Id"]], capture_output=True, text=True, check=False
        ).stderr
        for c in containers_of(sc.tamper_run["id"])
    )
    assert "the sha256 of the uploaded wheel does not match the pinned version" in supervisor_log
    # the original bytes back (same generation): the same pinned run succeeds again
    assert sc.restored_run["state"] == "succeeded", sc.restored_run


def test_replaced_object_fails_closed_before_dispatch(scenario: Scenario) -> None:
    """A new generation of the stored wheel: the pinned generation no longer exists, the
    run fails before a container starts, and putting the same bytes back does not revive
    the version (the inspected generation is gone, and the version was withdrawn; a new
    push is needed)."""
    sc = scenario
    assert sc.replace_run["state"] == "failed", sc.replace_run
    assert sc.replace_containers == []
    assert not _ran_upload_code(sc.replace_logs)
    after = sc.after_replace_run
    assert after.get("refused") in (409, 422) or after.get("state") == "failed", after


def test_replaced_object_is_reported_as_an_integrity_failure(scenario: Scenario) -> None:
    """F2: tampering, not an outage. The run fails ``INTEGRITY_FAILURE`` with class
    ``integrity`` and a plain reason, and the platform withdraws the version for safety
    (``security``; the uploader and the tenant admins are mailed, without uploaded text)."""
    run = scenario.replace_run
    problem = run.get("problem") or {}
    assert problem.get("code") == "INTEGRITY_FAILURE", problem
    assert run.get("failure_class") == "integrity", run
    assert "replaced or removed" in str(problem.get("detail")), problem
    version = scenario.replaced_version
    assert version.get("state") == "yanked", version
    assert version.get("yank_reason") == "security", version
