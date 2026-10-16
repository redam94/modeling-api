"""The v0.5 ``mapi`` commands against a real server over HTTP (uvicorn on a free port,
embedded Postgres, child runs executed in-process): validate, apply, run and watch a
workflow (the live event stream over a real socket), Help me choose, About cards and
governance policies - exactly as a user types them."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from mapi_cli.main import app as cli
from mapi_server.context import ServerContext
from mapi_server.settings import Settings
from mapi_server_testkit.client import serve_api
from mapi_server_testkit.workflows import catalog_file, drive, put_dataset
from mapi_spec.workflow import WorkflowRunStatus
from mapi_workflows.testing import weekly_series
from typer.testing import CliRunner, Result

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

DOC = """\
apiVersion: mapi/v1
kind: Workflow
metadata: {name: readiness, project: default}
inputs:
  data: {type: dataset}
nodes:
  - {id: adf, use: test.adf, inputs: {data: inputs.data}}
  - id: wanders
    use: branch.if
    with: {condition: "not nodes.adf.reject"}
  - {id: diff, use: transform.difference, inputs: {data: inputs.data}, when: wanders.then}
  - {id: kpss, use: test.kpss, inputs: {data: [diff.data, inputs.data]}}
  - {id: report, use: report.summary, inputs: {tests: [adf.result, kpss.result]}}
"""


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(update={"catalog_path": catalog_file(tmp_path)})


@pytest.fixture(autouse=True)
def isolated_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    for name in ("MAPI_URL", "MAPI_API_URL", "MAPI_TOKEN", "MAPI_PROFILE", "MAPI_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAPI_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("NO_COLOR", "1")
    yield


@pytest.fixture
async def live(ctx: ServerContext) -> AsyncIterator[str]:
    async with serve_api(ctx) as url:
        yield url


async def _mapi(url: str, *args: str, input: str | None = None) -> Result:
    runner = CliRunner()
    return await asyncio.to_thread(
        runner.invoke, cli, ["--url", url, *args], input=input, catch_exceptions=False
    )


async def test_a_workflow_from_the_command_line(
    live: str, ctx: ServerContext, tmp_path: Path
) -> None:
    await put_dataset(ctx, "sales", weekly_series(120, random_walk=True))
    f = tmp_path / "readiness.yaml"
    f.write_text(DOC, "utf-8")
    res = await _mapi(live, "workflows", "validate", str(f))
    assert res.exit_code == 0, res.output
    assert "adf → " in res.stdout
    res = await _mapi(live, "workflows", "apply", str(f), "-m", "first")
    assert res.exit_code == 0, res.output
    assert "workflow://default/readiness at revision 1" in res.stdout
    res = await _mapi(live, "workflows", "apply", str(f))  # unchanged: still revision 1
    assert "at revision 1" in res.stdout
    res = await _mapi(
        live, "--json", "workflows", "run", "readiness", "-i", "data=dataset://default/sales@latest"
    )
    assert res.exit_code == 0, res.output
    run_id = json.loads(res.stdout)["run_id"]
    assert await drive(ctx, run_id) is WorkflowRunStatus.SUCCEEDED
    res = await _mapi(live, "workflows", "watch", run_id)
    assert res.exit_code == 0, res.output
    assert "wanders took the then arm" in res.stderr  # from the live event stream
    assert "succeeded" in res.stdout and "Decisions" in res.stdout
    res = await _mapi(live, "workflows", "nodes", run_id)
    assert "transform.difference" in res.stdout
    res = await _mapi(live, "--json", "workflows", "runs", "readiness")
    assert [r["run_id"] for r in json.loads(res.stdout)] == [run_id]


async def test_choose_about_and_policies_from_the_command_line(live: str) -> None:
    res = await _mapi(
        live, "--json", "choose", "-a", "goal=forecast", "-a", "data_shape=one_series"
    )
    assert res.exit_code == 0, res.output
    ranked = json.loads(res.stdout)["items"]
    assert ranked and all(r["about"]["summary_plain"] for r in ranked)
    answers = "3\n1\n" + "\n" * 6  # forecast, one series, skip the rest
    res = await _mapi(live, "choose", "--about", "1", input=answers)
    assert res.exit_code == 0, res.output
    assert "Recommended for you" in res.stdout and "Use it when" in res.stdout
    res = await _mapi(live, "about", "ts.sarimax", "--expert")
    assert res.exit_code == 0 and "Example:" in res.stdout
    res = await _mapi(live, "--json", "governance", "policies", "ls")
    assert {p["scope"] for p in json.loads(res.stdout)} == {"tenant"}
    res = await _mapi(live, "governance", "checks", "--about", "null.permutation_target")
    assert res.exit_code == 0 and "shuffled" in res.stdout.lower()
