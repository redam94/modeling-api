"""Helpers that write RunSpecs to the GCS fake and run the real ``mapi-runner`` process."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from mapi_spec.executor import RunEvent, RunEventType, RunKind
from mapi_spec.loader import spec_hash
from mapi_spec.resources import ResourceClass
from mapi_spec.run import (
    OutputManifest,
    RunLimits,
    RunnerEnv,
    RunSpec,
    decode_event_line,
    new_hmac_key,
    new_nonce,
    verify_event,
)
from mapi_spec.storage import run_checkpoint_prefix, run_prefix

from fakegcs import FakeGcs

TENANT = "default"


@dataclass
class Run:
    """A RunSpec written to the fake's bootstrap bucket, plus how to start its runner."""

    gcs: FakeGcs
    rs: RunSpec
    env: dict[str, str]
    prefix: str

    @property
    def key(self) -> bytes:
        return self.rs.hmac_key_bytes()


@dataclass
class Result:
    code: int
    lines: list[str]
    events: list[RunEvent]
    stderr: str
    run: Run
    store: FakeGcs = field(repr=False)

    def of(self, kind: RunEventType | str) -> list[RunEvent]:
        return [e for e in self.events if e.type == RunEventType(kind)]

    @property
    def last(self) -> RunEvent:
        return self.events[-1]

    def manifest(self) -> OutputManifest:
        raw = self.store.get(self.store.buckets.runs, self.run.prefix + "manifest.json")
        return OutputManifest.model_validate_json(raw)

    def output(self, name: str) -> bytes:
        return self.store.get(self.store.buckets.runs, self.run.prefix + name)


def make_run(
    gcs: FakeGcs,
    kind: RunKind | str,
    spec: Mapping[str, Any] | None = None,
    *,
    inputs: Mapping[str, tuple[str, str]] | None = None,
    resume: Mapping[str, tuple[str, str]] | None = None,
    run_id: str | None = None,
    attempt: int = 1,
    checkpoints: bool = False,
    limits: Mapping[str, Any] | None = None,
    deadline_s: float = 900,
    input_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> Run:
    run_id = run_id or "run_" + secrets.token_hex(8)
    nonce = new_nonce()
    runs = gcs.buckets.runs
    prefix = run_prefix(TENANT, run_id, attempt)
    outputs: dict[str, Any] = {
        "manifest": gcs.sign_put(runs, prefix + "manifest.json"),
        "artifacts": gcs.post_policy(runs, prefix),
    }
    ckpt_prefix = None
    if checkpoints:
        ckpt_prefix = run_checkpoint_prefix(TENANT, run_id, attempt)
        outputs["checkpoints"] = gcs.post_policy(runs, ckpt_prefix)
    signed_inputs = {}
    for name, (bucket, obj) in (inputs or {}).items():
        get = gcs.sign_get(bucket, obj)
        if input_overrides and name in input_overrides:
            get = get.model_copy(update=dict(input_overrides[name]))
        signed_inputs[name] = get
    now = datetime.now(UTC)
    body = dict(spec or {})
    rs = RunSpec(
        run_id=run_id,
        attempt=attempt,
        nonce=nonce,
        kind=RunKind(kind),
        tenant_id=TENANT,
        project_id=TENANT,
        spec=body,
        spec_hash=spec_hash(body),
        catalog_digest="sha256:" + "0" * 64,
        inputs=signed_inputs,
        outputs=outputs,
        resume={n: gcs.sign_get(b, o) for n, (b, o) in (resume or {}).items()},
        checkpoint_prefix=ckpt_prefix,
        hmac_key=new_hmac_key(),
        limits=RunLimits.for_class(ResourceClass.S, **dict(limits or {})),
        resource_class=ResourceClass.S,
        deadline=now + timedelta(seconds=deadline_s),
        created_at=now,
    )
    gcs.put(gcs.buckets.bootstrap, nonce, rs.model_dump_json().encode(), "application/json")
    env = RunnerEnv(
        run_id=run_id,
        nonce=nonce,
        bootstrap_url=gcs.bootstrap_template(),
        exec_token=gcs.exec_token,
    ).to_environ()
    return Run(gcs=gcs, rs=rs, env=env, prefix=prefix)


def base_env() -> dict[str, str]:
    keep = ("PATH", "HOME", "LANG", "TMPDIR", "PYTENSOR_FLAGS", "NUMBA_CACHE_DIR")
    return {k: os.environ[k] for k in keep if k in os.environ}


def run_supervisor(
    run: Run,
    *,
    args: tuple[str, ...] = (),
    registry: str | None = None,
    timeout: float = 600,
    on_event: Callable[[RunEvent, subprocess.Popen[bytes]], None] | None = None,
    env: Mapping[str, str] | None = None,
) -> Result:
    argv = [sys.executable, "-m", "mapi_runner", *args]
    if registry:
        argv += ["--worker-registry", registry]
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**base_env(), **run.env, **(env or {})},
    )
    lines: list[str] = []
    events: list[RunEvent] = []
    err: list[bytes] = []

    def read_err() -> None:
        assert proc.stderr is not None
        err.append(proc.stderr.read())

    t = threading.Thread(target=read_err, daemon=True)
    t.start()
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.decode().rstrip("\n")
        lines.append(line)
        ev = decode_event_line(line)
        if ev is not None:
            events.append(ev)
            if on_event is not None:
                on_event(ev, proc)
    code = proc.wait(timeout=timeout)
    t.join(timeout=10)
    return Result(code, lines, events, b"".join(err).decode(errors="replace"), run, run.gcs)


def assert_signed_stream(result: Result) -> None:
    """Every stdout line is a valid event of this run, signed, with seq 0, 1, 2, ..."""
    assert result.lines, "the supervisor wrote no events"
    assert len(result.events) == len(result.lines), "stdout holds something other than events"
    for i, ev in enumerate(result.events):
        assert ev.run_id == result.run.rs.run_id
        assert ev.attempt == result.run.rs.attempt
        assert ev.seq == i
        assert verify_event(ev, result.run.key), f"event {i} fails its HMAC"
