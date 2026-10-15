"""429 handling: RATE_LIMITED is waited out and resent, QUOTA_EXCEEDED is raised at once
(with the server's Retry-After available through ``retry_after_of``)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
import respx
from mapi_client import (
    AsyncClient,
    Client,
    ClientConfig,
    RateLimited,
    RetryPolicy,
    RetryState,
    http_status_of,
    retry_after_of,
)
from mapi_client import _calls as calls
from mapi_client.errors import retry_after_seconds
from mapi_spec.api import Run, SpecDoc
from mapi_spec.errors import ErrorCode, Problem, QuotaExceeded

BASE = "http://api.test"
Sleeps = list[float]

V1 = f"{BASE}/v1"
Ex = Callable[..., dict[str, Any]]


def problem(code: ErrorCode, detail: str = "slow down") -> dict[str, Any]:
    return Problem.from_code(code, detail).model_dump(mode="json")


def throttled(code: ErrorCode, retry_after: str | None = None) -> httpx.Response:
    headers = {"content-type": "application/problem+json"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return httpx.Response(429, json=problem(code), headers=headers)


@pytest.fixture
def strict_client(api: respx.MockRouter, sleeps: Sleeps) -> Iterator[Client]:
    policy = RetryPolicy(max_retries=2, jitter=0.0, rate_limit_retries=3)
    with Client(config=ClientConfig(base_url=BASE), retry=policy, sleep=sleeps) as c:
        yield c


def test_rate_limited_get_waits_for_retry_after(
    strict_client: Client, api: respx.MockRouter, example: Ex, sleeps: Sleeps
) -> None:
    route = api.get(f"{V1}/runs/run_1").mock(
        side_effect=[
            throttled(ErrorCode.RATE_LIMITED, "7"),
            throttled(ErrorCode.RATE_LIMITED),
            httpx.Response(200, json=example(Run)),
        ]
    )
    assert strict_client.runs.get("run_1").id
    assert route.call_count == 3
    # Retry-After wins over the 0.5 s base; then exponential backoff (0.5 * 2)
    assert sleeps == [7.0, 1.0]


def test_rate_limited_post_is_resent_because_the_server_did_not_act(
    strict_client: Client, api: respx.MockRouter, example: Ex, sleeps: Sleeps
) -> None:
    route = api.post(f"{V1}/specs").mock(
        side_effect=[
            throttled(ErrorCode.RATE_LIMITED, "1"),
            httpx.Response(201, json=example(SpecDoc)),
        ]
    )
    strict_client.specs.create("apiVersion: mapi/v1\n")
    assert route.call_count == 2 and sleeps == [1.0]


def test_rate_limit_retries_are_bounded(
    strict_client: Client, api: respx.MockRouter, sleeps: Sleeps
) -> None:
    route = api.get(f"{V1}/runs/run_1").mock(return_value=throttled(ErrorCode.RATE_LIMITED, "2"))
    with pytest.raises(RateLimited) as err:
        strict_client.runs.get("run_1")
    assert route.call_count == 4  # 1 + rate_limit_retries
    assert sleeps == [2.0, 2.0, 2.0]
    assert err.value.code is ErrorCode.RATE_LIMITED
    assert retry_after_of(err.value) == 2.0
    assert http_status_of(err.value) == 429


def test_a_long_retry_after_is_not_waited_out(
    strict_client: Client, api: respx.MockRouter, sleeps: Sleeps
) -> None:
    api.get(f"{V1}/runs/run_1").mock(return_value=throttled(ErrorCode.RATE_LIMITED, "600"))
    with pytest.raises(RateLimited) as err:
        strict_client.runs.get("run_1")
    assert sleeps == [] and retry_after_of(err.value) == 600.0


def test_quota_exceeded_is_raised_at_once(
    strict_client: Client, api: respx.MockRouter, sleeps: Sleeps
) -> None:
    route = api.post(f"{V1}/runs").mock(return_value=throttled(ErrorCode.QUOTA_EXCEEDED, "3600"))
    with pytest.raises(QuotaExceeded) as err:
        strict_client.runs.create({"kind": "fit", "spec_id": "spc_1", "revision": 1})
    assert route.call_count == 1 and sleeps == []
    assert retry_after_of(err.value) == 3600.0
    # a concurrency quota has no reset time
    api.get(f"{V1}/runs/run_1").mock(return_value=throttled(ErrorCode.QUOTA_EXCEEDED))
    with pytest.raises(QuotaExceeded) as err2:
        strict_client.runs.get("run_1")
    assert retry_after_of(err2.value) is None


def test_quota_windows_can_be_waited_for_when_asked(
    api: respx.MockRouter, example: Ex, sleeps: Sleeps
) -> None:
    policy = RetryPolicy(jitter=0.0, max_quota_wait=60.0)
    with Client(config=ClientConfig(base_url=BASE), retry=policy, sleep=sleeps) as client:
        api.get(f"{V1}/runs/run_1").mock(
            side_effect=[
                throttled(ErrorCode.QUOTA_EXCEEDED, "30"),
                httpx.Response(200, json=example(Run)),
            ]
        )
        client.runs.get("run_1")
    assert sleeps == [30.0]


def test_a_bare_429_is_only_resent_for_idempotent_calls(
    strict_client: Client, api: respx.MockRouter, example: Ex, sleeps: Sleeps
) -> None:
    post = api.post(f"{V1}/specs").respond(429, text="Too Many Requests")
    with pytest.raises(RateLimited):
        strict_client.specs.create("apiVersion: mapi/v1\n")
    assert post.call_count == 1
    get = api.get(f"{V1}/runs/run_1").mock(
        side_effect=[httpx.Response(429, text="busy"), httpx.Response(200, json=example(Run))]
    )
    strict_client.runs.get("run_1")
    assert get.call_count == 2 and sleeps == [0.5]


def test_jitter_is_small_and_never_shortens_retry_after() -> None:
    policy = RetryPolicy(jitter=0.1)
    call = calls.get_run("run_1")
    for _ in range(50):
        state = RetryState()
        response = throttled(ErrorCode.RATE_LIMITED, "5")
        delay = policy.plan(call, state, response)
        assert delay is not None and 5.0 <= delay <= 5.5
        assert state.throttled == 1 and state.waited == delay


def test_policy_validation() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(rate_limit_retries=-1)
    with pytest.raises(ValueError):
        RetryPolicy(jitter=2)


def test_retry_after_http_dates() -> None:
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    when = format_datetime(now + timedelta(seconds=90), usegmt=True)
    response = httpx.Response(429, headers={"Retry-After": when})
    assert retry_after_seconds(response, now=now) == 90.0
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "soon"})) is None
    assert retry_after_seconds(httpx.Response(429)) is None


async def test_async_client_waits_for_rate_limits(api: respx.MockRouter, example: Ex) -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    policy = RetryPolicy(jitter=0.0)
    async with AsyncClient(config=ClientConfig(base_url=BASE), retry=policy, sleep=sleep) as c:
        api.get(f"{V1}/runs/run_1").mock(
            side_effect=[
                throttled(ErrorCode.RATE_LIMITED, "4"),
                httpx.Response(200, json=example(Run)),
            ]
        )
        await c.runs.get("run_1")
        api.post(f"{V1}/runs").mock(return_value=throttled(ErrorCode.QUOTA_EXCEEDED, "86400"))
        with pytest.raises(QuotaExceeded):
            await c.runs.create({"kind": "fit", "spec_id": "spc_1", "revision": 1})
    assert slept == [4.0]
