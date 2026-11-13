"""Typed access to the v0.17 golden dashboard fixtures (``golden/<case>.json``).

Tests import it by path (the fixtures directory is not a package)::

    import sys
    from importlib import util
    from pathlib import Path

    _p = Path(__file__).parent / "fixtures" / "dashboards_v017" / "loader.py"
    _spec = util.spec_from_file_location("dashboards_v017_loader", _p)
    golden = sys.modules.setdefault(_spec.name, util.module_from_spec(_spec))
    _spec.loader.exec_module(golden)   # registered first: the dataclasses need it

    m = golden.load_case("forecast_bsts")
    m.scores["predictive"].summary      # ScoreSummary
    m.scores["predictive"].forecast     # ForecastView | None

See README.md for what each case and role holds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, get_args

from mapi_spec.api import MetricSeries, ScoreSummary
from mapi_spec.capabilities import ModelManifest
from mapi_spec.dashboard_v2 import PresetKey
from mapi_spec.dashboards import ActualsSubmit
from mapi_spec.scorecard import Scorecard
from mapi_spec.views import CounterfactualView, FieldsView, ForecastView

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden"
FORMAT = "mapi.test.dashboard-material/v1"
MAX_BYTES = 200 * 1024
CENTRES = ("mean", "median", "q50", "p50", "point")
"""The v2 centre columns, in the order the readout looks for them (I-6)."""


@dataclass(frozen=True)
class ScoreCapture:
    """One score run of a case: the request's output selector, ``result.json`` (raw), the
    summary and the guided views it wrote."""

    role: str
    probe_case: str
    mode: str
    output: dict[str, Any]
    inline: bool
    horizon: int | None
    result: dict[str, Any]
    summary: ScoreSummary
    forecast: ForecastView | None
    counterfactual: CounterfactualView | None
    fields: FieldsView | None
    trimmed: dict[str, Any] | None = None

    def table(self, variable: str) -> Any:
        """The summary table of ``variable`` (KeyError when absent)."""
        for t in self.summary.tables:
            if t.variable == variable:
                return t
        raise KeyError(variable)


@dataclass(frozen=True)
class Material:
    """Everything a v2 readout of one preset is built from, plus derived actuals and facts."""

    case: str
    preset: PresetKey
    plugin: str
    representation: str
    primary_output: str
    note: str
    manifest: ModelManifest
    capabilities: dict[str, Any]
    input_contract: dict[str, Any]
    fit_metrics: list[MetricSeries]
    scorecard: Scorecard
    scores: dict[str, ScoreCapture]
    refusals: list[dict[str, Any]]
    actuals: dict[str, Any] | None
    facts: dict[str, Any]
    raw: dict[str, Any] = field(repr=False)

    def scalar_metrics(self) -> dict[str, float]:
        """Fit metrics with ``labels == {}`` (refit copies carry ``{fit: ...}``)."""
        return {m.key: m.last for m in self.fit_metrics if not m.labels}

    def actuals_submit(self, label: str | None = None) -> ActualsSubmit:
        """The derived actuals as a ``POST …/actuals`` body (raises when the case has none)."""
        if self.actuals is None:
            raise ValueError(f"{self.case} has no actuals")
        return ActualsSubmit.model_validate({"inputs": self.actuals["inputs"], "label": label})


def case_names() -> list[str]:
    """Every golden case, sorted."""
    return sorted(p.stem for p in GOLDEN.glob("*.json"))


@cache
def _raw(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((GOLDEN / f"{name}.json").read_text())
    return data


def raw_case(name: str) -> dict[str, Any]:
    """The bundle as plain JSON (a fresh copy, safe to mutate)."""
    copied: dict[str, Any] = json.loads(json.dumps(_raw(name)))
    return copied


def _view(cls: Any, obj: Any) -> Any:
    return None if obj is None else cls.model_validate(obj)


def load_case(name: str) -> Material:
    """Validate and return one bundle (pydantic models for every view)."""
    b = raw_case(name)
    if b.get("format") != FORMAT:
        raise ValueError(f"{name}: unknown format {b.get('format')!r}")
    if b["preset"] not in get_args(PresetKey):
        raise ValueError(f"{name}: unknown preset {b['preset']!r}")
    scores = {
        role: ScoreCapture(
            role=role,
            probe_case=s["probe_case"],
            mode=s["mode"],
            output=s["output"],
            inline=s["inline"],
            horizon=s.get("horizon"),
            result=s["result"],
            summary=ScoreSummary.model_validate(s["summary"]),
            forecast=_view(ForecastView, s.get("forecast")),
            counterfactual=_view(CounterfactualView, s.get("counterfactual")),
            fields=_view(FieldsView, s.get("fields")),
            trimmed=s.get("trimmed"),
        )
        for role, s in b["scores"].items()
    }
    return Material(
        case=b["case"],
        preset=b["preset"],
        plugin=b["plugin"],
        representation=b["representation"],
        primary_output=b["primary_output"],
        note=b["note"],
        manifest=ModelManifest.model_validate(b["manifest"]),
        capabilities=b["capabilities"],
        input_contract=b["input_contract"],
        fit_metrics=[MetricSeries.model_validate(m) for m in b["fit_metrics"]],
        scorecard=Scorecard.model_validate(b["scorecard"]),
        scores=scores,
        refusals=b["refusals"],
        actuals=b["actuals"],
        facts=b["facts"],
        raw=b,
    )


def load_capture(plugin: str, filename: str) -> Any:
    """A raw WP0 capture (``<plugin>/<case>.<kind>.json`` or ``probe.json``) as JSON."""
    return json.loads((HERE / plugin / filename).read_text())


def centre_column(columns: list[str]) -> str | None:
    """The centre statistic column of a summary table, if any."""
    return next((c for c in CENTRES if c in columns), None)
