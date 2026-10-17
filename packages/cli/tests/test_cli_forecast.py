"""``mapi endpoints forecast``: actuals and dates to forecast from a CSV in the endpoint's
input layout; the answer's conditioning note and summary table are printed, and ``schema``
shows the input table."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import respx
from mapi_spec.actuals import ForecastConditioning
from mapi_spec.api import EndpointSchema, Run
from mapi_spec.endpoints import EndpointSchemaSnapshot, example_score_call, input_table
from typer.testing import Result

Invoke = Callable[..., Result]
Ex = Callable[..., dict[str, Any]]

URL = "https://api.test"
V1 = f"{URL}/v1"
CONN = ("--url", URL, "--token", "tok-secret-value", "--project", "acme/growth")

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
        "score_modes": ["forecast", "predict"],
        "representation": "quantiles",
        "horizon_dim": "t",
        "future_required": ["price"],
        "fit_coords": {"t": {"kind": "time", "size": 144, "start": "1949-01-01",
                             "end": "1960-12-01", "freq": "MS"}},
        "conditions_on_actuals": True,
    }
)  # fmt: skip
SCHEMA = EndpointSchema(
    alias="airline",
    ref="endpoint://growth/airline",
    model_ref="model://growth/airline@v1",
    revision=1,
    snapshot=SNAP,
    example=example_score_call(SNAP).model_dump(mode="json", exclude_defaults=True),
    input_table=input_table(SNAP),
).model_dump(mode="json")
CONDITIONING = ForecastConditioning.model_json_schema()["examples"][0]


def _answer(example: Ex) -> dict[str, Any]:
    run = example(Run, kind="score", state="succeeded", progress=None)
    summary = {
        "tables": [{"variable": "y", "columns": ["t", "median", "q5", "q95"],
                    "rows": [["1961-04-01", 461.8, 431.0, None]]}],
        "truncated": False,
        "conditioning": CONDITIONING,
    }  # fmt: skip
    return {"done": True, "run": run, "outputs": [], "summary": summary}


def test_forecast_sends_the_csv_and_prints_the_conditioning(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    csv = tmp_path / "input.csv"
    csv.write_text(
        "t,y,price\n1961-01-01,417,10.5\n1961-02-01,391,10.5\n1961-03-01,419,10.9\n"
        "1961-04-01,,11.0\n1961-05-01,,\n",
        "utf-8",
    )
    api.get(f"{V1}/endpoints/airline/schema").respond(200, json=SCHEMA)
    score = api.post(f"{V1}/endpoints/airline/score").respond(200, json=_answer(example))
    res = mapi(*CONN, "endpoints", "forecast", "airline", str(csv))
    assert res.exit_code == 0, res.output
    sent = json.loads(score.calls.last.request.content)
    assert sent["mode"] == "forecast" and sent["horizon"] == {"dim": "t", "periods": 2}
    assert sent["future_covariates"] == "carry_last"
    assert sent["data"]["data_vars"]["y"]["data"] == [417, 391, 419, None, None]
    assert sent["data"]["data_vars"]["price"]["data"] == [10.5, 10.5, 10.9, 11.0, None]
    out = " ".join(res.stdout.split())
    assert "Forecast updated with 3 actuals" in out
    assert "461.8" in out and "median" in out


def test_forecast_from_actuals_and_dates(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    actuals = tmp_path / "actuals.json"
    actuals.write_text(json.dumps([{"t": "1961-01-01", "y": 417, "price": 10.5}]), "utf-8")
    api.get(f"{V1}/endpoints/airline/schema").respond(200, json=SCHEMA)
    res = mapi(
        *CONN, "endpoints", "forecast", "airline", "--actuals", str(actuals),
        "--future", "1961-02-01", "--future", "1961-03-01", "--dry-run",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    body = json.loads(res.stdout)
    assert body["horizon"] == {"dim": "t", "periods": 2}
    assert body["data"]["coords"]["t"]["data"] == ["1961-01-01", "1961-02-01", "1961-03-01"]
    assert body["data"]["data_vars"]["y"]["data"] == [417, None, None]
    res = mapi(
        *CONN, "endpoints", "forecast", "airline", "--actuals", str(actuals),
        "--periods", "5", "--dry-run",
    )  # fmt: skip
    assert json.loads(res.stdout)["horizon"]["periods"] == 5


def test_a_refusal_is_reported(mapi: Invoke, api: respx.MockRouter, tmp_path: Path) -> None:
    csv = tmp_path / "input.csv"
    csv.write_text("t,y\n1961-01-01,417\n", "utf-8")
    api.get(f"{V1}/endpoints/airline/schema").respond(200, json=SCHEMA)
    problem = {
        "type": "urn:mapi:error:ACTUALS_NOT_SUPPORTED",
        "title": "The model cannot update its forecast with supplied actuals",
        "status": 422,
        "code": "ACTUALS_NOT_SUPPORTED",
        "errors": [{"code": "ACTUALS_NOT_SUPPORTED", "pointer": "/data",
                    "message": "model 'ts.hsgp_trend@1.0.0' cannot update a forecast with "
                               "actuals",
                    "suggested_action": "Refit the model with the new data."}],
    }  # fmt: skip
    api.post(f"{V1}/endpoints/airline/score").respond(422, json=problem)
    res = mapi(*CONN, "endpoints", "forecast", "airline", str(csv))
    assert res.exit_code != 0
    assert "ACTUALS_NOT_SUPPORTED" in res.output and "Refit" in res.output


def test_schema_shows_the_input_table(mapi: Invoke, api: respx.MockRouter) -> None:
    api.get(f"{V1}/endpoints/airline/schema").respond(200, json=SCHEMA)
    res = mapi(*CONN, "endpoints", "schema", "airline")
    assert res.exit_code == 0, res.output
    out = " ".join(res.stdout.split())
    assert "Input table" in out and "forecasts can carry actuals" in out
    assert "leave empty for the periods to forecast" in out
