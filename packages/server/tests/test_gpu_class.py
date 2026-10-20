"""v0.15 resource class GPU on the server: placement per executor (Cloud Run GPU job for dl
models, docker device requests only with MAPI_DOCKER_GPUS, CPU fallback for local
processes), the dl plugin bases, sizing that never picks GPU, and metering of GPU attempts."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from mapi_server import __version__
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.executors import PlacementRefused, build_executor, check_placement
from mapi_server.executors.cloudrun import DEFAULT_PLUGIN_BASES, Base
from mapi_server.executors.docker import DockerExecutor
from mapi_server.executors.local_process import LocalProcessExecutor
from mapi_server.services.cost import bill_attempts
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.runs import load_run, now
from mapi_server.services.specs import class_for_size
from mapi_server.settings import DEFAULT_CLOUDRUN_BASES, ExecutorName, Settings
from mapi_server_testkit.builders import queued_fit
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_spec.cost import DEFAULT_PRICE_TABLE, ledger_entry
from mapi_spec.executor import PLUGIN_LABEL, RunKind, RunSubmit
from mapi_spec.resources import RESOURCE_CLASSES, ResourceClass
from sqlalchemy import select

TEMPLATE = "http://127.0.0.1:4443/storage/v1/b/mapi-local-bootstrap/o/{nonce}"


def _rs(rc: ResourceClass = ResourceClass.GPU, plugin: str = "dl") -> RunSubmit:
    return RunSubmit(
        run_id="run_1", attempt=1, kind=RunKind.FIT, resource_class=rc,
        labels={PLUGIN_LABEL: plugin},
    )  # fmt: skip


def test_docker_requests_a_gpu_only_when_enabled() -> None:
    images = {"dl": "mapi/base-dl:9"}
    cpu = DockerExecutor(image="img:1", images=images, bootstrap_url_template=TEMPLATE)
    gpu = DockerExecutor(image="img:1", images=images, bootstrap_url_template=TEMPLATE, gpus=True)
    for ex in (cpu, gpu):
        assert ResourceClass.GPU in ex.caps().classes
        check_placement(ex.caps(), trust_tier="internal", resource_class=ResourceClass.GPU)
    spec = RESOURCE_CLASSES[ResourceClass.GPU]
    opts = cpu.container_options(_rs(), "a" * 64)
    assert "device_requests" not in opts  # CPU fallback
    assert opts["image"] == "mapi/base-dl:9"
    assert opts["mem_limit"] == spec.ram_bytes and opts["nano_cpus"] == 8_000_000_000
    opts = gpu.container_options(_rs(), "a" * 64)
    assert opts["device_requests"] == [{"Driver": "nvidia", "Count": 1, "Capabilities": [["gpu"]]}]
    # other classes never get a device, even with GPUs enabled
    assert "device_requests" not in gpu.container_options(_rs(ResourceClass.M), "a" * 64)
    from docker.types import DeviceRequest

    request = DeviceRequest(**opts["device_requests"][0])
    assert request.driver == "nvidia" and request.count == 1


def test_docker_gpus_come_from_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    base = {"database_url": "postgresql://x/y", "executor": ExecutorName.DOCKER,
            "docker_network": "container:mapi-gcs-shim"}  # fmt: skip
    assert Settings(**base).docker_gpus is False  # type: ignore[arg-type]
    monkeypatch.setenv("MAPI_DOCKER_GPUS", "true")
    settings = Settings(**base)  # type: ignore[arg-type]
    assert settings.docker_gpus is True
    ex = build_executor(settings)
    assert isinstance(ex, DockerExecutor)
    assert "device_requests" in ex.container_options(_rs(), "a" * 64)
    assert settings.docker_images["dl"] == f"mapi/base-dl:{__version__}"


def test_the_cloud_run_gpu_job_comes_from_the_settings() -> None:
    from mapi_server.executors import cloudrun_inventory

    base = {"database_url": "postgresql://x/y", "cloudrun_project": "mapi-test"}
    off = cloudrun_inventory(Settings(**base))  # type: ignore[arg-type]
    assert off.gpu_bases == () and ResourceClass.GPU not in off.offered_classes
    on = cloudrun_inventory(Settings(**base, cloudrun_gpu_jobs=True))  # type: ignore[arg-type]
    assert on.gpu_bases == (Base.DL,) and on.jobs()[-1].endswith("/mapi-local-base-dl-gpu")


def test_local_processes_run_the_gpu_class_on_the_cpu(tmp_path: Path) -> None:
    ex = LocalProcessExecutor(command=["x"], bootstrap_url_template=TEMPLATE, workdir=tmp_path)
    check_placement(ex.caps(), trust_tier="internal", resource_class=ResourceClass.GPU)
    with pytest.raises(PlacementRefused):
        check_placement(ex.caps(), trust_tier="internal", resource_class=ResourceClass.XL)


def test_dl_and_sci_models_have_their_bases() -> None:
    assert DEFAULT_PLUGIN_BASES["dl"] is Base.DL and DEFAULT_PLUGIN_BASES["sci"] is Base.BAYES
    # the Cloud Run settings mirror the executor's table
    assert {k: v.value for k, v in DEFAULT_PLUGIN_BASES.items()} == DEFAULT_CLOUDRUN_BASES
    assert Settings(database_url="postgresql://x/y").cloudrun_plugin_bases["dl"] == "base-dl"


def test_sizing_never_picks_the_gpu_class() -> None:
    for size in (0, 1, 10**9, RESOURCE_CLASSES[ResourceClass.L].max_dataset_bytes):
        assert class_for_size(size) not in (ResourceClass.GPU, None)
    gpu_cap = RESOURCE_CLASSES[ResourceClass.GPU].max_dataset_bytes
    assert class_for_size(gpu_cap) is not ResourceClass.GPU


@pytest.mark.integration
async def test_a_gpu_attempt_is_metered_with_its_accelerator(ctx: ServerContext) -> None:
    run_id = await queued_fit(ctx)
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        run.resource_class = ResourceClass.GPU.value
        att = await s.get(m.RunAttempt, (run_id, run.attempt))
        assert att is not None
        att.submitted_at = now() - timedelta(minutes=10)
        att.finished_at = att.submitted_at + timedelta(seconds=600)
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id)
        [entry] = await bill_attempts(s, run)
    expected = ledger_entry(
        tenant_id=run.tenant_id, project_id=run.project_id, run_id=run.id, attempt=1,
        resource_class=ResourceClass.GPU, wall_seconds=600.0, recorded_at=entry.recorded_at,
    )  # fmt: skip
    assert entry.resource_class is ResourceClass.GPU
    assert entry.amount_micros == expected.amount_micros
    # the L4 second is priced on top of 8 vCPU and 32 GiB
    gpu = DEFAULT_PRICE_TABLE.price(ResourceClass.GPU)
    assert gpu.accelerator_second_nanos > 0
    cpu_only = (8 * 600 * gpu.vcpu_second_nanos + 32 * 600 * gpu.gib_second_nanos) // 1000
    assert entry.amount_micros == cpu_only + 600 * gpu.accelerator_second_nanos // 1000
    async with ctx.db.session() as s:
        [row] = (await s.execute(select(m.CostLedgerEntry))).scalars().all()
        assert row.resource_class == "GPU" and row.amount_micros == entry.amount_micros
