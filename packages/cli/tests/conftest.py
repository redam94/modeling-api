"""CLI test fixtures: an isolated environment and a CliRunner wrapper."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import respx
from mapi_cli.main import app
from pydantic import BaseModel
from typer.testing import CliRunner, Result


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in ("MAPI_URL", "MAPI_API_URL", "MAPI_TOKEN", "MAPI_PROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("MAPI_CONFIG", str(cfg))
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("NO_COLOR", "1")
    return cfg


Invoke = Callable[..., Result]


@pytest.fixture
def mapi() -> Invoke:
    """``mapi(*args)`` runs the CLI in-process and returns click's Result."""
    runner = CliRunner()

    def invoke(*args: str, input: str | None = None) -> Result:
        return runner.invoke(app, list(args), input=input, catch_exceptions=False)

    return invoke


def stdout_json(result: Result) -> Any:
    return json.loads(result.stdout)


@pytest.fixture
def as_json() -> Callable[[Result], Any]:
    return stdout_json


def _example(model: type[BaseModel], index: int = 0, **changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(model.model_json_schema()["examples"][index])
    data.update(changes)
    return data


@pytest.fixture
def example() -> Callable[..., dict[str, Any]]:
    return _example


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def lines(result: Result) -> Sequence[str]:
    return result.output.splitlines()
