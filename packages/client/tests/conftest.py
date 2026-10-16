"""Shared fixtures: an isolated config environment, a respx router and DTO examples."""

from __future__ import annotations

import copy
import json
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


# =========================================================================== fake endpoint


def _score_result(run_id: str, done: bool, outputs: list[dict[str, Any]]) -> dict[str, Any]:
    from mapi_spec.api import Run

    run = _example(
        Run, id=run_id, kind="score", state="succeeded" if done else "running", progress=None
    )
    return {"done": done, "run": run, "outputs": outputs if done else []}


class FakeEndpoint:
    """A respx-mocked endpoint that scores inline XrJSON payloads with a Python function.

    ``respond(ds) -> xr.Dataset`` maps a decoded payload to the output variables (without
    the ``stat``/``sample`` dims); the fake adds the summary (``mean`` and a 90% interval)
    and, when requested, deterministic draws around the mean. Results are served as a real
    ``score.zarr.zip`` DataTree from a signed storage link (CRC32C included).

    ``pending`` answers the first N calls of every idempotency key with ``202`` (still
    running); ``throttle`` answers the first N calls with ``429 RATE_LIMITED``.
    """

    def __init__(
        self,
        router: respx.MockRouter,
        respond: Callable[[Any], Any],
        *,
        alias: str = "plan-prod",
        snapshot: dict[str, Any] | None = None,
        pending: int = 0,
        throttle: int = 0,
        retry_after: int = 1,
    ) -> None:
        from mapi_spec.api import EndpointSchema

        self.alias = alias
        self.respond = respond
        self.pending = pending
        self.throttle = throttle
        self.retry_after = retry_after
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self._calls_per_key: dict[str, int] = {}
        self._results: dict[str, dict[str, Any]] = {}
        self._n = 0
        base = f"{BASE}/v1/endpoints/{alias}"
        schema = _example(EndpointSchema, alias=alias, ref=f"endpoint://growth/{alias}")
        if snapshot is not None:
            schema["snapshot"] = snapshot
        self.schema = schema
        self.schema_route = router.get(f"{base}/schema").respond(200, json=schema)
        self.score_route = router.post(f"{base}/score").mock(side_effect=self._score)
        self.validate_route = router.post(f"{base}/validate").mock(side_effect=self._validate)
        self.storage_route = router.get(
            scheme="http", host="storage.test", path__startswith="/results/"
        ).mock(side_effect=self._download)
        self._blobs: dict[str, bytes] = {}

    @property
    def score_calls(self) -> int:
        """Score requests that reached the endpoint (throttled ones included)."""
        return len(self.bodies)

    def _validate(self, request: Any) -> Any:
        import httpx

        body = json.loads(request.content)
        self.bodies.append(body)
        return httpx.Response(200, json={"valid": True, "issues": []})

    def _score(self, request: Any) -> Any:
        import httpx
        from mapi_spec.errors import ErrorCode, Problem

        body = json.loads(request.content)
        self.bodies.append(body)
        self.headers.append(dict(request.headers))
        if self.throttle > 0:
            self.throttle -= 1
            problem = Problem.from_code(ErrorCode.RATE_LIMITED, "slow down")
            return httpx.Response(
                429,
                json=problem.model_dump(mode="json"),
                headers={"Retry-After": str(self.retry_after)},
            )
        key = request.headers.get("Idempotency-Key", "")
        seen = self._calls_per_key.get(key, 0)
        self._calls_per_key[key] = seen + 1
        if key not in self._results:
            self._n += 1
            self._results[key] = self._compute(body, f"run_{self._n:026d}")
        result = self._results[key]
        if seen < self.pending:
            return httpx.Response(202, json=_score_result(result["run"]["id"], False, []))
        return httpx.Response(200, json=result)

    def _compute(self, body: dict[str, Any], run_id: str) -> dict[str, Any]:
        import numpy as np
        import xarray as xr
        from mapi_sdk.wire import from_xrjson, write_datatree_zip
        from mapi_spec.storage import crc32c, crc32c_to_base64

        payload = body.get("data") or body.get("scenario")
        ds = from_xrjson(payload) if isinstance(payload, dict) else xr.Dataset()
        out = self.respond(ds)
        stats = ["mean", "median", "hdi_90_lower", "hdi_90_upper"]
        summary = xr.Dataset(
            {
                str(n): xr.concat([v, v * 1.01, v * 0.9, v * 1.1], dim="stat").assign_coords(
                    stat=stats
                )
                for n, v in out.data_vars.items()
            }
        )
        nodes: dict[str, xr.Dataset] = {
            "/": xr.Dataset(attrs={"representation": "draws", "mode": body.get("mode", "predict")}),
            "/summary": summary,
        }
        draws = int((body.get("output") or {}).get("draws") or 0)
        if draws:
            noise = np.linspace(-0.05, 0.05, draws)
            noise -= noise.mean()
            factor = xr.DataArray(1.0 + noise, dims=["sample"], coords={"sample": np.arange(draws)})
            nodes["/draws"] = xr.Dataset({str(n): v * factor for n, v in out.data_vars.items()})
        blob = write_datatree_zip(xr.DataTree.from_dict(nodes))
        assert blob is not None
        name = f"{run_id}/score.zarr.zip"
        self._blobs[name] = blob
        link = {
            "name": "score.zarr.zip",
            "url": f"{STORAGE}/results/{name}?sig=1",
            "size": len(blob),
            "crc32c": crc32c_to_base64(crc32c(blob)),
            "generation": 1,
            "content_type": "application/zip",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        return _score_result(run_id, True, [link])

    def _download(self, request: Any) -> Any:
        import httpx

        name = request.url.path.removeprefix("/results/")
        assert "authorization" not in request.headers  # storage never sees API credentials
        return httpx.Response(200, content=self._blobs[name])


SCENARIO_SNAPSHOT: dict[str, Any] = {
    "inputs_stage": "raw",
    "input_schema": {
        "dims": {
            "scenario": {"kind": "categorical", "policy": "new_levels"},
            "channel": {"kind": "categorical"},
        },
        "vars": {
            "spend": {"dims": ["scenario", "channel"], "dtype": "float64", "role": "spend"},
            "revenue": {"dims": ["scenario"], "dtype": "float64", "role": "target"},
        },
    },
    "schema_hash": "sha256:" + "ab" * 32,
    "score_modes": ["predict"],
    "representation": "draws",
    "fit_coords": {
        "scenario": {"kind": "categorical", "size": 1, "values": ["observed"]},
        "channel": {"kind": "categorical", "size": 3, "values": ["tv", "search", "social"]},
    },
}
"""An endpoint whose model scores every ``scenario`` independently (batchable)."""


@pytest.fixture
def scenario_snapshot() -> dict[str, Any]:
    return copy.deepcopy(SCENARIO_SNAPSHOT)


@pytest.fixture
def fake_endpoint(api: respx.MockRouter) -> Callable[..., FakeEndpoint]:
    def make(respond: Callable[[Any], Any], **options: Any) -> FakeEndpoint:
        return FakeEndpoint(api, respond, **options)

    return make
