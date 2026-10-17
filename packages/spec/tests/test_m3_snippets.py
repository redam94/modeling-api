"""M3 snippets: golden outputs per language, and injection-safe quoting - generated code is
parsed back and must carry exactly the payload, URL and alias it was given.

Regenerate the goldens on purpose with
``MAPI_UPDATE_SNAPSHOTS=1 uv run pytest packages/spec/tests/test_m3_snippets.py``.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

import pytest
from mapi_spec.endpoints import EndpointScoreCall
from mapi_spec.snippets import (
    SNIPPET_LANGUAGES,
    SnippetContext,
    SnippetLanguage,
    js_literal,
    py_literal,
    r_string,
    render_snippet,
    render_snippets,
    sh_quote,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit

GOLDEN = Path(__file__).parent / "snapshots" / "snippets"
CTX = SnippetContext(
    base_url="https://api.mapi.example",
    project="acme/growth",
    alias="geo-mmm-prod",
    payload=EndpointScoreCall.model_json_schema()["examples"][0],
)
HOSTILE: dict[str, Any] = {
    "mode": "predict",
    "labels": {
        "note": 'it\'s "quoted" \\ back $(rm -rf /) `id` ${HOME} </script> \'\'\' """ %s {x}',
        "lines": "a\nb\rc\td e f\x00g",
        "emoji": "café 😀 ‮",
    },
    "nested": {"a": [1, 2.5, None, True, False, -0.0, 1e-300], "": {}},
}
HOSTILE_CTX = CTX.model_copy(update={"payload": HOSTILE})


@pytest.mark.parametrize("language", SNIPPET_LANGUAGES, ids=lambda x: x.value)
def test_golden_outputs(language: SnippetLanguage) -> None:
    snippet = render_snippet(language, CTX)
    path = GOLDEN / f"{language.value}.txt"
    if os.environ.get("MAPI_UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(snippet.code, "utf-8")
    assert snippet.code == path.read_text("utf-8"), f"regenerate {path.name} on purpose"
    assert "mapi_" not in snippet.code.replace("mapi_client", "")  # never a token
    assert "MAPI_TOKEN" in snippet.code


def test_render_snippets_defaults_to_every_language() -> None:
    every = render_snippets(CTX)
    assert [s.language for s in every.items] == list(SNIPPET_LANGUAGES)
    assert every.alias == "geo-mmm-prod"
    assert [s.language for s in render_snippets(CTX, ["curl"]).items] == [SnippetLanguage.CURL]
    with pytest.raises(ValueError):
        render_snippet("cobol", CTX)


# --------------------------------------------------------------------------- parse back


def _call(tree: ast.Module, attr: str) -> ast.Call:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name == attr:
                return node
    raise AssertionError(f"no call to {attr}")


def test_python_carries_the_exact_payload() -> None:
    code = render_snippet("python", HOSTILE_CTX).code
    tree = ast.parse(code)
    score = _call(tree, "score")
    assert ast.literal_eval(score.args[0]) == "geo-mmm-prod"
    assert ast.literal_eval(score.args[1]) == HOSTILE
    client = _call(tree, "Client")
    assert ast.literal_eval(client.args[0]) == "https://api.mapi.example"


def test_an_unknown_snippet_language_is_refused() -> None:
    with pytest.raises(ValueError):
        render_snippet("cobol", HOSTILE_CTX)


def test_notebook_carries_the_exact_payload() -> None:
    payload = {"mode": "predict", "data": {"coords": {}, "data_vars": {}}} | HOSTILE
    code = render_snippet("notebook", HOSTILE_CTX.model_copy(update={"payload": payload})).code
    tree = ast.parse(code)
    assert ast.literal_eval(_call(tree, "endpoint").args[0]) == "geo-mmm-prod"
    assigned = {
        t.id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    fields = {k: v for k, v in payload.items() if k != "data"}
    assert ast.literal_eval(assigned["data"]) == payload["data"]
    assert ast.literal_eval(assigned["call"]) == fields
    score = _call(tree, "score")
    assert [k.arg for k in score.keywords] == ["wait", None]  # wait=60, **call


def test_notebook_falls_back_when_a_field_shadows_a_keyword() -> None:
    payload = {"mode": "predict", "timeout": "10m"}
    code = render_snippet("notebook", CTX.model_copy(update={"payload": payload})).code
    tree = ast.parse(code)
    score = _call(tree, "score")
    assert ast.literal_eval(score.args[0]) == "geo-mmm-prod"
    assert "api.endpoints.score(" in code and "endpoint.score(" not in code


def _js_payload(code: str) -> Any:
    m = re.search(r"const payload = (.*?);\n", code, re.DOTALL)
    assert m is not None
    return json.loads(m.group(1))


@pytest.mark.parametrize("language", ["typescript", "apps_script"])
def test_javascript_carries_the_exact_payload(language: str) -> None:
    code = render_snippet(language, HOSTILE_CTX).code
    assert _js_payload(code) == HOSTILE
    assert " " not in code and " " not in code
    assert json.dumps(HOSTILE_CTX.url) in code


_SHEETS_HARNESS = """
const sent = [];
const sheets = {};
function sheet(name, values) {
  const s = { name, values, cleared: false, cells: {},
    getDataRange: () => ({ getValues: () => values }),
    clear: () => { s.cleared = true; s.cells = {}; },
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
  getSheetByName: (n) => sheets[n] || null, insertSheet: (n) => sheet(n, []) }) };
globalThis.Session = { getScriptTimeZone: () => "UTC" };
globalThis.Utilities = { formatDate: (d) => d.toISOString().slice(0, 10) };
globalThis.UrlFetchApp = { fetch: (url, opts) => {
  sent.push({ url, opts, payload: JSON.parse(opts.payload) });
  return { getResponseCode: () => 200, getContentText: () => JSON.stringify(ANSWER) };
} };
FUNC();
console.log(JSON.stringify({ sent, result: sheets.Result ? sheets.Result.cells : null }));
"""


def _run_apps_script(ctx: SnippetContext, sheet: Any, answer: Any) -> Any:
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    code = render_snippet("apps_script", ctx).code
    name = re.search(r"function (score\w+)\(", code)
    assert name is not None
    script = code + _SHEETS_HARNESS.replace("FUNC()", name.group(1) + "()")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sheet.js"
        path.write_text(script, "utf-8")
        out = subprocess.run(
            [node, str(path), json.dumps(sheet), json.dumps(answer)],
            capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


SUMMARY_ANSWER = {
    "done": True,
    "run": {"id": "run_1", "state": "succeeded"},
    "outputs": [],
    "summary": {
        "tables": [
            {"variable": "revenue", "columns": ["geo", "time", "mean", "hdi_90_lower"],
             "rows": [["de", "2026-01-05", 10.5, 9.0], ["fr", "2026-01-05", 7.25, None]]},
            {"variable": "roi", "columns": ["mean"], "rows": [[1.5]]},
        ],
        "truncated": False,
    },
}  # fmt: skip


def test_apps_script_writes_the_summary_table_to_the_sheet() -> None:
    """The Google Sheets card delivers numbers: the answer's summary lands in a "Result"
    sheet, one table per output (not just the word "done")."""
    run = _run_apps_script(CTX, None, SUMMARY_ANSWER)
    [call] = run["sent"]
    assert call["payload"] == CTX.payload and call["url"] == CTX.url
    assert call["opts"]["headers"]["Authorization"] == "Bearer tok"
    cells = run["result"]
    assert [cells[f"1,{c}"] for c in range(1, 6)] == ["variable", "geo", "time", "mean",
                                                      "hdi_90_lower"]  # fmt: skip
    assert [cells[f"2,{c}"] for c in range(1, 6)] == ["revenue", "de", "2026-01-05", 10.5, 9.0]
    assert cells["3,5"] == "" and cells["3,4"] == 7.25
    assert cells["4,1"] == "" and cells["5,1"] == "variable" and cells["6,2"] == 1.5
    pending = _run_apps_script(CTX, None, {"done": False, "run": {"id": "run_9"}})
    assert "Still running (run_9)" in pending["result"]["1,1"]


def test_apps_script_builds_the_payload_from_the_input_sheet() -> None:
    example = {
        "mode": "predict",
        "data": {
            "coords": {"time": {"dims": ["time"], "data": ["2026-01-05"]},
                       "geo": {"dims": ["geo"], "data": ["de"]}},
            "data_vars": {"spend": {"dims": ["time", "geo"], "data": [[0.0]]},
                          "price": {"dims": ["geo"], "data": [0.0]}},
        },
    }  # fmt: skip
    ctx = CTX.model_copy(update={"payload": example})
    sheet = [
        ["time", "geo", "spend", "price", "ignored"],
        ["date:2026-01-05", "de", 100, 2.5, "x"],
        ["date:2026-01-05", "fr", 80, 3, "x"],
        ["date:2026-01-12", "de", "", 2.5, "x"],
        ["", "", "", "", ""],
    ]
    run = _run_apps_script(ctx, sheet, SUMMARY_ANSWER)
    data = run["sent"][0]["payload"]["data"]
    assert run["sent"][0]["payload"]["mode"] == "predict"
    assert data["coords"] == {"time": {"dims": ["time"], "data": ["2026-01-05", "2026-01-12"]},
                              "geo": {"dims": ["geo"], "data": ["de", "fr"]}}  # fmt: skip
    assert data["data_vars"]["spend"] == {
        "dims": ["time", "geo"],
        "data": [[100, 80], [None, None]],
    }
    assert data["data_vars"]["price"] == {"dims": ["geo"], "data": [2.5, 3]}
    # the payload is a valid endpoint call
    EndpointScoreCall.model_validate(run["sent"][0]["payload"])


def test_curl_carries_the_exact_payload() -> None:
    code = render_snippet("curl", HOSTILE_CTX).code
    words = shlex.split(code.replace("\\\n", " "))
    assert words[:4] == ["curl", "-sS", "-X", "POST"]
    assert words[4] == HOSTILE_CTX.url
    assert json.loads(words[words.index("--data") + 1]) == HOSTILE
    assert "X-Mapi-Project: acme/growth" in words
    # the only shell expansion is the token variable, inside double quotes
    assert '"Authorization: Bearer $MAPI_TOKEN"' in code


def _r_unquote(text: str, start: int) -> str:
    assert text[start] == "'"
    out, i = [], start + 1
    while text[i] != "'":
        if text[i] == "\\":
            i += 1
            out.append({"n": "\n", "t": "\t"}.get(text[i], text[i]))
        else:
            out.append(text[i])
        i += 1
    return "".join(out)


def test_r_carries_the_exact_payload() -> None:
    code = render_snippet("r", HOSTILE_CTX).code
    body = _r_unquote(code, code.index("req_body_raw(") + len("req_body_raw("))
    assert json.loads(body) == HOSTILE
    url = _r_unquote(code, code.index("request(") + len("request("))
    assert url == HOSTILE_CTX.url


# ------------------------------------------------------------------------ hostile context


@pytest.mark.parametrize(
    "change",
    [
        {"base_url": "https://api.example.com/'; rm -rf / #"},
        {"base_url": 'https://api.example.com/"+alert(1)+"'},
        {"base_url": "javascript:alert(1)"},
        {"base_url": "https://api.example.com/$(id)"},
        {"base_url": "https://api.example.com/\nX: y"},
        {"alias": "a'b"},
        {"alias": "a b"},
        {"alias": "A"},
        {"project": "acme/growth;rm"},
        {"project": "acme"},
        {"token_env": "MAPI_TOKEN; rm -rf /"},
        {"wait": 61},
    ],
)
def test_hostile_context_values_are_refused(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SnippetContext.model_validate({**CTX.model_dump(), **change})


def test_trailing_slashes_and_literals() -> None:
    ctx = SnippetContext.model_validate({**CTX.model_dump(), "base_url": "http://127.0.0.1:18000/"})
    assert ctx.url == "http://127.0.0.1:18000/v1/endpoints/geo-mmm-prod/score"
    assert sh_quote("it's") == "'it'\\''s'"
    assert r_string("a'b\\c") == "'a\\'b\\\\c'"
    assert js_literal(" ", None) == '"\\u2028"'
    assert ast.literal_eval(py_literal(HOSTILE)) == HOSTILE
    with pytest.raises(ValueError):
        py_literal({"x": float("nan")})
    with pytest.raises(TypeError):
        py_literal({"x": object()})
