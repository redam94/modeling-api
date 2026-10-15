from __future__ import annotations

import pytest
from mapi_spec.template import TemplateError, deep_merge, placeholders, render

pytestmark = pytest.mark.unit


def test_whole_string_is_typed() -> None:
    assert render("{n}", {"n": 6}) == 6
    assert render("{xs}", {"xs": [1, 2]}) == [1, 2]
    assert render("{a.b}", {"a": {"b": True}}) is True
    assert render("{x}", {"x": None}) is None


def test_interpolation_and_escapes() -> None:
    assert render("{v}_lag{n}", {"v": "spend", "n": 2}) == "spend_lag2"
    assert render("{b}-{f}", {"b": False, "f": 1.5}) == "false-1.5"
    assert render("{{literal}}", {}) == "{literal}"
    with pytest.raises(TemplateError, match="scalar"):
        render("x{xs}", {"xs": [1]})


def test_functions() -> None:
    p = {"lags": [3, 1, 2], "k": 4}
    assert render("{min:lags}", p) == 1
    assert render("{max:lags}", p) == 3
    assert render("{len:lags}", p) == 3
    assert render("{min:k}", p) == 4
    with pytest.raises(TemplateError):
        render("{len:k}", p)
    with pytest.raises(TemplateError):
        render("{min:s}", {"s": ["a"]})


def test_splice_and_key_loops() -> None:
    tpl = {
        "dims": ["{*groups}", "time"],
        "vars": {"{*vars as v}": {"dims": ["{v}"], "src": "{v}"}},
        "lagged": {"{*vars as v}_lag": {"from": "{v}"}},
    }
    out = render(tpl, {"groups": ["geo", "sku"], "vars": ["a", "b"]})
    assert out == {
        "dims": ["geo", "sku", "time"],
        "vars": {"a": {"dims": ["a"], "src": "a"}, "b": {"dims": ["b"], "src": "b"}},
        "lagged": {"a_lag": {"from": "a"}, "b_lag": {"from": "b"}},
    }
    assert render(["{*none}"], {"none": None}) == []


def test_errors_carry_paths() -> None:
    with pytest.raises(TemplateError) as exc:
        render({"a": [{"b": "{missing}"}]}, {})
    assert exc.value.path == ("a", 0, "b")
    with pytest.raises(TemplateError, match="list"):
        render(["{*x}"], {"x": "s"})
    with pytest.raises(TemplateError, match="strings"):
        render({"{*x as i}": 1}, {"x": [1]})
    with pytest.raises(TemplateError, match="duplicate"):
        render({"{*x as i}": 1, "a": 2}, {"x": ["a"]})
    with pytest.raises(TemplateError, match="invalid placeholder"):
        render("{1bad}", {})
    with pytest.raises(TemplateError, match="no field"):
        render("{a.c}", {"a": {"b": 1}})
    with pytest.raises(TemplateError, match="keys must render"):
        render({"{n}": 1}, {"n": 3})


def test_placeholders_scoping() -> None:
    tpl = {
        "{*vars as v}": {"units": "{to}", "name": "{v}"},
        "x": ["{*dims}", "{min:lags}", "{a.b}"],
        "{key}": 1,
    }
    assert placeholders(tpl) == {"vars", "to", "dims", "lags", "a", "key"}


def test_deep_merge() -> None:
    base = {"a": {"b": 1, "c": 2}, "d": [1]}
    assert deep_merge(base, {"a": {"c": None, "e": 3}, "d": [2]}) == {
        "a": {"b": 1, "e": 3},
        "d": [2],
    }
    assert deep_merge(1, {"x": 1}) == {"x": 1}
