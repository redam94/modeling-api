"""``mapi explore …`` and ``mapi optimize …`` (v0.5).

The Data Explorer and the Model Explorer compute their sections in the sandbox (an
``explore`` run; asking the same again is answered from the cache). ``mapi explore`` waits
for the run, lists every section (and why one was skipped) and, with ``--out DIR``, writes
each section's view as JSON - the same views the web app plots.

``mapi optimize`` splits a marketing budget across channels with mmm-framework's planner on
a fitted marketing-mix model and prints the plan: current and recommended spend, the
expected result with its range, and the uplift.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from mapi_spec.api import Exploration, Optimization
from mapi_spec.loader import parse_text
from mapi_spec.views import ComparisonView
from rich.text import Text

from mapi_cli.output import Output, safe
from mapi_cli.resolve import parse_assignments, resolve_dataset, resolve_model
from mapi_cli.state import cli_errors, state

app = typer.Typer(
    help="Explore a dataset or a model (sections computed in the sandbox), compare models.",
    no_args_is_help=True,
)

_SECTION_STYLE = {"ok": "green", "skipped": "dim", "error": "red"}


def _write_views(api: Any, out: Output, exp: Exploration, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for link in exp.views:
        name = link.name.removeprefix("ui/explore/")
        if "/" in name or not name.endswith(".json"):
            continue
        path = dest / name
        api.download(link, path)
        written.append(path)
    return written


def report_exploration(
    api: Any, out: Output, exp: Exploration, *, dest: Path | None, show: str | None
) -> None:
    written = _write_views(api, out, exp, dest) if dest is not None and exp.done else []
    if show is not None:
        view = api.explorations.view(exp, show)
        out.emit_json(view) if out.json_mode else typer.echo(json.dumps(view, indent=2))
        return
    if out.json_mode:
        out.emit_json({"exploration": exp, "written": [str(p) for p in written]})
        return
    out.kv(
        [
            ("exploration", exp.run.id),
            ("of", exp.ref),
            ("state", exp.run.state.value),
        ]
    )
    if exp.index is not None:
        out.table(
            ["section", "status", "note"],
            [
                (s.section, safe(s.status, _SECTION_STYLE.get(s.status, "")), s.message_plain or "")
                for s in exp.index.sections
            ],
            title="Sections",
        )
    if written:
        out.success(f"wrote {len(written)} view(s) to {dest}")
    elif exp.done:
        out.info("--out DIR writes every view as JSON; --show SECTION prints one")


@app.command("dataset")
@cli_errors
def dataset(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="dataset://project/name@vN, name@vN or name")],
    version: Annotated[int | None, typer.Option("--version", "-v", min=1)] = None,
    section: Annotated[
        list[str] | None,
        typer.Option("--section", "-s", help="overview, timeseries, stationarity, ccf, stl, ..."),
    ] = None,
    kpi: Annotated[
        str | None, typer.Option("--kpi", help="The result to relate drivers to")
    ] = None,
    facet_by: Annotated[list[str] | None, typer.Option("--facet-by")] = None,
    max_lag: Annotated[int | None, typer.Option("--max-lag", min=1)] = None,
    option: Annotated[
        list[str] | None, typer.Option("--option", help="Any other request field, key=value")
    ] = None,
    dest: Annotated[Path | None, typer.Option("--out", "-o", help="Write the views here")] = None,
    show: Annotated[str | None, typer.Option("--show", help="Print one section's view")] = None,
    timeout: Annotated[float, typer.Option("--timeout", min=1)] = 900.0,
) -> None:
    """Explore a dataset version: profile, distributions, time plots, missingness, outliers,
    correlations, cross-correlation with the KPI, ACF/PACF, stationarity, STL, VIF, group
    comparisons and data-quality warnings."""
    st = state(ctx)
    fields: dict[str, Any] = parse_assignments(option or [], what="--option")
    if section:
        fields["sections"] = section
    if facet_by:
        fields["facet_by"] = facet_by
    fields.update({k: v for k, v in {"kpi": kpi, "max_lag": max_lag}.items() if v is not None})
    with st.client() as api:
        ds_id, ver = resolve_dataset(api, ref, version)
        exp = api.datasets.explore(ds_id, ver, fields, timeout=timeout)
        report_exploration(api, st.output, exp, dest=dest, show=show)


@app.command("model")
@cli_errors
def model(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="model://project/name@vN, name@vN or name")],
    version: Annotated[int | None, typer.Option("--version", "-v", min=1)] = None,
    section: Annotated[
        list[str] | None,
        typer.Option("--section", "-s", help="params, diagnostics, fit, residuals, irf, ..."),
    ] = None,
    horizon: Annotated[int | None, typer.Option("--horizon", min=1)] = None,
    compare_with: Annotated[list[str] | None, typer.Option("--compare-with")] = None,
    option: Annotated[list[str] | None, typer.Option("--option")] = None,
    dest: Annotated[Path | None, typer.Option("--out", "-o")] = None,
    show: Annotated[str | None, typer.Option("--show")] = None,
    timeout: Annotated[float, typer.Option("--timeout", min=1)] = 900.0,
) -> None:
    """Explore a model version: parameters, diagnostics, fit vs actual, residual tests,
    decomposition, response curves, IRF/FEVD, forecasts, backtests and comparisons."""
    st = state(ctx)
    fields: dict[str, Any] = parse_assignments(option or [], what="--option")
    if section:
        fields["sections"] = section
    if compare_with:
        fields["compare_with"] = compare_with
    if horizon is not None:
        fields["horizon"] = horizon
    with st.client() as api:
        model_id, ver = resolve_model(api, ref, version)
        exp = api.models.explore(model_id, ver, fields, timeout=timeout)
        report_exploration(api, st.output, exp, dest=dest, show=show)


@app.command("compare")
@cli_errors
def compare(
    ctx: typer.Context,
    models: Annotated[list[str], typer.Argument(help="Two to eleven model://… refs")],
    horizon: Annotated[int | None, typer.Option("--horizon", min=1)] = None,
    dest: Annotated[Path | None, typer.Option("--out", "-o")] = None,
    timeout: Annotated[float, typer.Option("--timeout", min=1)] = 900.0,
) -> None:
    """Compare model versions on their shared holdout: metrics, ranking and pairwise
    Diebold-Mariano tests (is the difference more than luck?)."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        exp = api.models.compare(models, horizon=horizon, timeout=timeout)
        if dest is not None or out.json_mode:
            report_exploration(api, out, exp, dest=dest, show=None)
            return
        view = ComparisonView.model_validate(api.explorations.view(exp, "comparison"))
    out.table(
        ["model", *view.metrics],
        [[m, *(_metric(view.table.get(m, {}).get(k)) for k in view.metrics)] for m in view.models],
        title="Holdout metrics (lower is better for errors)",
    )
    for metric, order in view.ranking.items():
        out.out.print(Text(f"best {metric}: ", style="bold") + safe(" > ".join(order)))
    if view.tests:
        out.table(
            ["comparison", "statistic", "p-value", "verdict"],
            [
                (
                    t.var or t.test,
                    _metric(t.statistic),
                    _metric(t.p_value),
                    safe(t.verdict_plain, "bold" if t.reject else ""),
                )
                for t in view.tests
            ],
            title="Diebold-Mariano: is the difference more than luck?",
        )


def _metric(value: float | None) -> str:
    return "-" if value is None else f"{value:.4g}"


@app.command("get")
@cli_errors
def get(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Explore run id")],
    dest: Annotated[Path | None, typer.Option("--out", "-o")] = None,
    show: Annotated[str | None, typer.Option("--show")] = None,
) -> None:
    """An exploration's sections (and with --out its views)."""
    st = state(ctx)
    with st.client() as api:
        report_exploration(api, st.output, api.explorations.get(run_id), dest=dest, show=show)


# ====================================================================== optimise


def _money(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}"


def print_plan(out: Output, opt: Optimization) -> None:
    if out.json_mode:
        out.emit_json(opt)
        return
    res = opt.result
    if res is None:
        out.kv([("optimisation", opt.run.id), ("state", opt.run.state.value)])
        return
    units = f" {res.spend_units}" if res.spend_units else ""
    out.kv(
        [
            ("optimisation", opt.run.id),
            ("objective", res.objective_label),
            ("budget", f"{_money(res.total_budget)}{units} (now {_money(res.current_total)})"),
            ("horizon", f"{res.horizon} periods" if res.horizon else None),
            (
                f"expected {res.kpi}",
                f"{res.expected_kpi.mean:,.4g}"
                + (f" (now {res.current_kpi.mean:,.4g})" if res.current_kpi else ""),
            ),
            ("uplift", f"{res.uplift.mean:+,.4g}" if res.uplift else None),
            (
                "chance it beats the current plan",
                f"{res.prob_positive_uplift:.0%}" if res.prob_positive_uplift is not None else None,
            ),
            ("draws", res.n_draws),
        ]
    )
    out.table(
        ["channel", "geo", "now", "recommended", "change", "next unit returns", "note"],
        [
            (
                a.channel,
                a.geo or "",
                _money(a.current_spend),
                Text(_money(a.optimal_spend), style="bold"),
                f"{a.change_pct:+.0f}%" if a.change_pct is not None else "",
                f"{a.marginal_roi:.3g}" if a.marginal_roi is not None else "",
                "beyond the spend seen in the data" if a.extrapolated else "",
            )
            for a in res.allocation
        ],
        title="Plan",
    )
    if res.frontier:
        out.table(
            ["budget", f"expected {res.kpi}", "next unit returns"],
            [
                (
                    _money(p.total_budget),
                    f"{p.expected_kpi:,.4g}",
                    f"{p.marginal_roi:.3g}" if p.marginal_roi else "",
                )
                for p in res.frontier
            ],
            title="What more (or less) budget buys",
        )
    for note in res.notes_plain:
        out.info(f"note: {note}")


@cli_errors
def optimize(
    ctx: typer.Context,
    model_ref: Annotated[str, typer.Argument(help="model://project/name@vN, name@vN or name")],
    version: Annotated[int | None, typer.Option("--version", "-v", min=1)] = None,
    budget: Annotated[float | None, typer.Option("--budget", min=0, help="Total to spend")] = None,
    change_pct: Annotated[
        float | None, typer.Option("--change-pct", help="Or: change today's total by N %")
    ] = None,
    objective: Annotated[
        str | None, typer.Option("--objective", help="mean (default), p10 (cautious), cvar5, ...")
    ] = None,
    channel: Annotated[
        list[str] | None,
        typer.Option(
            "--channel", help="Bounds per channel: tv='{\"max_multiplier\": 1.5}' (repeat)"
        ),
    ] = None,
    frontier: Annotated[
        int | None, typer.Option("--frontier", min=3, max=50, help="Also price N budgets")
    ] = None,
    by_geo: Annotated[bool, typer.Option("--by-geo", help="Allocate per geo and channel")] = False,
    request: Annotated[
        Path | None,
        typer.Option("--request", help="An OptimizeRequest YAML/JSON (flags override it)"),
    ] = None,
    timeout: Annotated[float, typer.Option("--timeout", min=1)] = 900.0,
) -> None:
    """Split a marketing budget across channels with a fitted marketing-mix model."""
    st = state(ctx)
    base: dict[str, Any] = {}
    if request is not None:
        loaded = parse_text(request.read_text("utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"{request} must hold an OptimizeRequest mapping")
        base = loaded
    fields: dict[str, Any] = {
        "total_budget": budget,
        "budget_change_pct": change_pct,
        "objective": objective,
        "by_geo": by_geo or None,
        "frontier": {"n_points": frontier} if frontier else None,
    }
    if channel:
        bounds = parse_assignments(channel, what="--channel")
        for name, value in bounds.items():
            if not isinstance(value, dict):
                raise ValueError(f"--channel {name}= takes a JSON object of bounds")
        fields["channels"] = {**base.get("channels", {}), **bounds}
    with st.client() as api:
        model_id, ver = resolve_model(api, model_ref, version)
        opt = api.models.optimize(model_id, ver, base, timeout=timeout, **fields)
    print_plan(st.output, opt)
