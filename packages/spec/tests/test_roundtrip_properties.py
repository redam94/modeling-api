"""Hypothesis round trips: document -> canonical JSON / YAML -> document."""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator
from mapi_spec import DataSchema, ModelSpec, ScoreRequest, export_schemas
from mapi_spec.loader import canonical_json, dump_yaml, load_document, parse_yaml, spec_hash

pytestmark = pytest.mark.unit

SCHEMAS = export_schemas()
VALIDATORS = {
    name: Draft202012Validator(schema) for name, schema in SCHEMAS.items() if name.endswith(".json")
}

names = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True).filter(lambda s: s != "raw")
slugs = st.from_regex(r"[a-z][a-z0-9-]{0,10}[a-z0-9]", fullmatch=True)
texts = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters="﻿"),
    max_size=12,
)
finite = st.floats(allow_nan=False, allow_infinity=False, width=64)
scalars = st.one_of(st.none(), st.booleans(), st.integers(-(2**40), 2**40), finite, texts)
json_values = st.recursive(
    scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4), st.dictionaries(names, children, max_size=4)
    ),
    max_leaves=12,
)
json_objects = st.dictionaries(names, json_values, max_size=4)
ranges = st.sampled_from(["^1", "~1.2", ">=1.0.0 <2", "1.2.3", "*", "^0.3 || ^1"])
use_refs = st.builds(
    lambda ns, n, r: f"{ns}.{n}@{r}" if r != "*" else f"{ns}.{n}",
    st.sampled_from(["core", "mmm", "sklearn", "ts"]),
    names,
    ranges,
)
dataset_refs = st.builds(
    lambda p, n, v: f"dataset://{p}/{n}" + (f"@{v}" if v else ""),
    slugs,
    slugs,
    st.sampled_from([None, "latest", "v3", "12", "sha256:" + "ab" * 32]),
)
policies = st.sampled_from(["fixed", "extend", "new_levels"])
durations = st.sampled_from(["45m", "1h30m", 3600, "10m", "2h"])


@st.composite
def splits(draw: st.DrawFn, step_ids: list[str]) -> dict[str, Any]:
    after = draw(st.sampled_from([None, "raw", *step_ids]))
    kind = draw(st.sampled_from(["time_holdout", "last_per_group", "as_of", "leave_group_out"]))
    base: dict[str, Any] = {"kind": kind}
    if after is not None:
        base["after_step"] = after
    if kind == "time_holdout":
        base["dim"] = draw(names)
        which = draw(st.sampled_from(["last", "fraction", "after"]))
        base[which] = {
            "last": draw(st.integers(1, 50)),
            "fraction": draw(st.floats(0.01, 0.99)),
            "after": draw(texts),
        }[which]
    elif kind == "last_per_group":
        base |= {"dim": draw(names), "within": draw(st.lists(names, min_size=1, max_size=3))}
        base["min_len"] = draw(st.integers(1, 5))
    elif kind == "as_of":
        base |= {"dim": draw(names), "as_of": draw(st.one_of(texts, st.integers(0, 100)))}
    else:
        base |= {"dim": draw(names), "n": draw(st.integers(1, 10))}
    return base


@st.composite
def model_spec_dicts(draw: st.DrawFn) -> dict[str, Any]:
    step_ids = draw(st.lists(names, unique=True, max_size=5))
    pipeline = [{"id": i, "use": draw(use_refs), "with": draw(json_objects)} for i in step_ids]
    settings_ = draw(json_objects)
    if draw(st.booleans()):
        settings_["dim_policies"] = draw(st.dictionaries(names, policies, max_size=3))
    fit: dict[str, Any] = {
        "mode": draw(st.sampled_from(["evaluate_then_refit", "train_only"])),
    }
    if draw(st.booleans()):
        fit |= {
            "engine": draw(st.sampled_from(["nutpie", "pymc"])),
            "tune": draw(st.integers(0, 2000)),
            "draws": draw(st.integers(1, 5000)),
            "chains": draw(st.integers(1, 8)),
            "target_accept": draw(st.floats(0.5, 0.99)),
            "seed": draw(st.integers(0, 2**32 - 1)),
            "keep": {
                "log_likelihood": draw(st.booleans()),
                "posterior_predictive": draw(
                    st.one_of(st.booleans(), st.builds(lambda t: {"thin": t}, st.integers(1, 9)))
                ),
            },
        }
    return {
        "apiVersion": "mapi/v1",
        "kind": "ModelSpec",
        "metadata": {
            "name": draw(slugs),
            "project": draw(slugs),
            "labels": draw(st.dictionaries(names, names, max_size=3)),
        },
        "data": {
            "dataset": draw(dataset_refs),
            "side_tables": draw(
                st.dictionaries(
                    names,
                    st.builds(
                        lambda r, p: {"ref": r, "score_policy": p},
                        dataset_refs,
                        st.sampled_from(["pinned", "latest", "supplied"]),
                    ),
                    max_size=2,
                )
            ),
            "bind": {"dims": {"geo": "market"}, "vars": {"y": "revenue"}},
            "split": draw(st.one_of(st.none(), splits(step_ids))),
        },
        "pipeline": pipeline,
        "model": {"use": draw(use_refs), "settings": settings_, "fit": fit},
        "outputs": {
            "derived": draw(st.lists(names, max_size=3)),
            "plots": draw(st.lists(use_refs, max_size=3)),
            "ui_draws": draw(st.integers(0, 2000)),
        },
        "evaluate": {"metrics": draw(st.lists(names, max_size=3))},
        "execution": {
            "resource_class": draw(st.sampled_from(["S", "M", "L", "XL"])),
            "timeout": draw(durations),
        },
    }


@st.composite
def score_request_dicts(draw: st.DrawFn) -> dict[str, Any]:
    mode = draw(st.sampled_from(["predict", "forecast", "counterfactual", "simulate"]))
    out: dict[str, Any] = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": draw(
            st.one_of(
                st.builds(lambda p, a: f"endpoint://{p}/{a}", slugs, slugs),
                st.builds(lambda p, a: f"model://{p}/{a}@v2", slugs, slugs),
            )
        ),
        "mode": mode,
        "inputs_stage": draw(st.sampled_from(["raw", "scale_spend"])),
        "select": draw(
            st.dictionaries(names, st.lists(st.one_of(names, st.integers()), max_size=3))
        ),
        "output": {
            "kind": draw(st.sampled_from(["expected", "predictive"])),
            "draws": draw(st.integers(0, 500)),
        },
    }
    if mode == "forecast" or draw(st.booleans()):
        out["horizon"] = {"dim": "time", "periods": draw(st.integers(1, 100))}
    if mode == "counterfactual" or draw(st.booleans()):
        op = draw(st.sampled_from(["multiply", "add", "set"]))
        out["interventions"] = [
            {"var": draw(names), "sel": {"channel": draw(names)}, op: draw(finite)}
        ]
    if draw(st.booleans()):
        out["scenario"] = draw(dataset_refs)
    if draw(st.booleans()):
        out["output"]["summary"] = draw(
            st.lists(
                st.sampled_from(["mean", "sd", "hdi_90", "q5", "q97_5", "median"]), unique=True
            )
        )
    return out


SETTINGS = settings(
    max_examples=int(os.environ.get("MAPI_HYPOTHESIS_EXAMPLES", "60")),
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


@SETTINGS
@given(model_spec_dicts())
def test_model_spec_roundtrip(data: dict[str, Any]) -> None:
    spec = ModelSpec.model_validate(data)
    text = canonical_json(spec)
    again = load_document(text)
    assert again == spec
    assert canonical_json(again) == text
    assert spec_hash(again) == spec_hash(spec)
    # YAML round trip through the strict loader
    assert load_document(dump_yaml(spec)) == spec
    # canonical JSON satisfies the exported JSON Schema (pydantic and JSON Schema agree)
    errors = list(VALIDATORS["modelspec.json"].iter_errors(json.loads(text)))
    assert errors == []


@SETTINGS
@given(score_request_dicts())
def test_score_request_roundtrip(data: dict[str, Any]) -> None:
    req = ScoreRequest.model_validate(data)
    text = canonical_json(req)
    again = load_document(text)
    assert again == req
    assert spec_hash(again) == spec_hash(req)
    assert load_document(dump_yaml(req)) == req
    assert list(VALIDATORS["scorerequest.json"].iter_errors(json.loads(text))) == []
    assert list(VALIDATORS["document.json"].iter_errors(json.loads(text))) == []


@SETTINGS
@given(json_objects)
def test_yaml_dump_parse_roundtrip_for_json_values(value: dict[str, Any]) -> None:
    assert parse_yaml(dump_yaml(value)) == value


@SETTINGS
@given(model_spec_dicts(), st.randoms())
def test_hash_independent_of_key_order(data: dict[str, Any], rnd: Any) -> None:
    def shuffle(node: Any) -> Any:
        if isinstance(node, dict):
            items = list(node.items())
            rnd.shuffle(items)
            return {k: shuffle(v) for k, v in items}
        if isinstance(node, list):
            return [shuffle(v) for v in node]
        return node

    a = ModelSpec.model_validate(data)
    b = ModelSpec.model_validate(shuffle(data))
    assert spec_hash(a) == spec_hash(b)


@st.composite
def data_schemas(draw: st.DrawFn) -> dict[str, Any]:
    dim_names = draw(st.lists(names, min_size=1, max_size=4, unique=True))
    dims = {}
    for d in dim_names:
        kind = draw(st.sampled_from(["categorical", "time", "integer"]))
        spec: dict[str, Any] = {"kind": kind}
        if kind == "time":
            spec |= {"policy": "extend", "freq": draw(st.sampled_from(["D", "W-SUN", "MS"]))}
            if draw(st.booleans()):
                spec["max_extension"] = draw(st.integers(0, 100))
        elif kind == "categorical" and draw(st.booleans()):
            spec |= {"policy": "new_levels", "new_level_strategy": "pooled"}
        dims[d] = spec
    var_names = draw(st.lists(names.filter(lambda n: n not in dim_names), max_size=4, unique=True))
    vars_ = {
        v: {
            "dims": draw(st.lists(st.sampled_from(dim_names), unique=True)),
            "dtype": draw(st.sampled_from(["float64", "int64", "bool", "str"])),
            "role": draw(st.sampled_from(["target", "covariate", "media", "control"])),
            "units": draw(st.one_of(st.none(), st.sampled_from(["EUR", "1", "impressions"]))),
            "future_required": draw(st.booleans()),
        }
        for v in var_names
    }
    return {"dims": dims, "vars": vars_}


@SETTINGS
@given(data_schemas())
def test_data_schema_json_roundtrip(data: dict[str, Any]) -> None:
    s = DataSchema.model_validate(data)
    assert DataSchema.from_json(s.to_json()) == s
    assert DataSchema.model_validate_json(canonical_json(s)) == s
    assert list(VALIDATORS["dataschema.json"].iter_errors(json.loads(s.to_json()))) == []
