"""Snippets for a forecasting endpoint with an input table (``SnippetContext.input_table``):
every language adds the call over the periods after the fit (to fill with actuals), and the
Apps Script reads an "Input" sheet of actuals and dates to forecast even though the example
call sends no data, then writes what the forecast was conditioned on to "Result".

Goldens: ``snapshots/snippets/<language>_actuals.txt`` (regenerate on purpose with
``MAPI_UPDATE_SNAPSHOTS=1``). The Apps Script runs in Node against stand-ins for Apps
Script's services.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from mapi_spec.actuals import ForecastConditioning
from mapi_spec.endpoints import (
    EndpointSchemaSnapshot,
    EndpointScoreCall,
    example_score_call,
    input_table,
)
from mapi_spec.snippets import SnippetContext, SnippetLanguage, render_snippet

pytestmark = pytest.mark.unit


def snapshot(*, conditions_on_actuals: bool = True) -> EndpointSchemaSnapshot:
    """A monthly forecast of ``y`` with a ``price`` driver, fitted through 1960-12."""
    return EndpointSchemaSnapshot.model_validate(
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
            "conditions_on_actuals": conditions_on_actuals,
        }
    )  # fmt: skip


GOLDEN = Path(__file__).parent / "snapshots" / "snippets"
SNAP = snapshot()
TABLE = input_table(SNAP)
CTX = SnippetContext(
    base_url="https://api.mapi.example",
    project="acme/growth",
    alias="sales-forecast",
    payload=example_score_call(SNAP).model_dump(mode="json", by_alias=True, exclude_defaults=True),
    input_table=TABLE,
)
CONDITIONING = ForecastConditioning.model_json_schema()["examples"][0]


@pytest.mark.parametrize(
    "language",
    [lang for lang in SnippetLanguage if lang is not SnippetLanguage.NOTEBOOK],
    ids=lambda x: x.value,
)
def test_golden_outputs(language: SnippetLanguage) -> None:
    code = render_snippet(language, CTX).code
    path = GOLDEN / f"{language.value}_actuals.txt"
    if os.environ.get("MAPI_UPDATE_SNAPSHOTS") == "1":
        path.write_text(code, "utf-8")
    assert code == path.read_text("utf-8"), f"regenerate {path.name} on purpose"
    assert "mapi_" not in code.replace("mapi_client", "")


def test_the_table_call_is_a_valid_forecast_over_the_periods_after_the_fit() -> None:
    call = CTX.table_call
    assert call is not None
    parsed = EndpointScoreCall.model_validate(call)
    assert parsed.mode == "forecast" and parsed.horizon is not None
    assert (parsed.horizon.dim, parsed.horizon.periods) == ("t", 8)
    assert call["future_covariates"] == "carry_last"
    assert call["output"] == CTX.payload["output"]
    data = call["data"]
    assert isinstance(data, dict)
    assert data["coords"]["t"]["data"][0] == "1961-01-01"  # type: ignore[index,call-overload]
    assert data["data_vars"]["y"]["data"] == [None] * 8  # type: ignore[index,call-overload]
    no_table = CTX.model_copy(update={"input_table": None})
    assert no_table.table_call is None
    for lang in ("python", "typescript", "curl", "r"):
        assert "actuals" not in render_snippet(lang, no_table).code.lower()


def test_python_forecasts_the_table_rows() -> None:
    tree = ast.parse(render_snippet("python", CTX).code)
    rows = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "rows"
    )
    assert ast.literal_eval(rows) == CTX.table_rows
    forecast = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "forecast"
    )  # fmt: skip
    assert ast.literal_eval(forecast.args[0]) == "sales-forecast"
    assert isinstance(forecast.args[1], ast.Name) and forecast.args[1].id == "rows"


def test_typescript_curl_and_r_carry_the_table_call() -> None:
    ts = render_snippet("typescript", CTX).code
    m = re.search(r"const actualsPayload = (.*?);\n", ts, re.DOTALL)
    assert m is not None and json.loads(m.group(1)) == CTX.table_call
    words = shlex.split(render_snippet("curl", CTX).code.replace("\\\n", " "))
    bodies = [json.loads(words[i + 1]) for i, w in enumerate(words) if w == "--data"]
    assert bodies == [CTX.payload, CTX.table_call]
    r = render_snippet("r", CTX).code
    second = r.index("req_body_raw(", r.index("forecast <-")) + len("req_body_raw(")
    end = r.index("', type", second)
    assert json.loads(r[second + 1 : end].replace("\\'", "'")) == CTX.table_call
    assert "conditioning$message" in r


def test_a_model_without_the_capability_gets_a_dates_only_note() -> None:
    plain = SnippetContext.model_validate(
        {**CTX.model_dump(), "input_table": input_table(snapshot(conditions_on_actuals=False))}
    )
    code = render_snippet("python", plain).code
    assert "ACTUALS_NOT_SUPPORTED" in code and "latest actuals" not in code
    script = render_snippet("apps_script", plain).code
    assert "refuses actuals" in script


# ------------------------------------------------------------------------------ apps script

_HARNESS = """
const sent = [];
const sheets = {};
function sheet(name, values) {
  const s = { name, values, cells: {}, frozen: 0,
    getDataRange: () => ({ getValues: () => values }),
    clear: () => { s.cells = {}; },
    setFrozenRows: (n) => { s.frozen = n; },
    getRange: (r, c, nr, nc) => ({
      setValue: (v) => { s.cells[r + "," + c] = v; },
      setValues: (grid) => {
        if (grid.length !== nr || grid.some((row) => row.length !== nc)) throw new Error("shape");
        grid.forEach((row, i) => row.forEach((v, j) => { s.cells[(r + i) + "," + (c + j)] = v; }));
      },
    }),
  };
  sheets[name] = s;
  return s;
}
const INPUT = JSON.parse(process.argv[2]);
const ANSWER = JSON.parse(process.argv[3]);
const asCell = (c) =>
  typeof c === "string" && c.startsWith("date:") ? new Date(c.slice(5) + "T00:00:00Z") : c;
if (INPUT) sheet("Input", INPUT.map((r) => r.map(asCell)));
globalThis.PropertiesService = { getScriptProperties: () => ({ getProperty: () => "tok" }) };
globalThis.SpreadsheetApp = { getActiveSpreadsheet: () => ({
  getSheetByName: (n) => sheets[n] || null, insertSheet: (n) => sheet(n, []),
  getSpreadsheetTimeZone: () => "UTC" }) };
globalThis.Session = { getScriptTimeZone: () => "Europe/Berlin" };
globalThis.Utilities = { formatDate: (d, zone, fmt) => {
  if (zone !== "UTC" || fmt !== "yyyy-MM-dd") throw new Error("format " + zone + " " + fmt);
  return d.toISOString().slice(0, 10);
} };
globalThis.UrlFetchApp = { fetch: (url, opts) => {
  sent.push({ url, payload: JSON.parse(opts.payload) });
  const text = JSON.stringify(ANSWER.body);
  return { getResponseCode: () => ANSWER.status, getContentText: () => text };
} };
let error = null;
try { FUNC(); } catch (e) { error = String(e.message || e); }
const cells = {};
for (const [n, s] of Object.entries(sheets)) cells[n] = s.cells;
console.log(JSON.stringify({ sent, cells, error, frozen: sheets.Input ? sheets.Input.frozen : 0 }));
"""

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _run(ctx: SnippetContext, sheet: Any, answer: Any, entry: str = "scoreSalesForecast") -> Any:
    assert NODE is not None
    code = render_snippet("apps_script", ctx).code
    out = subprocess.run(
        [NODE, "-e", code + _HARNESS.replace("FUNC()", entry + "()"), "x",
         json.dumps(sheet), json.dumps(answer)],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _answer(conditioning: Any = CONDITIONING) -> dict[str, Any]:
    return {
        "status": 200,
        "body": {
            "done": True,
            "run": {"id": "run_1", "state": "succeeded"},
            "outputs": [],
            "summary": {
                "tables": [{"variable": "y", "columns": ["t", "median", "q5", "q95"],
                            "rows": [["1961-04-01", 461.8, 431.0, 492.6]]}],
                "truncated": False,
                "conditioning": conditioning,
            },
        },
    }  # fmt: skip


INPUT_SHEET = [
    ["t", "y", "price"],
    ["date:1961-01-01", 417, 10.5],
    ["date:1961-02-01", 391, 10.5],
    ["1961-03-01", 419, 10.9],
    ["date:1961-04-01", "", 11.0],
    ["date:1961-05-01", "", ""],
    ["", "", ""],
]


@needs_node
def test_the_input_sheet_sends_actuals_and_dates_to_forecast() -> None:
    run = _run(CTX, INPUT_SHEET, _answer())
    assert run["error"] is None
    [call] = run["sent"]
    payload = call["payload"]
    assert payload["mode"] == "forecast" and payload["future_covariates"] == "carry_last"
    assert payload["horizon"] == {"dim": "t", "periods": 2}  # exactly the two blank months
    assert payload["data"] == {
        "coords": {"t": {"dims": ["t"], "data": ["1961-01-01", "1961-02-01", "1961-03-01",
                                                 "1961-04-01", "1961-05-01"]}},
        "data_vars": {"y": {"dims": ["t"], "data": [417, 391, 419, None, None]},
                      "price": {"dims": ["t"], "data": [10.5, 10.5, 10.9, 11.0, None]}},
    }  # fmt: skip
    EndpointScoreCall.model_validate(payload)
    result = run["cells"]["Result"]
    assert result["1,1"] == CONDITIONING["message"]
    assert result["2,1"] == ""
    assert [result[f"3,{c}"] for c in range(1, 6)] == ["variable", "t", "median", "q5", "q95"]
    assert result["4,2"] == "1961-04-01"


@needs_node
def test_a_sheet_that_ends_with_an_actual_keeps_the_horizon() -> None:
    sheet = [["y", "t"], ["417", "date:1961-01-01"], [391, "date:1961-02-01"]]
    run = _run(CTX, sheet, _answer(None))
    payload = run["sent"][0]["payload"]
    assert payload["horizon"]["periods"] == 8
    assert "price" not in payload["data"]["data_vars"]  # no column: not sent
    assert run["cells"]["Result"]["1,1"] == "variable"  # no conditioning note


@needs_node
def test_dates_only_forecast_every_listed_period() -> None:
    sheet = [["t", "y"], ["date:1961-01-01", ""], ["date:1961-02-01", ""], ["1961-03-01", ""]]
    payload = _run(CTX, sheet, _answer())["sent"][0]["payload"]
    assert payload["horizon"]["periods"] == 3
    assert payload["data"]["data_vars"]["y"]["data"] == [None, None, None]


@needs_node
@pytest.mark.parametrize(
    ("sheet", "message"),
    [
        ([["date", "y"], ["date:1961-01-01", 1]], 'needs a column named "t"'),
        ([["t", "sales"], ["date:1961-01-01", 1]], "named one of: y, price"),
        ([["t", "y"], ["date:1961-01-01", 1], ["", 2]], 'row 3 of the Input sheet has no "t"'),
        ([["t", "y"], ["1961-01-01", 1], ["date:1961-01-01", 2]], "two different values"),
    ],
)
def test_a_malformed_input_sheet_is_explained(sheet: Any, message: str) -> None:
    run = _run(CTX, sheet, _answer())
    assert run["sent"] == [] and message in run["error"]


@needs_node
def test_a_refusal_is_written_to_the_result_sheet() -> None:
    problem = {
        "type": "urn:mapi:error:ACTUALS_NOT_SUPPORTED",
        "title": "The model cannot update its forecast with supplied actuals",
        "status": 422,
        "code": "ACTUALS_NOT_SUPPORTED",
        "errors": [{"code": "ACTUALS_NOT_SUPPORTED", "severity": "error", "pointer": "/data",
                    "message": "model 'ts.hsgp_trend@1.0.0' cannot update a forecast with "
                               "actuals",
                    "suggested_action": "Refit the model with the new data."}],
    }  # fmt: skip
    run = _run(CTX, INPUT_SHEET, {"status": 422, "body": problem})
    assert run["error"].startswith("422: ")
    result = run["cells"]["Result"]
    assert result["1,1"].startswith("ACTUALS_NOT_SUPPORTED: model 'ts.hsgp_trend@1.0.0'")
    assert result["2,1"] == "What to do: Refit the model with the new data."
    failed = {
        "status": 200,
        "body": {"done": True, "run": {"id": "run_2", "state": "failed", "problem": problem},
                 "outputs": [], "summary": None},
    }  # fmt: skip
    cells = _run(CTX, INPUT_SHEET, failed)["cells"]["Result"]
    assert cells["1,1"] == "failed: no summary in the answer"
    assert cells["2,1"].startswith("ACTUALS_NOT_SUPPORTED: ")


@needs_node
def test_the_input_sheet_function_lays_out_the_table() -> None:
    run = _run(CTX, None, _answer(), entry="scoreSalesForecastInputSheet")
    assert run["error"] is None and run["frozen"] == 1
    cells = run["cells"]["Input"]
    assert [cells[f"1,{c}"] for c in (1, 2, 3)] == ["t", "y", "price"]
    assert [cells[f"2,{c}"] for c in (1, 2, 3)] == ["1961-01-01", "", ""]
    assert cells["9,1"] == "1961-08-01" and "10,1" not in cells
    again = _run(CTX, [["t"]], _answer(), entry="scoreSalesForecastInputSheet")
    assert "already" in again["error"]


@needs_node
def test_a_sheet_with_only_its_header_forecasts_from_the_fit() -> None:
    payload = _run(CTX, [["t", "y", "price"], ["", "", ""]], _answer())["sent"][0]["payload"]
    assert payload["data"] is None and payload["horizon"] == {"dim": "t", "periods": 8}
    EndpointScoreCall.model_validate(payload)


@needs_node
def test_numbers_stored_as_text_and_blank_text_cells() -> None:
    sheet = [
        ["t", "y", "price"],
        ["date:1961-01-01", "450", " 10.5"],
        ["date:1961-02-01", " ", "1,250"],
        ["date:1961-03-01", "", "#N/A"],
    ]
    payload = _run(CTX, sheet, _answer())["sent"][0]["payload"]
    assert payload["data"]["data_vars"]["y"]["data"] == [450, None, None]
    assert payload["data"]["data_vars"]["price"]["data"] == [10.5, 1250, "#N/A"]
    assert payload["horizon"]["periods"] == 2  # the blank-text row is a period to forecast


@needs_node
def test_headers_are_matched_trimmed_and_ignoring_case() -> None:
    sheet = [["T ", "y ", "Price"], ["date:1961-01-01", 450, 1], ["date:1961-02-01", "", 1]]
    payload = _run(CTX, sheet, _answer())["sent"][0]["payload"]
    assert payload["data"]["data_vars"]["y"]["data"] == [450, None]
    assert payload["data"]["coords"]["t"]["data"] == ["1961-01-01", "1961-02-01"]
    assert payload["horizon"]["periods"] == 1


@needs_node
def test_an_unrecognised_target_column_is_an_error_not_a_plain_forecast() -> None:
    sheet = [["t", "sales", "price"], ["date:1961-01-01", 450, 1], ["date:1961-02-01", "", 1]]
    run = _run(CTX, sheet, _answer())
    assert run["sent"] == []
    assert '"sales"' in run["error"] and "y" in run["error"]
