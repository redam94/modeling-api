"""The v0.5 documentation's Python client walkthrough (``docs/snippets/client_v05.py``) and
the workflow cookbook's canonical document (``docs/snippets/cookbook_readiness_arimax.yaml``)
run as published against a real server over HTTP: embedded Postgres, the storage emulator
front, and the workflow's children executed in-process while the snippet waits.

The only edit is the project name: the pages use ``growth``, the test server ``default``.
"""

from __future__ import annotations

import asyncio
import os
import runpy
from pathlib import Path

import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.settings import Settings
from mapi_server_testkit.builders import EXEC_TOKEN
from mapi_server_testkit.client import serve_api
from mapi_server_testkit.store_http import serve_store
from mapi_server_testkit.workflows import catalog_file, drive, put_dataset
from mapi_spec.workflow import WorkflowRunStatus
from mapi_workflows.testing import weekly_series
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.filterwarnings("ignore")]

DOCS = Path(__file__).resolve().parents[3] / "docs" / "snippets"


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(update={"catalog_path": catalog_file(tmp_path)})


def _localise(text: str) -> str:
    return text.replace("project: growth", "project: default").replace(
        "dataset://growth/", "dataset://default/"
    )


async def _drive_workflow_runs(ctx: ServerContext, stop: asyncio.Event) -> list[str]:
    """Play the orchestrator and the runner for every workflow run the snippet starts."""
    driven: list[str] = []
    while not stop.is_set():
        async with ctx.db.session() as s:
            rows = (
                await s.execute(
                    select(m.WorkflowRunRow.run_id).where(
                        m.WorkflowRunRow.status.in_(("queued", "running"))
                    )
                )
            ).scalars()
            pending = [r for r in rows if r not in driven]
        for run_id in pending:
            assert await drive(ctx, run_id, max_rounds=80) is WorkflowRunStatus.SUCCEEDED
            driven.append(run_id)
        await asyncio.sleep(0.05)
    return driven


async def test_the_client_walkthrough_runs_as_published(
    ctx: ServerContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await put_dataset(ctx, "weekly_sales", weekly_series(156, random_walk=True, beta=3.0))
    work = tmp_path / "work"
    work.mkdir()
    yaml_text = (DOCS / "cookbook_readiness_arimax.yaml").read_text("utf-8")
    (work / "weekly-readiness.yaml").write_text(_localise(yaml_text), "utf-8")
    script = work / "client_v05.py"
    script.write_text(_localise((DOCS / "client_v05.py").read_text("utf-8")), "utf-8")
    for name in ("MAPI_TOKEN", "MAPI_PROFILE", "MAPI_PROJECT", "MAPI_API_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAPI_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.chdir(work)
    async with (
        serve_store(
            ctx.store,  # type: ignore[arg-type]
            exec_token=EXEC_TOKEN,
            bootstrap_bucket=ctx.buckets.bootstrap,
        ),
        serve_api(ctx) as url,
    ):
        monkeypatch.setenv("MAPI_URL", url)
        stop = asyncio.Event()
        driver = asyncio.create_task(_drive_workflow_runs(ctx, stop))
        try:
            await asyncio.to_thread(runpy.run_path, os.fspath(script), run_name="__main__")
        finally:
            stop.set()
            driven = await driver
    assert len(driven) == 1
    assert (work / "evidence.html").stat().st_size > 1000
