"""Shared fixtures: an isolated config environment, a respx router and DTO examples."""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from mapi_client import Client, ClientConfig
from pydantic import BaseModel

BASE = "http://api.test"
STORAGE = "http://storage.test"


def _example(model: type[BaseModel], index: int = 0, **changes: Any) -> dict[str, Any]:
    """A DTO's validated schema example (deep-copied) with some fields replaced."""
    data = copy.deepcopy(model.model_json_schema()["examples"][index])
    data.update(changes)
    return data


@pytest.fixture
def example() -> Callable[..., dict[str, Any]]:
    return _example


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Never read the developer's real config file or MAPI_* variables."""
    for name in ("MAPI_URL", "MAPI_API_URL", "MAPI_TOKEN", "MAPI_PROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "mapi-config.toml"
    monkeypatch.setenv("MAPI_CONFIG", str(path))
    return path


class Sleeps(list[float]):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


@pytest.fixture
def client(api: respx.MockRouter, sleeps: Sleeps) -> Iterator[Client]:
    with Client(config=ClientConfig(base_url=BASE, max_retries=2), sleep=sleeps) as c:
        yield c
