from __future__ import annotations

from typing import Any

import pytest
from mapi_spec.capabilities import (
    REPRESENTATION_RULES,
    Capabilities,
    Diagnostic,
    ModelManifest,
    OutputSelector,
    Representation,
    ResourceHint,
    can_aggregate,
    default_summary,
    parse_summary,
    summary_allowed,
)
from mapi_spec.plots import PlotBindings, PlotProvenance, PlotSpec, referenced_datasets
from mapi_spec.resources import RESOURCE_CLASSES, ResourceClass
from pydantic import ValidationError

pytestmark = pytest.mark.unit

R = Representation


@pytest.mark.parametrize(
    ("rep", "over", "how", "allowed", "flagged"),
    [
        (R.DRAWS, ["geo"], "sum", True, False),
        (R.DRAWS, ["geo", "time"], "max", True, False),
        (R.QUANTILES, ["geo"], "sum", False, False),
        (R.INTERVAL, ["time"], "mean", False, False),
        (R.MOMENTS, ["geo"], "sum", False, False),
        (R.POINT, ["geo"], "sum", True, True),
        (R.POINT, ["geo"], "max", False, False),
        (R.QUANTILES, [], "sum", True, False),
    ],
)
def test_can_aggregate(
    rep: Representation, over: list[str], how: str, allowed: bool, flagged: bool
) -> None:
    v = can_aggregate(rep, over, how)
    assert (v.allowed, v.flagged) == (allowed, flagged)
    if not allowed:
        assert v.reason


def test_summaries() -> None:
    assert parse_summary("hdi_90").level == pytest.approx(0.9)
    assert parse_summary("q97_5").level == pytest.approx(0.975)
    assert parse_summary("q5").kind == "quantile"
    assert parse_summary("mean").kind == "mean"
    for bad in ["hdi_0", "hdi_100", "q0", "avg", "q97_5_1"]:
        with pytest.raises(ValueError):
            parse_summary(bad)
    assert summary_allowed(R.DRAWS, "hdi_90")
    assert not summary_allowed(R.QUANTILES, "hdi_90")
    assert summary_allowed(R.QUANTILES, "q10")
    assert not summary_allowed(R.INTERVAL, "mean")
    assert summary_allowed(R.INTERVAL, "interval")
    assert not summary_allowed(R.POINT, "sd")
    assert summary_allowed(R.MOMENTS, "eti_90")
    assert not summary_allowed(R.DRAWS, "nonsense")
    for rep in Representation:
        assert all(summary_allowed(rep, s) for s in default_summary(rep))
        assert rep in REPRESENTATION_RULES


def test_capabilities() -> None:
    c = Capabilities.model_validate(
        {
            "inference": "mcmc",
            "representation": "draws",
            "score_modes": ["forecast", "predict", "predict"],
            "owns": ["scaling", "carryover"],
        }
    )
    assert c.score_modes == frozenset({"predict", "forecast"})
    dumped = c.model_dump(mode="json")
    assert dumped["score_modes"] == ["forecast", "predict"]  # sorted, deterministic
    assert dumped["owns"] == ["carryover", "scaling"]
    with pytest.raises(ValidationError, match="loo_invalid_reason"):
        Capabilities(
            inference="kalman", representation=R.DRAWS, score_modes={"filter"}, loo_valid=False
        )
    with pytest.raises(ValidationError):
        Capabilities(inference="mcmc", representation=R.DRAWS, score_modes=set())
    with pytest.raises(ValidationError):
        Capabilities.model_validate(
            {"inference": "mcmc", "representation": "draws", "score_modes": ["optimize"]}
        )


def _manifest(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "mmm.geo",
        "version": "1.0.0",
        "capabilities": {"inference": "map", "representation": "point", "score_modes": ["predict"]},
        "schema_template": {"template": {}},
    }
    base.update(kw)
    return base


def test_manifest_validation() -> None:
    m = ModelManifest.model_validate(
        _manifest(derived=[{"name": "contrib", "dims": ["geo"]}], plots=["core.ppc_fan"])
    )
    assert m.derived_names() == {"contrib"}
    with pytest.raises(ValidationError, match="version range"):
        ModelManifest.model_validate(_manifest(id="mmm.geo@^1"))
    with pytest.raises(ValidationError):
        ModelManifest.model_validate(_manifest(version="1.0"))
    with pytest.raises(ValidationError, match="JSON Schema"):
        ModelManifest.model_validate(_manifest(settings_schema={"type": "nonsense"}))
    with pytest.raises(ValidationError, match="unique"):
        ModelManifest.model_validate(
            _manifest(derived=[{"name": "a", "dims": []}, {"name": "a", "dims": []}])
        )
    with pytest.raises(ValidationError):
        ModelManifest.model_validate(_manifest(plots=["NotAnId"]))


def test_resources() -> None:
    assert ResourceClass.S < ResourceClass.M < ResourceClass.L < ResourceClass.XL
    assert ResourceClass.XL > ResourceClass.S and ResourceClass.M >= ResourceClass.M
    assert ResourceClass.M <= ResourceClass.L
    assert ResourceClass.S.next_up() is ResourceClass.M and ResourceClass.XL.next_up() is None
    assert sorted([ResourceClass.XL, ResourceClass.S, ResourceClass.L]) == [
        ResourceClass.S,
        ResourceClass.L,
        ResourceClass.XL,
    ]
    assert RESOURCE_CLASSES[ResourceClass.M].vcpu == 4
    assert ResourceClass.S.__lt__(3) is NotImplemented
    with pytest.raises(ValidationError, match="default_class"):
        ResourceHint(min_class=ResourceClass.L, default_class=ResourceClass.S)


def test_output_selector() -> None:
    o = OutputSelector()
    assert o.summary is None and o.summary_for(R.QUANTILES) == default_summary(R.QUANTILES)
    o = OutputSelector(summary=["mean", "hdi_90"])
    assert o.summary_for(R.QUANTILES) == ["mean", "hdi_90"]
    with pytest.raises(ValidationError, match="unique"):
        OutputSelector(summary=["mean", "mean"])
    with pytest.raises(ValidationError, match="unknown summary"):
        OutputSelector(summary=["average"])
    with pytest.raises(ValidationError):
        OutputSelector(draws=-1)


def test_diagnostic() -> None:
    d = Diagnostic(
        code="RHAT_HIGH",
        severity="warning",
        metric="rhat",
        value=1.08,
        threshold=1.01,
        message_expert="max r-hat 1.08 > 1.01 for beta_channel",
        message_plain="The model has not settled down yet.",
        suggested_action="Run more tuning steps.",
    )
    assert not d.passed
    with pytest.raises(ValidationError):
        Diagnostic(code="bad code", message_expert="x", message_plain="y")


# --------------------------------------------------------------------------------- plots

PROV = {"recipe": "core.ppc_fan", "generated_at": "2026-10-14T12:00:00Z", "bundle_id": "b1"}


def _plot(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "ppc_fan",
        "title": "Fit",
        "kind": "vega-lite",
        "spec": {
            "data": {"name": "fan"},
            "mark": "area",
            "layer": [{"data": {"name": "obs"}, "mark": "point"}],
        },
        "datasets": {
            "fan": {"kind": "data_ref", "path": "ui/plotdata/fan.arrow"},
            "obs": {"kind": "inline_arrow", "data": "QUJD"},
        },
        "bindings": {"x": "dim:time", "y": "role:target", "facet": "dim:geo"},
        "schema_version": "v5",
        "provenance": PROV,
    }
    base.update(kw)
    return base


def test_plotspec_ok_and_roundtrip() -> None:
    p = PlotSpec.model_validate(_plot())
    assert p.provenance.source == "recipe"
    assert PlotSpec.model_validate_json(p.model_dump_json()) == p


def test_plotspec_rejects_urls_and_unknown_names() -> None:
    with pytest.raises(ValidationError, match="not URL"):
        PlotSpec.model_validate(_plot(spec={"data": {"url": "https://evil/x.csv"}}))
    with pytest.raises(ValidationError, match="undeclared datasets"):
        PlotSpec.model_validate(_plot(spec={"data": {"name": "ghost"}}))
    with pytest.raises(ValidationError, match="pattern"):
        PlotSpec.model_validate(
            _plot(datasets={"fan": {"kind": "data_ref", "path": "a://b"}}, spec={"mark": "bar"})
        )
    with pytest.raises(ValidationError, match="segments"):
        PlotSpec.model_validate(
            _plot(datasets={"fan": {"kind": "data_ref", "path": "ui/../x"}}, spec={"mark": "bar"})
        )
    with pytest.raises(ValidationError):
        PlotSpec.model_validate(_plot(datasets={"bad name": {"kind": "inline_arrow", "data": ""}}))
    # Vega-Lite top-level inline datasets are allowed references
    PlotSpec.model_validate(
        _plot(spec={"datasets": {"inline": []}, "data": {"name": "inline"}}, datasets={})
    )


def test_plotly_refs() -> None:
    spec = {
        "data": [{"type": "scatter", "x": {"$ref": "contrib.time"}, "y": {"$ref": "contrib.y"}}]
    }
    names, violations = referenced_datasets("plotly", spec)
    assert names == {"contrib"} and violations == []
    PlotSpec.model_validate(
        _plot(
            kind="plotly",
            spec=spec,
            datasets={"contrib": {"kind": "inline_arrow", "data": ""}},
            schema_version="2.35.2",
        )
    )
    with pytest.raises(ValidationError, match="undeclared"):
        PlotSpec.model_validate(_plot(kind="plotly", spec=spec, datasets={}))


def test_bindings_and_provenance() -> None:
    with pytest.raises(ValidationError, match="unknown role"):
        PlotBindings(y="role:revenue")
    with pytest.raises(ValidationError):
        PlotBindings(x="time")
    PlotBindings(tooltip=["var:y", "stat:mean"])
    with pytest.raises(ValidationError, match="exactly one"):
        PlotProvenance.model_validate(
            {"recipe": "a", "plugin": "b", "generated_at": PROV["generated_at"]}
        )
    assert (
        PlotProvenance.model_validate({"author": "me", "generated_at": PROV["generated_at"]}).source
        == "author"
    )
    assert (
        PlotProvenance.model_validate({"plugin": "p", "generated_at": PROV["generated_at"]}).source
        == "plugin"
    )
