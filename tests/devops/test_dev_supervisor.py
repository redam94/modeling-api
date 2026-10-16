"""scripts/dev.py: service table, selection, preflight, output and the process supervisor.

The supervisor tests run real (tiny) Python subprocesses, so they exercise process groups,
signals, readiness probes and output pumping exactly as ``make dev`` does.
"""

from __future__ import annotations

import asyncio
import io
import os
import signal
import socket
import sys
import textwrap
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PY = sys.executable


def _cfg(dev: ModuleType, tmp_path: Path, **env: str) -> Any:
    return dev.DevConfig.from_environ({"MAPI_DATA_DIR": str(tmp_path / "data"), **env})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ================================================================================ configuration


class TestConfig:
    def test_defaults_follow_localdev_contract(self, dev: ModuleType, tmp_path: Path) -> None:
        from mapi_spec import localdev

        cfg = _cfg(dev, tmp_path)
        assert cfg.api_url.endswith(f":{localdev.API_PORT}")
        assert cfg.gcs_endpoint.endswith(f":{localdev.GCS_SHIM_PORT}")
        assert cfg.web_origin.endswith(f":{localdev.WEB_PORT}")
        assert cfg.plots_origin.endswith(f":{localdev.PLOT_SANDBOX_PORT}")
        assert cfg.database_url is None  # -> embedded pgserver
        assert cfg.pg_dir == tmp_path / "data" / "pg"

    def test_relative_data_dir_is_anchored_at_repo_root(self, dev: ModuleType) -> None:
        cfg = dev.DevConfig.from_environ({"MAPI_DATA_DIR": ".local-test"})
        assert cfg.data_dir == dev.ROOT / ".local-test"

    def test_service_env_carries_every_contract_variable(
        self, dev: ModuleType, tmp_path: Path
    ) -> None:
        from mapi_spec import localdev

        env = _cfg(dev, tmp_path, MAPI_ENV="dev1").service_env("postgresql://x@h/db")
        for name in (
            localdev.ENV_ENV,
            localdev.ENV_DATABASE_URL,
            localdev.ENV_GCS_ENDPOINT,
            localdev.ENV_API_URL,
            localdev.ENV_WEB_ORIGIN,
            localdev.ENV_PLOTS_ORIGIN,
            localdev.ENV_DATA_DIR,
        ):
            assert env[name]
        assert env["MAPI_ENV"] == "dev1"
        assert "DATABASE_URL" not in _cfg(dev, tmp_path).service_env(None)

    def test_the_stack_runs_the_dev_profile_with_an_internal_token(
        self, dev: ModuleType, tmp_path: Path
    ) -> None:
        cfg = _cfg(dev, tmp_path)
        env = cfg.service_env(None)
        # the dev IdP + seeded accounts; a client MAPI_PROFILE never reaches the servers
        assert env["MAPI_PROFILE"] == "dev"
        token = env["MAPI_INTERNAL_TOKEN"]
        assert len(token) >= 32 and cfg.service_env(None)["MAPI_INTERNAL_TOKEN"] == token
        assert cfg.internal_token_file.stat().st_mode & 0o777 == 0o600
        single = _cfg(dev, tmp_path, MAPI_STACK_PROFILE="single-tenant").service_env(None)
        assert single["MAPI_PROFILE"] == "single-tenant" and "MAPI_INTERNAL_TOKEN" not in single
        # the dev profile mails through the outbox in the data dir (GET /dev/outbox)
        assert env["MAPI_OUTBOX_DIR"] == str(cfg.data_dir / "outbox")
        assert "MAPI_OUTBOX_DIR" not in single
        assert dev.main(["status"], {"MAPI_STACK_PROFILE": "nope"}) == 2

    def test_the_dev_profile_trusts_a_local_webhook_receiver(
        self, dev: ModuleType, tmp_path: Path
    ) -> None:
        import ssl

        from cryptography import x509

        cfg = _cfg(dev, tmp_path)
        env = cfg.service_env(None)
        tls = cfg.webhook_tls_dir
        assert env["MAPI_WEBHOOK_CA_FILE"] == str(tls / "ca.pem")
        assert env["MAPI_WEBHOOK_DEV_ALLOWLIST"] == "127.0.0.1,localhost"
        assert (tls / "key.pem").stat().st_mode & 0o777 == 0o600
        # the receiver's certificate verifies against the CA for both names, and is reused
        cert = x509.load_pem_x509_certificate((tls / "cert.pem").read_bytes())
        sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert {str(v) for v in sans.get_values_for_type(x509.IPAddress)} == {"127.0.0.1"}
        assert sans.get_values_for_type(x509.DNSName) == ["localhost"]
        ctx = ssl.create_default_context(cafile=str(tls / "ca.pem"))
        ctx.load_cert_chain(tls / "cert.pem", tls / "key.pem")  # the pair matches
        before = (tls / "cert.pem").read_bytes()
        cfg.service_env(None)
        assert (tls / "cert.pem").read_bytes() == before
        # the caller's own settings win, and other profiles get none of it
        mine = _cfg(
            dev, tmp_path, MAPI_WEBHOOK_CA_FILE="/etc/ca.pem", MAPI_WEBHOOK_DEV_ALLOWLIST=""
        ).service_env(None)
        assert "MAPI_WEBHOOK_CA_FILE" not in mine and "MAPI_WEBHOOK_DEV_ALLOWLIST" not in mine
        multi = _cfg(dev, tmp_path, MAPI_STACK_PROFILE="multi-tenant").service_env(None)
        assert "MAPI_WEBHOOK_CA_FILE" not in multi and "MAPI_WEBHOOK_DEV_ALLOWLIST" not in multi

    def test_outbox_prints_the_newest_links(
        self, dev: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        box = tmp_path / "box"
        environ = {"MAPI_DATA_DIR": str(tmp_path / "data"), "MAPI_OUTBOX_DIR": str(box)}
        assert dev.main(["outbox"], environ) == 0
        assert "no e-mail" in capsys.readouterr().out
        box.mkdir()
        for i, to in enumerate(("ana@acme.example", "bo@acme.example")):
            (box / f"2026-10-16T10-0{i}-00_{i}.json").write_text(
                json.dumps({"to": to, "subject": "Verify", "sent_at": f"t{i}", "body": "",
                            "tags": {"link": f"http://web/verify-email?token={i}"}})
            )  # fmt: skip
        assert dev.main(["outbox", "--to", "ana@acme.example"], environ) == 0
        out = capsys.readouterr().out
        assert "ana@acme.example" in out and "token=0" in out and "bo@" not in out

    def test_same_origin_web_and_plots_is_rejected(self, dev: ModuleType) -> None:
        rc = dev.main(
            ["status"],
            {
                "MAPI_WEB_ORIGIN": "http://127.0.0.1:5173",
                "MAPI_PLOTS_ORIGIN": "http://127.0.0.1:5173",
            },
        )
        assert rc == 2

    def test_endpoint_parsing(self, dev: ModuleType) -> None:
        assert dev.Endpoint.from_url("http://127.0.0.1:9000/x") == dev.Endpoint("127.0.0.1", 9000)
        assert dev.Endpoint.from_url("https://example.org").port == 443
        with pytest.raises(ValueError):
            dev.Endpoint.from_url("gs://bucket/x")


class TestServiceTable:
    def test_ports_follow_overridden_urls(self, dev: ModuleType, tmp_path: Path) -> None:
        cfg = _cfg(dev, tmp_path, MAPI_API_URL="http://127.0.0.1:9100")
        api = {s.name: s for s in dev.build_services(cfg, {})}["api"]
        assert api.argv[-4:] == ("--host", "127.0.0.1", "--port", "9100")
        assert api.ready.url == "http://127.0.0.1:9100/healthz"

    def test_documented_entry_points(self, dev: ModuleType, tmp_path: Path) -> None:
        services = {s.name: s for s in dev.build_services(_cfg(dev, tmp_path), {})}
        assert set(services) == {"gcs-shim", "migrate", "api", "worker", "web", "plots"}
        assert Path(services["gcs-shim"].argv[0]).name == "mapi-gcs-shim"
        assert services["gcs-shim"].argv[1] == "serve"
        assert services["web"].env["VITE_API_BASE"] == "http://127.0.0.1:8000"
        assert services["plots"].env["VITE_APP_ORIGINS"] == "http://127.0.0.1:5173"
        assert services["plots"].env["VITE_API_URL"] == "http://127.0.0.1:8000"
        assert services["gcs-shim"].argv[-2:] == ("--root", str(tmp_path / "data" / "gcs"))
        assert [Path(services[n].argv[0]).name for n in ("migrate", "api", "worker")] == [
            "mapi-server"
        ] * 3
        assert services["migrate"].argv[1:] == ("migrate",)
        assert services["worker"].argv[1:] == ("worker",)
        assert services["migrate"].oneshot
        assert services["web"].argv[:4] == ("pnpm", "--filter", "@mapi/web", "dev")
        assert services["plots"].argv[:4] == ("pnpm", "--filter", "@mapi/plot-sandbox", "dev")
        assert "--strictPort" in services["web"].argv
        assert services["worker"].env == {"MAPI_EXECUTOR": "local-process"}

    def test_command_override(self, dev: ModuleType, tmp_path: Path) -> None:
        env = {"MAPI_DEV_GCS_SHIM_CMD": "python -m my_shim --port '4443'"}
        shim = {s.name: s for s in dev.build_services(_cfg(dev, tmp_path), env)}["gcs-shim"]
        assert shim.argv == ("python", "-m", "my_shim", "--port", "4443")
        assert [r.kind for r in shim.requires] == ["exe"]
        assert dev.override_var("gcs-shim") == "MAPI_DEV_GCS_SHIM_CMD"


class TestSelection:
    def _services(self, dev: ModuleType, tmp_path: Path) -> list[Any]:
        return dev.build_services(_cfg(dev, tmp_path), {})

    def test_default_is_everything_in_dependency_order(
        self, dev: ModuleType, tmp_path: Path
    ) -> None:
        names = [s.name for s in dev.select(self._services(dev, tmp_path))]
        assert set(names) == {"gcs-shim", "migrate", "api", "worker", "web", "plots"}
        assert names.index("migrate") < names.index("api") < names.index("web")
        assert names.index("gcs-shim") < names.index("api")
        assert names.index("migrate") < names.index("worker")

    def test_only_pulls_in_dependencies(self, dev: ModuleType, tmp_path: Path) -> None:
        names = [s.name for s in dev.select(self._services(dev, tmp_path), only=["api"])]
        assert names == ["gcs-shim", "migrate", "api"]

    def test_without_drops_leaves(self, dev: ModuleType, tmp_path: Path) -> None:
        names = {
            s.name for s in dev.select(self._services(dev, tmp_path), without=["web", "plots"])
        }
        assert names == {"gcs-shim", "migrate", "api", "worker"}

    def test_without_a_dependency_is_an_error(self, dev: ModuleType, tmp_path: Path) -> None:
        with pytest.raises(dev.SelectionError, match="web depends on api"):
            dev.select(self._services(dev, tmp_path), without=["api"])

    def test_unknown_and_contradictory(self, dev: ModuleType, tmp_path: Path) -> None:
        services = self._services(dev, tmp_path)
        with pytest.raises(dev.SelectionError, match="unknown service"):
            dev.select(services, only=["nope"])
        with pytest.raises(dev.SelectionError, match="both selected and excluded"):
            dev.select(services, only=["api"], without=["api"])

    def test_cycle_detection(self, dev: ModuleType) -> None:
        a = dev.Service("a", ("x",), depends_on=("b",))
        b = dev.Service("b", ("x",), depends_on=("a",))
        with pytest.raises(dev.SelectionError, match="cycle"):
            dev.topo_order([a, b])
        with pytest.raises(dev.SelectionError, match="not selected"):
            dev.topo_order([a])

    def test_topo_order_is_stable(self, dev: ModuleType) -> None:
        svcs = [dev.Service(n, ("x",)) for n in "cab"]
        assert [s.name for s in dev.topo_order(svcs)] == ["c", "a", "b"]


class TestPreflight:
    def test_reports_missing_requirements(self, dev: ModuleType, tmp_path: Path) -> None:
        svc = dev.Service(
            "x",
            ("x",),
            requires=(
                dev.Requirement("module", "definitely_not_a_module_xyz", "hint-a"),
                dev.Requirement("exe", "definitely-not-an-exe-xyz", "hint-b"),
                dev.Requirement("file", "missing/file.json", "hint-c"),
                dev.Requirement("module", "json", "fine"),
            ),
        )
        problems = dev.preflight([svc], tmp_path)
        assert len(problems) == 3
        assert all(p.startswith("x: ") for p in problems)
        assert any("hint-a" in p for p in problems) and any("hint-c" in p for p in problems)

    def test_reports_busy_port(self, dev: ModuleType, tmp_path: Path) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            port = s.getsockname()[1]
            svc = dev.Service("api", ("x",), port=dev.Endpoint("127.0.0.1", port))
            problems = dev.preflight([svc], tmp_path)
        assert len(problems) == 1 and "already in use" in problems[0]


class TestOutput:
    def test_format_line(self, dev: ModuleType) -> None:
        assert dev.format_line("api", 6, None, "hello") == "api    | hello"
        colored = dev.format_line("api", 3, "36", "hi")
        assert colored == "\033[36mapi\033[0m | hi"

    def test_use_color(self, dev: ModuleType) -> None:
        class Tty(io.StringIO):
            def isatty(self) -> bool:
                return True

        assert dev.use_color(Tty(), {"TERM": "xterm"})
        assert not dev.use_color(Tty(), {"NO_COLOR": "1"})
        assert not dev.use_color(Tty(), {"TERM": "dumb"})
        assert not dev.use_color(io.StringIO(), {})
        assert dev.use_color(io.StringIO(), {"FORCE_COLOR": "1"})

    def test_prefix_width_covers_all_names(self, dev: ModuleType) -> None:
        buf = io.StringIO()
        out = dev.Output(buf, ["a", "long-name"], color=False)
        out.line("a", "x")
        out.say("y")
        assert buf.getvalue().splitlines() == ["a         | x", "dev       | y"]


class TestUrlsAndState:
    def test_libpq_url(self, dev: ModuleType) -> None:
        assert dev.libpq_url("postgresql+psycopg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
        assert dev.libpq_url("postgresql://postgres:@/mapi?host=/tmp/s") == (
            "postgresql://postgres:@/mapi?host=/tmp/s"
        )
        with pytest.raises(ValueError):
            dev.libpq_url("mysql://x")

    def test_state_round_trip_and_liveness(self, dev: ModuleType, tmp_path: Path) -> None:
        path = tmp_path / "dev" / "supervisor.json"
        assert dev.State.read(path) is None
        dev.State(os.getpid(), 1.0, {"api": 123}, None, True).write(path)
        assert dev.live_state(path).services == {"api": 123}
        dev.State(2**22 + 12345, 1.0, {}, None, True).write(path)  # pid that cannot exist
        assert dev.State.read(path) is not None
        assert dev.live_state(path) is None
        path.write_text("{not json")
        assert dev.State.read(path) is None

    def test_pid_alive(self, dev: ModuleType) -> None:
        assert dev.pid_alive(os.getpid())
        assert not dev.pid_alive(0)


# =================================================================================== supervisor


def _py(code: str) -> tuple[str, ...]:
    return (PY, "-u", "-c", textwrap.dedent(code))


def _server_code(port: int, *, lines: str = "", ignore_term: bool = False) -> str:
    """A long-running process that prints, then listens on ``port``."""
    return textwrap.dedent(
        f"""
        import signal, socket, sys, time
        {"signal.signal(signal.SIGTERM, signal.SIG_IGN)" if ignore_term else ""}
        {lines}
        s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", {port})); s.listen()
        print("listening", flush=True)
        while True:
            time.sleep(0.05)
        """
    )


def _run(
    dev: ModuleType,
    services: list[Any],
    *,
    stop_after: float | None = None,
    grace: float = 2.0,
) -> tuple[int, str, Any]:
    buf = io.StringIO()
    out = dev.Output(buf, [s.name for s in services], color=False)
    started: list[dict[str, int]] = []
    sup = dev.Supervisor(
        services,
        dict(os.environ),
        out,
        cwd=Path.cwd(),
        grace=grace,
        poll=0.05,
        on_started=started.append,
    )

    async def main() -> int:
        if stop_after is not None:
            asyncio.get_running_loop().call_later(stop_after, sup.request_stop)
        return int(await sup.run())

    rc = asyncio.run(main())
    return rc, buf.getvalue(), sup


@pytest.mark.integration
class TestSupervisor:
    def test_start_in_order_prefix_output_and_clean_stop(self, dev: ModuleType) -> None:
        port = _free_port()
        services = [
            dev.Service("setup", _py("print('migrated')"), oneshot=True),
            dev.Service(
                "server",
                _py(_server_code(port, lines="print('hello from server', flush=True)")),
                depends_on=("setup",),
                ready=dev.TcpProbe("127.0.0.1", port),
            ),
            dev.Service(
                "tail", _py("import time\nwhile True: time.sleep(0.05)"), depends_on=("server",)
            ),
        ]
        rc, text, sup = _run(dev, services, stop_after=1.5)
        assert rc == 0
        lines = text.splitlines()
        assert "setup  | migrated" in lines
        assert "server | hello from server" in lines
        first = next(i for i, ln in enumerate(lines) if ln.startswith("dev    | starting server"))
        assert lines.index("dev    | setup finished") < first  # one-shot completes first
        starts = [ln for ln in lines if ln.startswith("dev    | starting ")]
        assert [s.split(":")[0].split()[-1] for s in starts] == ["setup", "server", "tail"]
        assert any("server is up (tcp 127.0.0.1:" in ln for ln in lines)
        stops = [ln for ln in lines if ln.startswith("dev    | stopping ")]
        assert stops == ["dev    | stopping tail", "dev    | stopping server"]  # reverse order
        assert all(r.proc.returncode is not None for r in sup.running.values())

    def test_oneshot_failure_stops_startup(self, dev: ModuleType) -> None:
        services = [
            dev.Service("migrate", _py("import sys; print('boom'); sys.exit(3)"), oneshot=True),
            dev.Service("api", _py("print('should not start')"), depends_on=("migrate",)),
        ]
        rc, text, sup = _run(dev, services)
        assert rc == 1
        assert "migrate exited with status 3" in text
        assert "should not start" not in text
        assert "api" not in sup.running

    def test_unexpected_exit_tears_down_the_stack(self, dev: ModuleType) -> None:
        services = [
            dev.Service("steady", _py("import time\nwhile True: time.sleep(0.05)")),
            dev.Service("flaky", _py("import time, sys; time.sleep(0.3); sys.exit(7)")),
        ]
        rc, text, sup = _run(dev, services)
        assert rc == 1
        assert "flaky exited unexpectedly with status 7" in text
        assert sup.running["steady"].proc.returncode is not None

    def test_exit_before_ready_is_reported(self, dev: ModuleType) -> None:
        port = _free_port()
        services = [
            dev.Service(
                "api",
                _py("import sys; print('cannot bind'); sys.exit(1)"),
                ready=dev.TcpProbe("127.0.0.1", port),
            )
        ]
        rc, text, _ = _run(dev, services)
        assert rc == 1
        assert "api exited with status 1 before it was ready" in text
        assert "api | cannot bind" in text

    def test_ready_timeout(self, dev: ModuleType) -> None:
        port = _free_port()
        services = [
            dev.Service(
                "slow",
                _py("import time\nwhile True: time.sleep(0.05)"),
                ready=dev.TcpProbe("127.0.0.1", port),
                ready_timeout=0.5,
            )
        ]
        t0 = time.monotonic()
        rc, text, sup = _run(dev, services)
        assert rc == 1
        assert "slow not ready after" in text
        assert time.monotonic() - t0 < 10
        assert sup.running["slow"].proc.returncode is not None

    def test_sigterm_ignoring_process_is_killed_after_grace(self, dev: ModuleType) -> None:
        port = _free_port()
        services = [
            dev.Service(
                "stubborn",
                _py(_server_code(port, ignore_term=True)),
                ready=dev.TcpProbe("127.0.0.1", port),
            )
        ]
        t0 = time.monotonic()
        rc, text, sup = _run(dev, services, stop_after=0.5, grace=0.5)
        assert rc == 0
        assert "stubborn did not stop in 0s: SIGKILL" in text or "SIGKILL" in text
        assert sup.running["stubborn"].proc.returncode == -signal.SIGKILL
        assert time.monotonic() - t0 < 10

    def test_process_group_children_are_stopped(self, dev: ModuleType, tmp_path: Path) -> None:
        pidfile = tmp_path / "child.pid"
        code = f"""
            import subprocess, sys, time
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            open({str(pidfile)!r}, "w").write(str(child.pid))
            print("spawned", flush=True)
            while True:
                time.sleep(0.05)
        """
        services = [dev.Service("parent", _py(code))]
        rc, _, _ = _run(dev, services, stop_after=1.0)
        assert rc == 0
        child = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while dev.pid_alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _running_not_zombie(child)

    def test_only_oneshots_returns_after_success(self, dev: ModuleType) -> None:
        rc, text, _ = _run(dev, [dev.Service("m", _py("print('ok')"), oneshot=True)])
        assert rc == 0 and "m   | ok" in text
        assert "Ctrl-C" not in text

    def test_missing_executable(self, dev: ModuleType) -> None:
        rc, text, _ = _run(dev, [dev.Service("ghost", ("/nonexistent/binary-xyz",))])
        assert rc == 1 and "ghost could not start" in text

    def test_started_callback_records_pids(self, dev: ModuleType) -> None:
        services = [dev.Service("a", _py("import time\nwhile True: time.sleep(0.05)"))]
        buf = io.StringIO()
        seen: list[dict[str, int]] = []
        sup = dev.Supervisor(
            services,
            dict(os.environ),
            dev.Output(buf, ["a"], False),
            grace=1,
            poll=0.05,
            on_started=seen.append,
        )

        async def main() -> int:
            asyncio.get_running_loop().call_later(0.5, sup.request_stop)
            return int(await sup.run())

        assert asyncio.run(main()) == 0
        assert seen and set(seen[0]) == {"a"} and seen[0]["a"] > 0


def _running_not_zombie(pid: int) -> bool:
    """True if ``pid`` is alive and not a zombie (ps is available on macOS and Linux)."""
    import subprocess

    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    stat = out.stdout.strip()
    return bool(stat) and not stat.startswith("Z")


# ===================================================================================== postgres


@pytest.mark.integration
@pytest.mark.slow
def test_embedded_postgres_lifecycle(
    dev: ModuleType, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """pgserver starts PostgreSQL 16 with a ``mapi`` database; psql works; stop is clean."""
    import subprocess

    pytest.importorskip("pgserver")
    pgdata = tmp_path_factory.mktemp("pg") / "pg"
    pg = dev.EmbeddedPostgres(pgdata)
    assert not pg.running()
    url = pg.start()
    try:
        assert url.startswith("postgresql://") and "/mapi" in url
        assert pg.running()
        assert pg.start() == url  # idempotent attach
        psql = dev.EmbeddedPostgres.bin_dir() / "psql"
        result = subprocess.run(
            [str(psql), dev.libpq_url(url), "-tAc", "SHOW server_version_num"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip().startswith("16")
    finally:
        pg.stop()
    assert not pg.running()
