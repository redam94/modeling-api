"""The v0.4 tutorials' code that is not Python is real too.

* ``docs/snippets/sheets_endpoint.gs`` is exactly the Apps Script the server generates for
  an endpoint (``mapi_spec.snippets``), so the Google Sheets tutorial shows what the
  Integrations hub shows;
* both Apps Scripts of that tutorial run in Node against stand-ins for Apps Script's
  ``UrlFetchApp``, ``SpreadsheetApp``, ``PropertiesService`` and ``Utilities``, with answers
  built from the platform's own DTOs (``ScoreCallResult``, ``ForecastView``), and the test
  checks what they send and what they write into the sheet.

The Python snippets of the tutorials (``bayesinsight_import.py``, ``embed_chart.py``,
``webhook_receiver.py``, ``atlas_budget.py``) run in ``test_docs_snippets.py``.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from mapi_spec.api import (
    ArtifactLink,
    EndpointSchema,
    EndpointScoreCall,
    Run,
    ScoreCallResult,
    ScoreSummary,
)
from mapi_spec.snippets import SnippetContext, render_snippet
from mapi_spec.views import FORECAST_VIEW, ForecastView

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "docs" / "snippets"
NODE = shutil.which("node")
BASE = "https://api.mapi.example"
SCORE_URL = f"{BASE}/v1/endpoints/geo-mmm-prod/score"
VIEW_URL = "https://storage.googleapis.com/mapi-prod-bundles/forecast.json?X-Goog-Signature=x"

needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")

#: Runs an Apps Script file with stand-ins for the Apps Script services; prints what it did.
HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const [script, fixtures, entry] = process.argv.slice(2);
const fx = JSON.parse(fs.readFileSync(fixtures, "utf-8"));
const fetched = [];
const writes = [];
const sheets = {};
function sheet(name) {
  const range = (row, col) => ({
    setValues: (values) => writes.push({ sheet: name, row, col, values }),
    setValue: (value) => writes.push({ sheet: name, row, col, values: [[value]] }),
  });
  return {
    clear: () => writes.push({ sheet: name, cleared: true }),
    clearContents: () => writes.push({ sheet: name, cleared: true }),
    getRange: (a, b) => (typeof a === "string" ? range(1, 1) : range(a, b)),
  };
}
const services = {
  PropertiesService: {
    getScriptProperties: () => ({
      getProperty: (k) => (k === "MAPI_TOKEN" ? "mapi_from_properties" : null),
    }),
  },
  Utilities: { formatDate: () => "2026-10-16" },
  Session: { getScriptTimeZone: () => "Europe/Berlin" },
  UrlFetchApp: {
    fetch(url, options) {
      fetched.push({ url, options: options || null });
      const answer = fx[url];
      if (!answer) throw new Error("unexpected fetch " + url);
      return {
        getResponseCode: () => answer.status,
        getContentText: () => JSON.stringify(answer.body),
      };
    },
  },
  SpreadsheetApp: {
    getActiveSpreadsheet: () => ({
      getSheetByName: (n) => sheets[n] || null,
      insertSheet: (n) => (sheets[n] = sheet(n)),
    }),
    getActiveSheet: () => sheet("Sheet1"),
  },
};
vm.createContext(services);
vm.runInContext(fs.readFileSync(script, "utf-8"), services, { filename: script });
vm.runInContext(entry + "()", services);
process.stdout.write(JSON.stringify({ fetched, writes }));
"""


def _example(model: Any) -> dict[str, Any]:
    return copy.deepcopy(model.model_json_schema()["examples"][0])


def _endpoint_call() -> dict[str, Any]:
    call: dict[str, Any] = _example(EndpointSchema)["example"]
    return call


def _forecast_view() -> dict[str, Any]:
    history = [101.5, 98.2, 104.9]
    forecast = {0.05: [88.0, 85.1], 0.25: [96.3, 94.0], 0.5: [101.2, 99.8], 0.75: [106.4, 105.2],
                0.95: [114.9, 115.3]}  # fmt: skip
    view = ForecastView.model_validate(
        {
            "kpi": "revenue",
            "units": "kEUR",
            "dim": "time",
            "history": 3,
            "split_at": "2026-10-18",
            "summed_over": ["geo"],
            "fan": {
                "dim": "time",
                "coords": ["2026-09-27", "2026-10-04", "2026-10-11", "2026-10-18", "2026-10-25"],
                "quantiles": list(forecast),
                "values": [[None, None, None, *v] for v in forecast.values()],
                "observed": [*history, None, None],
                "units": "kEUR",
            },
            "total": {"mean": 201.0, "sd": 9.0, "quantiles": {"0.05": 186.0, "0.95": 216.0}},
        }
    )
    return view.model_dump(mode="json", exclude_none=True)


def _score_result(*, done: bool = True, state: str = "succeeded") -> dict[str, Any]:
    link = _example(ArtifactLink)
    outputs = [
        link | {"name": "score.zarr.zip"},
        link | {"name": FORECAST_VIEW, "url": VIEW_URL, "content_type": "application/json"},
    ]
    run = _example(Run) | {"kind": "score", "state": state, "progress": None}
    succeeded = done and state == "succeeded"
    result = ScoreCallResult.model_validate(
        {
            "done": done,
            "run": run,
            "outputs": outputs if succeeded else [],
            "summary": _example(ScoreSummary) if succeeded else None,
        }
    )
    return result.model_dump(mode="json")


def _run(tmp_path: Path, script: str, entry: str, answers: dict[str, Any]) -> dict[str, Any]:
    assert NODE is not None
    (tmp_path / "harness.js").write_text(HARNESS, "utf-8")
    (tmp_path / "answers.json").write_text(json.dumps(answers), "utf-8")
    args = [NODE, str(tmp_path / "harness.js"), str(SNIPPETS / script)]
    proc = subprocess.run(
        [*args, str(tmp_path / "answers.json"), entry],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out: dict[str, Any] = json.loads(proc.stdout)
    return out


def test_the_endpoint_apps_script_is_what_the_server_generates() -> None:
    ctx = SnippetContext(
        base_url=BASE, project="acme/growth", alias="geo-mmm-prod", payload=_endpoint_call()
    )
    shown = (SNIPPETS / "sheets_endpoint.gs").read_text("utf-8")
    assert shown == render_snippet("apps_script", ctx).code, (
        "regenerate docs/snippets/sheets_endpoint.gs from mapi_spec.snippets"
    )


@needs_node
def test_the_endpoint_apps_script_runs(tmp_path: Path) -> None:
    did = _run(tmp_path, "sheets_endpoint.gs", "scoreGeoMmmProd",
               {SCORE_URL: {"status": 200, "body": _score_result()}})  # fmt: skip
    (call,) = did["fetched"]
    assert call["options"]["headers"]["Authorization"] == "Bearer mapi_from_properties"
    assert json.loads(call["options"]["payload"]) == _endpoint_call()
    (table,) = _example(ScoreSummary)["tables"]
    assert did["writes"] == [
        {"sheet": "Result", "cleared": True},
        {
            "sheet": "Result",
            "row": 1,
            "col": 1,
            "values": [
                ["variable", *table["columns"]],
                *([table["variable"], *row] for row in table["rows"]),
            ],
        },
    ]


@needs_node
def test_the_endpoint_apps_script_says_when_the_run_is_still_going(tmp_path: Path) -> None:
    result = _score_result(done=False, state="running")
    did = _run(tmp_path, "sheets_endpoint.gs", "scoreGeoMmmProd",
               {SCORE_URL: {"status": 202, "body": result}})  # fmt: skip
    (cell,) = [w for w in did["writes"] if not w.get("cleared")]
    assert cell["sheet"] == "Result"
    assert cell["values"][0][0].startswith("Still running (" + result["run"]["id"])


@needs_node
def test_the_forecast_apps_script_writes_the_forecast(tmp_path: Path) -> None:
    view = _forecast_view()
    answers = {
        SCORE_URL: {"status": 200, "body": _score_result()},
        VIEW_URL: {"status": 200, "body": view},
    }
    did = _run(tmp_path, "sheets_forecast.gs", "refreshForecast", answers)
    score, fetch_view = did["fetched"]
    headers = score["options"]["headers"]
    assert headers["Authorization"] == "Bearer mapi_from_properties"
    assert headers["X-Mapi-Project"] == "acme/growth"
    assert headers["Idempotency-Key"] == "sheets-geo-mmm-prod-2026-10-16"
    assert headers["Prefer"] == "wait=30"
    sent = json.loads(score["options"]["payload"])
    assert sent == _endpoint_call()  # the documented call is the endpoint's example
    EndpointScoreCall.model_validate(sent)
    assert fetch_view == {"url": VIEW_URL, "options": None}  # the signed link, no token
    writes = [w for w in did["writes"] if not w.get("cleared")]
    assert did["writes"][0] == {"sheet": "Forecast", "cleared": True}
    header, rows, note = (w["values"] for w in writes)
    assert header == [["time", "Actual revenue", "5%", "25%", "Median", "75%", "95%"]]
    assert rows[0] == ["2026-09-27", 101.5, "", "", "", "", ""]
    assert rows[-1] == ["2026-10-25", "", 85.1, 94.0, 99.8, 105.2, 115.3]
    assert len(rows) == len(view["fan"]["coords"])
    assert note == [["Forecast from 2026-10-18", "expected revenue (kEUR)"]]


@needs_node
@pytest.mark.parametrize(
    ("status", "result", "message"),
    [
        (202, _score_result(done=False, state="running"), "Still running (running)"),
        (200, _score_result(state="failed"), "The forecast failed: failed"),
    ],
    ids=["still-running", "failed"],
)
def test_the_forecast_apps_script_explains_a_run_without_a_forecast(
    tmp_path: Path, status: int, result: dict[str, Any], message: str
) -> None:
    did = _run(tmp_path, "sheets_forecast.gs", "refreshForecast",
               {SCORE_URL: {"status": status, "body": result}})  # fmt: skip
    assert len(did["fetched"]) == 1
    (cell,) = [w for w in did["writes"] if not w.get("cleared")]
    assert cell["values"][0][0].startswith(message)
