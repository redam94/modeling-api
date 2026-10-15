"""Role/dim-bound templates rendered against real (synthetic) data."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("altair")

from mapi_plots import examples as ex  # noqa: E402
from mapi_plots.convert import arrow_to_frame  # noqa: E402
from mapi_plots.recipes import RecipeDataError, builtin_templates, infer_schema, render_template  # noqa: E402
from mapi_plots.templates import TemplateError  # noqa: E402
from mapi_spec.schema import DataSchema  # noqa: E402

pytestmark = pytest.mark.unit


def test_target_over_time_applies_to_a_geo_panel() -> None:
    tmpl = builtin_templates()["tmpl.target_over_time"]
    data = ex.panel_data().rename(week="time")
    plot = render_template(tmpl, data)
    frame = arrow_to_frame(plot.datasets["data"])
    assert list(frame.columns) == ["time", "geo", "revenue"]
    assert len(frame) == 60
    assert plot.spec["encoding"]["y"] == {
        "field": "revenue",
        "type": "quantitative",
        "title": "revenue (EUR)",
    }
    assert plot.title == "revenue over time"
    assert plot.caption_plain == "The revenue series over time, one line per geo."


def test_template_degrades_without_optional_facet() -> None:
    tmpl = builtin_templates()["tmpl.target_over_time"]
    data = ex.panel_data().rename(week="time").sel(geo="north", drop=True)[["revenue"]]
    with pytest.raises(TemplateError):
        render_template(tmpl, data)
    plot = render_template(tmpl, data, optional=["color"])
    assert "color" not in plot.spec["encoding"]
    assert list(arrow_to_frame(plot.datasets["data"]).columns) == ["time", "revenue"]


def test_summary_fan_template_uses_stat_columns() -> None:
    tmpl = builtin_templates()["tmpl.summary_fan"]
    tree = ex.score_tree(with_draws=False)
    summary = tree["summary"].to_dataset().rename(week="time").sel(geo="east", drop=True)
    plot = render_template(tmpl, {"summary": summary}, group="summary", value="revenue")
    frame = arrow_to_frame(plot.datasets["data"])
    assert list(frame.columns) == ["time", "mean", "hdi_90_lower", "hdi_90_upper"]
    np.testing.assert_allclose(frame["hdi_90_upper"] - frame["hdi_90_lower"], 30.0)
    layer = plot.spec["layer"][0]["encoding"]
    assert layer["y2"] == {"field": "hdi_90_upper"}
    assert plot.provenance.bundle_id is None


def test_schema_inference_and_explicit_schema() -> None:
    data = ex.panel_data()
    schema = infer_schema(data)
    assert schema.dims["week"].kind == "time" and schema.dims["geo"].kind == "categorical"
    assert schema.vars["revenue"].role.value == "target" and schema.vars["revenue"].units == "EUR"
    data.attrs["mapi_schema"] = schema.model_dump_json()
    assert infer_schema(data) == schema
    data.attrs["mapi_schema"] = schema.model_dump(mode="json")
    assert infer_schema(data) == schema
    mixed = xr.Dataset(
        {
            "flag": (("i",), np.array([True, False])),
            "label": (("i",), np.array(["a", "b"])),
            "n": (("i",), np.array([1, 2])),
            "when": (("i",), np.array(["2024-01-01", "2024-01-02"], dtype="datetime64[ns]")),
        },
        coords={"i": [0.5, 1.5]},
    )
    inferred = infer_schema(mixed)
    assert {k: v.dtype for k, v in inferred.vars.items()} == {
        "flag": "bool",
        "label": "str",
        "n": "int64",
        "when": "datetime64[ns]",
    }
    assert inferred.dims["i"].kind == "float"


def test_render_template_errors() -> None:
    tmpl = builtin_templates()["tmpl.target_over_time"]
    with pytest.raises(RecipeDataError, match="not a template"):
        render_template(tmpl.model_copy(update={"bindings": None}), ex.panel_data())
    fan = builtin_templates()["tmpl.summary_fan"]
    plain = ex.panel_data().rename(week="time")
    with pytest.raises(RecipeDataError, match="stat"):
        render_template(fan, plain)
    summary = ex.score_tree(with_draws=False)["summary"].to_dataset().rename(week="time")
    renamed = summary.assign_coords(stat=["a", "b", "c", "d", "e"])
    with pytest.raises(RecipeDataError, match="no statistic"):
        render_template(fan, renamed)
    with pytest.raises(RecipeDataError, match="rows"):
        render_template(tmpl, plain, max_rows=5)
    schema: Any = DataSchema.model_validate(
        {
            "dims": {"time": {"kind": "time"}, "geo": {"kind": "categorical"}},
            "vars": {"ghost": {"dims": ["time"], "dtype": "float64", "role": "target"}},
        }
    )
    with pytest.raises(RecipeDataError, match="no variable"):
        render_template(tmpl, plain, schema)
