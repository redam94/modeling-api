"""DockerExecutor against a real daemon (marked ``docker``; auto-skipped without one)."""

from __future__ import annotations

import asyncio

import pytest
from mapi_server.executors import DockerExecutor
from mapi_spec._types import Duration
from mapi_spec.executor import ExecState, RunKind, RunSubmit
from mapi_spec.run import RunnerPhase, StatePayload, encode_event_line, make_event

pytestmark = pytest.mark.docker

TEMPLATE = "http://127.0.0.1:4443/storage/v1/b/mapi-local-bootstrap/o/{nonce}"


async def test_container_runs_locked_down_and_reports_events() -> None:
    line = encode_event_line(make_event("run_d", 1, 0, StatePayload(phase=RunnerPhase.BOOTSTRAP)))
    script = (
        "import os, sys\n"
        "assert os.environ['MAPI_RUN_ID'] == 'run_d'\n"
        "try:\n    open('/etc/x', 'w')\nexcept OSError:\n    pass\n"
        "else:\n    sys.exit(3)\n"
        f"print({line!r})\n"
    )
    ex = DockerExecutor(
        image="python:3.12-slim",
        bootstrap_url_template=TEMPLATE,
        command=["python", "-c", script],
    )
    rs = RunSubmit(run_id="run_d", attempt=1, kind=RunKind.FIT, timeout=Duration(120))
    nonce = "f" * 64
    h = await ex.submit(rs, nonce)
    try:
        event = await asyncio.wait_for(ex.event_source().__anext__(), 120)
        assert event.run_id == "run_d"
        for _ in range(240):
            st = await ex.status(h)
            if st.state.terminal:
                break
            await asyncio.sleep(0.5)
        assert st.state is ExecState.SUCCEEDED and st.exit_code == 0
        assert await ex.adopt(nonce) is not None
    finally:
        container = ex.client.containers.get(h.external_id)
        container.remove(force=True)
