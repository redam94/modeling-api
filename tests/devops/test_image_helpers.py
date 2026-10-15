"""images/common: the health probe, the build-time package guard, the uv installer, the runner
entrypoint (cache seeding + argument handling) and the build-time catalog export."""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from .conftest import ROOT, load_script


@pytest.fixture(scope="module")
def hc() -> ModuleType:
    return load_script("images/common/healthcheck.py", "mapi_devtools_healthcheck")


@pytest.fixture(scope="module")
def dists() -> ModuleType:
    return load_script("images/common/assert_dists.py", "mapi_devtools_assert_dists")


@pytest.fixture(scope="module")
def installer() -> ModuleType:
    return load_script("images/common/install_packages.py", "mapi_devtools_install")


@pytest.fixture
def http_server() -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            code = 200 if self.path == "/healthz" else 503
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ================================================================================ healthcheck


class TestHealthcheck:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("postgresql://u:secret@db:5432/mapi", ("db", 5432)),
            ("postgresql+psycopg://u:p@db/mapi", ("db", 5432)),
            ("http://127.0.0.1:4443", ("127.0.0.1", 4443)),
            ("https://example.org/x", ("example.org", 443)),
        ],
    )
    def test_host_port(self, hc: ModuleType, url: str, expected: tuple[str, int]) -> None:
        assert hc.host_port(url) == expected

    def test_host_port_rejects_sockets_and_unknown_schemes(self, hc: ModuleType) -> None:
        with pytest.raises(hc.Unhealthy, match="unix socket"):
            hc.host_port("postgresql://postgres:@/mapi?host=/tmp/sock")
        with pytest.raises(hc.Unhealthy, match="no default"):
            hc.host_port("redis://cache/0")

    @pytest.mark.integration
    def test_http(self, hc: ModuleType, http_server: str) -> None:
        assert hc.run(["http", f"{http_server}/healthz"]) == 0
        assert hc.run(["http", f"{http_server}/broken"]) == 1
        assert hc.run(["http", "file:///etc/passwd"]) == 1

    @pytest.mark.integration
    def test_tcp(self, hc: ModuleType, http_server: str) -> None:
        port = http_server.rsplit(":", 1)[1]
        assert hc.run(["tcp", f"127.0.0.1:{port}"]) == 0
        assert hc.run(["--timeout", "0.5", "tcp", f"127.0.0.1:{_closed_port()}"]) == 1
        assert hc.run(["tcp", "no-port"]) == 1

    @pytest.mark.integration
    def test_env_tcp_never_prints_credentials(
        self, hc: ModuleType, http_server: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        port = http_server.rsplit(":", 1)[1]
        env = {
            "DATABASE_URL": f"postgresql://u:s3cr3t@127.0.0.1:{_closed_port()}/db",
            "MAPI_GCS_ENDPOINT": f"http://127.0.0.1:{port}",
        }
        assert hc.run(["env-tcp", "MAPI_GCS_ENDPOINT"], env) == 0
        assert hc.run(["--timeout", "0.5", "env-tcp", "DATABASE_URL"], env) == 1
        assert hc.run(["env-tcp", "NOT_SET"], env) == 1
        assert "s3cr3t" not in capsys.readouterr().err

    def test_import(self, hc: ModuleType) -> None:
        assert hc.run(["import", "json", "mapi_spec"]) == 0
        assert hc.run(["import", "definitely_not_a_module_xyz"]) == 1


# =============================================================================== assert_dists


class TestAssertDists:
    def test_violations(self, dists: ModuleType) -> None:
        installed = {"numpy", "mapi-server", "pydantic-core"}
        found = {"numpy", "pydantic_core"}
        problems = dists.violations(
            ["mapi-server", "Pydantic_Core", "fastapi"],
            ["numpy", "xarray"],
            ["pydantic_core"],
            dists=installed,
            find_module=lambda m: object() if m in found else None,
        )
        assert problems == [
            "required distribution 'fastapi' is not installed",
            "forbidden distribution 'numpy' is installed",
            "forbidden module 'numpy' is importable",
            "forbidden module 'pydantic_core' is importable",
        ]

    def test_against_this_environment(self, dists: ModuleType) -> None:
        assert dists.main(["--present", "mapi-spec", "pydantic", "--absent", "nope-xyz"]) == 0
        assert dists.main(["--absent", "pydantic"]) == 1


# ============================================================================ install_packages


class TestInstallPackages:
    def test_spec_parsing(self, installer: ModuleType) -> None:
        assert installer.PackageSpec.parse("mapi-server[docker]") == installer.PackageSpec(
            "mapi-server", ("docker",)
        )
        assert installer.PackageSpec.parse("Mapi-Runner").name == "mapi-runner"
        for bad in ("", "mapi server", "x[", "-x", "x;rm -rf /"):
            with pytest.raises(ValueError):
                installer.PackageSpec.parse(bad)

    def test_commands(self, installer: ModuleType) -> None:
        specs = [installer.PackageSpec.parse(s) for s in ("mapi-runner", "mapi-server[docker,gcp]")]
        deps = installer.uv_commands(specs, deps_only=True, no_install=["nvidia-nccl-cu13"])
        full = installer.uv_commands(specs, deps_only=False)
        base = ["uv", "sync", "--frozen", "--no-dev", "--inexact", "--package"]
        assert deps[0] == [
            *base,
            "mapi-runner",
            "--no-install-package",
            "nvidia-nccl-cu13",
            "--no-install-workspace",
        ]
        assert full[1] == [
            *base,
            "mapi-server",
            "--extra",
            "docker",
            "--extra",
            "gcp",
            "--no-editable",
        ]
        with pytest.raises(ValueError):
            installer.uv_commands([], deps_only=True)

    def test_dry_run(self, installer: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
        assert installer.main(["--dry-run", "--deps-only", "mapi-gcs-shim", "--no-install"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("+ uv sync --frozen") and "--no-install-workspace" in out


# ================================================================================= entrypoint


def _entrypoint_env(tmp_path: Path) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "mapi-runner"
    fake.write_text('#!/bin/sh\necho "runner:$*"\n')
    fake.chmod(0o755)
    seed = tmp_path / "seed" / "pytensor" / "compiledir_x" / "tmpabc"
    seed.mkdir(parents=True)
    (seed / "mod.so").write_bytes(b"\x7fELF")
    return {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "MAPI_CACHE_SEED": str(tmp_path / "seed"),
        "MAPI_CACHE_DIR": str(tmp_path / "cache"),
    }


def _gnu_cp() -> bool:
    out = subprocess.run(["cp", "--version"], capture_output=True, text=True, check=False)
    return out.returncode == 0 and "GNU" in out.stdout


@pytest.mark.integration
class TestRunnerEntrypoint:
    script = str(ROOT / "images/common/runner-entrypoint.sh")

    def _run(self, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", self.script, *args], env=env, capture_output=True, text=True, check=False
        )

    def test_default_and_flag_arguments_run_the_runner(self, tmp_path: Path) -> None:
        env = _entrypoint_env(tmp_path)
        assert self._run(env).stdout.strip() == "runner:"
        assert self._run(env, "--version").stdout.strip() == "runner:--version"
        assert self._run(env, "echo", "other").stdout.strip() == "other"
        assert Path(env["HOME"]).is_dir()

    @pytest.mark.skipif(not _gnu_cp(), reason="the image uses GNU cp -as (Debian)")
    def test_seeds_a_symlink_farm_once(self, tmp_path: Path) -> None:
        env = _entrypoint_env(tmp_path)
        assert self._run(env).returncode == 0
        linked = Path(env["MAPI_CACHE_DIR"]) / "pytensor/compiledir_x/tmpabc/mod.so"
        assert (
            linked.is_symlink()
            and linked.resolve()
            == (Path(env["MAPI_CACHE_SEED"]) / "pytensor/compiledir_x/tmpabc/mod.so").resolve()
        )
        assert (Path(env["MAPI_CACHE_DIR"]) / ".seeded").exists()
        linked.unlink()
        self._run(env)  # already seeded: not redone
        assert not linked.exists()

    def test_missing_seed_degrades_with_a_warning(self, tmp_path: Path) -> None:
        env = _entrypoint_env(tmp_path)
        shutil.rmtree(env["MAPI_CACHE_SEED"])
        result = self._run(env)
        assert result.returncode == 0 and result.stdout.strip() == "runner:"
        assert "compile from scratch" in result.stderr

    def test_unwritable_cache_dir_is_reported(self, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("root can write anywhere")
        env = _entrypoint_env(tmp_path)
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        env["MAPI_CACHE_DIR"] = str(ro / "cache")
        try:
            result = self._run(env)
        finally:
            ro.chmod(0o700)
        assert result.returncode == 0 and "not writable" in result.stderr


# ======================================================================== warm (catalog only)


@pytest.mark.integration
def test_warm_exports_the_catalog(tmp_path: Path) -> None:
    warm = load_script("images/common/warm.py", "mapi_devtools_warm")
    summary = warm.export_catalog(tmp_path)
    catalog = json.loads((tmp_path / "catalog.json").read_text())
    digest = (tmp_path / "catalog.digest").read_text().strip()
    assert digest == summary["digest"] and digest.startswith("sha256:")
    assert {"steps", "models", "plots"} <= set(catalog)
    assert "mmm.hierarchical_geo" in summary["models"]

    from mapi_spec.catalog import Catalog
    from mapi_spec.run import catalog_digest

    assert catalog_digest(Catalog.model_validate(catalog)) == digest  # round-trips


def test_scripts_are_stdlib_only_where_they_must_be() -> None:
    """Helpers that run with the *system* python in builder stages may not import packages."""
    for rel in (
        "images/common/install_packages.py",
        "images/common/healthcheck.py",
        "images/common/assert_dists.py",
        "scripts/lock_closure.py",
    ):
        code = (
            "import sys, runpy; sys.argv=['x','--help']\n"
            f"try:\n    runpy.run_path({str(ROOT / rel)!r}, run_name='__main__')\n"
            "except SystemExit as e:\n    sys.exit(e.code)\n"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{rel}: {result.stderr[-500:]}"
