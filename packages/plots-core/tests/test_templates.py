from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from mapi_plots.sanitize import sanitize_plot
from mapi_plots.templates import (
    BoundField,
    TemplateError,
    apply_template,
    resolve_bindings,
    template_issues,
    vl_field,
)
from mapi_spec.errors import ErrorCode
from mapi_spec.plots import PlotBindings, PlotProvenance, PlotSpec
from mapi_spec.schema import DataSchema

pytestmark = pytest.mark.unit

GEO = DataSchema.model_validate(
    {
        "dims": {
            "geo": {"kind": "categorical", "policy": "new_levels"},
            "time": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
            "channel": {"kind": "categorical"},
        },
        "vars": {
            "y": {"dims": ["geo", "time"], "dtype": "float64", "role": "target", "units": "EUR"},
            "spend": {"dims": ["geo", "time", "channel"], "dtype": "float64", "role": "spend"},
            "promo.flag": {"dims": ["geo", "time"], "dtype": "bool", "role": "control"},
        },
    }
)
NO_GEO = DataSchema.model_validate(
    {
        "dims": {"t": {"kind": "integer", "policy": "extend"}, "time": {"kind": "time"}},
        "vars": {"sales": {"dims": ["time"], "dtype": "int64", "role": "target"}},
    }
)


def fan_template(**bindings: Any) -> PlotSpec:
    spec: dict[str, Any] = {
        "data": {"name": "data"},
        "title": "{{y.title}} over {{x}}",
        "mark": "line",
        "encoding": {"x": {"$bind": "x"}, "y": {"$bind": "y"}, "facet": {"$bind": "facet"}},
        "transform": [{"filter": "isValid(datum['{{y}}'])"}],
    }
    return PlotSpec(
        id="tmpl.fan",
        title="{{y}} by {{facet}}",
        kind="vega-lite",
        spec=spec,
        datasets={"data": {"kind": "inline_arrow", "data": ""}},
        bindings=PlotBindings(**bindings),
        schema_version="v6",
        caption_plain="The {{y}} series per {{facet}}.",
        provenance=PlotProvenance(author="tests", generated_at=datetime(2026, 1, 1, tzinfo=UTC)),
    )


def test_role_and_dim_bindings_resolve() -> None:
    b = PlotBindings(x="dim:time", y="role:target", facet="dim:geo", tooltip=["role:spend"])
    out = resolve_bindings(b, GEO)
    assert out["x"] == BoundField("x", "dim:time", "dim", "time", "temporal")
    y = out["y"]
    assert isinstance(y, BoundField) and y.name == "y" and y.units == "EUR"
    assert y.title == "y (EUR)" and y.vl_type == "quantitative"
    facet = out["facet"]
    assert isinstance(facet, BoundField) and facet.vl_type == "nominal"
    assert [f.name for f in out["tooltip"]] == ["spend"]  # type: ignore[union-attr]


def test_apply_template_fills_placeholders_and_sanitizes() -> None:
    plot = apply_template(fan_template(x="dim:time", y="role:target", facet="dim:geo"), GEO)
    enc = plot.spec["encoding"]
    assert enc == {
        "x": {"field": "time", "type": "temporal", "title": "time"},
        "y": {"field": "y", "type": "quantitative", "title": "y (EUR)"},
        "facet": {"field": "geo", "type": "nominal", "title": "geo"},
    }
    assert plot.spec["title"] == "y (EUR) over time"
    assert plot.spec["transform"] == [{"filter": "isValid(datum['y'])"}]
    assert plot.title == "y by geo"
    assert plot.caption_plain == "The y series per geo."
    sanitize_plot(plot)


def test_same_template_applies_to_another_family_member() -> None:
    tmpl = fan_template(x="dim:time", y="role:target", facet="dim:geo")
    with pytest.raises(TemplateError) as info:
        apply_template(tmpl, NO_GEO)
    assert info.value.code is ErrorCode.SCHEMA_INVALID
    assert info.value.pointer == "/bindings/facet"
    plot = apply_template(tmpl, NO_GEO, optional=["facet"])
    assert "facet" not in plot.spec["encoding"]
    assert plot.spec["encoding"]["y"]["type"] == "quantitative"
    assert plot.title == "sales by "
    assert template_issues(tmpl, NO_GEO) and not template_issues(tmpl, NO_GEO, optional=["facet"])


def test_ambiguous_and_missing_bindings() -> None:
    two_targets = GEO.model_copy(
        update={"vars": {**GEO.vars, "y2": GEO.vars["y"]}},
    )
    with pytest.raises(TemplateError, match="ambiguous"):
        resolve_bindings(PlotBindings(y="role:target"), two_targets)
    with pytest.raises(TemplateError, match="no variable has role"):
        resolve_bindings(PlotBindings(y="role:media"), GEO)
    with pytest.raises(TemplateError, match="no variable"):
        resolve_bindings(PlotBindings(y="var:nope"), GEO)
    with pytest.raises(TemplateError, match="no dim"):
        resolve_bindings(PlotBindings(x="dim:nope"), GEO)
    out = resolve_bindings(PlotBindings(tooltip=["role:target"]), two_targets)
    assert [f.name for f in out["tooltip"]] == ["y", "y2"]  # type: ignore[union-attr]


def test_var_coord_and_stat_bindings() -> None:
    out = resolve_bindings(
        PlotBindings(
            color="var:promo.flag",
            x="coord:time",
            row="coord:other",
            band_lower="stat:hdi_90_lower",
        ),
        GEO,
    )
    color = out["color"]
    assert isinstance(color, BoundField)
    assert color.vl_type == "nominal" and color.field == "promo\\.flag"
    assert out["x"].vl_type == "temporal"  # type: ignore[union-attr]
    assert out["row"].vl_type == "nominal"  # type: ignore[union-attr]
    assert out["band_lower"].kind == "stat"  # type: ignore[union-attr]
    ordered = DataSchema.model_validate(
        {
            "dims": {"k": {"kind": "categorical", "ordered": True}, "i": {"kind": "index"}},
            "vars": {"d": {"dims": ["k"], "dtype": "datetime64[ns]"}},
        }
    )
    res = resolve_bindings(PlotBindings(x="dim:k", y="var:d", color="dim:i"), ordered)
    assert [res[c].vl_type for c in ("x", "y", "color")] == ["ordinal", "temporal", "ordinal"]  # type: ignore[union-attr]


def test_placeholder_errors() -> None:
    tmpl = fan_template(x="dim:time", y="role:target")
    with pytest.raises(TemplateError, match="unbound channel 'facet'"):
        apply_template(tmpl, GEO)
    no_bindings = tmpl.model_copy(update={"bindings": None})
    with pytest.raises(TemplateError, match="not a template"):
        apply_template(no_bindings, GEO)
    text_only = tmpl.model_copy(
        update={"spec": {**tmpl.spec, "encoding": {"x": {"$bind": "x"}}}, "caption_plain": None}
    )
    with pytest.raises(TemplateError, match="unbound channel 'facet'"):
        apply_template(text_only, GEO)  # title still mentions {{facet}}
    ok = apply_template(text_only, GEO, title="{{y.field}} / {{x.type}}")
    assert ok.title == "y / temporal"


def test_tooltip_list_binding_and_escaping() -> None:
    tmpl = fan_template(x="dim:time", y="role:target", tooltip=["role:target", "var:promo.flag"])
    spec = {**tmpl.spec, "encoding": {"tooltip": {"$bind": "tooltip"}}, "title": "{{tooltip}}"}
    plot = apply_template(
        tmpl.model_copy(update={"spec": spec, "title": "t", "caption_plain": None}), GEO
    )
    assert [t["field"] for t in plot.spec["encoding"]["tooltip"]] == ["y", "promo\\.flag"]
    assert plot.spec["title"] == "y"
    assert vl_field("a.b[0]") == "a\\.b\\[0\\]"
