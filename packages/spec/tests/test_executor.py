"""Executor protocol shapes are pure Pydantic and round-trip through JSON."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from mapi_spec.errors import ErrorCode, Problem
from mapi_spec.executor import (
    ExecState,
    ExecStatus,
    ExecutorCaps,
    Handle,
    Isolation,
    RunEvent,
    RunEventType,
    RunKind,
    RunSubmit,
)
from mapi_spec.resources import ResourceClass
from pydantic import ValidationError


def test_caps_placement_rule() -> None:
    local = ExecutorCaps(isolation=Isolation.NONE)
    assert not local.allows_public_tenants()
    assert str(local.max_runtime) == "1h"
    cloud = ExecutorCaps(
        isolation=Isolation.MICROVM,
        egress_enforced=True,
        dns_enforced=True,
        classes=[ResourceClass.S, ResourceClass.M],
        max_runtime="24h",
    )
    assert cloud.allows_public_tenants()
    assert Isolation.MICROVM > Isolation.GVISOR
    assert ExecutorCaps.model_validate_json(cloud.model_dump_json()) == cloud


def test_submit_handle_status_event_roundtrip() -> None:
    rs = RunSubmit(run_id="run-1", kind=RunKind.FIT, payload={"spec": {"a": 1}}, timeout="45m")
    assert RunSubmit.model_validate_json(rs.model_dump_json()) == rs
    with pytest.raises(ValidationError):
        RunSubmit(run_id="bad id!", kind=RunKind.FIT)
    with pytest.raises(ValidationError):
        RunSubmit(run_id="r", kind=RunKind.FIT, extra=1)  # type: ignore[call-arg]
    h = Handle(executor="inproc", run_id="run-1", nonce="n" * 16)
    assert Handle.model_validate(h.model_dump()) == h
    with pytest.raises(ValidationError):
        Handle(executor="x", run_id="r", nonce="short")
    st = ExecStatus(state=ExecState.FAILED, problem=Problem.from_code(ErrorCode.TIMEOUT, "slow"))
    assert ExecStatus.model_validate_json(st.model_dump_json()) == st
    assert ExecState.FAILED.terminal and not ExecState.RUNNING.terminal
    ev = RunEvent(
        run_id="run-1", seq=0, type=RunEventType.PROGRESS, ts=datetime.now(UTC), payload={"f": 0.5}
    )
    assert RunEvent.model_validate_json(ev.model_dump_json()) == ev
