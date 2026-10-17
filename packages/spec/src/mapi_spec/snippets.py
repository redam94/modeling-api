"""Code snippets for calling an endpoint (plan §11 Integrations hub; M3).

Pure functions, so the server (``GET /v1/endpoints/{alias}/snippets``), the CLI and the docs
render identical code. Every snippet scores ``POST /v1/endpoints/{alias}/score`` with
``Prefer: wait=60`` and an example payload, reading the API token from the environment
(``MAPI_TOKEN``; Apps Script: a script property) - a snippet never contains a secret.

**Actuals and future dates.** With the endpoint's input table
(:attr:`SnippetContext.input_table`, ``EndpointSchema.input_table``) the Apps Script reads an
"Input" sheet in that layout - actual rows with the target filled, rows to forecast with it
blank, dates as ``yyyy-MM-dd`` - even when the example call sends no data, can create that
sheet, and writes the forecast's conditioning note above the tables in "Result". The other
snippets add a forecast over the table's rows (:attr:`SnippetContext.table_call`) to fill
with actuals (:mod:`mapi_spec.actuals`).

**Quoting.** Values are embedded with each language's own literal syntax, never by string
pasting: Python literals via ``repr``, JavaScript/TypeScript via JSON (with U+2028/U+2029
escaped), shell via single quotes (``'`` → ``'\\''``), R via escaped single-quoted strings.
The base URL, project and alias are validated first (:class:`SnippetContext`), so even a
hostile value cannot break out of its literal; tests parse the generated code back.
"""

from __future__ import annotations

import json
import re
import textwrap
from collections.abc import Iterable
from enum import StrEnum
from typing import Any, Final

from pydantic import Field, JsonValue, field_validator

from mapi_spec._dto import ResponseModel, dto_examples
from mapi_spec._types import StrictModel
from mapi_spec.endpoints import InputTable, table_forecast_call
from mapi_spec.xrjson import XrJsonDataset

__all__ = [
    "SNIPPET_LANGUAGES",
    "Snippet",
    "SnippetContext",
    "SnippetLanguage",
    "SnippetSet",
    "js_literal",
    "py_literal",
    "r_string",
    "render_snippet",
    "render_snippets",
    "sh_quote",
]

_BASE_URL_RE: Final = re.compile(
    r"^https?://[A-Za-z0-9.\-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~\-/]*)?$"
)
_SLUG: Final = r"[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?"
_ENV_RE: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class SnippetLanguage(StrEnum):
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    CURL = "curl"
    R = "r"
    APPS_SCRIPT = "apps_script"
    NOTEBOOK = "notebook"


SNIPPET_LANGUAGES: Final = tuple(SnippetLanguage)


class Snippet(ResponseModel):
    """Ready-to-run code. ``code`` is platform-generated; it embeds the example payload."""

    language: SnippetLanguage
    title: str = Field(..., max_length=200)
    code: str = Field(..., max_length=200_000)
    requires: str | None = Field(None, max_length=500, description="What to install first")

    model_config = dto_examples(
        {
            "language": "curl",
            "title": "curl",
            "code": "curl -sS -X POST 'https://api.example.com/v1/endpoints/geo-mmm-prod/score' "
            '\\\n  -H "Authorization: Bearer $MAPI_TOKEN" \\\n  ...',
            "requires": None,
        }
    )


class SnippetSet(ResponseModel):
    """``GET /v1/endpoints/{alias}/snippets`` (``?language=`` narrows it)."""

    alias: str
    items: list[Snippet] = Field(default_factory=list)

    model_config = dto_examples(
        {"alias": "geo-mmm-prod", "items": [Snippet.model_json_schema()["examples"][0]]}
    )


class SnippetContext(StrictModel):
    """What a snippet is about. ``payload`` is an ``EndpointScoreCall`` body (JSON).

    ``input_table`` (``EndpointSchema.input_table``) is the endpoint's inline input as a long
    table: the Apps Script reads an "Input" sheet with its columns even when ``payload``
    sends no data, and, for a forecasting endpoint with example rows, every snippet adds a
    forecast over those rows (:attr:`table_call`) - the call to fill with actuals.
    """

    base_url: str = Field(..., max_length=500)
    project: str = Field(..., pattern=rf"^{_SLUG}/{_SLUG}$")
    alias: str = Field(..., pattern=rf"^{_SLUG}$")
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    input_table: InputTable | None = None
    token_env: str = "MAPI_TOKEN"
    wait: int = Field(60, ge=0, le=60)

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.rstrip("/")
        if not _BASE_URL_RE.match(v):
            raise ValueError("base_url must be a plain http(s) URL (host, port and path only)")
        return v

    @field_validator("token_env")
    @classmethod
    def _env(cls, v: str) -> str:
        if not _ENV_RE.match(v):
            raise ValueError("token_env must be an upper-case environment variable name")
        return v

    @property
    def url(self) -> str:
        return f"{self.base_url}/v1/endpoints/{self.alias}/score"

    @property
    def table_rows(self) -> list[dict[str, JsonValue]]:
        """The input table's example rows as mappings (column -> cell)."""
        t = self.input_table
        if t is None:
            return []
        return [dict(zip(t.header, row, strict=True)) for row in t.rows]

    @property
    def table_call(self) -> dict[str, JsonValue] | None:
        """A forecast over the input table's example rows (``None`` without them): the
        periods after the fit with the target and drivers empty - the call a caller fills
        with actuals (:mod:`mapi_spec.actuals`). Empty drivers are carried forward."""
        t = self.input_table
        rows = self.table_rows
        if t is None or not rows or t.horizon_dim is None:
            return None
        output = self.payload.get("output")
        call = table_forecast_call(t, rows, output=output if isinstance(output, dict) else None)
        out: dict[str, JsonValue] = call.model_dump(
            mode="json", by_alias=True, exclude_defaults=True
        )
        out["future_covariates"] = call.future_covariates.value
        if isinstance(call.data, XrJsonDataset):
            out["data"] = call.data.model_dump(mode="json", exclude_defaults=True)
        return out

    @property
    def targets(self) -> list[str]:
        t = self.input_table
        return [c.name for c in t.columns if c.role == "target"] if t is not None else []


# ===================================================================================== literals


def py_literal(value: Any, indent: int = 0) -> str:
    """A Python expression equal to the JSON ``value`` (``ast.literal_eval``-safe)."""
    pad, inner = " " * indent, " " * (indent + 4)
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [f"{inner}{str(k)!r}: {py_literal(v, indent + 4)}," for k, v in value.items()]
        return "{\n" + "\n".join(items) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        if all(not isinstance(v, dict | list) for v in value):
            return "[" + ", ".join(py_literal(v) for v in value) + "]"
        items = [f"{inner}{py_literal(v, indent + 4)}," for v in value]
        return "[\n" + "\n".join(items) + f"\n{pad}]"
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("payloads are JSON: NaN and infinity are not allowed")
        return repr(value)
    if isinstance(value, str):
        return repr(value)
    raise TypeError(f"not a JSON value: {type(value).__name__}")


def js_literal(value: Any, indent: int | None = 2) -> str:
    """A JavaScript/TypeScript expression equal to the JSON ``value`` (JSON is a JS
    expression; U+2028 and U+2029 are escaped for engines before ES2019)."""
    text = json.dumps(value, indent=indent, ensure_ascii=False, allow_nan=False)
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def sh_quote(text: str) -> str:
    """A POSIX shell single-quoted word (``'`` becomes ``'\\''``)."""
    return "'" + text.replace("'", "'\\''") + "'"


def r_string(text: str) -> str:
    """An R single-quoted string literal."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line if line else line for line in text.splitlines())


def _camel(alias: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[-_]", alias) if p)


# ===================================================================================== snippets


def _wrap(text: str, prefix: str, width: int = 92) -> str:
    """``text`` as comment lines starting with ``prefix``."""
    return "\n".join(textwrap.wrap(text, width, initial_indent=prefix, subsequent_indent=prefix))


def _actuals_note(ctx: SnippetContext, empty: str) -> str:
    """What the table call is for (``empty``: the language's null)."""
    t = ctx.input_table
    targets = ", ".join(repr(n) for n in ctx.targets) or "the target"
    if t is not None and t.conditions_on_actuals:
        return (
            f"Forecast with your latest actuals and the dates to forecast: in {targets} put the "
            f"actuals you have for the periods after the fitted history and leave {empty} on "
            "the periods to forecast (driver values left empty there are carried forward). The "
            "model updates its state with the actuals - it is not refitted - and "
            "summary.conditioning in the answer says what the forecast was conditioned on."
        )
    return (
        f"Forecast given dates: the periods to forecast with {targets} left {empty} and the "
        "driver values you know (empty ones are carried forward). This model forecasts from "
        "the end of its fitted history; it refuses target values (ACTUALS_NOT_SUPPORTED)."
    )


def _py_row(row: dict[str, JsonValue]) -> str:
    return "{" + ", ".join(f"{k!r}: {py_literal(v)}" for k, v in row.items()) + "}"


def _python(ctx: SnippetContext) -> Snippet:
    code = f"""import os

from mapi_client import Client

api = Client(
    {py_literal(ctx.base_url)},
    token=os.environ[{py_literal(ctx.token_env)}],
    project={py_literal(ctx.project)},
)
result = api.endpoints.score(
    {py_literal(ctx.alias)},
    {py_literal(ctx.payload, 4)},
    wait={ctx.wait},
)
print(result.run.state, [link.name for link in result.outputs])
# the numbers: one table per output (coordinates, then mean and HDI)
for table in result.summary.tables if result.summary else []:
    print(table.variable, table.columns, table.rows[:5])
"""
    if ctx.table_call is not None:
        rows = "\n".join(f"    {_py_row(r)}," for r in ctx.table_rows)
        code += f"""
{_wrap(_actuals_note(ctx, "None"), "# ")}
# (A pandas DataFrame with these columns works too.)
rows = [
{rows}
]
forecast = api.endpoints.forecast({py_literal(ctx.alias)}, rows, wait={ctx.wait})
print(forecast.conditioning.message if forecast.conditioning else forecast.run.state)
for table in forecast.summary.tables if forecast.summary else []:
    print(table.variable, table.columns, table.rows[:5])
"""
    return Snippet(
        language=SnippetLanguage.PYTHON,
        title="Python (mapi-client)",
        code=code,
        requires="pip install mapi-client",
    )


def _typescript(ctx: SnippetContext) -> Snippet:
    headers = {
        "Content-Type": "application/json",
        "X-Mapi-Project": ctx.project,
        "Prefer": f"wait={ctx.wait}",
    }
    header_lines = "\n".join(f"    {js_literal(k)}: {js_literal(v)}," for k, v in headers.items())
    code = f"""const payload = {js_literal(ctx.payload)};

const response = await fetch({js_literal(ctx.url)}, {{
  method: "POST",
  headers: {{
    Authorization: `Bearer ${{process.env[{js_literal(ctx.token_env)}]}}`,
{header_lines}
  }},
  body: JSON.stringify(payload),
}});
if (!response.ok) {{
  throw new Error(`${{response.status}}: ${{await response.text()}}`);
}}
const result = await response.json(); // 200: done with outputs; 202: still running
// the numbers: one table per output (coordinates, then mean and HDI)
console.log(result.done ? result.summary?.tables : result.run.id);
"""
    if ctx.table_call is not None:
        code += f"""
{_wrap(_actuals_note(ctx, "null"), "// ")}
// Send it as the body instead of `payload`.
const actualsPayload = {js_literal(ctx.table_call)};
// result.summary?.conditioning?.message, e.g. "Forecast updated with 3 actuals (...)"
"""
    return Snippet(
        language=SnippetLanguage.TYPESCRIPT,
        title="TypeScript (fetch, Node 18+ or a browser)",
        code=code,
    )


def _curl_call(ctx: SnippetContext, payload: dict[str, JsonValue]) -> str:
    return (
        f"curl -sS -X POST {sh_quote(ctx.url)} \\\n"
        f'  -H "Authorization: Bearer ${ctx.token_env}" \\\n'
        f"  -H {sh_quote('Content-Type: application/json')} \\\n"
        f"  -H {sh_quote('X-Mapi-Project: ' + ctx.project)} \\\n"
        f"  -H {sh_quote(f'Prefer: wait={ctx.wait}')} \\\n"
        f"  --data {sh_quote(json.dumps(payload, indent=2, ensure_ascii=False))}\n"
    )


def _curl(ctx: SnippetContext) -> Snippet:
    code = (
        _curl_call(ctx, ctx.payload)
        + '# The "summary" field of the answer holds the numbers (mean and HDI per coordinate).\n'
    )
    if ctx.table_call is not None:
        code += (
            "\n"
            + _wrap(_actuals_note(ctx, "null"), "# ")
            + "\n"
            + _curl_call(ctx, ctx.table_call)
            + '# "summary.conditioning.message" says what the forecast was conditioned on.\n'
        )
    return Snippet(language=SnippetLanguage.CURL, title="curl", code=code)


def _r(ctx: SnippetContext) -> Snippet:
    body = json.dumps(ctx.payload, indent=2, ensure_ascii=False)
    prefer = f"wait={ctx.wait}"
    code = f"""library(httr2)

resp <- request({r_string(ctx.url)}) |>
  req_auth_bearer_token(Sys.getenv({r_string(ctx.token_env)})) |>
  req_headers(`X-Mapi-Project` = {r_string(ctx.project)}, Prefer = {r_string(prefer)}) |>
  req_body_raw({r_string(body)}, type = "application/json") |>
  req_perform()
result <- resp_body_json(resp)
str(result$run$state)
# the numbers: one table per output (coordinates, then mean and HDI)
str(result$summary$tables)
"""
    if ctx.table_call is not None:
        actuals = json.dumps(ctx.table_call, indent=2, ensure_ascii=False)
        code += f"""
{_wrap(_actuals_note(ctx, "null"), "# ")}
forecast <- request({r_string(ctx.url)}) |>
  req_auth_bearer_token(Sys.getenv({r_string(ctx.token_env)})) |>
  req_headers(`X-Mapi-Project` = {r_string(ctx.project)}, Prefer = {r_string(prefer)}) |>
  req_body_raw({r_string(actuals)}, type = "application/json") |>
  req_perform() |>
  resp_body_json()
cat(forecast$summary$conditioning$message, "\\n")
str(forecast$summary$tables)
"""
    return Snippet(
        language=SnippetLanguage.R,
        title="R (httr2)",
        code=code,
        requires='install.packages("httr2")',
    )


def _apps_script_input_doc(ctx: SnippetContext, name: str) -> str:
    """The header comment's paragraph on the Input sheet."""
    t = ctx.input_table
    if t is None or not t.columns:
        return (
            '// Input (optional): a sheet named "Input" in long format - a header row naming the\n'
            "// endpoint's dims and input variables, then one row per coordinate - replaces the\n"
            "// example's inline data."
        )
    header = ", ".join(t.header)
    text = (
        f'Input (optional): a sheet named "Input" in long format - a header row ({header}), then '
        "one row per coordinate, dates as yyyy-MM-dd - is sent as the call's data."
    )
    if t.horizon_dim is not None and ctx.targets:
        targets = ", ".join(ctx.targets)
        if t.conditions_on_actuals:
            text += (
                f" Fill {targets} with your latest actuals and leave it blank on the dates to "
                "forecast: the model updates its state with the actuals (it is not refitted) "
                "and forecasts the blank rows."
            )
        else:
            text += (
                f" List the dates to forecast with {targets} left blank; this model forecasts "
                "from the end of its fitted history and refuses actuals."
            )
        if any(c.role == "driver" for c in t.columns):
            text += " Driver cells left blank on those dates are carried forward."
    if t.rows:
        text += f" Run {name}InputSheet() once to create the sheet with the periods after the fit."
    return _wrap(text, "// ")


def _apps_script(ctx: SnippetContext) -> Snippet:
    name = "score" + _camel(ctx.alias)
    t = ctx.input_table
    inputs = js_literal(t.variables if t is not None and t.columns else None)
    sheet_fn = ""
    if t is not None and t.rows:
        cells = [[("" if c is None else c) for c in r] for r in t.rows]
        table = "[\n" + "".join(f"    {js_literal(r, None)},\n" for r in cells) + "  ]"
        sheet_fn = f"""
// Creates the "Input" sheet: the header row and the periods after the fitted history, every
// value blank. Fill in what you know, then run {name}().
function {name}InputSheet() {{
  const book = SpreadsheetApp.getActiveSpreadsheet();
  if (book.getSheetByName("Input")) {{
    throw new Error('There is an "Input" sheet already: rename or delete it to start over.');
  }}
  const header = {js_literal(t.header, None)};
  const rows = {table};
  const sheet = book.insertSheet("Input");
  sheet.getRange(1, 1, 1, header.length).setValues([header]);
  if (rows.length) sheet.getRange(2, 1, rows.length, header.length).setValues(rows);
  sheet.setFrozenRows(1);
}}
"""
    code = f"""// Extensions > Apps Script. Store the token once under Project Settings >
// Script properties as {ctx.token_env}.
//
{_apps_script_input_doc(ctx, name)}
// The answer's summary (per coordinate) is written to a sheet named "Result", under a note
// on what a forecast was conditioned on.
function {name}() {{
  const token = PropertiesService.getScriptProperties().getProperty({js_literal(ctx.token_env)});
  const payload = {_indent(js_literal(ctx.payload), 2).lstrip()};
  const inputs = {_indent(inputs, 2).lstrip()};
  const book = SpreadsheetApp.getActiveSpreadsheet();
  const input = book.getSheetByName("Input");
  if (input) {{
    const key = payload.scenario && payload.scenario.data_vars ? "scenario" : "data";
    const zone = book.getSpreadsheetTimeZone
      ? book.getSpreadsheetTimeZone()
      : Session.getScriptTimeZone();
    payload[key] = mapiSheetToXr_(input.getDataRange().getValues(), payload[key], inputs, zone);
    if (key === "data" && payload.mode === "forecast" && payload.horizon) {{
      payload.horizon.periods = mapiForecastPeriods_(payload, payload.horizon.periods);
    }}
  }}
  const response = UrlFetchApp.fetch({js_literal(ctx.url)}, {{
    method: "post",
    contentType: "application/json",
    headers: {{
      Authorization: "Bearer " + token,
      "X-Mapi-Project": {js_literal(ctx.project)},
      Prefer: {js_literal(f"wait={ctx.wait}")},
    }},
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  }});
  const out = book.getSheetByName("Result") || book.insertSheet("Result");
  if (response.getResponseCode() >= 300) {{
    const problem = mapiProblemRows_(response.getContentText());
    if (problem.length) {{
      out.clear();
      out.getRange(1, 1, problem.length, 1).setValues(problem);
    }}
    throw new Error(response.getResponseCode() + ": " + response.getContentText());
  }}
  const result = JSON.parse(response.getContentText());
  out.clear();
  if (!result.done) {{
    const note = "Still running (" + result.run.id + "): run the function again in a minute.";
    out.getRange(1, 1).setValue(note);
    return result;
  }}
  const tables = mapiSummaryRows_(result.summary);
  if (!tables.length) {{
    const why = result.run.problem ? mapiProblemRows_(JSON.stringify(result.run.problem)) : [];
    const rows = [[result.run.state + ": no summary in the answer"]].concat(why);
    out.getRange(1, 1, rows.length, 1).setValues(rows);
    return result;
  }}
  const conditioning = result.summary.conditioning;
  const rows = conditioning ? [[conditioning.message], [""]].concat(tables) : tables;
  const width = Math.max(...rows.map((r) => r.length));
  const grid = rows.map((r) => r.concat(new Array(width - r.length).fill("")));
  out.getRange(1, 1, grid.length, width).setValues(grid);
  return result;
}}
{sheet_fn}
// The summary tables one below the other: a header row ("variable", the coordinate
// columns, the statistics), one row per coordinate, a blank row between tables.
function mapiSummaryRows_(summary) {{
  const rows = [];
  for (const table of (summary && summary.tables) || []) {{
    if (rows.length) rows.push([""]);
    rows.push(["variable"].concat(table.columns));
    for (const row of table.rows) {{
      rows.push([table.variable].concat(row.map((c) => (c === null ? "" : c))));
    }}
  }}
  if (summary && summary.truncated) {{
    rows.push(["(truncated: the full result is in the run's outputs)"]);
  }}
  return rows;
}}

// A problem answer (or a failed run's problem) as rows: what went wrong, what to do.
function mapiProblemRows_(text) {{
  let problem;
  try {{
    problem = JSON.parse(text);
  }} catch (e) {{
    return [];
  }}
  if (!problem || !problem.code) return [];
  const first = (problem.errors && problem.errors[0]) || {{}};
  const rows = [[problem.code + ": " + (first.message || problem.detail || problem.title)]];
  if (first.suggested_action) rows.push(["What to do: " + first.suggested_action]);
  return rows;
}}

// A long table (a header row, then rows) as an inline dataset: columns named like a dim
// hold coordinates, columns named like an input variable (`inputs`, else the example's
// variables) hold values; empty cells become null - in a forecast an empty target is a
// period to forecast. Dates are sent as yyyy-MM-dd.
function mapiSheetToXr_(values, example, inputs, zone) {{
  let vars = inputs;
  if (!vars && example && example.data_vars) {{
    vars = {{}};
    for (const [name, spec] of Object.entries(example.data_vars)) vars[name] = spec.dims;
  }}
  if (!vars) throw new Error("this endpoint's example call sends no inline data to replace");
  const header = values[0].map(String);
  const rows = values.slice(1).filter((r) => r.some((c) => c !== ""));
  const used = Object.keys(vars).filter((name) => header.indexOf(name) >= 0);
  if (!used.length) {{
    throw new Error("the Input sheet needs a column named one of: " + Object.keys(vars).join(", "));
  }}
  const column = (name) => {{
    const i = header.indexOf(name);
    if (i < 0) throw new Error('the Input sheet needs a column named "' + name + '"');
    return i;
  }};
  const cell = (v) => (v instanceof Date ? Utilities.formatDate(v, zone, "yyyy-MM-dd") : v);
  const coords = {{}};
  const index = {{}};
  for (const name of used) {{
    for (const dim of vars[name]) {{
      if (index[dim]) continue;
      const i = column(dim);
      index[dim] = new Map();
      const seen = [];
      rows.forEach((r, k) => {{
        const key = cell(r[i]);
        if (key === "") {{
          throw new Error("row " + (k + 2) + ' of the Input sheet has no "' + dim + '"');
        }}
        if (!index[dim].has(key)) {{
          index[dim].set(key, seen.length);
          seen.push(key);
        }}
      }});
      coords[dim] = {{ dims: [dim], data: seen }};
    }}
  }}
  const data_vars = {{}};
  for (const name of used) {{
    const dims = vars[name];
    const i = column(name);
    const value = (r) => (r[i] === "" ? null : cell(r[i]));
    if (!dims.length) {{
      data_vars[name] = {{ dims: [], data: rows.length ? value(rows[0]) : null }};
      continue;
    }}
    const last = dims.length - 1;
    const empty = (k) => coords[dims[k]].data.map(() => (k < last ? empty(k + 1) : null));
    const data = empty(0);
    for (const r of rows) {{
      const v = value(r);
      if (v === null) continue;
      const at = dims.map((dim) => index[dim].get(cell(r[column(dim)])));
      let node = data;
      for (let k = 0; k < last; k++) node = node[at[k]];
      if (node[at[last]] !== null && node[at[last]] !== v) {{
        throw new Error('"' + name + '" has two different values at one row of the Input sheet');
      }}
      node[at[last]] = v;
    }}
    data_vars[name] = {{ dims: dims, data: data }};
  }}
  return {{ coords: coords, data_vars: data_vars }};
}}

// The periods to forecast: the rows after the last one with a target value (every row
// when none has one); the horizon's own periods when the sheet ends with an actual.
function mapiForecastPeriods_(payload, fallback) {{
  const dim = payload.horizon.dim;
  const labels = payload.data.coords[dim] ? payload.data.coords[dim].data : [];
  const filled = new Set();
  for (const name of {js_literal(ctx.targets, None)}) {{
    const spec = payload.data.data_vars[name];
    if (!spec) continue;
    const axis = spec.dims.indexOf(dim);
    const walk = (node, depth, at) => {{
      if (depth === spec.dims.length) {{
        if (node !== null) filled.add(at);
        return;
      }}
      node.forEach((child, k) => walk(child, depth + 1, depth === axis ? k : at));
    }};
    if (axis >= 0) walk(spec.data, 0, -1);
  }}
  const order = labels.map((_, k) => k).sort((a, b) => (labels[a] < labels[b] ? -1 : 1));
  if (!filled.size) return labels.length || fallback;
  let after = 0;
  for (let k = order.length - 1; k >= 0 && !filled.has(order[k]); k--) after++;
  return after || fallback;
}}
"""
    return Snippet(
        language=SnippetLanguage.APPS_SCRIPT, title="Google Sheets (Apps Script)", code=code
    )


#: Keyword arguments of ``RemoteEndpoint.score`` itself; a payload field with one of these names
#: cannot be passed as a keyword, so such a payload goes through ``api.endpoints.score``.
_REMOTE_SCORE_KWARGS: Final = frozenset({"data", "group", "wait", "timeout", "conform"})


def _notebook(ctx: SnippetContext) -> Snippet:
    head = f"""# A notebook cell (Jupyter or marimo): the endpoint as an object, results as xarray.
import os

from mapi_client import Client

api = Client(
    {py_literal(ctx.base_url)},
    token=os.environ[{py_literal(ctx.token_env)}],
    project={py_literal(ctx.project)},
)
endpoint = api.endpoint({py_literal(ctx.alias)})
schema = endpoint.schema()  # the input contract and an example call
"""
    fields = {k: v for k, v in ctx.payload.items() if k != "data"}
    if _REMOTE_SCORE_KWARGS.isdisjoint(fields) and all(k.isidentifier() for k in fields):
        body = f"""
# The example call. For your own inputs, pass an xarray.Dataset shaped like
# schema.snapshot.input_schema as `data`.
data = {py_literal(ctx.payload.get("data"))}
call = {py_literal(fields)}
ds = endpoint.score(data, wait={ctx.wait}, **call)
ds  # the summary: one variable per output, with a `stat` dim (mean, hdi_90_lower, ...)
"""
    else:
        body = f"""
call = {py_literal(ctx.payload)}
result = api.endpoints.score({py_literal(ctx.alias)}, call, wait={ctx.wait})
ds = result.tree["summary"].to_dataset()
ds
"""
    return Snippet(
        language=SnippetLanguage.NOTEBOOK,
        title="Notebook (mapi-client, xarray)",
        code=head + body,
        requires="pip install 'mapi-client[xarray]'",
    )


_RENDER = {
    SnippetLanguage.PYTHON: _python,
    SnippetLanguage.TYPESCRIPT: _typescript,
    SnippetLanguage.CURL: _curl,
    SnippetLanguage.R: _r,
    SnippetLanguage.APPS_SCRIPT: _apps_script,
    SnippetLanguage.NOTEBOOK: _notebook,
}


def render_snippet(language: SnippetLanguage | str, ctx: SnippetContext) -> Snippet:
    """One snippet (``ValueError`` for an unknown language)."""
    return _RENDER[SnippetLanguage(language)](ctx)


def render_snippets(
    ctx: SnippetContext, languages: Iterable[SnippetLanguage | str] | None = None
) -> SnippetSet:
    """Snippets in ``languages`` (default: all, in :data:`SNIPPET_LANGUAGES` order)."""
    langs = SNIPPET_LANGUAGES if languages is None else [SnippetLanguage(x) for x in languages]
    return SnippetSet(alias=ctx.alias, items=[render_snippet(lang, ctx) for lang in langs])
