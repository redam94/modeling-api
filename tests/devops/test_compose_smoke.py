"""Boot the compose control plane and check that ``api`` and ``worker`` really start.

Brings up ``postgres``, ``gcs-shim``, ``migrate``, ``api`` and ``worker`` from the built
``mapi/*`` images (``make docker-build``) under a throw-away project name, then asks each
container what it resolved. Marked ``docker``: the default suite never runs it, and it skips
itself when the daemon or the images are missing. A small override resets the published
ports, so it runs next to ``make dev`` (or anything else holding 4443/8000).

Run it with ``uv run pytest tests/devops/test_compose_smoke.py -m docker``.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from .conftest import ROOT

pytestmark = [pytest.mark.docker, pytest.mark.slow]

Compose = Callable[..., subprocess.CompletedProcess[str]]

VERSION = os.environ.get("MAPI_VERSION", "0.4.0")
IMAGES = (f"mapi/server:{VERSION}", f"mapi/worker:{VERSION}", f"mapi/gcs-shim:{VERSION}")

#: Printed by ``python -I -c`` inside a container: what the control plane resolved at start-up.
PROBE = """
import json
from mapi_server.app import load_signer
from mapi_server.settings import Settings
s = Settings()
print(json.dumps({"digest": s.resolve_image_digest(), "key_id": load_signer(s).key_id}))
"""

#: Run in ``worker``: sign an example bundle manifest the way its finalizer does.
SIGN = """
from mapi_server.app import load_signer
from mapi_server.settings import Settings
from mapi_spec.run import BundleManifest
example = BundleManifest.model_json_schema()["examples"][0]
example.pop("signature")
manifest = load_signer(Settings()).sign(BundleManifest.model_validate(example))
print(manifest.model_dump_json())
"""

#: Run in ``api`` with the manifest on stdin: what ``verified_manifest`` checks.
VERIFY = """
import json, sys
from mapi_server.app import load_signer
from mapi_server.settings import Settings
from mapi_spec.run import BundleManifest
manifest = BundleManifest.model_validate_json(sys.stdin.read())
print(json.dumps({"verified": load_signer(Settings()).verify(manifest)}))
"""


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Compose]:
    if shutil.which("docker") is None or _docker("info").returncode != 0:
        pytest.skip("the Docker daemon is not available")
    missing = [i for i in IMAGES if _docker("image", "inspect", i).returncode != 0]
    if missing:
        pytest.skip(f"build the images first (make docker-build): missing {missing}")
    override = tmp_path_factory.mktemp("compose") / "compose.smoke.yml"
    override.write_text("services:\n  gcs-shim:\n    ports: !reset []\n", encoding="utf-8")
    project = f"mapi-smoke-{secrets.token_hex(4)}"
    env = {**os.environ, "POSTGRES_PASSWORD": secrets.token_urlsafe(18), "MAPI_VERSION": VERSION}
    base = [
        "docker",
        "compose",
        "--project-directory",
        str(ROOT),
        "-f",
        str(ROOT / "docker-compose.yml"),
        "-f",
        str(override),
        "-p",
        project,
    ]

    def compose(
        *args: str, check: bool = True, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*base, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
            check=check,
            input=input,
        )

    try:
        up = compose("up", "-d", "--wait", "--no-build", "api", "worker", check=False)
        if up.returncode != 0:
            logs = compose("logs", "--no-color", "--tail", "80", check=False).stdout
            pytest.fail(f"compose up failed:\n{up.stderr}\n{logs}")
        yield compose
    finally:
        compose("down", "--volumes", "--remove-orphans", check=False)


def probe(
    compose: Compose, service: str, code: str = PROBE, input: str | None = None
) -> dict[str, Any]:
    out = compose("exec", "-T", service, "python", "-I", "-c", code, input=input).stdout
    data = json.loads(out.strip().splitlines()[-1])
    assert isinstance(data, dict)
    return data


def test_api_and_worker_start_and_agree_on_the_image_digest(stack: Compose) -> None:
    running = stack("ps", "--status", "running", "--services").stdout.split()
    assert {"api", "worker"} <= set(running)
    health = stack(
        "exec",
        "-T",
        "api",
        "python",
        "-I",
        "/opt/mapi/bin/healthcheck.py",
        "http",
        "http://127.0.0.1:8000/healthz",
        check=False,
    )
    assert health.returncode == 0, health.stdout + health.stderr
    api, worker = probe(stack, "api"), probe(stack, "worker")
    assert api["digest"] == worker["digest"]
    assert api["digest"].startswith("sha256:")


def test_api_verifies_what_the_worker_signs_across_restarts(stack: Compose) -> None:
    """The worker's finalizer signs bundle manifests and the API verifies them: both load
    the one key migrate created on the `keys` volume, and a restart keeps it."""
    api, worker = probe(stack, "api"), probe(stack, "worker")
    assert api["key_id"] == worker["key_id"]
    signed = stack("exec", "-T", "worker", "python", "-I", "-c", SIGN).stdout.strip()
    manifest = signed.splitlines()[-1]
    assert probe(stack, "api", VERIFY, input=manifest) == {"verified": True}
    stack("restart", "api", "worker")
    stack("up", "-d", "--wait", "--no-build", "api", "worker")
    assert probe(stack, "api")["key_id"] == api["key_id"]
    assert probe(stack, "worker")["key_id"] == worker["key_id"]
    assert probe(stack, "api", VERIFY, input=manifest) == {"verified": True}


def test_lock_digest_matches_the_source_tree_when_freshly_built(stack: Compose) -> None:
    """Informational when the images are older than the working tree."""
    from mapi_server.settings import local_image_digest

    digest = probe(stack, "api")["digest"]
    if digest != local_image_digest(Path(ROOT / "uv.lock")):
        pytest.skip("the images were built from another uv.lock (rebuild to compare)")
