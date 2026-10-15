"""The in-process executor implements the Executor protocol end to end."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.executors import InprocExecutor, mapping_resolver
from mapi_sdk.registry import Registry
from mapi_sdk.wire import write_zarr_zip
from mapi_spec import ModelSpec
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import (
    ExecState,
    Executor,
    Handle,
    Isolation,
    RunEventType,
    RunKind,
    RunSubmit,
)

SCORE = {
    "apiVersion": "mapi/v1",
    "kind": "ScoreRequest",
    "model": "endpoint://growth/toy",
    "mode": "predict",
    "data": "dataset://growth/window@v1",
}


def _fit_submit(spec: ModelSpec, run_id: str = "fit-1") -> RunSubmit:
    return RunSubmit(
        run_id=run_id,
        kind=RunKind.FIT,
        payload={
            "spec": spec.model_dump(mode="json", by_alias=True, exclude_defaults=True),
            "side_tables": {"fx": "dataset://growth/fx_daily@latest"},
        },
    )


@pytest.fixture
def executor(
    registry: Registry, panel: tuple[xr.Dataset, xr.Dataset], tmp_path: Path
) -> InprocExecutor:
    data, fx = panel
    return InprocExecutor(
        registry=registry,
        resolver={
            "dataset://growth/eu_media_daily@latest": data,
            "dataset://growth/fx_daily@latest": fx,
            "dataset://growth/window@v1": data.isel(date=slice(-14, None)),
        },
        output_dir=tmp_path / "runs",
    )


async def test_fit_then_score_with_events(
    executor: InprocExecutor, e33_spec: Callable[..., ModelSpec], tmp_path: Path
) -> None:
    assert isinstance(executor, Executor)
    caps = executor.caps()
    assert caps.isolation is Isolation.NONE and not caps.allows_public_tenants()
    seen: list[str] = []

    async def listen() -> None:
        async for ev in executor.event_source():
            seen.append(ev.type.value)
            if ev.type in (RunEventType.COMPLETE, RunEventType.ERROR):
                return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0)
    h = await executor.submit(_fit_submit(e33_spec(metrics="rmse")), nonce="nonce-fit-0001")
    again = await executor.submit(_fit_submit(e33_spec(metrics="rmse")), nonce="nonce-fit-0001")
    assert again == h  # duplicate submit is safe
    assert await executor.adopt("nonce-fit-0001") == h
    assert await executor.adopt("nonce-unknown-000") is None
    status = await executor.wait(h, timeout=60)
    assert status.state is ExecState.SUCCEEDED and status.exit_code == 0
    await asyncio.wait_for(listener, 5)
    assert "progress" in seen and seen[-1] == "complete"
    events = executor.events(h)
    assert [e.seq for e in events] == list(range(len(events)))
    bundle = executor.result(h)
    assert isinstance(bundle, Bundle)
    assert (tmp_path / "runs" / "fit-1" / "a1" / "manifest.json").exists()
    executor.put_bundle("mem", bundle)
    sh = await executor.submit(
        RunSubmit(
            run_id="score-1", kind=RunKind.SCORE, payload={"request": SCORE, "bundle": "mem"}
        ),
        nonce="nonce-score-001",
    )
    assert (await executor.wait(sh, timeout=60)).state is ExecState.SUCCEEDED
    tree = executor.result(sh)
    assert isinstance(tree, xr.DataTree) and "summary" in tree
    dir_ref = str(tmp_path / "runs" / "fit-1" / "a1")
    yaml_req = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://g/t\nmode: predict\n"
    dh = await executor.submit(
        RunSubmit(
            run_id="score-2",
            kind=RunKind.SCORE,
            payload={"request": yaml_req, "bundle": dir_ref, "data": "dataset://growth/window@v1"},
        ),
        nonce="nonce-score-002",
    )
    assert (await executor.wait(dh, timeout=60)).state is ExecState.SUCCEEDED


async def test_failures_are_problems(
    executor: InprocExecutor, e33_spec: Callable[..., ModelSpec]
) -> None:
    bad = RunSubmit(run_id="bad-1", kind=RunKind.FIT, payload={"spec": {"kind": "Nope"}})
    h = await executor.submit(bad, nonce="nonce-bad-00001")
    status = await executor.wait(h, timeout=10)
    assert status.state is ExecState.FAILED and status.problem is not None
    assert status.exit_code == 1
    missing = RunSubmit(
        run_id="bad-2", kind=RunKind.SCORE, payload={"request": SCORE, "bundle": "/nope"}
    )
    h2 = await executor.submit(missing, nonce="nonce-bad-00002")
    st2 = await executor.wait(h2, timeout=10)
    assert st2.problem is not None and st2.problem.code is ErrorCode.REF_UNRESOLVED
    other = RunSubmit(run_id="bad-3", kind=RunKind.RENDER)
    h3 = await executor.submit(other, nonce="nonce-bad-00003")
    st3 = await executor.wait(h3, timeout=10)
    assert st3.problem is not None and st3.problem.code is ErrorCode.CAPABILITY_UNSUPPORTED
    with pytest.raises(MapiError) as err:
        await executor.status(Handle(executor="inproc", run_id="x", nonce="nonce-nope-0000"))
    assert err.value.code is ErrorCode.NOT_FOUND
    nobundle = RunSubmit(run_id="bad-4", kind=RunKind.SCORE, payload={"request": SCORE})
    h4 = await executor.submit(nobundle, nonce="nonce-bad-00004")
    assert (await executor.wait(h4, timeout=10)).state is ExecState.FAILED
    noreq = RunSubmit(run_id="bad-5", kind=RunKind.SCORE, payload={"request": 3, "bundle": "x"})
    h5 = await executor.submit(noreq, nonce="nonce-bad-00005")
    assert (await executor.wait(h5, timeout=10)).problem is not None


async def test_cancel_before_and_during(
    executor: InprocExecutor, e33_spec: Callable[..., ModelSpec]
) -> None:
    h = await executor.submit(
        _fit_submit(e33_spec(metrics="rmse"), "fit-c1"), nonce="nonce-cancel-01"
    )
    await executor.cancel(h)
    status = await executor.wait(h, timeout=60)
    assert status.state is ExecState.CANCELLED
    await executor.cancel(h)  # idempotent on terminal runs
    h2 = await executor.submit(
        _fit_submit(e33_spec(metrics="rmse"), "fit-c2"), nonce="nonce-cancel-02"
    )
    await asyncio.sleep(0)  # let it start running
    await executor.cancel(h2)
    st2 = await executor.wait(h2, timeout=60)
    assert st2.state in (ExecState.CANCELLED, ExecState.SUCCEEDED)


def test_mapping_resolver_reads_files(tmp_path: Path) -> None:
    ds = xr.Dataset({"x": (("a",), [1.0, 2.0])})
    path = tmp_path / "d.zarr.zip"
    write_zarr_zip(ds, path)
    resolve = mapping_resolver({})
    xr.testing.assert_identical(resolve(f"file://{path}"), ds)
    with pytest.raises(MapiError) as err:
        resolve("dataset://x/y@latest")
    assert err.value.code is ErrorCode.REF_UNRESOLVED
    assert InprocExecutor(resolver=lambda ref: ds).resolver("anything") is ds
