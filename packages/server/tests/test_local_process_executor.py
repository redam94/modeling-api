"""LocalProcessExecutor: env-only identifiers, the stdout event pipe, exit codes, cancel,
hard timeouts, orphan adoption and the multi-tenant refusal."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest
from mapi_server.executors import (
    ExecutorRefused,
    LocalProcessExecutor,
    PlacementRefused,
    build_executor,
    check_placement,
)
from mapi_server.settings import ExecutorName, Profile, Settings
from mapi_spec._types import Duration
from mapi_spec.executor import ExecState, Handle, Isolation, RunKind, RunSubmit
from mapi_spec.run import RunnerPhase, StatePayload, encode_event_line, make_event

TEMPLATE = "http://127.0.0.1:9/storage/v1/b/mapi-local-bootstrap/o/{nonce}"
NONCE = "a" * 64


def _script(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "sup.py"
    path.write_text(textwrap.dedent(body))
    return [sys.executable, str(path)]


def _submit(run_id: str = "run_1", timeout: int = 60) -> RunSubmit:
    return RunSubmit(run_id=run_id, attempt=1, kind=RunKind.FIT, timeout=Duration(timeout))


async def _wait_done(ex: LocalProcessExecutor, h: Handle, limit: float = 10) -> None:
    async def loop() -> None:
        while not (await ex.status(h)).state.terminal:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(loop(), limit)


def test_refuses_multi_tenant_profile(tmp_path: Path) -> None:
    with pytest.raises(ExecutorRefused):
        LocalProcessExecutor(
            command=["mapi-runner"],
            bootstrap_url_template=TEMPLATE,
            workdir=tmp_path,
            profile=Profile.MULTI_TENANT,
        )
    settings = Settings(
        database_url="postgresql://x/y",
        profile=Profile.MULTI_TENANT,
        executor=ExecutorName.LOCAL_PROCESS,
        data_dir=tmp_path,
    )
    with pytest.raises(ExecutorRefused):
        build_executor(settings)
    with pytest.raises(ValueError):
        LocalProcessExecutor(
            command=["x"], bootstrap_url_template="file:///{nonce}", workdir=tmp_path
        )
    with pytest.raises(ValueError):
        LocalProcessExecutor(
            command=["x"],
            bootstrap_url_template=TEMPLATE,
            workdir=tmp_path,
            extra_env={"MAPI_SECRET": "1"},
        )


def test_caps_and_placement(tmp_path: Path) -> None:
    ex = LocalProcessExecutor(command=["x"], bootstrap_url_template=TEMPLATE, workdir=tmp_path)
    caps = ex.caps()
    assert caps.isolation is Isolation.PROCESS and not caps.allows_public_tenants()
    from mapi_spec.resources import ResourceClass

    check_placement(caps, trust_tier="internal", resource_class=ResourceClass.S)
    with pytest.raises(PlacementRefused):
        check_placement(caps, trust_tier="public", resource_class=ResourceClass.S)
    with pytest.raises(PlacementRefused):
        check_placement(caps, trust_tier="internal", resource_class=ResourceClass.XL)


async def test_environment_and_event_pipe(tmp_path: Path) -> None:
    good = encode_event_line(make_event("run_1", 1, 0, StatePayload(phase=RunnerPhase.FIT)))
    foreign = encode_event_line(make_event("run_2", 1, 0, StatePayload(phase=RunnerPhase.FIT)))
    cmd = _script(
        tmp_path,
        f"""
        import json, os, sys
        print("not an event, ignored")
        print({good!r})
        print({foreign!r})
        print('{{"mapi_event": {{"run_id": 5}}}}')
        sys.stderr.write("stderr tail line\\n")
        env = {{k: v for k, v in os.environ.items()}}
        open("env.json", "w").write(json.dumps(env))
        print(json.dumps({{"argv": sys.argv[1:]}}))
        sys.exit(10)
        """,
    )
    ex = LocalProcessExecutor(
        command=cmd,
        bootstrap_url_template=TEMPLATE,
        workdir=tmp_path / "work",
        exec_token="tok",
        parent_env={
            "PATH": "/usr/bin:/bin",
            "DATABASE_URL": "postgresql://secret",
            "HOME": "/h",
            "MAPI_INTERNAL_TOKEN": "nope",
        },
        extra_env={"EXTRA": "1"},
    )
    h = await ex.submit(_submit(), NONCE)
    assert h.external_id and h.external_id.startswith("pid:")
    assert await ex.submit(_submit(), NONCE) == h  # duplicate submit → same execution
    await _wait_done(ex, h)
    st = await ex.status(h)
    assert st.state is ExecState.FAILED and st.exit_code == 10
    assert st.message is not None and "stderr tail line" in st.message
    event = await asyncio.wait_for(ex.event_source().__anext__(), 2)
    assert event.run_id == "run_1" and event.type == "state"
    assert ex._events.empty()  # the foreign and malformed lines were dropped
    env = json.loads((tmp_path / "work" / "runs" / NONCE[:24] / "env.json").read_text())
    assert env["MAPI_RUN_ID"] == "run_1" and env["MAPI_NONCE"] == NONCE
    assert env["MAPI_BOOTSTRAP_URL"] == TEMPLATE and env["MAPI_EXEC_TOKEN"] == "tok"
    assert env["EXTRA"] == "1" and env["HOME"] == "/h"
    assert "DATABASE_URL" not in env and "MAPI_INTERNAL_TOKEN" not in env
    assert not (tmp_path / "work" / "pids" / f"{NONCE}.json").exists()  # cleaned up


async def test_success_cancel_and_timeout(tmp_path: Path) -> None:
    ok = LocalProcessExecutor(
        command=_script(tmp_path, "import sys; sys.exit(0)"),
        bootstrap_url_template=TEMPLATE,
        workdir=tmp_path / "w1",
    )
    h = await ok.submit(_submit(), NONCE)
    await _wait_done(ok, h)
    assert (await ok.status(h)).state is ExecState.SUCCEEDED

    hang = """
    import signal, sys, time
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    time.sleep(60)
    """
    ex = LocalProcessExecutor(
        command=_script(tmp_path, hang),
        bootstrap_url_template=TEMPLATE,
        workdir=tmp_path / "w2",
        cancel_grace_s=2,
    )
    h = await ex.submit(_submit(), "b" * 64)
    await asyncio.sleep(0.3)
    assert (await ex.status(h)).state is ExecState.RUNNING
    await ex.cancel(h)
    await _wait_done(ex, h)
    st = await ex.status(h)
    assert st.state is ExecState.CANCELLED and st.exit_code == 143

    stubborn = """
    import signal, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
    """
    ex2 = LocalProcessExecutor(
        command=_script(tmp_path, stubborn),
        bootstrap_url_template=TEMPLATE,
        workdir=tmp_path / "w3",
        cancel_grace_s=0.3,
        max_runtime_s=1,
    )
    h2 = await ex2.submit(_submit(timeout=1), "c" * 64)
    await _wait_done(ex2, h2, limit=15)
    st2 = await ex2.status(h2)
    # killed by the watchdog after ignoring SIGTERM: a timeout (143), not an OOM kill (137)
    assert st2.exit_code == 143 and st2.message and "hard timeout" in st2.message


async def test_orphan_adoption_by_pid_file(tmp_path: Path) -> None:
    cmd = _script(tmp_path, "import time; time.sleep(30)")
    first = LocalProcessExecutor(
        command=cmd, bootstrap_url_template=TEMPLATE, workdir=tmp_path / "w", cancel_grace_s=1
    )
    h = await first.submit(_submit(), NONCE)
    # a new orchestrator process (fresh executor) finds the child by nonce
    second = LocalProcessExecutor(
        command=cmd, bootstrap_url_template=TEMPLATE, workdir=tmp_path / "w", cancel_grace_s=1
    )
    adopted = await second.adopt(NONCE)
    assert adopted is not None and adopted.external_id == h.external_id
    assert (await second.status(adopted)).state is ExecState.RUNNING
    assert await second.adopt("d" * 64) is None
    await second.cancel(adopted)
    await _wait_done(first, h)
    st = await second.status(adopted)
    assert st.state is ExecState.UNKNOWN  # exit code of an adopted child is unavailable
    assert (
        await second.status(Handle(executor="local-process", run_id="r", nonce="e" * 64))
    ).state is ExecState.UNKNOWN
    await first.aclose()


async def test_cancel_returns_at_once_and_escalates_in_the_background(tmp_path: Path) -> None:
    """The reconciler cancels inline in its serial pass: cancel() must not wait out the
    grace period (that would stall every other run), but SIGKILL still follows it."""
    import time

    stubborn = """
    import signal, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print("ready", flush=True)
    time.sleep(60)
    """
    ex = LocalProcessExecutor(
        command=_script(tmp_path, stubborn),
        bootstrap_url_template=TEMPLATE,
        workdir=tmp_path / "w",
        cancel_grace_s=1.5,
    )
    h = await ex.submit(_submit(), NONCE)
    await asyncio.sleep(0.5)
    started = time.monotonic()
    await ex.cancel(h)
    await ex.cancel(h)  # repeated by later reconcile passes: still no wait
    assert time.monotonic() - started < 0.5
    assert (await ex.status(h)).state is ExecState.RUNNING  # SIGTERM ignored for now
    await _wait_done(ex, h, limit=10)
    st = await ex.status(h)
    assert st.state is ExecState.CANCELLED and st.exit_code == 137
    await ex.aclose()


async def test_exit_is_reported_only_after_the_consumer_drained_the_events(
    tmp_path: Path,
) -> None:
    """A chatty worker can leave a long event backlog behind its exit; until the ingestor has
    processed it (the ``complete`` event is last), the execution must not look finished, or
    the reconciler's complete-event grace runs out on a successful run."""
    from mapi_spec.run import LogPayload

    n = 40
    lines = [
        encode_event_line(make_event("run_1", 1, i, LogPayload(stream="stdout", line=f"l{i}")))
        for i in range(n)
    ]
    cmd = _script(
        tmp_path,
        f"""
        import sys
        for line in {lines!r}:
            print(line)
        sys.exit(0)
        """,
    )
    ex = LocalProcessExecutor(command=cmd, bootstrap_url_template=TEMPLATE, workdir=tmp_path / "w")
    h = await ex.submit(_submit(), NONCE)
    source = ex.event_source()
    first = await asyncio.wait_for(source.__anext__(), 5)  # the ingestor is consuming
    consumed = 1
    assert first.seq == 0
    await asyncio.sleep(0.5)  # the process has exited by now; the backlog is still queued
    while consumed < n:
        assert not (await ex.status(h)).state.terminal, f"terminal after {consumed}/{n} events"
        await asyncio.wait_for(source.__anext__(), 5)  # slow ingest: one at a time
        consumed += 1
    # the last event was handed out; it counts as processed once the consumer comes back
    assert not (await ex.status(h)).state.terminal
    nxt = asyncio.ensure_future(source.__anext__())
    await asyncio.sleep(0.05)
    assert (await ex.status(h)).state is ExecState.SUCCEEDED
    nxt.cancel()
    await ex.aclose()
