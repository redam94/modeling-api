"""Plots: render a recipe, wrap your own chart, and see the sanitizer at work."""

# --8<-- [start:recipe]
from mapi_plots.examples import example_inputs
from mapi_plots.recipes import get_recipe

inputs = example_inputs()  # synthetic bundle/score trees for every recipe (xarray only)
data, options = inputs["core.ppc_fan"]
plot = get_recipe("core.ppc_fan").render(data, **options)

print(plot.kind, plot.persona)  # vega-lite both
print(sorted(plot.datasets))  # named Arrow tables travel next to the spec, never as URLs
print(plot.caption_plain[:60], "...")
# --8<-- [end:recipe]
assert plot.kind == "vega-lite" and plot.datasets

# --8<-- [start:all]
for recipe_id, (data, options) in example_inputs().items():
    rendered = get_recipe(recipe_id).render(data, **options)
    print(f"{recipe_id:28} {rendered.kind:10} {rendered.persona}")
# --8<-- [end:all]

# --8<-- [start:template]
import numpy as np
import pandas as pd
import xarray as xr
from mapi_plots.recipes import builtin_templates, render_template

weekly = xr.Dataset(
    {"revenue": (("geo", "time"), np.random.default_rng(0).gamma(20.0, 50.0, (3, 12)),
                 {"role": "target", "units": "EUR"})},
    coords={"geo": ["DE", "FR", "UK"], "time": pd.date_range("2026-01-04", periods=12, freq="W-SUN")},
)
tmpl = builtin_templates()["tmpl.target_over_time"]
print(tmpl.bindings)  # x="dim:time", y="role:target", color="dim:geo" - no variable names
chart = render_template(tmpl, weekly)  # the schema is inferred; roles come from attrs
one_market = render_template(tmpl, weekly.sel(geo="DE", drop=True), optional=["color"])
# --8<-- [end:template]
assert chart.kind == "vega-lite" and one_market.kind == "vega-lite"

# --8<-- [start:altair]
import altair as alt
import pandas as pd
from mapi_plots import from_altair

frame = pd.DataFrame({"week": pd.date_range("2026-01-04", periods=6, freq="W-SUN"),
                      "revenue": [10.0, 12.5, 11.0, 14.2, 15.1, 13.8]})
chart = alt.Chart(frame).mark_line().encode(x="week:T", y="revenue:Q")
mine = from_altair(chart, id="acme.revenue", caption_plain="Weekly revenue.")
print(list(mine.datasets))  # the inline data became a named dataset

import plotly.express as px
from mapi_plots import from_plotly

fig = px.scatter(frame, x="week", y="revenue")
theirs = from_plotly(fig, id="acme.scatter")  # typed arrays -> named Arrow datasets
print(list(theirs.datasets))  # one dataset per trace, referenced as {"$ref": "trace0.y"}
# --8<-- [end:altair]
assert mine.datasets and theirs.datasets

# --8<-- [start:sanitize]
from mapi_plots import check_plot, sanitize_plot
from mapi_plots.sanitize import SanitizeError

hostile = {
    "id": "evil.chart",
    "title": "Totally normal chart",
    "kind": "vega-lite",
    "spec": {
        "data": {"url": "https://attacker.example/steal.json"},  # fetches are not allowed
        "mark": "point",
        "encoding": {"href": {"field": "link"}},  # neither is navigation
    },
}
try:
    sanitize_plot(hostile)
except SanitizeError as err:
    print(err.code, sorted(rule.value for rule in err.rules))  # PLOT_UNSAFE ['href', 'url_data']

for issue in check_plot(hostile):  # editor-friendly: every problem, never raises
    print(issue.pointer, "-", issue.message)
# /spec/data/url - data must be referenced by dataset name, never by URL
# /spec/encoding/href - href links are not allowed in plots
# /spec/$schema - added $schema
# --8<-- [end:sanitize]
try:
    sanitize_plot(hostile)
    raise AssertionError("expected PLOT_UNSAFE")
except SanitizeError as err:
    assert err.code == "PLOT_UNSAFE"
