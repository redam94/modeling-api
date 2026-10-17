"""Forecasts with actuals from Python: ``Endpoints.forecast`` / ``RemoteEndpoint.forecast``
build the call from long tables (a DataFrame, row mappings, columns) in the endpoint's input
layout, and ``ScoreResult.conditioning`` says what the forecast was conditioned on. The
generated Python snippet's actuals call runs as-is."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx
from mapi_client import AsyncClient, Client, ClientConfig
from mapi_client._m3_common import forecast_call, table_records
from mapi_spec.actuals import ForecastConditioning
from mapi_spec.api import EndpointSchema, Run
from mapi_spec.endpoints import EndpointSchemaSnapshot, example_score_call, input_table
from mapi_spec.snippets import SnippetContext, render_snippet

BASE = "http://api.test"
V1 = f"{BASE}/v1"

SNAP = EndpointSchemaSnapshot.model_validate(
    {
        "input_schema": {
            "dims": {"t": {"kind": "time", "freq": "MS", "policy": "extend"}},
            "vars": {
                "y": {"dims": ["t"], "dtype": "int64", "role": "target"},
                "price": {"dims": ["t"], "dtype": "float64", "future_required": True},
            },
        },
        "schema_hash": "sha256:" + "a" * 64,
        "score_modes": ["evaluate", "forecast", "predict"],
        "representation": "quantiles",
        "horizon_dim": "t",
        "future_required": ["price"],
        "fit_coords": {"t": {"kind": "time", "size": 144, "start": "1949-01-01",
                             "end": "1960-12-01", "freq": "MS"}},
        "conditions_on_actuals": True,
    }
)  # fmt: skip


def schema(*, table: bool = True) -> EndpointSchema:
    return EndpointSchema(
        alias="airline",
        ref="endpoint://growth/airline",
        model_ref="model://growth/airline@v1",
        revision=1,
        snapshot=SNAP,
        example=example_score_call(SNAP).model_dump(mode="json", exclude_defaults=True),
        input_table=input_table(SNAP) if table else None,
    )


CONDITIONING = ForecastConditioning.model_json_schema()["examples"][0]


def answer(conditioning: Any = CONDITIONING) -> dict[str, Any]:
    run = Run.model_json_schema()["examples"][0] | {
        "state": "succeeded",
        "kind": "score",
        "progress": None,
    }
    summary = {
        "tables": [{"variable": "y", "columns": ["t", "median"], "rows": [["1961-04-01", 461.8]]}],
        "truncated": False,
        "conditioning": conditioning,
    }
    return {"done": True, "run": run, "outputs": [], "summary": summary}


# ------------------------------------------------------------------------- forecast_call


def test_a_table_of_actuals_and_dates_to_forecast() -> None:
    rows = [
        {"t": "1961-01-01", "y": 417, "price": 10.5},
        {"t": "1961-02-01", "y": 391, "price": 10.5},
        {"t": "1961-03-01", "y": 419, "price": 10.9},
        {"t": "1961-04-01", "y": None, "price": 11.0},
        {"t": "1961-05-01", "y": None, "price": None},
    ]
    call = forecast_call(schema(), rows)
    assert call.mode == "forecast" and call.horizon is not None
    assert (call.horizon.dim, call.horizon.periods) == ("t", 2)
    assert call.future_covariates.value == "carry_last"
    assert call.output.summary == ["median", "q5", "q95"]  # the endpoint example's
    data = call.inline_inputs()["inline.data"]
    assert data.data_vars["y"].data == [417, 391, 419, None, None]
    assert data.data_vars["price"].data == [10.5, 10.5, 10.9, 11.0, None]
    # the same from columns, and with the input table computed locally (older servers)
    columns = {k: [r[k] for r in rows] for k in rows[0]}
    assert forecast_call(schema(table=False), columns) == call


def test_actuals_and_future_given_apart() -> None:
    actuals = [{"t": "1961-01-01", "y": 417}, {"t": "1961-02-01", "y": 391}]
    call = forecast_call(schema(), actuals=actuals, future=["1961-03-01", "1961-04-01"])
    assert call.horizon is not None and call.horizon.periods == 2
    data = call.inline_inputs()["inline.data"]
    assert data.coords["t"].data == ["1961-01-01", "1961-02-01", "1961-03-01", "1961-04-01"]
    assert data.data_vars["y"].data == [417, 391, None, None]
    # a number of periods after the last actual
    appended = forecast_call(schema(), actuals=actuals, future=6)
    assert appended.horizon is not None and appended.horizon.periods == 6
    assert len(appended.inline_inputs()["inline.data"].coords["t"].data) == 2  # type: ignore[arg-type]
    # future rows with driver values
    future = [{"t": "1961-03-01", "price": 11.0}]
    with_drivers = forecast_call(schema(), actuals=actuals, future=future)
    assert with_drivers.inline_inputs()["inline.data"].data_vars["price"].data == [None, None, 11.0]
    # no rows at all: a plain forecast from the end of the fitted history
    plain = forecast_call(schema(), periods=4)
    assert plain.horizon is not None and plain.horizon.periods == 4 and plain.data is None


def test_bad_forecast_calls_are_refused() -> None:
    with pytest.raises(ValueError, match="must leave the target"):
        forecast_call(schema(), future=[{"t": "1961-03-01", "y": 5}])
    with pytest.raises(ValueError, match="once"):
        forecast_call(schema(), future=3, periods=4)
    with pytest.raises(TypeError):
        forecast_call(schema(), future=True)
    not_forecasting = schema().model_copy(
        update={"input_table": None, "snapshot": SNAP.model_copy(update={"horizon_dim": None})}
    )
    with pytest.raises(ValueError, match="does not forecast"):
        forecast_call(not_forecasting, [{"t": "1961-01-01", "y": 1}])
    with pytest.raises(ValueError, match="different lengths"):
        table_records({"t": ["a", "b"], "y": [1]})
    with pytest.raises(TypeError):
        table_records(["1961-01-01"])


def test_a_dataframe_is_a_table() -> None:
    pd = pytest.importorskip("pandas")
    frame = pd.DataFrame(
        {"t": pd.date_range("1961-01-01", periods=4, freq="MS"), "y": [417, 391, None, None]}
    )
    call = forecast_call(schema(), frame)
    assert call.horizon is not None and call.horizon.periods == 2
    data = call.inline_inputs()["inline.data"]
    assert data.coords["t"].data[0] == "1961-01-01"  # type: ignore[index]
    assert data.data_vars["y"].data == [417.0, 391.0, None, None]


# ----------------------------------------------------------------------------- over HTTP


@pytest.fixture
def scoped() -> Any:
    cfg = ClientConfig(
        base_url=BASE, project="acme/growth", token="t0k", allow_insecure_token=True, max_retries=0
    )
    with Client(config=cfg, sleep=lambda s: None) as c:
        yield c


def test_endpoints_forecast_sends_the_table_and_reads_the_conditioning(scoped: Client) -> None:
    with respx.mock(base_url=BASE, assert_all_called=True) as api:
        api.get(f"{V1}/endpoints/airline/schema").respond(
            200, json=schema().model_dump(mode="json")
        )
        route = api.post(f"{V1}/endpoints/airline/score").respond(200, json=answer())
        res = scoped.endpoints.forecast(
            "airline", actuals=[{"t": "1961-01-01", "y": 417}], future=2, download=False
        )
    sent = json.loads(route.calls.last.request.content)
    assert sent["mode"] == "forecast" and sent["horizon"] == {"dim": "t", "periods": 2}
    assert sent["data"]["data_vars"]["y"]["data"] == [417]
    assert res.conditioning is not None and res.conditioning.conditioned
    assert res.conditioning.message.startswith("Forecast updated with 3 actuals")
    # the remote endpoint fetches the schema once
    with respx.mock(base_url=BASE, assert_all_called=True) as api:
        get = api.get(f"{V1}/endpoints/airline/schema").respond(
            200, json=schema().model_dump(mode="json")
        )
        api.post(f"{V1}/endpoints/airline/score").respond(200, json=answer(None))
        remote = scoped.endpoint("airline")
        for _ in range(2):
            out = remote.forecast([{"t": "1961-01-01", "y": None}], download=False)
            assert out.conditioning is None
        assert get.call_count == 1


def test_the_async_client_forecasts_too() -> None:
    async def go() -> tuple[dict[str, Any], str | None]:
        cfg = ClientConfig(
            base_url=BASE, project="acme/growth", token="t", allow_insecure_token=True,
            max_retries=0,
        )  # fmt: skip
        async with AsyncClient(config=cfg) as c:
            with respx.mock(base_url=BASE, assert_all_called=True) as api:
                api.get(f"{V1}/endpoints/airline/schema").respond(
                    200, json=schema().model_dump(mode="json")
                )
                route = api.post(f"{V1}/endpoints/airline/score").respond(200, json=answer())
                res = await c.endpoint("airline").forecast(
                    [{"t": "1961-01-01", "y": 417}, {"t": "1961-02-01", "y": None}],
                    download=False,
                )
        sent = json.loads(route.calls.last.request.content)
        return sent, res.conditioning.message if res.conditioning else None

    sent, message = asyncio.run(go())
    assert sent["horizon"]["periods"] == 1 and message is not None


def test_the_python_snippets_actuals_call_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    base = "https://api.example.com"
    ctx = SnippetContext(
        base_url=base,
        project="acme/growth",
        alias="airline",
        payload=example_score_call(SNAP).model_dump(mode="json", exclude_defaults=True),
        input_table=input_table(SNAP),
    )
    code = render_snippet("python", ctx).code
    monkeypatch.setenv("MAPI_TOKEN", "snippet-token")
    printed: list[tuple[Any, ...]] = []
    scope: dict[str, Any] = {"print": lambda *a: printed.append(a)}
    with respx.mock(base_url=base, assert_all_called=True) as router:
        router.get("/v1/endpoints/airline/schema").respond(
            200, json=schema().model_dump(mode="json")
        )
        score = router.post("/v1/endpoints/airline/score").mock(
            side_effect=lambda req: httpx.Response(200, json=answer())
        )
        exec(compile(code, "<python snippet>", "exec"), scope)
        scope["api"].close()
    example, forecast = (json.loads(c.request.content) for c in score.calls)
    assert example["horizon"] == {"dim": "t", "periods": 8} and example.get("data") is None
    assert forecast["data"]["coords"]["t"]["data"][0] == "1961-01-01"
    assert forecast["horizon"] == {"dim": "t", "periods": 8}
    assert (CONDITIONING["message"],) in printed
