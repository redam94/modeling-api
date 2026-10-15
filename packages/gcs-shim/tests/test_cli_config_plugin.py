"""Config/keys, IAM bindings, the CLI (in-process and as a subprocess) and the pytest plugin."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from importlib.metadata import entry_points
from pathlib import Path

import httpx
import pytest
from mapi_gcs_shim import ShimCredentials, ShimServer
from mapi_gcs_shim.__main__ import main
from mapi_gcs_shim.config import (
    PRINCIPAL_CONTROL,
    PRINCIPAL_EXEC,
    BucketConfig,
    PrincipalConfig,
    ShimConfig,
    check_bucket_name,
    config_path,
    default_config,
    init_root,
    key_file_path,
)
from mapi_gcs_shim.iam import Binding, Permission
from pydantic import SecretStr, ValidationError

# ------------------------------------------------------------------------- config + IAM


@pytest.mark.unit
def test_default_config_matches_the_plan() -> None:
    cfg = default_config("dev")
    names = {b.name: b for b in cfg.buckets}
    assert set(names) == {
        f"mapi-dev-{p}" for p in ("quarantine", "bootstrap", "runs", "bundles", "data")
    }
    assert names["mapi-dev-bundles"].write_once
    assert names["mapi-dev-quarantine"].lifecycle_age_seconds == 7 * 86400
    assert names["mapi-dev-bootstrap"].lifecycle_age_seconds == 86400
    control, exec_ = cfg.principal(PRINCIPAL_CONTROL), cfg.principal(PRINCIPAL_EXEC)
    for bucket in names:
        assert control.can(Permission.OBJECTS_CREATE, bucket)
    assert control.can(Permission.BUCKETS_LIST, None) and control.can(Permission.SHIM_ADMIN, None)
    assert exec_.permissions("mapi-dev-bootstrap") == {
        Permission.OBJECTS_GET,
        Permission.OBJECTS_DELETE,
    }
    for bucket in names:
        if bucket != "mapi-dev-bootstrap":
            assert exec_.permissions(bucket) == frozenset()
    assert exec_.permissions(None) == frozenset()
    assert cfg.by_token(control.token.get_secret_value()) is control
    assert cfg.by_token("nope") is None
    assert cfg.by_email(exec_.email) is exec_
    assert control.token.get_secret_value() not in repr(cfg)
    with pytest.raises(KeyError):
        cfg.principal("nobody")


@pytest.mark.unit
def test_config_validation() -> None:
    p = default_config().principals[0]
    with pytest.raises(ValidationError, match="unique"):
        ShimConfig(principals=(p, p))
    with pytest.raises(ValidationError):
        PrincipalConfig(
            id="Bad Id", email="x@y.z", token=SecretStr("t" * 20), secret=SecretStr("s" * 20)
        )
    with pytest.raises(ValidationError, match="16 characters"):
        PrincipalConfig(
            id="ok", email="x@y.z", token=SecretStr("short"), secret=SecretStr("s" * 20)
        )
    with pytest.raises(ValidationError, match="unknown role"):
        Binding(role="roles/owner")
    with pytest.raises(ValidationError, match="role or at least one"):
        Binding(bucket="b")
    with pytest.raises(ValidationError, match="locked retention"):
        BucketConfig(name="mapi-local-x", retention_locked=True)
    for bad in ("Upper", "storage", "a..b", "goog-x", "my-google-bucket", "x", "a/b"):
        with pytest.raises(ValueError):
            check_bucket_name(bad)


@pytest.mark.unit
def test_init_root_is_idempotent_and_private(tmp_path: Path) -> None:
    root = tmp_path / "gcs"
    cfg1 = init_root(root, env="local")
    cfg2 = init_root(root, env="other")  # existing config wins
    assert cfg1 == cfg2 and cfg2.env == "local"
    assert stat.S_IMODE(config_path(root).stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "_shim").stat().st_mode) == 0o700
    raw = json.loads(config_path(root).read_text())
    assert raw["principals"][0]["token"] == cfg1.principals[0].token.get_secret_value()


# ------------------------------------------------------------------------- CLI


@pytest.mark.unit
def test_cli_init_and_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "gcs"
    assert main(["init", "--root", str(root), "--endpoint", "http://127.0.0.1:4999"]) == 0
    out = capsys.readouterr().out
    assert "control:" in out and "exec:" in out
    creds = ShimCredentials.load(key_file_path(root, "exec"))
    assert creds.endpoint == "http://127.0.0.1:4999"
    assert stat.S_IMODE(key_file_path(root, "exec").stat().st_mode) == 0o600
    assert main(["key", "--root", str(root), "exec", "--field", "token"]) == 0
    assert capsys.readouterr().out.strip() == creds.token.get_secret_value()
    assert main(["key", "--root", str(root), "exec", "--field", "email"]) == 0
    assert capsys.readouterr().out.strip() == creds.client_email
    assert main(["key", "--root", str(root), "control"]) == 0
    assert capsys.readouterr().out.strip() == str(key_file_path(root, "control"))
    assert main(["key", "--root", str(tmp_path / "empty"), "exec"]) == 1


@pytest.mark.integration
def test_cli_serves_until_sigterm(tmp_path: Path) -> None:
    root = tmp_path / "gcs"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mapi_gcs_shim",
            "--root",
            str(root),
            "--port",
            "0",
            "--sweep-interval",
            "0",
            "--log-level",
            "warning",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "MAPI_ENV": "local"},
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert "serving" in line, line
        endpoint = line.rsplit(" at ", 1)[1].strip()
        deadline = time.monotonic() + 15
        while True:
            try:
                r = httpx.get(f"{endpoint}/_shim/healthz", timeout=1)
                break
            except httpx.TransportError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        assert r.json()["status"] == "ok"
        creds = ShimCredentials.from_root(root, "control")
        assert creds.endpoint == endpoint
        r = httpx.get(
            f"{endpoint}/storage/v1/b",
            headers={"Authorization": f"Bearer {creds.token.get_secret_value()}"},
        )
        assert len(r.json()["items"]) == 5
        # a second server on the same root refuses to start
        second = subprocess.run(
            [sys.executable, "-m", "mapi_gcs_shim", "serve", "--root", str(root), "--port", "0"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert second.returncode != 0 and "another GCS shim" in second.stdout + second.stderr
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
    assert proc.returncode == 0


# ------------------------------------------------------------------------- plugin + server


@pytest.mark.unit
def test_plugin_is_registered_as_pytest11_entry_point() -> None:
    eps = {ep.name: ep.value for ep in entry_points(group="pytest11")}
    assert eps.get("mapi_gcs_shim") == "mapi_gcs_shim.pytest_plugin"


@pytest.mark.integration
def test_session_fixture_is_shared(gcs_shim_session: ShimServer) -> None:
    r = httpx.get(f"{gcs_shim_session.endpoint}/_shim/healthz")
    assert r.status_code == 200
    assert gcs_shim_session.auth_headers()["Authorization"].startswith("Bearer shim-")


@pytest.mark.integration
def test_server_lifecycle_and_persistence(tmp_path: Path) -> None:
    root = tmp_path / "gcs"
    server = ShimServer(root)
    with pytest.raises(RuntimeError, match="not running"):
        _ = server.config
    with server:
        endpoint = server.endpoint
        token = server.token()
        r = httpx.post(
            f"{endpoint}/upload/storage/v1/b/{server.buckets.runs}/o",
            params={"uploadType": "media", "name": "persist/me"},
            content=b"kept",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        with pytest.raises(RuntimeError, match="already running"):
            server.start()
    server.stop()  # idempotent
    with ShimServer(root) as again:
        assert again.token() == token  # secrets are stable across restarts
        r = httpx.get(
            f"{again.endpoint}/storage/v1/b/{again.buckets.runs}/o/persist%2Fme",
            params={"alt": "media"},
            headers=again.auth_headers(),
        )
        assert r.content == b"kept"


@pytest.mark.integration
def test_background_sweeper_runs(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    future = datetime.now(UTC) + timedelta(days=30)
    with ShimServer(tmp_path / "gcs", sweep_interval=0.05) as shim:
        r = httpx.post(
            f"{shim.endpoint}/upload/storage/v1/b/{shim.buckets.quarantine}/o",
            params={"uploadType": "media", "name": "t/default/uploads/u"},
            content=b"x",
            headers=shim.auth_headers(),
        )
        assert r.status_code == 200
        shim.app.clock = lambda: future  # the sweeper reads the app clock each period
        url = f"{shim.endpoint}/storage/v1/b/{shim.buckets.quarantine}/o/t%2Fdefault%2Fuploads%2Fu"
        deadline = time.monotonic() + 10
        while httpx.get(url, headers=shim.auth_headers()).status_code != 404:
            assert time.monotonic() < deadline, "the lifecycle sweeper never ran"
            time.sleep(0.05)
