from __future__ import annotations

import copy
from typing import Any

import pytest
from mapi_spec import (
    Backtest,
    Campaign,
    DatasetRef,
    EndpointRef,
    ModelSpec,
    ScoreRequest,
    Sweep,
)
from mapi_spec._types import Duration
from mapi_spec.examples import E33_YAML, SCORE_YAML, XGB_YAML
from mapi_spec.loader import load_document, parse_yaml
from mapi_spec.schema import DimPolicy
from mapi_spec.spec import SBC, ScorePolicy, TimeHoldoutSplit
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _e33() -> dict[str, Any]:
    data = parse_yaml(E33_YAML)
    assert isinstance(data, dict)
    return data


def test_e33_parses() -> None:
    spec = ModelSpec.model_validate(_e33())
    assert spec.metadata.name == "eu-geo-mmm"
    assert isinstance(spec.data.dataset, DatasetRef)
    assert spec.data.side_tables["fx"].score_policy is ScorePolicy.SUPPLIED
    assert isinstance(spec.data.split, TimeHoldoutSplit)
    assert spec.data.split.after_step == "weekly"
    assert [s.id for s in spec.pipeline][:2] == ["to_eur", "weekly"]
    assert spec.pipeline[0].with_["to"] == "EUR"
    assert spec.model.dim_policies == {"geo": DimPolicy.FIXED}
    assert "dim_policies" not in spec.model.plugin_settings
    assert spec.model.fit.keep.posterior_predictive.thin == 4  # type: ignore[union-attr]
    assert spec.execution.timeout == Duration(45 * 60)
    assert spec.stage_names[0] == "raw" and spec.stage_names[-1] == "hols"


def test_xgb_and_score_parse() -> None:
    xgb = load_document(XGB_YAML)
    assert isinstance(xgb, ModelSpec) and xgb.data.split is not None
    req = load_document(SCORE_YAML)
    assert isinstance(req, ScoreRequest)
    assert isinstance(req.model, EndpointRef)
    assert req.inputs_stage == "scale_spend"
    assert req.interventions[0].multiply == 1.2
    assert req.output.draws == 200


def _invalid(data: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        ModelSpec.model_validate(data)


def test_modelspec_rules() -> None:
    d = _e33()
    d["pipeline"].append(copy.deepcopy(d["pipeline"][0]))
    _invalid(d, "duplicate step ids")

    d = _e33()
    d["data"]["split"]["after_step"] = "nope"
    _invalid(d, "not a step id")

    d = _e33()
    d["pipeline"][0]["id"] = "raw"
    _invalid(d, "reserved")

    d = _e33()
    d["extra_field"] = 1
    _invalid(d, "Extra inputs")

    d = _e33()
    d["model"]["settings"]["dim_policies"] = {"geo": "loose"}
    _invalid(d, "dim_policies.geo")

    d = _e33()
    d["model"]["settings"]["dim_policies"] = ["geo"]
    _invalid(d, "mapping")

    d = _e33()
    d["data"]["bind"]["vars"]["y2"] = "revenue_local"
    _invalid(d, "bound more than once")

    d = _e33()
    d["data"]["bind"]["vars"]["geo"] = "something"
    _invalid(d, "bound as both dim and variable")

    d = _e33()
    d["execution"]["timeout"] = "30s"
    _invalid(d, "between 1m and 24h")

    d = _e33()
    d["apiVersion"] = "mapi/v2"
    _invalid(d, "mapi/v1")

    d = _e33()
    d["model"]["fit"]["target_accept"] = 1.0
    _invalid(d, "less than 1")


@pytest.mark.parametrize(
    ("split", "match"),
    [
        ({"kind": "time_holdout", "dim": "time"}, "exactly one"),
        ({"kind": "time_holdout", "dim": "time", "last": 2, "fraction": 0.2}, "exactly one"),
        ({"kind": "leave_group_out", "dim": "geo"}, "exactly one"),
        (
            {"kind": "mask_block", "dims": ["geo"], "fraction": 0.1, "block": {"x": 1}},
            "not in dims",
        ),
        ({"kind": "mask_block", "dims": ["geo"], "fraction": 0.1, "block": {"geo": 0}}, ">= 1"),
        ({"kind": "unknown", "dim": "x"}, "does not match any of the expected tags"),
    ],
)
def test_split_rules(split: dict[str, Any], match: str) -> None:
    d = _e33()
    d["data"]["split"] = split
    _invalid(d, match)


def test_split_variants_parse() -> None:
    for split in [
        {"kind": "last_per_group", "dim": "trip", "within": ["hh"], "min_len": 3},
        {"kind": "as_of", "dim": "time", "as_of": "2026-01-04", "gap": 1},
        {"kind": "leave_group_out", "dim": "geo", "groups": ["DE"]},
        {"kind": "mask_block", "dims": ["geo", "time"], "fraction": 0.1, "block": {"time": 4}},
    ]:
        d = _e33()
        d["data"]["split"] = split
        spec = ModelSpec.model_validate(d)
        assert spec.data.split is not None and spec.data.split.kind == split["kind"]
        assert spec.data.split.split_dims()


def _score(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "model://growth/eu-geo-mmm@v3",
    }
    base.update(kw)
    return base


def test_score_request_rules() -> None:
    ScoreRequest.model_validate(_score())
    with pytest.raises(ValidationError, match="needs a horizon"):
        ScoreRequest.model_validate(_score(mode="forecast"))
    with pytest.raises(ValidationError, match="interventions or a scenario"):
        ScoreRequest.model_validate(_score(mode="counterfactual"))
    with pytest.raises(ValidationError, match="needs data"):
        ScoreRequest.model_validate(_score(mode="filter"))
    with pytest.raises(ValidationError, match="exactly one of multiply"):
        ScoreRequest.model_validate(
            _score(interventions=[{"var": "spend", "multiply": 1.1, "add": 2}])
        )
    r = ScoreRequest.model_validate(_score(interventions=[{"var": "spend", "set": 0}]))
    assert r.interventions[0].set_ == 0
    assert r.model_dump(by_alias=True, mode="json")["interventions"][0]["set"] == 0
    with pytest.raises(ValidationError):
        ScoreRequest.model_validate(_score(model="dataset://growth/x"))


def _wrapper(kind: str, **kw: Any) -> dict[str, Any]:
    return {
        "apiVersion": "mapi/v1",
        "kind": kind,
        "metadata": {"name": "w", "project": "growth"},
        "base": "spec://growth/eu-geo-mmm@r2",
        **kw,
    }


def test_wrappers() -> None:
    sw = Sweep.model_validate(
        _wrapper(
            "Sweep",
            grid={"/model/settings/pooling": ["partial", "full"], "/model/fit/draws": [500, 1000]},
            compare_by="elpd_loo",
        )
    )
    assert len(sw.points()) == 4
    assert {"/model/settings/pooling": "full", "/model/fit/draws": 1000} in sw.points()
    with pytest.raises(ValidationError, match="identity"):
        Sweep.model_validate(_wrapper("Sweep", grid={"/metadata/name": ["a"]}, compare_by="x"))
    with pytest.raises(ValidationError, match="no values"):
        Sweep.model_validate(_wrapper("Sweep", grid={"/a": []}, compare_by="x"))
    with pytest.raises(ValidationError, match="at most 1000"):
        Sweep.model_validate(
            _wrapper("Sweep", grid={"/a": list(range(40)), "/b": list(range(40))}, compare_by="x")
        )
    with pytest.raises(ValidationError, match="pointer"):
        Sweep.model_validate(_wrapper("Sweep", grid={"a": [1]}, compare_by="x"))
    bt = Backtest.model_validate(
        _wrapper("Backtest", as_of=["2026-01-04", "2026-04-05"], horizon=8)
    )
    assert bt.dim == "time"
    assert SBC.model_validate(_wrapper("SBC", n=50)).n == 50
    assert Campaign.model_validate(_wrapper("Campaign", rounds=5, batch_size=2)).rounds == 5
    with pytest.raises(ValidationError):
        SBC.model_validate(_wrapper("SBC", n=0))


def test_duration_type() -> None:
    assert str(Duration.parse("90m")) == "1h30m"
    assert str(Duration.parse(3600)) == "1h"
    assert str(Duration.parse("1h0m5s")) == "1h5s"
    assert str(Duration(0)) == "0s"
    assert Duration.parse("1m") < Duration.parse("2m")
    assert Duration.parse(Duration(5)) == Duration(5)
    assert hash(Duration(5)) == hash(Duration.parse("5s"))
    assert "Duration" in repr(Duration(5))
    for bad in ["", "5", "1d", "m"]:
        with pytest.raises(ValueError):
            Duration.parse(bad)
    with pytest.raises(ValueError):
        Duration.parse(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Duration(-1)
