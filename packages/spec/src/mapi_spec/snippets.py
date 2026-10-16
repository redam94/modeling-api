"""Code snippets for calling an endpoint (plan §11 Integrations hub; M3).

Pure functions, so the server (``GET /v1/endpoints/{alias}/snippets``), the CLI and the docs
render identical code. Every snippet scores ``POST /v1/endpoints/{alias}/score`` with
``Prefer: wait=60`` and an example payload, reading the API token from the environment
(``MAPI_TOKEN``; Apps Script: a script property) - a snippet never contains a secret.

**Quoting.** Values are embedded with each language's own literal syntax, never by string
pasting: Python literals via ``repr``, JavaScript/TypeScript via JSON (with U+2028/U+2029
escaped), shell via single quotes (``'`` → ``'\\''``), R via escaped single-quoted strings.
The base URL, project and alias are validated first (:class:`SnippetContext`), so even a
hostile value cannot break out of its literal; tests parse the generated code back.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Any, Final

from pydantic import Field, JsonValue, field_validator

from mapi_spec._dto import ResponseModel, dto_examples
from mapi_spec._types import StrictModel

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
    ATLAS = "atlas"
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
    """What a snippet is about. ``payload`` is an ``EndpointScoreCall`` body (JSON)."""

    base_url: str = Field(..., max_length=500)
    project: str = Field(..., pattern=rf"^{_SLUG}/{_SLUG}$")
    alias: str = Field(..., pattern=rf"^{_SLUG}$")
    payload: dict[str, JsonValue] = Field(default_factory=dict)
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
    return Snippet(
        language=SnippetLanguage.TYPESCRIPT,
        title="TypeScript (fetch, Node 18+ or a browser)",
        code=code,
    )


def _curl(ctx: SnippetContext) -> Snippet:
    code = (
        f"curl -sS -X POST {sh_quote(ctx.url)} \\\n"
        f'  -H "Authorization: Bearer ${ctx.token_env}" \\\n'
        f"  -H {sh_quote('Content-Type: application/json')} \\\n"
        f"  -H {sh_quote('X-Mapi-Project: ' + ctx.project)} \\\n"
        f"  -H {sh_quote(f'Prefer: wait={ctx.wait}')} \\\n"
        f"  --data {sh_quote(json.dumps(ctx.payload, indent=2, ensure_ascii=False))}\n"
        '# The "summary" field of the answer holds the numbers (mean and HDI per coordinate).\n'
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
    return Snippet(
        language=SnippetLanguage.R,
        title="R (httr2)",
        code=code,
        requires='install.packages("httr2")',
    )


def _apps_script(ctx: SnippetContext) -> Snippet:
    name = "score" + _camel(ctx.alias)
    code = f"""// Extensions > Apps Script. Store the token once under Project Settings >
// Script properties as {ctx.token_env}.
//
// Input (optional): a sheet named "Input" in long format - a header row naming the
// endpoint's dims and input variables, then one row per coordinate - replaces the
// example's inline data. The answer's summary (mean and HDI per coordinate) is written
// to a sheet named "Result".
function {name}() {{
  const token = PropertiesService.getScriptProperties().getProperty({js_literal(ctx.token_env)});
  const payload = {_indent(js_literal(ctx.payload), 2).lstrip()};
  const book = SpreadsheetApp.getActiveSpreadsheet();
  const input = book.getSheetByName("Input");
  if (input) {{
    const key = payload.scenario && payload.scenario.data_vars ? "scenario" : "data";
    payload[key] = mapiSheetToXr_(input.getDataRange().getValues(), payload[key]);
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
  if (response.getResponseCode() >= 300) {{
    throw new Error(response.getResponseCode() + ": " + response.getContentText());
  }}
  const result = JSON.parse(response.getContentText());
  const out = book.getSheetByName("Result") || book.insertSheet("Result");
  out.clear();
  if (!result.done) {{
    const note = "Still running (" + result.run.id + "): run the function again in a minute.";
    out.getRange(1, 1).setValue(note);
    return result;
  }}
  const rows = mapiSummaryRows_(result.summary);
  if (!rows.length) {{
    out.getRange(1, 1).setValue(result.run.state + ": no summary in the answer");
    return result;
  }}
  const width = Math.max(...rows.map((r) => r.length));
  const grid = rows.map((r) => r.concat(new Array(width - r.length).fill("")));
  out.getRange(1, 1, grid.length, width).setValues(grid);
  return result;
}}

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

// A long table (a header row, then rows) as an inline dataset shaped like the example's:
// columns named like its dims hold coordinates, columns named like its variables hold
// values; empty cells become null.
function mapiSheetToXr_(values, example) {{
  if (!example || !example.data_vars) {{
    throw new Error("this endpoint's example call sends no inline data to replace");
  }}
  const header = values[0].map(String);
  const rows = values.slice(1).filter((r) => r.some((c) => c !== ""));
  const column = (name) => {{
    const i = header.indexOf(name);
    if (i < 0) throw new Error('the Input sheet needs a column named "' + name + '"');
    return i;
  }};
  const cell = (v) =>
    v instanceof Date ? Utilities.formatDate(v, Session.getScriptTimeZone(), "yyyy-MM-dd") : v;
  const coords = {{}};
  const index = {{}};
  for (const spec of Object.values(example.data_vars)) {{
    for (const dim of spec.dims) {{
      if (index[dim]) continue;
      const i = column(dim);
      index[dim] = new Map();
      const seen = [];
      for (const r of rows) {{
        const key = cell(r[i]);
        if (!index[dim].has(key)) {{
          index[dim].set(key, seen.length);
          seen.push(key);
        }}
      }}
      coords[dim] = {{ dims: [dim], data: seen }};
    }}
  }}
  const data_vars = {{}};
  for (const [name, spec] of Object.entries(example.data_vars)) {{
    const i = column(name);
    const value = (r) => (r[i] === "" ? null : r[i]);
    if (!spec.dims.length) {{
      data_vars[name] = {{ dims: [], data: rows.length ? value(rows[0]) : null }};
      continue;
    }}
    const last = spec.dims.length - 1;
    const empty = (k) => coords[spec.dims[k]].data.map(() => (k < last ? empty(k + 1) : null));
    const data = empty(0);
    for (const r of rows) {{
      const at = spec.dims.map((dim) => index[dim].get(cell(r[column(dim)])));
      let node = data;
      for (let k = 0; k < at.length - 1; k++) node = node[at[k]];
      node[at[at.length - 1]] = value(r);
    }}
    data_vars[name] = {{ dims: spec.dims, data: data }};
  }}
  return {{ coords: coords, data_vars: data_vars }};
}}
"""
    return Snippet(
        language=SnippetLanguage.APPS_SCRIPT, title="Google Sheets (Apps Script)", code=code
    )


def _atlas(ctx: SnippetContext) -> Snippet:
    code = f"""import os

from mapi_client import Client
from mapi_client.atlas import RemoteModel

api = Client(
    {py_literal(ctx.base_url)},
    token=os.environ[{py_literal(ctx.token_env)}],
    project={py_literal(ctx.project)},
)
# An Atlas model backed by the endpoint: Atlas's optimiser scores budgets against it.
model = RemoteModel(api, endpoint={py_literal(ctx.alias)})
"""
    return Snippet(
        language=SnippetLanguage.ATLAS,
        title="Atlas (RemoteModel)",
        code=code,
        requires="pip install 'mapi-client[atlas]'",
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
    SnippetLanguage.ATLAS: _atlas,
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
