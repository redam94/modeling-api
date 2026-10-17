"""Write the mock-mode fixtures of the M3.5 explorers, About cards and budget optimisation.

The web app's mock API (``src/mocks/explore.ts``: ``vite --mode mock``, the Playwright smoke
suite and the explorer screenshots) serves views computed by the real platform code, so the
screens are exercised against what a server really returns:

* **About cards and Help me choose**: ``catalog_abouts`` over the installed catalog (as
  ``GET /v1/catalog/abouts``), the chooser questions (``GET /v1/catalog/chooser``) and a
  few rankings by :func:`mapi_spec.about.recommend` (``GET /v1/catalog/recommendations``),
  which ``src/mocks/chooser.test.ts`` replays against the mock's TypeScript port;
* **Data Explorer**: a synthetic weekly geo panel (three regions, three channels, prices and
  temperature, with a gap and an outlier) explored by the runner's own section functions:
  the quick sections an ingest writes, and every section of an explore run faceted by
  region, each with its ``mapi_plots.explorer`` plots and the dataset About;
* **Model Explorer**: real fits of conformance fixtures (``guided_mmm`` on mmm-framework,
  two ``ts.sarimax`` versions and a ``ts.ets`` on ``sm_arimax``, ``ts.vecm`` on
  ``sm_vecm_macro``) explored by the runner, including forecasts, backtests and a
  comparison with Diebold-Mariano tests;
* **Budget optimisation**: the mmm-framework planning result of the MMM.

It also writes ``src/features/explore/views.gen.ts``: TypeScript types of every explorer view
(:mod:`mapi_spec.views`), generated with openapi-typescript from the Pydantic schemas.

Usage::

    uv run python apps/web/scripts/gen_explorer_fixtures.py           # refit and rewrite (minutes)
    uv run python apps/web/scripts/gen_explorer_fixtures.py --check   # contracts only (seconds)

Writing needs the runner, the plugins and pnpm. ``--check`` needs only ``mapi_spec``: every
committed view must still validate against its :mod:`mapi_spec.views` model, the chooser and
the built-in About cards must equal the current ones, and the view types must have been
generated from the current schemas (their hash is recorded in the TypeScript file).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from mapi_spec.about import CHOOSER_QUESTIONS, ChooserQuestionnaire, DatasetAbout
from mapi_spec.abouts import builtin_about_kind, builtin_abouts
from mapi_spec.governance import CheckResult
from mapi_spec.optimize import OptimizeResult
from mapi_spec.views import (
    AcfPacfView,
    BacktestView,
    ComparisonView,
    CorrelationView,
    CrossCorrelationView,
    DataOverviewView,
    DecompositionView,
    DistributionsView,
    ExplorerIndex,
    FevdView,
    FitVsActualView,
    GroupComparisonView,
    IrfView,
    MissingnessView,
    ModelDiagnosticsView,
    ModelForecastView,
    OutliersView,
    ParamTableView,
    QualityView,
    ResidualDiagnosticsView,
    ResponseCurvesView,
    StationarityView,
    StlView,
    TimeSeriesView,
    VifView,
)
from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

WEB: Final = Path(__file__).resolve().parent.parent
OUT: Final = WEB / "src" / "mocks" / "explorer.gen.json"
TYPES: Final = WEB / "src" / "features" / "explore" / "views.gen.ts"

#: One model per section view (``format`` tags differ, so the reader dispatches on them).
VIEW_MODELS: Final[dict[str, type[BaseModel]]] = {
    "overview": DataOverviewView,
    "distributions": DistributionsView,
    "timeseries": TimeSeriesView,
    "missingness": MissingnessView,
    "outliers": OutliersView,
    "correlation": CorrelationView,
    "ccf": CrossCorrelationView,
    "acf_pacf": AcfPacfView,
    "stationarity": StationarityView,
    "stl": StlView,
    "vif": VifView,
    "groups": GroupComparisonView,
    "quality": QualityView,
    "params": ParamTableView,
    "diagnostics": ModelDiagnosticsView,
    "fit": FitVsActualView,
    "residuals": ResidualDiagnosticsView,
    "decomposition": DecompositionView,
    "response_curves": ResponseCurvesView,
    "irf": IrfView,
    "fevd": FevdView,
    "forecast": ModelForecastView,
    "backtest": BacktestView,
    "comparison": ComparisonView,
}

#: Answer sets replayed by the TypeScript port of ``recommend`` (kind, limit, answers).
CHOOSER_CASES: Final[list[tuple[str | None, int, dict[str, list[str]]]]] = [
    (None, 10, {}),
    (None, 10, {"goal": ["forecast"], "data_shape": ["one_series"], "experience": ["beginner"]}),
    (
        None,
        8,
        {
            "goal": ["explain_marketing"],
            "data_shape": ["panel"],
            "uncertainty": ["essential"],
            "speed": ["can_wait"],
        },
    ),
    (
        None,
        10,
        {
            "goal": ["compare_series", "forecast"],
            "data_shape": ["several_series"],
            "frequency": ["monthly"],
            "drivers": ["yes"],
            "experience": ["expert"],
        },
    ),
    (None, 5, {"goal": ["plan_budget"], "speed": ["seconds"], "history": ["short"]}),
    ("check", 12, {"goal": ["validate_model"], "experience": ["intermediate"]}),
    ("step", 10, {"goal": ["check_data"], "frequency": ["daily"]}),
    # no starting point is made for these goals with these answers: none "not made for" them
    (None, 6, {"goal": ["check_data"], "experience": ["beginner"]}),
    (None, 6, {"goal": ["validate_model"], "data_shape": ["one_series"]}),
    ("model", 10, {"goal": ["check_data"]}),
    ("model", 50, {"goal": ["predict_values"], "data_shape": ["table"], "uncertainty": ["nice"]}),
]

DATASET_REF: Final = "dataset://default/eu_media_daily@v1"
MMM_REF: Final = "model://default/eu-geo-mmm@v1"
ARIMAX_V1: Final = "model://default/sales-arimax@v1"
ARIMAX_V2: Final = "model://default/sales-arimax@v2"
ETS_REF: Final = "model://default/sales-ets@v1"
VECM_REF: Final = "model://default/macro-vecm@v1"
GENERATED_AT: Final = "2026-10-16T09:00:00Z"
#: Governance checks that need no refits (as the integration test of every family).
CHEAP_CHECKS: Final = (
    "convergence",
    "data.quality",
    "residuals.whiteness",
    "holdout.accuracy",
    "baseline.beats_naive",
    "plausibility.signs",
)
MAX_PLOTS_BYTES: Final = 40_000


# ======================================================================== small helpers


def _round(obj: Any, sig: int = 7) -> Any:
    """Floats rounded to ``sig`` significant digits (keeps the fixture small)."""
    if isinstance(obj, float):
        if not math.isfinite(obj) or obj == 0.0:
            return obj
        digits = sig - math.floor(math.log10(abs(obj))) - 1
        return round(obj, max(0, min(15, digits)))
    if isinstance(obj, list):
        return [_round(x, sig) for x in obj]
    if isinstance(obj, dict):
        return {k: _round(v, sig) for k, v in obj.items()}
    return obj


def _fix_times(obj: Any) -> Any:
    """Generation timestamps replaced by a constant (stable diffs)."""
    if isinstance(obj, list):
        return [_fix_times(x) for x in obj]
    if isinstance(obj, dict):
        out = {k: _fix_times(v) for k, v in obj.items()}
        for key in ("generated_at", "computed_at"):
            if key in out and isinstance(out[key], str):
                out[key] = GENERATED_AT
        return out
    return obj


def _schema_doc() -> dict[str, Any]:
    """An OpenAPI document holding the JSON Schemas of the explorer views."""
    _, defs = models_json_schema(
        [(m, "serialization") for m in [*VIEW_MODELS.values(), ExplorerIndex]],
        ref_template="#/components/schemas/{model}",
    )
    return {
        "openapi": "3.1.0",
        "info": {"title": "mapi_spec.views explorer views", "version": "1"},
        "paths": {},
        "components": {"schemas": defs.get("$defs", {})},
    }


def _schema_hash() -> str:
    text = json.dumps(_schema_doc(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _builtin_abouts() -> list[dict[str, Any]]:
    """The built-in cards of the kinds the server always lists (no plugin needed)."""
    keep = {"split", "template", "workflow_template", "node_kind", "node", "check"}
    out = []
    for item_id, about in builtin_abouts().items():
        kind = builtin_about_kind(item_id)
        if kind is not None and kind.value in keep:
            out.append(
                {
                    "kind": kind.value,
                    "id": item_id,
                    "about": about.model_dump(mode="json", exclude_none=True),
                }
            )
    return sorted(out, key=lambda a: (a["kind"], a["id"]))


# ================================================================================ writing


def _abouts_and_chooser() -> dict[str, Any]:
    from mapi_sdk.registry import default_registry
    from mapi_server.api.meta import catalog_abouts
    from mapi_spec.about import AboutKind, chooser_answers_from_query, recommend

    items = catalog_abouts(default_registry().catalog())
    starting = {AboutKind.MODEL, AboutKind.TEMPLATE, AboutKind.WORKFLOW_TEMPLATE}
    blocks = {AboutKind.STEP, AboutKind.NODE, AboutKind.CHECK}  # as the server's fallback
    cases = []
    for kind, limit, query in CHOOSER_CASES:
        answers = chooser_answers_from_query(query)
        candidates = [
            (a.kind, a.id, a.about)
            for a in items
            if (a.kind in starting if kind is None else a.kind.value == kind)
        ]
        fallback = [(a.kind, a.id, a.about) for a in items if a.kind in blocks]
        result = recommend(
            answers, candidates, limit=limit, fallback=fallback if kind is None else None
        )
        cases.append(
            {
                "kind": kind,
                "limit": limit,
                "query": query,
                "result": result.model_dump(mode="json", exclude_none=True),
            }
        )
    return {
        "abouts": [a.model_dump(mode="json", exclude_none=True) for a in items],
        "chooser": ChooserQuestionnaire(questions=list(CHOOSER_QUESTIONS)).model_dump(
            mode="json", exclude_none=True
        ),
        "chooser_cases": cases,
    }


def _synthetic_panel() -> tuple[Any, Any]:
    """Three years of weekly media spend and revenue for three regions (synthetic)."""
    import numpy as np
    import pandas as pd
    import xarray as xr
    from mapi_spec.schema import DataSchema

    rng = np.random.default_rng(20261016)
    weeks = pd.date_range("2023-01-01", periods=156, freq="W-SUN")
    geos = ["north", "south", "west"]
    channels = ["tv", "search", "social"]
    n, g, c = len(weeks), len(geos), len(channels)
    t = np.arange(n)
    season = np.sin(2 * np.pi * (t - 8) / 52)
    spend = np.zeros((n, g, c))
    for j, scale in enumerate([1.2, 1.0, 0.8]):
        flight = ((t // 6) % 2 == 0).astype(float)  # TV runs in six-week bursts
        spend[:, j, 0] = scale * (9000 * flight + 1500) * (1 + 0.05 * rng.standard_normal(n))
        spend[:, j, 1] = scale * (2600 + 900 * season) * (1 + 0.08 * rng.standard_normal(n))
        spend[:, j, 2] = scale * (600 + 14 * t) * (1 + 0.15 * rng.standard_normal(n))
    spend = np.clip(spend, 0, None)

    def carry(x: np.ndarray, decay: float) -> np.ndarray:
        out = np.zeros_like(x)
        for i in range(len(x)):
            out[i] = x[i] + (decay * out[i - 1] if i else 0.0)
        return out

    price = 2.5 + np.cumsum(0.02 * rng.standard_normal((n, g)), axis=0)
    temperature = 11 + 9 * np.sin(2 * np.pi * (t - 14) / 52)[:, None] + rng.normal(0, 1.5, (n, g))
    revenue = np.zeros((n, g))
    for j, base in enumerate([52000.0, 47000.0, 39000.0]):
        media = (
            0.9 * 18000 * np.tanh(carry(spend[:, j, 0], 0.6) / 25000)
            + 1.4 * 9000 * np.tanh(carry(spend[:, j, 1], 0.2) / 6000)
            + 0.6 * 6000 * np.tanh(carry(spend[:, j, 2], 0.3) / 4000)
        )
        revenue[:, j] = (
            base * (1 + 0.0015 * t)
            + 7000 * season
            - 9000 * (price[:, j] - 2.5)
            + media
            + rng.normal(0, 1800, n)
        )
    revenue[80, 0] *= 1.6  # a one-off spike (a data error or an event?)
    spend[30:34, 2, 1] = np.nan  # four missing weeks of search spend in the west
    ds = xr.Dataset(
        {
            "revenue": (("week", "geo"), revenue),
            "spend": (("week", "geo", "channel"), spend),
            "price": (("week", "geo"), price),
            "temperature": (("week", "geo"), temperature),
        },
        coords={"week": weeks.values, "geo": geos, "channel": channels},
    )
    ds["revenue"].attrs["units"] = "EUR"
    ds["spend"].attrs["units"] = "EUR"
    schema = DataSchema.model_validate(
        {
            "dims": {
                "week": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
                "geo": {"kind": "categorical", "policy": "new_levels"},
                "channel": {"kind": "categorical", "policy": "fixed"},
            },
            "vars": {
                "revenue": {
                    "dims": ["week", "geo"],
                    "dtype": "float64",
                    "role": "target",
                    "units": "EUR",
                },
                "spend": {
                    "dims": ["week", "geo", "channel"],
                    "dtype": "float64",
                    "role": "spend",
                    "units": "EUR",
                    "missing": "allow",
                },
                "price": {"dims": ["week", "geo"], "dtype": "float64", "role": "control"},
                "temperature": {"dims": ["week", "geo"], "dtype": "float64", "role": "control"},
            },
        }
    )
    return ds, schema


def _sections(
    funcs: Mapping[str, Callable[[Any], BaseModel]],
    sections: list[str],
    context: Any,
    *,
    target: str,
    ref: str,
) -> dict[str, Any]:
    """Every section's view and plots, and the index, exactly as ``write_sections`` writes
    them (without a run context)."""
    from mapi_plots.explorer import explorer_plots
    from mapi_runner.worker.explore import explore_plots_name
    from mapi_runner.worker.explore_data import SectionSkipped
    from mapi_spec.views import ExplorerSectionRef, explore_view_name

    refs: list[ExplorerSectionRef] = []
    views: dict[str, Any] = {}
    plots: dict[str, Any] = {}
    for section in sections:
        name = explore_view_name(section)
        try:
            body = funcs[section](context).model_dump(mode="json", exclude_none=True)
        except SectionSkipped as exc:
            refs.append(
                ExplorerSectionRef(
                    section=section,
                    view=name,
                    status="skipped",
                    message_plain=f"Not available: {exc}."[:500],
                )
            )
            continue
        views[section] = _round(body)
        made = _round(_fix_times(explorer_plots(body)))
        extra: dict[str, Any] = {}
        # Plots repeat the view's data; large ones are left out to keep the fixture small
        # (the app draws its own interactive plots from the views).
        if made and len(json.dumps(made, separators=(",", ":"))) <= MAX_PLOTS_BYTES:
            plots[section] = made
            extra["plots"] = explore_plots_name(section)
        refs.append(ExplorerSectionRef(section=section, view=name, **extra))
    index = ExplorerIndex.model_validate({"target": target, "ref": ref, "sections": refs})
    return {
        "index": index.model_dump(mode="json", exclude_none=True),
        "views": views,
        "plots": plots,
    }


def _dataset() -> dict[str, Any]:
    from mapi_runner.worker.explore_data import CHEAP_SECTIONS, DATA_SECTION_FUNCS, data_context
    from mapi_sdk.schema import data_summary
    from mapi_spec.about import describe_dataset
    from mapi_spec.explore import DataExploreRequest
    from mapi_spec.views import DATA_SECTIONS

    ds, schema = _synthetic_panel()
    ingest = _sections(
        DATA_SECTION_FUNCS,
        list(CHEAP_SECTIONS),
        data_context(ds, schema),
        target="dataset",
        ref=DATASET_REF,
    )
    request = DataExploreRequest.model_validate(
        {"kpi": "revenue", "facet_by": ["geo"], "max_lag": 12, "max_series": 30}
    )
    explore = _sections(
        DATA_SECTION_FUNCS,
        list(DATA_SECTIONS),
        data_context(ds, schema, request),
        target="dataset",
        ref=DATASET_REF,
    )
    # The mock serves the ingest copy of a section the explore run computed identically.
    for section, body in ingest["views"].items():
        if explore["views"].get(section) == body:
            del explore["views"][section]
            explore["plots"].pop(section, None)
    about = describe_dataset(
        schema,
        data_summary(ds, schema),
        description="Weekly media spend and revenue per region, exported from the finance "
        "warehouse every Monday.",
    )
    return {
        "ref": DATASET_REF,
        "about": about.model_dump(mode="json", exclude_none=True),
        "ingest": ingest,
        "explore": {"request": request.model_dump(mode="json"), **explore},
    }


def _fit(fixture: str, change: Callable[[dict[str, Any]], None] | None = None) -> tuple[Any, ...]:
    from mapi_conformance.runner import load_fixture
    from mapi_sdk.harness import fit_spec
    from mapi_sdk.registry import default_registry
    from mapi_spec.spec import ModelSpec

    fx = load_fixture(fixture)
    data, tables = fx.generate(0, "tiny")
    spec = fx.spec("tiny")
    if change is not None:
        doc = spec.model_dump(mode="json", by_alias=True, exclude_unset=True)
        change(doc)
        spec = ModelSpec.model_validate(doc)
    reg = default_registry()
    bundle = fit_spec(spec, data, registry=reg, raw_schema=fx.raw_schema(data), side_tables=tables)
    return bundle, data, tables, reg


def _explore_model(
    fitted: tuple[Any, ...],
    ref: str,
    *,
    extra: list[str],
    request: dict[str, Any],
    compare: list[tuple[str, Any]] | None = None,
) -> dict[str, Any]:
    from mapi_runner.worker.explore_model import FIT_SECTIONS, MODEL_SECTION_FUNCS, ModelContext
    from mapi_runner.worker.refit import Refitter
    from mapi_spec.explore import ModelExploreRequest
    from mapi_spec.governance import RefitBudget

    bundle, data, tables, reg = fitted
    req = ModelExploreRequest.model_validate(request)
    budget = RefitBudget(max_refits=6, draws=50, tune=50, chains=1)

    def context() -> Any:
        return ModelContext(
            bundle,
            data,
            reg,
            req,
            ref=ref,
            side_tables=tables,
            compare=list(compare or []),
            refitter=Refitter(bundle, reg, budget, side_tables=tables),
        )

    fit_time = _sections(MODEL_SECTION_FUNCS, list(FIT_SECTIONS), context(), target="model",
                         ref=ref)  # fmt: skip
    on_demand = _sections(MODEL_SECTION_FUNCS, extra, context(), target="model", ref=ref)
    return {
        "ref": ref,
        "fit": fit_time,
        "explore": {"request": req.model_dump(mode="json", exclude_none=True), **on_demand},
    }


def _governance(fitted: tuple[Any, ...], checks: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    """Check results measured by the runner's governance checks (as a validation's)."""
    from mapi_runner.worker.governance import GovernanceEnv, measure
    from mapi_runner.worker.refit import Refitter
    from mapi_spec.governance import PolicyCheck, RefitBudget

    bundle, data, tables, reg = fitted
    budget = RefitBudget(max_refits=40, draws=50, tune=50, chains=1)
    env = GovernanceEnv(
        bundle, data, reg, Refitter(bundle, reg, budget, side_tables=tables), side_tables=tables
    )
    out = []
    for check, params in checks:
        result = measure(env, PolicyCheck(check=check, params=params))
        out.append(_round(_fix_times(result.model_dump(mode="json", exclude_none=True))))
    return out


def _models() -> tuple[dict[str, Any], dict[str, Any]]:
    from mapi_runner.worker.optimize import label_result
    from mapi_sdk.models import ScoreContext
    from mapi_spec.optimize import OptimizeRequest

    backtest = {
        "kind": "expanding_window",
        "horizons": [1, 2, 4],
        "n_folds": 3,
        "min_train": 24,
    }
    out: dict[str, Any] = {}
    mmm = _fit("guided_mmm")
    out[MMM_REF] = _explore_model(
        mmm,
        MMM_REF,
        extra=["forecast", "backtest"],
        request={"sections": ["forecast", "backtest"], "horizon": 8, "backtest": backtest},
    )

    def arimax_v2(doc: dict[str, Any]) -> None:
        doc["model"]["settings"]["order"] = [2, 0, 1]

    def ets(doc: dict[str, Any]) -> None:
        doc["model"]["use"] = "ts.ets@^1"
        doc["model"]["settings"] = {"series_dims": ["geo"], "trend": "add"}

    v1 = _fit("sm_arimax")
    v2 = _fit("sm_arimax", arimax_v2)
    e1 = _fit("sm_arimax", ets)
    ts_backtest = {**backtest, "min_train": 48, "horizons": [1, 4, 8]}
    out[ARIMAX_V1] = _explore_model(
        v1,
        ARIMAX_V1,
        extra=["forecast", "backtest"],
        request={"sections": ["forecast", "backtest"], "horizon": 12, "backtest": ts_backtest},
    )
    out[ARIMAX_V2] = _explore_model(
        v2,
        ARIMAX_V2,
        extra=["forecast", "backtest", "comparison"],
        request={
            "sections": ["forecast", "backtest", "comparison"],
            "horizon": 12,
            "backtest": ts_backtest,
            "compare_with": [ARIMAX_V1, ETS_REF],
        },
        compare=[(ARIMAX_V1, v1[0]), (ETS_REF, e1[0])],
    )
    out[ETS_REF] = _explore_model(
        e1, ETS_REF, extra=["forecast"], request={"sections": ["forecast"], "horizon": 12}
    )
    vecm = _fit("sm_vecm_macro")
    out[VECM_REF] = _explore_model(
        vecm, VECM_REF, extra=["forecast"], request={"sections": ["forecast"], "horizon": 12}
    )
    cheap = [(c, {}) for c in CHEAP_CHECKS]
    out[MMM_REF]["governance"] = _governance(mmm, cheap)
    out[ARIMAX_V1]["governance"] = _governance(
        v1, [*cheap, ("null.permutation_target", {"n_permutations": 19})]
    )

    bundle, _data, _tables, reg = mmm
    man = bundle.model.manifest
    plugin = reg.model(f"{man.id}@{man.version}")()
    request = OptimizeRequest.model_validate(
        {
            "objective": "mean",
            "channels": {"tv": {"min_multiplier": 0.5, "max_multiplier": 1.5}},
            "frontier": {"n_points": 9},
            "max_draws": 60,
            "seed": 3,
        }
    )
    plan = plugin.optimize(bundle.model, request, ScoreContext(seed=3))
    result = label_result(plan.result, bundle)
    optimize = {
        "model": MMM_REF,
        "request": request.model_dump(mode="json", exclude_none=True),
        "result": _round(_fix_times(result.model_dump(mode="json", exclude_none=True))),
    }
    return out, optimize


def _write_types() -> None:
    """``views.gen.ts`` from the view schemas (openapi-typescript, as ``schema.d.ts``)."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "views.openapi.json"
        out = Path(tmp) / "views.d.ts"
        src.write_text(json.dumps(_schema_doc(), indent=1), "utf-8")
        subprocess.run(
            [
                "pnpm",
                "exec",
                "openapi-typescript",
                str(src),
                "-o",
                str(out),
                "--default-non-nullable",
                "false",
            ],
            cwd=WEB,
            check=True,
            capture_output=True,
        )
        body = out.read_text("utf-8")
    header = (
        "/**\n"
        " * Explorer view types (mapi_spec.views), generated by\n"
        " * apps/web/scripts/gen_explorer_fixtures.py with openapi-typescript. Do not edit.\n"
        f" * schema-sha256: {_schema_hash()}\n"
        " */\n"
    )
    TYPES.parent.mkdir(parents=True, exist_ok=True)
    TYPES.write_text(header + body, "utf-8")


def render() -> str:
    doc: dict[str, Any] = {"generated_at": GENERATED_AT}
    doc.update(_abouts_and_chooser())
    doc["dataset"] = _dataset()
    doc["models"], doc["optimize"] = _models()
    return json.dumps(doc, separators=(",", ":"), sort_keys=True) + "\n"


# ================================================================================ checking


def check() -> list[str]:
    """Problems with the committed fixtures and types (empty: up to date)."""
    problems: list[str] = []
    if not OUT.exists():
        return [f"{OUT} is missing"]
    doc = json.loads(OUT.read_text("utf-8"))

    def views(where: str, part: Mapping[str, Any]) -> None:
        ExplorerIndex.model_validate(part["index"])
        for section, body in part.get("views", {}).items():
            try:
                VIEW_MODELS[section].model_validate(body)
            except Exception as exc:  # pragma: no cover - reported, not raised
                problems.append(f"{where} {section}: {str(exc)[:300]}")

    ds = doc["dataset"]
    DatasetAbout.model_validate(ds["about"])
    views("dataset ingest", ds["ingest"])
    views("dataset explore", ds["explore"])
    for ref, m in doc["models"].items():
        views(f"{ref} fit", m["fit"])
        views(f"{ref} explore", m["explore"])
        for result in m.get("governance", []):
            CheckResult.model_validate(result)
    OptimizeResult.model_validate(doc["optimize"]["result"])
    chooser = ChooserQuestionnaire(questions=list(CHOOSER_QUESTIONS)).model_dump(
        mode="json", exclude_none=True
    )
    if doc["chooser"] != chooser:
        problems.append("the chooser questions changed")
    builtin = {(a["kind"], a["id"]): a["about"] for a in _builtin_abouts()}
    served = {(a["kind"], a["id"]): a["about"] for a in doc["abouts"] if (a["kind"], a["id"])
              in builtin}  # fmt: skip
    if served != builtin:
        problems.append("the built-in About cards changed")
    if not TYPES.exists() or f"schema-sha256: {_schema_hash()}" not in TYPES.read_text("utf-8"):
        problems.append("the explorer view types are stale")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the fixtures are stale")
    args = parser.parse_args(argv)
    if args.check:
        problems = check()
        for p in problems:
            print(p, file=sys.stderr)
        if problems:
            print("run: uv run python apps/web/scripts/gen_explorer_fixtures.py", file=sys.stderr)
        return 1 if problems else 0
    warnings.filterwarnings("ignore")
    _write_types()
    OUT.write_text(render(), "utf-8")
    print(f"wrote {OUT.relative_to(WEB)} ({OUT.stat().st_size // 1024} KiB)")
    print(f"wrote {TYPES.relative_to(WEB)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
