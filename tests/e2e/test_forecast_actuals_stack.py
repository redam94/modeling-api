"""Forecasts with actuals and future dates through the *running* stack (``make dev``, then
``make e2e``).

The reported journey, from outside the processes, in a fresh project of the ``default``
tenant (see :mod:`m35_stack` for the sign-in and the project):

1. The monthly airline passenger series (Box & Jenkins, 1949-01 to 1960-12, 144 int64
   counts) goes through the ``ts_readiness_arimax`` gallery workflow with a 12-month season;
   the model it selects is promoted to the endpoint ``ts-forecast``.
2. The endpoint's schema says its forecasts can carry actuals and offers the input table.
3. The Python client sends three months of actuals and four dates to forecast: the forecast
   starts after the last actual, covers exactly the requested dates, says what it was
   conditioned on, and moves when the actuals change (two very different sets of actuals).
   The fit's own last months sent as actuals reproduce the plain forecast.
4. The Apps Script the server generates for the endpoint runs in Node against stand-ins for
   the Sheets services, reading an **Input** sheet with the same three actuals and four
   blank dates; its ``UrlFetchApp`` calls the live API, so the JSON it sends is exactly what
   Sheets would send. The answer equals the client's, and the **Result** sheet starts with
   the conditioning note.
5. A model that cannot condition on actuals (``ts.hsgp_trend``, a tiny PyMC fit) refuses them
   with ``422 ACTUALS_NOT_SUPPORTED`` - never the old silent ignore - through the client and
   through the Apps Script, which writes the problem into **Result**.

``MAPI_E2E_KEEP=1`` keeps the project afterwards.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_client import Client
from mapi_client.oidc import dev_idp_login
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.jobs import DATASET_SCHEMA_ATTR
from mapi_spec.schema import DataSchema

sys.path.insert(0, str(Path(__file__).parent))
import m35_stack as m35

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.slow]

RUN: Final = uuid.uuid4().hex[:6]
ALIAS: Final = "ts-forecast"
REFUSING: Final = "trend-forecast"
NODE: Final = shutil.which("node")
CURL: Final = shutil.which("curl")

#: Monthly international airline passengers in thousands, 1949-01 to 1960-12.
AIRLINE: Final = [
    112, 118, 132, 129, 121, 135, 148, 148, 136, 119, 104, 118,
    115, 126, 141, 135, 125, 149, 170, 170, 158, 133, 114, 140,
    145, 150, 178, 163, 172, 178, 199, 199, 184, 162, 146, 166,
    171, 180, 193, 181, 183, 218, 230, 242, 209, 191, 172, 194,
    196, 196, 236, 235, 229, 243, 264, 272, 237, 211, 180, 201,
    204, 188, 235, 227, 234, 264, 302, 293, 259, 229, 203, 229,
    242, 233, 267, 269, 270, 315, 364, 347, 312, 274, 237, 278,
    284, 277, 317, 313, 318, 374, 413, 405, 355, 306, 271, 306,
    315, 301, 356, 348, 355, 422, 465, 467, 404, 347, 305, 336,
    340, 318, 362, 348, 363, 435, 491, 505, 404, 359, 310, 337,
    360, 342, 406, 396, 420, 472, 548, 559, 463, 407, 362, 405,
    417, 391, 419, 461, 472, 535, 622, 606, 508, 461, 390, 432,
]  # fmt: skip
ACTUALS: Final = [450, 430, 480]
"""Three months after the fit (1961-01 to 1961-03)."""
WILD: Final = [9999, 1, 50000]
"""The values tried on the user's stack, which used to give the same numbers."""
FUTURE: Final = 4
"""Dates to forecast after the actuals (1961-04 to 1961-07)."""

HSGP_SPEC: Final = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: airline-trend, project: {project}}}
data:
  dataset: dataset://{project}/airline@latest
  bind: {{dims: {{t: t}}, vars: {{y: y}}}}
  split: {{kind: time_holdout, dim: t, last: 12}}
model:
  use: ts.hsgp_trend@^1
  settings: {{m: 12, season_order: 2}}
  fit: {{mode: evaluate_then_refit, seed: 1, tune: 40, draws: 40, chains: 1}}
evaluate: {{metrics: [rmse]}}
"""


def _months(first: str, n: int) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(first, periods=n, freq="MS")]


def airline() -> xr.Dataset:
    """The series as the user's endpoint has it: ``y(t)`` int64, ``freq: MS``, no drivers."""
    assert len(AIRLINE) == 144 and sum(AIRLINE) == 40363
    t = pd.date_range("1949-01-01", periods=144, freq="MS").as_unit("ns")
    schema = DataSchema.model_validate(
        {
            "dims": {"t": {"kind": "time", "policy": "extend", "freq": "MS"}},
            "vars": {"y": {"dims": ["t"], "dtype": "int64", "role": "target", "missing": "forbid"}},
        }
    )
    ds = xr.Dataset({"y": (("t",), np.asarray(AIRLINE, dtype=np.int64))}, coords={"t": t})
    return ds.assign_attrs({DATASET_SCHEMA_ATTR: schema.model_dump_json(exclude_none=True)})


@dataclass
class Stack:
    op: Client
    world: m35.World
    token: str
    model_ref: str


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    url = m35.api_url()
    try:
        ready = httpx.get(f"{url}/readyz", timeout=10)
    except httpx.TransportError as exc:
        pytest.fail(f"the API is not reachable at {url} ({exc}); start it with `make dev`")
    assert ready.status_code == 200, ready.text
    if httpx.get(f"{url}/dev/outbox", timeout=10).status_code != 200:
        pytest.skip("the actuals journey needs a dev-profile stack (make dev)")
    token = dev_idp_login(url, m35.OPERATOR)
    with Client(url, token=token) as op:
        world = m35.create_world(op, "actuals", RUN)
        try:
            m35.upload(op, world, "airline", airline())
            wf = op.workflows.create(template="ts_readiness_arimax")
            started = op.workflows.run(
                wf.id,
                inputs={"data": world.ref("airline")},
                params={"season": 12, "horizons": [1, 3, 6], "n_folds": 4},
            )
            run = op.workflows.wait(started.run_id, timeout=m35.RUN_TIMEOUT_S)
            assert run.status.value == "succeeded", run
            model_ref = run.outputs["model"].ref
            assert model_ref is not None and model_ref.startswith(f"model://{world.project}/")
            op.endpoints.promote(
                ALIAS, model_ref, override=True, reason="forecasts with actuals e2e"
            )
            yield Stack(op=op, world=world, token=token, model_ref=model_ref)
        finally:
            if os.environ.get("MAPI_E2E_KEEP", "") != "1":
                m35.cleanup(world.project)


def _rows(summary: Any) -> list[list[Any]]:
    """The first summary table: one row per period, the date first."""
    assert summary is not None and summary.tables, summary
    return [list(r) for r in summary.tables[0].rows]


def _forecast(stack: Stack, ys: list[int | None], alias: str = ALIAS) -> Any:
    """The client's long-table forecast: ``y`` from 1961-01 on (``None``: a date to forecast)."""
    table = pd.DataFrame({"t": pd.date_range("1961-01-01", periods=len(ys), freq="MS"), "y": ys})
    return stack.op.endpoints.forecast(alias, table, timeout=600)


# ====================================================================== 2. the schema


def test_the_schema_offers_actuals_and_the_input_table(stack: Stack) -> None:
    schema = stack.op.endpoints.schema(ALIAS)
    assert schema.snapshot.conditions_on_actuals is True
    snap = schema.snapshot.model_dump(mode="json")
    assert snap["input_schema"]["dims"]["t"]["freq"] == "MS"
    example = schema.actuals_example
    assert example is not None
    assert example["mode"] == "forecast"
    assert example["data"]["coords"]["t"]["data"][0] == "1961-01-01"
    table = schema.input_table
    assert table is not None and table.conditions_on_actuals
    assert [c.name for c in table.columns][:2] == ["t", "y"]
    assert table.rows[0][0] == "1961-01-01"


# ============================================================== 3. the Python client


def test_the_client_forecasts_the_dates_after_the_actuals(stack: Stack) -> None:
    res = _forecast(stack, [*ACTUALS, *[None] * FUTURE])
    cond = res.conditioning
    assert cond is not None and cond.conditioned and cond.n_actuals == 3
    assert cond.fit_end == "1960-12-01" and cond.actuals_through == "1961-03-01"
    assert cond.horizon_source == "rows" and cond.periods == FUTURE and cond.parameters == "fitted"
    (series,) = cond.series
    assert (series.forecast_from, series.forecast_to) == ("1961-04-01", "1961-07-01")
    assert "3 actuals (1961-01-01 to 1961-03-01)" in cond.message
    assert "not re-estimated" in cond.message
    rows = _rows(res.summary)
    # the actual months (one-step-ahead predictions) and exactly the requested dates
    assert [r[0] for r in rows] == _months("1961-01-01", 3 + FUTURE)

    plain = stack.op.endpoints.score(
        ALIAS,
        mode="forecast",
        horizon={"dim": "t", "periods": 3 + FUTURE},
        future_covariates="carry_last",
        output={"summary": ["median", "q5", "q95"]},
        timeout=600,
    )
    assert plain.conditioning is not None and not plain.conditioning.conditioned
    plain_rows = _rows(plain.summary)
    assert [r[0] for r in plain_rows] == _months("1961-01-01", 3 + FUTURE)
    # the actuals moved the forecast of 1961-04..07 away from the plain one
    assert plain_rows[3:] != rows[3:]

    wild = _forecast(stack, [*WILD, *[None] * FUTURE])
    assert wild.conditioning is not None and wild.conditioning.actuals_through == "1961-03-01"
    wild_rows = _rows(wild.summary)
    assert [r[0] for r in wild_rows] == [r[0] for r in rows]
    assert wild_rows[3:] != rows[3:]  # different actuals, a different forecast


def test_the_fits_own_last_months_as_actuals_reproduce_the_plain_forecast(stack: Stack) -> None:
    periods = 6
    plain = stack.op.endpoints.score(
        ALIAS,
        mode="forecast",
        horizon={"dim": "t", "periods": periods},
        output={"summary": ["median", "q5", "q95"]},
        timeout=600,
    )
    own = pd.DataFrame({"t": pd.date_range("1960-10-01", periods=3, freq="MS"), "y": AIRLINE[-3:]})
    res = stack.op.endpoints.forecast(ALIAS, own, periods=periods, timeout=600)
    cond = res.conditioning
    assert cond is not None and cond.conditioned and cond.revised == 3
    assert cond.horizon_source == "horizon" and cond.periods == periods
    rows = _rows(res.summary)
    assert [r[0] for r in rows] == _months("1960-10-01", 3 + periods)
    np.testing.assert_allclose(
        np.asarray([r[1:] for r in rows[3:]], dtype=float),
        np.asarray([r[1:] for r in _rows(plain.summary)], dtype=float),
        rtol=1e-9,
    )


# ======================================================================= 4. Google Sheets

#: Runs an Apps Script with stand-ins for the Sheets services; ``UrlFetchApp`` calls the
#: live API through curl. Prints what was fetched, what the API answered and what was written.
HARNESS: Final = r"""
const fs = require("fs");
const vm = require("vm");
const { execFileSync } = require("child_process");
const [script, fixtures, entry] = process.argv.slice(2);
const fx = JSON.parse(fs.readFileSync(fixtures, "utf-8"));
const fetched = [];
const writes = [];
const sheets = {};
function sheet(name, values) {
  const range = (row, col) => ({
    setValues: (v) => writes.push({ sheet: name, row, col, values: v }),
    setValue: (v) => writes.push({ sheet: name, row, col, values: [[v]] }),
  });
  return {
    clear: () => writes.push({ sheet: name, cleared: true }),
    getRange: (a, b) => range(a, b),
    getDataRange: () => ({ getValues: () => values || [] }),
    setFrozenRows: (n) => writes.push({ sheet: name, frozen: n }),
  };
}
const services = {
  PropertiesService: {
    getScriptProperties: () => ({ getProperty: (k) => (k === "MAPI_TOKEN" ? fx.token : null) }),
  },
  Utilities: {
    formatDate: (d, zone, fmt) => {
      if (fmt !== "yyyy-MM-dd") throw new Error("unexpected date format " + fmt);
      return new Intl.DateTimeFormat("en-CA", { timeZone: zone }).format(d);
    },
  },
  Session: { getScriptTimeZone: () => "Etc/UTC" },
  UrlFetchApp: {
    fetch(url, options) {
      const args = ["-sS", "-X", (options.method || "get").toUpperCase(), url,
                    "-o", "-", "-w", "\n%{http_code}"];
      if (options.contentType) args.push("-H", "Content-Type: " + options.contentType);
      for (const [k, v] of Object.entries(options.headers || {})) args.push("-H", k + ": " + v);
      if (options.payload) args.push("--data-binary", options.payload);
      const out = execFileSync(fx.curl, args, { encoding: "utf-8", maxBuffer: 1 << 26 });
      const cut = out.lastIndexOf("\n");
      const body = out.slice(0, cut);
      const status = Number(out.slice(cut + 1));
      fetched.push({ url, headers: options.headers, payload: options.payload, status, body });
      return { getResponseCode: () => status, getContentText: () => body };
    },
  },
};
services.SpreadsheetApp = {
  getActiveSpreadsheet: () => ({
    getSheetByName: (n) => sheets[n] || null,
    insertSheet: (n) => (sheets[n] = sheet(n)),
    getSpreadsheetTimeZone: () => "America/New_York",
  }),
};
vm.createContext(services);
// Sheets hands date cells over as Date objects of the script's own realm
services.__raw = fx.input;
vm.runInContext(
  "__input = __raw.map((r) => r.map((c) => (c && c.date ? new Date(c.date + 'T12:00:00Z') : c)))",
  services,
);
sheets.Input = sheet("Input", services.__input);
vm.runInContext(fs.readFileSync(script, "utf-8"), services, { filename: script });
let error = null;
try {
  vm.runInContext(entry + "()", services);
} catch (e) {
  error = String(e && e.message ? e.message : e);
}
process.stdout.write(JSON.stringify({ fetched, writes, error }));
"""


def _apps_script(stack: Stack, alias: str, tmp: Path) -> tuple[Path, str]:
    """The endpoint's generated Apps Script (pointed at the live API) and its entry point."""
    snippets = stack.op.endpoints.snippets(alias, ["apps_script"], base_url=m35.api_url())
    (snippet,) = snippets.items
    code = snippet.code
    path = tmp / f"{alias}.gs"
    path.write_text(code, "utf-8")
    entry = code.split("function ", 1)[1].split("(", 1)[0]
    assert entry.startswith("score") and f"{entry}InputSheet" in code
    return path, entry


def _sheet(ys: list[int | None]) -> list[list[Any]]:
    """An Input sheet: the header, then one row per month from 1961-01 (date cells)."""
    rows: list[list[Any]] = [["t", "y"]]
    for month, y in zip(_months("1961-01-01", len(ys)), ys, strict=True):
        rows.append([{"date": month}, "" if y is None else y])
    return rows


def _run_script(stack: Stack, script: Path, entry: str, ys: list[int | None]) -> dict[str, Any]:
    assert NODE is not None and CURL is not None
    work = script.parent
    (work / "harness.js").write_text(HARNESS, "utf-8")
    fixtures = {"token": stack.token, "curl": CURL, "input": _sheet(ys)}
    (work / "fixtures.json").write_text(json.dumps(fixtures), "utf-8")
    deadline = time.monotonic() + 600
    while True:  # a run still going after Prefer: wait=60 is collected by running again
        proc = subprocess.run(
            [NODE, str(work / "harness.js"), str(script), str(work / "fixtures.json"), entry],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        did: dict[str, Any] = json.loads(proc.stdout)
        notes = [w for w in did["writes"] if not w.get("cleared")]
        still = notes and str(notes[0]["values"][0][0]).startswith("Still running")
        if not still or time.monotonic() > deadline:
            return did
        time.sleep(2)


@pytest.mark.skipif(NODE is None or CURL is None, reason="needs Node.js and curl")
def test_the_apps_script_sends_the_input_sheet_and_writes_the_note(
    stack: Stack, tmp_path: Path
) -> None:
    script, entry = _apps_script(stack, ALIAS, tmp_path)
    did = _run_script(stack, script, entry, [*ACTUALS, *[None] * FUTURE])
    assert did["error"] is None, did
    call = did["fetched"][-1]
    assert call["url"] == f"{m35.api_url()}/v1/endpoints/{ALIAS}/score"
    assert call["status"] == 200, call["body"]
    sent = json.loads(call["payload"])
    # exactly what Sheets sends: the dates as yyyy-MM-dd, blanks as null, the horizon set to
    # the blank rows
    assert sent["mode"] == "forecast"
    assert sent["horizon"] == {"dim": "t", "periods": FUTURE}
    assert sent["data"] == {
        "coords": {"t": {"dims": ["t"], "data": _months("1961-01-01", 3 + FUTURE)}},
        "data_vars": {"y": {"dims": ["t"], "data": [*ACTUALS, *[None] * FUTURE]}},
    }
    answer = json.loads(call["body"])
    cond = answer["summary"]["conditioning"]
    assert cond["conditioned"] and cond["actuals_through"] == "1961-03-01"
    assert cond["series"][0]["forecast_from"] == "1961-04-01" and cond["periods"] == FUTURE
    # the Result sheet: the conditioning note, a blank row, then the table
    (result,) = [w for w in did["writes"] if w["sheet"] == "Result" and not w.get("cleared")]
    grid = result["values"]
    assert grid[0][0] == cond["message"] and "1961-03-01" in grid[0][0]
    assert grid[2][0] == "variable"
    sheet_rows = [r[1:] for r in grid[3:] if r[0]]
    assert [r[0] for r in sheet_rows] == _months("1961-01-01", 3 + FUTURE)

    # the same JSON sent by hand answers with the client's numbers
    client_rows = _rows(_forecast(stack, [*ACTUALS, *[None] * FUTURE]).summary)
    by_hand = httpx.post(
        f"{m35.api_url()}/v1/endpoints/{ALIAS}/score",
        json=sent,
        headers={
            "Authorization": f"Bearer {stack.token}",
            "X-Mapi-Project": f"{m35.TENANT}/{stack.world.project}",
            "Prefer": "wait=60",
        },
        timeout=120,
    )
    assert by_hand.status_code == 200, by_hand.text
    hand_rows = by_hand.json()["summary"]["tables"][0]["rows"]
    np.testing.assert_allclose(
        np.asarray([r[1:] for r in hand_rows], dtype=float),
        np.asarray([r[1:] for r in client_rows], dtype=float),
        rtol=1e-9,
    )
    np.testing.assert_allclose(
        np.asarray([r[1:] for r in sheet_rows], dtype=float),
        np.asarray([r[1:] for r in client_rows], dtype=float),
        rtol=1e-9,
    )

    # other actuals in the sheet: another forecast
    wild = _run_script(stack, script, entry, [*WILD, *[None] * FUTURE])
    assert wild["error"] is None, wild
    wild_answer = json.loads(wild["fetched"][-1]["body"])
    wild_rows = wild_answer["summary"]["tables"][0]["rows"]
    assert [r[0] for r in wild_rows] == [r[0] for r in hand_rows]
    assert wild_rows[3:] != hand_rows[3:]


# ================================================== 5. a model that cannot use actuals


@pytest.fixture(scope="module")
def refusing(stack: Stack) -> str:
    """``ts.hsgp_trend`` (no ``conditions_on_actuals``) fitted on the same series, promoted."""
    rev = stack.op.specs.upsert(HSGP_SPEC.format(project=stack.world.project))
    run = stack.op.runs.fit(rev.spec_id, rev.revision)
    final = stack.op.runs.wait(run.id, timeout=m35.RUN_TIMEOUT_S)
    assert final.state.value == "succeeded", final.problem
    stack.op.endpoints.promote(
        REFUSING,
        f"model://{stack.world.project}/airline-trend@v1",
        override=True,
        reason="a model that cannot condition on actuals",
    )
    return REFUSING


def test_a_model_that_cannot_use_actuals_refuses_them(stack: Stack, refusing: str) -> None:
    schema = stack.op.endpoints.schema(refusing)
    assert schema.snapshot.conditions_on_actuals is False and schema.actuals_example is None
    with pytest.raises(MapiError) as err:
        _forecast(stack, [*ACTUALS, *[None] * FUTURE], alias=refusing)
    problem = err.value
    assert problem.code is ErrorCode.ACTUALS_NOT_SUPPORTED and problem.status == 422
    issue = problem.issues[0]
    assert "cannot update a forecast with actuals" in issue.message
    assert "3 periods (1961-01-01 to 1961-03-01)" in issue.message
    assert issue.suggested_action is not None and "Fit the model again" in issue.suggested_action
    # dates alone (no actuals) are still forecast from the end of the fit
    dates = _forecast(stack, [None] * FUTURE, alias=refusing)
    assert dates.conditioning is not None and not dates.conditioning.conditioned
    assert [r[0] for r in _rows(dates.summary)] == _months("1961-01-01", FUTURE)


@pytest.mark.skipif(NODE is None or CURL is None, reason="needs Node.js and curl")
def test_the_apps_script_writes_the_refusal_into_result(
    stack: Stack, refusing: str, tmp_path: Path
) -> None:
    script, entry = _apps_script(stack, refusing, tmp_path)
    did = _run_script(stack, script, entry, [*ACTUALS, *[None] * FUTURE])
    assert did["fetched"][-1]["status"] == 422
    assert did["error"] is not None and did["error"].startswith("422: ")
    (result,) = [w for w in did["writes"] if w["sheet"] == "Result" and not w.get("cleared")]
    lines = [r[0] for r in result["values"]]
    assert lines[0].startswith("ACTUALS_NOT_SUPPORTED: ")
    assert lines[1].startswith("What to do: Refit the model")
