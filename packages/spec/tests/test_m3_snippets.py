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


def test_atlas_is_valid_python() -> None:
    tree = ast.parse(render_snippet("atlas", HOSTILE_CTX).code)
    model = _call(tree, "RemoteModel")
    assert ast.literal_eval(model.keywords[0].value) == "geo-mmm-prod"


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
