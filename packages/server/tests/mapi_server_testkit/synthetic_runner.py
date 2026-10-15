"""A protocol-following stand-in supervisor for executor conformance and load tests.

The same coroutine (:func:`supervise`) runs **in-process** as a
:class:`~mapi_server.executors.fake_cloudrun.FakeCloudRun` workload (stdout lines become Cloud
Logging entries → Pub/Sub pushes) and **as a subprocess** of the ``local-process`` executor
(``python synthetic_runner.py``; stdout is the event pipe). It reads ``MAPI_RUN_ID`` /
``MAPI_NONCE`` / ``MAPI_BOOTSTRAP_URL`` (and the local-only ``MAPI_EXEC_TOKEN``), GETs and
DELETEs its bootstrap RunSpec (per task when ``CLOUD_RUN_TASK_COUNT`` > 1), prints
HMAC-signed events, uploads outputs through the RunSpec's signed POST policy, PUTs a signed
manifest and emits ``complete`` — or misbehaves as the run label ``test.mapi/mode`` says:

``ok`` · ``fail_user`` (exit 10) · ``oom`` (exit 137) · ``hang`` (until SIGTERM → 143) ·
``hang_first`` (attempt 1 hangs, later attempts succeed) · ``gate`` (pauses after its first
events until ``$SYNTH_GATE_DIR/<run id>`` exists) · ``checkpoint`` (attempt 1 uploads a
checkpoint and waits for SIGTERM, then flushes a partial bundle and exits 16; attempts with
``resume`` inputs complete) · ``fanout`` (each task writes ``tasks/{i}/result.json``) ·
``sleep`` (sleeps ``test.mapi/sleep-ms`` before completing).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from mapi_spec.errors import ErrorCode
from mapi_spec.run import (
    OUTPUT_ARTIFACTS,
    OUTPUT_CHECKPOINTS,
    OUTPUT_MANIFEST,
    CheckpointPayload,
    CompletePayload,
    ErrorPayload,
    FailureClass,
    InputDigest,
    LogPayload,
    ManifestObject,
    MetricPayload,
    OutputManifest,
    ProgressPayload,
    RunnerEnv,
    RunnerPhase,
    RunSpec,
    StatePayload,
    encode_event_line,
    make_event,
    sign_output_manifest,
)
from mapi_spec.storage import PostPolicy, SignedPut, crc32c, crc32c_to_base64, md5_base64

try:  # imported as part of the test kit
    from .fake_runner import outputs_for
except ImportError:  # run as a script by the local-process executor
    from fake_runner import outputs_for  # type: ignore[no-redef]

from mapi_server.executors.tasks import TaskEnv, task_seq

__all__ = ["GATE_ENV", "MODE_LABEL", "SLEEP_LABEL", "Supervisor", "supervise"]

MODE_LABEL = "test.mapi/mode"
SLEEP_LABEL = "test.mapi/sleep-ms"
GATE_ENV = "SYNTH_GATE_DIR"

Emit = Callable[[str], Awaitable[None]]


class Supervisor:
    def __init__(self, rs: RunSpec, emit: Emit, http: httpx.AsyncClient, task: TaskEnv) -> None:
        self.rs = rs
        self._emit = emit
        self.http = http
        self.task = task
        self.seq = 0

    async def event(self, payload: Any) -> None:
        seq = task_seq(self.task.index, self.seq)
        ev = make_event(self.rs.run_id, self.rs.attempt, seq, payload, key=self.rs.hmac_key)
        self.seq += 1
        await self._emit(encode_event_line(ev))

    async def post(self, output: str, rel: str, data: bytes, ctype: str) -> ManifestObject:
        policy = self.rs.outputs[output]
        assert isinstance(policy, PostPolicy)
        key = policy.prefix + rel
        r = await self.http.post(
            policy.url, data={**policy.fields, "key": key}, files={"file": (rel, data, ctype)}
        )
        r.raise_for_status()
        return ManifestObject(
            key=key,
            size=len(data),
            crc32c=crc32c_to_base64(crc32c(data)),
            md5=md5_base64(data),
            content_type=ctype,
        )

    async def finish(self, mode: str) -> int:
        """Upload the outputs, the signed manifest and emit ``complete``."""
        rs = self.rs
        await self.event(MetricPayload(name="rmse", value=0.25))
        objects: dict[str, ManifestObject] = {}
        for name, (data, ctype) in outputs_for(rs, mode).items():
            objects[name] = await self.post(OUTPUT_ARTIFACTS, name, data, ctype)
        status = "partial" if mode == "partial" else "complete"
        manifest = OutputManifest(
            tenant_id=rs.tenant_id,
            project_id=rs.project_id,
            run_id=rs.run_id,
            attempt=rs.attempt,
            kind=rs.kind,
            status=status,
            prefix=rs.outputs[OUTPUT_ARTIFACTS].prefix,  # type: ignore[union-attr]
            spec_hash=rs.spec_hash,
            catalog_digest=rs.catalog_digest,
            inputs={
                n: InputDigest(generation=g.generation, size=g.size, crc32c=g.crc32c)
                for n, g in rs.inputs.items()
            },
            objects=objects,
            runner_version="synthetic-0",
            lib_versions={"synthetic": "0"},
            created_at=datetime.now(UTC),
        )
        body = sign_output_manifest(manifest, rs.hmac_key).model_dump_json().encode()
        put = rs.outputs[OUTPUT_MANIFEST]
        assert isinstance(put, SignedPut)
        r = await self.http.put(put.url, content=body, headers=put.headers)
        r.raise_for_status()
        await self.event(
            CompletePayload(
                status=status,
                manifest_name=put.name,
                manifest_crc32c=crc32c_to_base64(crc32c(body)),
                objects=len(objects),
                total_bytes=sum(o.size for o in objects.values()),
            )
        )
        return 16 if status == "partial" else 0


async def _gate(rs: RunSpec, gate_dir: str | None, sigterm: asyncio.Event) -> bool:
    """Wait for ``$SYNTH_GATE_DIR/<run id>``; ``False`` if SIGTERM came first."""
    path = Path(gate_dir or ".") / rs.run_id
    while not path.exists():
        if sigterm.is_set():
            return False
        await asyncio.sleep(0.02)
    return True


async def supervise(
    env: Mapping[str, str],
    *,
    emit: Emit,
    sigterm: asyncio.Event,
    http: httpx.AsyncClient,
) -> int:
    """Run one task of one attempt; returns the supervisor exit code."""
    try:
        renv = RunnerEnv.from_environ(env)
        task = TaskEnv.from_environ(env)
    except ValueError:
        return 2
    url = renv.bootstrap_url.replace("{nonce}", task.bootstrap_nonce())
    headers = {"Authorization": f"Bearer {renv.exec_token}"} if renv.exec_token else {}
    r = await http.get(url, params={"alt": "media"}, headers=headers)
    if r.status_code != 200:
        return 2  # no bootstrap object: a duplicate or stale execution does no work
    await http.delete(url, headers=headers)
    rs = RunSpec.model_validate_json(r.content)
    if rs.run_id != renv.run_id or rs.nonce != task.bootstrap_nonce():
        return 2
    mode = rs.labels.get(MODE_LABEL, "ok")
    if mode == "hang_first":
        mode = "hang" if rs.attempt == 1 else "ok"
    sup = Supervisor(rs, emit, http, task)
    await sup.event(StatePayload(phase=RunnerPhase.BOOTSTRAP))
    await sup.event(ProgressPayload(phase=RunnerPhase.FIT, fraction=0.5, message="halfway"))
    await sup.event(LogPayload(stream="stderr", line=f"task {task.index}/{task.count}"))
    if mode == "hang":
        await sigterm.wait()
        return 143
    if mode == "oom":
        return 137
    if mode == "fail_user":
        await sup.event(
            ErrorPayload(
                code=ErrorCode.USER_CODE_ERROR,
                failure_class=FailureClass.USER,
                message="the model plugin raised an exception",
                untrusted_text="Traceback (most recent call last):\n  ValueError: boom",
            )
        )
        return 10
    if mode == "gate":
        if not await _gate(rs, env.get(GATE_ENV), sigterm):
            return 143
        await sup.event(ProgressPayload(phase=RunnerPhase.FIT, fraction=0.9, message="resumed"))
    if mode == "sleep":
        await asyncio.sleep(int(rs.labels.get(SLEEP_LABEL, "0")) / 1000)
    if mode == "fanout":
        result = {
            "task": task.index,
            "count": task.count,
            "run": rs.run_id,
            "child": rs.labels.get("test.mapi/child"),
        }
        data = json.dumps(result).encode()
        await sup.post(
            OUTPUT_ARTIFACTS, f"tasks/{task.index}/result.json", data, "application/json"
        )
        return 0
    if mode == "checkpoint":
        if rs.resume:  # a resumed attempt: the earlier segment is reused, not redone
            await sup.event(LogPayload(stream="stdout", line=f"resuming from {sorted(rs.resume)}"))
            return await sup.finish("ok")
        if OUTPUT_CHECKPOINTS not in rs.outputs:
            return 12
        blob = b"segment-0" * 64
        obj = await sup.post(OUTPUT_CHECKPOINTS, "segment-0000.zarr.zip", blob, "application/zip")
        await sup.event(
            CheckpointPayload(
                name="segment-0000.zarr.zip", size=obj.size, crc32c=obj.crc32c, segment=0
            )
        )
        await sigterm.wait()
        return await sup.finish("partial")
    return await sup.finish("ok")


def main() -> int:
    """``python synthetic_runner.py`` (the local-process executor's supervisor command)."""

    async def run() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, stop.set)

        async def emit(line: str) -> None:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

        async with httpx.AsyncClient(timeout=30) as http:
            return await supervise(os.environ, emit=emit, sigterm=stop, http=http)

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
