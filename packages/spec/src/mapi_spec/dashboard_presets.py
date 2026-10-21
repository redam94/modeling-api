"""Presets of layout 2 dashboards: which preset fits a model, the starting draft of each
preset, the upgrade of a layout 1 spec, and the checks of a spec against what the model
version offers (v0.17, pure Python).

A **preset** (:data:`~mapi_spec.dashboard_v2.PresetKey`) is data (:data:`PRESETS`): the kinds,
intents and inputs it allows (:data:`~mapi_spec.dashboard_v2.PRESET_RULES`), the lead
sentences of its readouts and a builder of its default panels. The **source catalog**
(:class:`~mapi_spec.dashboard_v2.SourceCatalog`) is what one model version offers: the
server builds it from the real artefacts (summary names, fit metrics, plots, the input
contract); :func:`catalog_from_manifest` builds the part that the manifest alone gives, for
the presets endpoint before any run and for tests.

* :func:`suggest_presets` ranks the compatible presets (the first rule that matches is the
  best; ``generic`` is always last) with plain reasons and the inputs each cannot offer.
* :func:`compile_preset` builds a valid :class:`~mapi_spec.dashboard_v2.DashboardSpecV2` from
  a preset and a catalog (the inputs start off: the analyst turns them on and confirms the
  ranges).
* :func:`upgrade_v1` converts a layout 1 spec: a media mix model becomes the
  ``mmm_planner`` preset with the same channels, tiles, what-ifs and actuals; another model
  (a forecast with a placeholder channel) becomes the preset of its family without channels.
* :func:`check_spec_v2` finds what a spec asks that the model version cannot give (pointers
  ``/panels/<i>/...``): unknown outputs and metrics, reductions the representation does not
  allow (D6: ``sum``/``mean`` of quantiles, intervals or moments are refused; of point
  predictions flagged), intents that do not fit the output, inputs on fixed rows.

No numpy, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Final, Literal, NamedTuple, cast

from mapi_spec.capabilities import (
    REPRESENTATION_RULES,
    Capabilities,
    ModelManifest,
    OutputDecl,
    OutputMeaning,
    Representation,
)
from mapi_spec.dashboard_v2 import (
    PRESET_RULES,
    UPGRADE_NOTE_RANGES,
    AccuracySource,
    ActualsInputsV2,
    CatalogInput,
    CatalogMetric,
    CatalogOutput,
    ChartIntent,
    ChartPanel,
    DashboardLimitsV2,
    DashboardSpecV2,
    DimBind,
    KpiPanel,
    MetricSource,
    MmmPanel,
    MmmPlanner,
    NotePanel,
    OutputShape,
    OutputSource,
    PresetKey,
    PresetRules,
    PresetSuggestion,
    RecipePanel,
    Reduce,
    ScorecardSource,
    SourceCatalog,
    SubjectDef,
    TablePanel,
    VarBind,
    ViewSource,
)
from mapi_spec.dashboards import (
    ChannelDef,
    DashboardSource,
    DashboardSpec,
    RefreshPolicy,
    TileUnit,
)
from mapi_spec.errors import ErrorCode, Issue, Severity, json_pointer
from mapi_spec.schema import DataSchema, DimPolicy

__all__ = [
    "CLASSIFICATION_PLOTS",
    "LEADS",
    "PRESETS",
    "PRESET_ORDER",
    "SAMPLE_DIMS",
    "AnyPanel",
    "PresetDef",
    "catalog_from_manifest",
    "catalog_output",
    "check_spec_v2",
    "compile_preset",
    "detect_preset",
    "suggest_presets",
    "upgrade_v1",
]

AnyPanel = KpiPanel | ChartPanel | TablePanel | NotePanel | RecipePanel | MmmPanel
ReduceHow = Literal["none", "select", "last", "next", "mean", "sum", "share_above", "top_class"]
Stat = Literal["centre", "low", "high", "value"]
Centre = Literal["mean", "median", "q50", "p50", "point"]

SAMPLE_DIMS: Final = frozenset({"chain", "draw", "sample", "quantile", "bound", "moment"})
"""Dims of the uncertainty representation (never a dim of a summary table)."""
CLASSIFICATION_PLOTS: Final = frozenset({"core.roc_pr", "core.confusion", "core.calibration"})
AREA_PLOTS: Final = frozenset({"sci.area_map"})
PRESET_ORDER: Final[tuple[PresetKey, ...]] = (
    "mmm_planner",
    "image",
    "field_map",
    "causal_impact",
    "classifier",
    "forecast",
    "regression",
    "generic",
)
"""Detection order: the first preset whose rule matches is the best suggestion."""

_TIME_NAME: Final = re.compile(
    r"^(time|date|week|month|quarter|year|day|period|t|ds|step)$", re.IGNORECASE
)
_ROW_NAMES: Final = frozenset({"obs", "index", "row", "case", "id", "sample_id", "record"})
_CLASS_DIMS: Final = ("class", "label", "category")
_STATE_DIMS: Final = ("state", "regime")
_FORECAST_FAMILIES: Final = frozenset({"forecast", "ts_multivariate", "regime"})
_CENTRE: Final[Mapping[Representation, Centre]] = {
    Representation.DRAWS: "mean",
    Representation.QUANTILES: "median",
    Representation.INTERVAL: "point",
    Representation.POINT: "mean",
    Representation.MOMENTS: "mean",
}
_ERROR_METRICS: Final = ("rmse", "mae", "mape", "smape", "wape")
_PERIOD_WORDS: Final = {"week", "month", "quarter", "year", "day"}

LEADS: Final[Mapping[PresetKey, Mapping[str, str]]] = {
    "mmm_planner": {},
    "forecast": {
        "next": "We expect {centre} {noun} next {period}, likely between {low} and {high}.",
        "next_point": "We expect {centre} {noun} next {period}.",
    },
    "regression": {
        "case": "For the case you set, the model predicts {centre} {noun}, likely between "
        "{low} and {high}.",
        "case_point": "For the case you set, the model predicts {centre} {noun}.",
    },
    "classifier": {
        "chance": "The model gives a {p}% chance of {positive_label} for this case.",
        "top": "The most likely result for this case is {label} ({p}% chance).",
        "accuracy": "On the {n} cases you sent, the model was right {acc}% of the time.",
    },
    "causal_impact": {
        "effect": "The change added about {total} {noun} (likely {low} to {high}), {rel}% more "
        "than without it.",
        "effect_short": "The change added about {total} {noun}.",
    },
    "field_map": {
        "map": "The map shows the {noun} that the model expects in each place.",
        "share": "{k} of {n} areas are above {threshold}.",
    },
    "image": {"accuracy": "On the test images, the model was right {acc}% of the time."},
    "generic": {
        "value": "The model gives {centre} {noun}, likely between {low} and {high}.",
        "value_point": "The model gives {centre} {noun}.",
    },
}
"""Lead sentences of a readout (plain text, filled server-side from numbers only, at most
400 characters). The MMM planner keeps the layout 1 leads."""


# ================================================================================= catalog


def _is_time(dim: str) -> bool:
    return bool(_TIME_NAME.match(dim))


def _class_dim(dims: Sequence[str], decl: OutputDecl | None = None) -> str | None:
    if decl is not None and decl.classes_dim is not None:
        return decl.classes_dim
    return next((d for d in _CLASS_DIMS if d in dims), None)


def guess_meaning(
    name: str, dims: Sequence[str], decl: OutputDecl | None, plots: Iterable[str] = ()
) -> OutputMeaning | None:
    """The declared ``meaning`` of an output, else a guess: a ``class`` dim (or a classifier
    plot of the primary output ``y_hat``) is ``class_probability``, a ``state`` dim
    ``state_probability``, an output named ``effect`` an ``effect``."""
    if decl is not None and decl.meaning is not None:
        return decl.meaning
    if any(d in dims for d in _STATE_DIMS):
        return "state_probability"
    if any(d in dims for d in _CLASS_DIMS):
        return "class_probability"
    if name == "y_hat" and CLASSIFICATION_PLOTS & set(plots):
        return "class_probability"
    if name == "effect":
        return "effect"
    return None


def _intents(
    name: str,
    dims: Sequence[str],
    time_dim: str | None,
    shape: OutputShape,
    meaning: OutputMeaning | None,
) -> list[ChartIntent]:
    out: list[ChartIntent]
    if shape == "grid":
        out = ["field"]
    elif shape == "areal":
        out = ["area_bars", "bars", "range"]
    elif shape == "classification":
        out = ["probability_bars", "bars", "calibration", "confusion", "range"]
    elif shape == "time":
        out = ["fan", "line", "range", "residuals", "scatter"]
    elif shape == "categorical":
        out = ["bars", "range", "scatter", "residuals"]
    else:
        out = ["range", "bars"]
    if "component" in dims or name in ("contributions", "trend", "level"):
        out.append("decomposition")
    if meaning == "state_probability" or any(d in dims for d in _STATE_DIMS):
        out.append("state_sequence")
    if meaning == "effect" or name == "effect":
        out += ["effect", "diverging"]
    return list(dict.fromkeys(out))


def catalog_output(
    name: str,
    dims: Sequence[str],
    *,
    shape: OutputShape,
    representation: Representation,
    time_dim: str | None = None,
    meaning: OutputMeaning | None = None,
    manifest_name: str | None = None,
    classes: Sequence[str] = (),
    units: str | None = None,
    predictive_ok: bool | None = None,
) -> CatalogOutput:
    """One catalog output with what may be asked of it: the stats (``low``/``high`` only
    with an uncertainty representation), the reductions (``last``/``next`` with a time dim;
    ``sum``/``mean`` for draws, and point outputs flagged; ``share_above`` over cells;
    ``top_class`` of class probabilities), the chart intents of its shape, and whether a
    predictive score gives an outcome range (not for point outputs or probabilities)."""
    dims = [d for d in dims if d not in SAMPLE_DIMS]
    stats: list[Stat] = ["centre"]
    if representation is not Representation.POINT:
        stats += ["low", "high"]
    reduces: list[ReduceHow] = ["none", "select"]
    if time_dim is not None:
        reduces += ["last", "next"]
    if dims and REPRESENTATION_RULES[representation].aggregate in ("any", "linear"):
        reduces += ["mean", "sum"]
    if dims and shape != "classification":
        reduces.append("share_above")
    if meaning in ("class_probability", "state_probability") and (
        _class_dim(dims) or any(d in dims for d in _STATE_DIMS)
    ):
        reduces.append("top_class")
    if predictive_ok is None:
        predictive_ok = representation is not Representation.POINT and meaning not in (
            "class_probability",
            "state_probability",
        )
    return CatalogOutput(
        name=name,
        manifest_name=manifest_name or name,
        dims=list(dims),
        time_dim=time_dim,
        shape=shape,
        representation=representation,
        meaning=meaning,
        classes=list(classes)[:50],
        centre=_CENTRE[representation],
        stats=stats,
        reduces=reduces,
        intents=_intents(name, dims, time_dim, shape, meaning),
        predictive_ok=predictive_ok,
        units=units,
    )


def _static_shape(
    dims: Sequence[str],
    decl: OutputDecl,
    time_dim: str | None,
    meaning: OutputMeaning | None,
    plots: set[str],
    *,
    rows: bool,
) -> OutputShape:
    if decl.grid is not None:
        return "grid"
    if AREA_PLOTS & plots and dims and time_dim is None:
        return "areal"
    if meaning == "class_probability":
        return "classification"
    if not dims and not rows:
        return "scalar"
    if time_dim is not None:
        return "time"
    return "categorical"


def catalog_from_manifest(
    manifest: ModelManifest, *, schema: DataSchema | None = None
) -> SourceCatalog:
    """The catalog the manifest alone gives (manifest names; no fit metrics, scorecard or
    pictures; inputs only with the resolved input ``schema``). The server replaces it with the
    catalog of the real artefacts when the model version has them."""
    caps = manifest.capabilities
    plots = set(manifest.plots)
    outputs: list[CatalogOutput] = []
    row_dim: str | None = None
    time_dims: list[str] = []
    for name, decl in manifest.outputs.items():
        dims = [d for d in decl.full_dims(schema) if d not in SAMPLE_DIMS]
        time_dim = next((d for d in dims if _is_time(d)), None)
        if time_dim is not None:
            time_dims.append(time_dim)
        meaning = guess_meaning(name, dims, decl, plots if name == "y_hat" else ())
        rows = decl.dims_of is not None
        shape = _static_shape(dims, decl, time_dim, meaning, plots, rows=rows)
        if row_dim is None:
            row_dim = next((d for d in dims if d in _ROW_NAMES), None)
        outputs.append(
            catalog_output(
                name,
                dims,
                shape=shape,
                representation=caps.representation,
                time_dim=time_dim,
                meaning=meaning,
                units=decl.units,
            )
        )
    inputs: list[CatalogInput] = []
    row_policy: DimPolicy | None = None
    if schema is not None:
        inputs = _schema_inputs(schema)
        if row_dim is not None and row_dim in schema.dims:
            row_policy = schema.dims[row_dim].policy
    return SourceCatalog(
        outputs=outputs[:64],
        inputs=inputs[:128],
        score_modes=sorted(caps.score_modes),
        conditions_on_actuals=caps.conditions_on_actuals,
        input_kind=caps.input,
        row_dim=row_dim,
        row_policy=row_policy,
        time_dim=time_dims[0] if time_dims else None,
        has_grid=any(o.shape == "grid" for o in outputs),
        family=manifest.model_family,
        namespace=manifest.id.split(".", 1)[0],
    )


def _schema_inputs(schema: DataSchema) -> list[CatalogInput]:
    out: list[CatalogInput] = []
    for name, var in schema.vars.items():
        if var.role.value not in ("covariate", "media", "spend", "control", "exposure"):
            continue
        kind: Literal["number", "integer", "choice", "date", "toggle", "text"]
        dtype = str(var.dtype)
        if dtype.startswith("bool"):
            kind = "toggle"
        elif dtype.startswith("int"):
            kind = "integer"
        elif dtype.startswith(("float", "num")):
            kind = "number"
        elif dtype.startswith("datetime"):
            kind = "date"
        else:
            kind = "text"
        lo, hi = var.bounds if var.bounds is not None else (None, None)
        out.append(CatalogInput(var=name, kind=kind, dtype=dtype[:32], bound_min=lo, bound_max=hi))
    for name, dim in schema.dims.items():
        dim_kind: Literal["choice", "date"] = "date" if str(dim.kind) == "time" else "choice"
        out.append(CatalogInput(dim=name, kind=dim_kind, policy=dim.policy))
    return out


# ================================================================================== detection


class _Facts(NamedTuple):
    family: str | None
    namespace: str | None
    decision: bool
    input_kind: str
    plots: frozenset[str]
    modes: frozenset[str]
    outputs: tuple[CatalogOutput, ...]
    has_rows: bool
    row_policy: DimPolicy | None
    has_grid: bool


def _facts(
    manifest: ModelManifest | None, caps: Capabilities | None, catalog: SourceCatalog
) -> _Facts:
    outputs = tuple(catalog.outputs)
    if not outputs and manifest is not None:
        outputs = tuple(catalog_from_manifest(manifest).outputs)
    plots = {p.recipe for p in catalog.pictures}
    rows = catalog.row_dim is not None
    if manifest is not None:
        plots |= set(manifest.plots)
        rows = rows or any(d.dims_of is not None for d in manifest.outputs.values())
    rows = rows or any(d in _ROW_NAMES for o in outputs for d in o.dims)
    modes = set(catalog.score_modes)
    if caps is not None:
        modes |= set(caps.score_modes)
    return _Facts(
        family=manifest.model_family if manifest is not None else catalog.family,
        namespace=manifest.id.split(".", 1)[0] if manifest is not None else catalog.namespace,
        decision=caps.decision if caps is not None else False,
        input_kind=caps.input if caps is not None else catalog.input_kind,
        plots=frozenset(plots),
        modes=frozenset(modes),
        outputs=outputs,
        has_rows=rows,
        row_policy=catalog.row_policy,
        has_grid=catalog.has_grid or any(o.shape == "grid" for o in outputs),
    )


def _rule(preset: PresetKey, f: _Facts, *, primary: bool) -> list[str] | None:
    """The reasons the preset fits (``None`` when it does not). ``primary``: the strict
    detection rule; else the looser compatibility the analyst may still pick."""
    time_outputs = [o for o in f.outputs if o.time_dim is not None]
    if preset == "mmm_planner":
        if f.decision or f.family == "mmm":
            return ["The model is a media mix model: the planner shows channels and what-ifs."]
        return None
    if preset == "image":
        if f.input_kind == "collection":
            return ["The model learns from a collection of images."]
        return None
    if preset == "field_map":
        if f.has_grid or any(o.shape in ("grid", "areal") for o in f.outputs):
            return ["The model writes maps (a grid or areas)."]
        if AREA_PLOTS & f.plots:
            return ["The model draws its results on areas."]
        if f.namespace == "sci":
            return ["The model is a science model: its results are shown as values and maps."]
        return None
    if preset == "causal_impact":
        if f.namespace == "causal" or f.family == "causal":
            return ["The model estimates the effect of a change (a causal model)."]
        return None
    if preset == "classifier":
        if any(o.meaning == "class_probability" or o.shape == "classification" for o in f.outputs):
            return ["The model gives the chance of each class."]
        if CLASSIFICATION_PLOTS & f.plots:
            return ["The model is judged as a classifier."]
        return None
    if preset == "forecast":
        if "forecast" not in f.modes or not time_outputs:
            return None
        if not primary or f.family in _FORECAST_FAMILIES or f.namespace == "ts":
            return ["The model forecasts an output over time."]
        return None
    if preset == "regression":
        if f.has_rows and ("predict" in f.modes):
            return ["The model predicts one value per case."]
        return None
    return ["Any model: one value and one chart of its main output."]


def _blocked(preset: PresetKey, f: _Facts) -> list[str]:
    allowed = PRESET_RULES[preset].inputs
    out: list[str] = []
    fixed = f.row_policy == DimPolicy.FIXED
    if "scenario_rows" in allowed:
        if f.input_kind == "collection":
            out.append("scenario_rows: the model reads images, not values")
        elif preset == "forecast" and "forecast" not in f.modes:
            out.append("scenario_rows: the model has no forecast mode")
        elif preset != "forecast" and "predict" not in f.modes:
            out.append("scenario_rows: the model has no predict mode")
        elif fixed and preset not in ("forecast", "field_map"):
            out.append("scenario_rows: the model's cases are fixed (no new cases)")
    if "batch" in allowed:
        if f.input_kind == "collection":
            out.append("batch: for signed-in analysts only (not for clients or share links)")
        elif not f.has_rows:
            out.append("batch: the model has no row dim")
        elif fixed:
            out.append("batch: the model's cases are fixed (no new cases)")
    if "actuals" in allowed:
        if preset == "forecast" and "forecast" not in f.modes:
            out.append("actuals: the model has no forecast mode")
        elif preset == "causal_impact" and "forecast" not in f.modes:
            out.append("actuals: compared only; the effect updates after the analyst refits")
    if "whatif" in allowed and "counterfactual" not in f.modes:
        out.append("whatif: the model has no counterfactual mode")
    return out[:10]


def detect_preset(
    catalog: SourceCatalog,
    manifest: ModelManifest | None = None,
    caps: Capabilities | None = None,
) -> PresetKey:
    """The best preset for a model version (the first rule of :data:`PRESET_ORDER`)."""
    f = _facts(manifest, caps or (manifest.capabilities if manifest else None), catalog)
    return next(p for p in PRESET_ORDER if _rule(p, f, primary=True) is not None)


def suggest_presets(
    manifest: ModelManifest, caps: Capabilities, catalog: SourceCatalog
) -> list[PresetSuggestion]:
    """The compatible presets, best first, with plain reasons and the inputs each cannot
    offer. The first is the detected preset (:func:`detect_preset`); ``generic`` is last."""
    f = _facts(manifest, caps, catalog)
    best = next(p for p in PRESET_ORDER if _rule(p, f, primary=True) is not None)
    order = [best] + [p for p in PRESET_ORDER if p not in (best, "generic")] + ["generic"]
    out: list[PresetSuggestion] = []
    for p in dict.fromkeys(order):
        reasons = _rule(p, f, primary=p == best)
        if reasons is None:
            continue
        if p == "generic" and best == "generic":
            reasons = [*reasons, "No preset of a model family matches this model."]
        out.append(PresetSuggestion(preset=p, reasons=reasons, blocked_inputs=_blocked(p, f)))
    return out[:8]


# ==================================================================================== panels


class _Ctx(NamedTuple):
    catalog: SourceCatalog
    subject: SubjectDef
    output: CatalogOutput | None
    period: str


def _metric(cat: SourceCatalog, *keys: str) -> list[CatalogMetric]:
    found: list[CatalogMetric] = []
    for k in keys:
        m = next((m for m in cat.metrics if m.key == k and not m.labels), None)
        if m is not None:
            found.append(m)
    return found


def _out(cat: SourceCatalog, name: str) -> CatalogOutput | None:
    return next((o for o in cat.outputs if o.name == name), None)


def _band(o: CatalogOutput | None) -> Literal["outcome", "mean", "none"]:
    if o is None:
        return "outcome"
    if o.representation is Representation.POINT:
        return "none"
    return "outcome" if o.predictive_ok else "mean"


_METRIC_TITLES: Final[Mapping[str, tuple[str, Literal["up", "down", "none"]]]] = {
    "rmse": ("Typical error (RMSE)", "down"),
    "mae": ("Typical error (MAE)", "down"),
    "mape": ("Typical error in percent", "down"),
    "r2": ("Share of variation explained", "up"),
    "coverage_90": ("Results inside the 90% range", "none"),
    "coverage_80": ("Results inside the 80% range", "none"),
    "auc": ("How well the model ranks cases (AUC)", "up"),
    "accuracy": ("How often the model is right", "up"),
    "brier": ("Probability error (Brier)", "down"),
    "effect_total": ("Total effect of the change", "up"),
    "effect_relative": ("Effect in percent", "up"),
    "p_effect_positive": ("Chance that the effect is positive", "up"),
    "pre_mape": ("Fit before the change (error in percent)", "down"),
    "att": ("Average effect on the treated", "up"),
}


def _metric_kpis(cat: SourceCatalog, keys: Sequence[str], limit: int) -> list[AnyPanel]:
    out: list[AnyPanel] = []
    for m in _metric(cat, *keys)[:limit]:
        title, good = _METRIC_TITLES.get(m.key, (m.key.replace("_", " ").capitalize(), "none"))
        out.append(
            KpiPanel(
                id=f"m_{m.key}"[:32],
                title=title,
                source=MetricSource(key=m.key),
                stat="value",
                band="none",
                compare="none",
                good=good,
            )
        )
    return out


def _chart(
    pid: str, title: str, intent: ChartIntent, o: CatalogOutput, **kw: object
) -> ChartPanel | None:
    if intent not in o.intents:
        return None
    return ChartPanel(
        id=pid,
        title=title,
        intent=intent,
        source=OutputSource(variable=o.name),
        band=_band(o) if o.representation is not Representation.POINT else "none",
        **kw,
    )


def _value_kpi(pid: str, title: str, o: CatalogOutput, **kw: object) -> KpiPanel:
    return KpiPanel(
        id=pid,
        title=title,
        source=OutputSource(variable=o.name),
        band=_band(o),
        **kw,
    )


def _noun(subject: SubjectDef) -> str:
    return subject.label.lower()


def _pictures(cat: SourceCatalog, recipes: Sequence[str]) -> list[AnyPanel]:
    out: list[AnyPanel] = []
    for r in recipes:
        pic = next((p for p in cat.pictures if p.recipe == r), None)
        if pic is None:
            continue
        pid = "p_" + re.sub(r"[^a-z0-9_]", "_", r.split(".", 1)[-1])[:28]
        out.append(
            RecipePanel(
                id=pid,
                title=(pic.title or r)[:80],
                recipe=r,
                plot_id=pic.plot_id,
            )
        )
    return out


def _panels_forecast(c: _Ctx) -> list[AnyPanel]:
    o, s = c.output, c.subject
    out: list[AnyPanel] = []
    if o is None:
        return out
    if "next" in o.reduces:
        out.append(
            _value_kpi("next", f"{s.label} next {c.period}"[:80], o, reduce=Reduce(how="next"))
        )
    if o.representation is Representation.DRAWS and "forecast" in c.catalog.score_modes:
        out.append(
            KpiPanel(
                id="horizon_total",
                title=f"{s.label} over the forecast"[:80],
                source=ViewSource(view="forecast_total"),
                band=_band(o),
            )
        )
    out.append(
        KpiPanel(
            id="typical_error",
            title="Typical error",
            source=AccuracySource(metric="mae"),
            stat="value",
            band="none",
            good="down",
        )
    )
    out.append(
        KpiPanel(
            id="inside_range",
            title="Results inside the likely range",
            source=AccuracySource(metric="band_hit_rate"),
            stat="value",
            band="none",
            good="none",
        )
    )
    for p in (
        _chart("fan", f"{s.label} and forecast"[:80], "fan", o),
        ChartPanel(
            id="accuracy",
            title="Accuracy over time",
            intent="accuracy_history",
            source=AccuracySource(metric="mape"),
        ),
    ):
        if p is not None:
            out.append(p)
    if o.time_dim is not None:
        cols = [*o.dims, "centre"] + (["low", "high"] if "low" in o.stats else [])
        out.append(
            TablePanel(
                id="next_periods",
                title=f"The next {c.period}s"[:80] if c.period != "period" else "The next periods",
                source=OutputSource(variable=o.name),
                columns=cols[:12],
                max_rows=12,
            )
        )
    for other in c.catalog.outputs:
        if other.name == o.name or other.time_dim is None:
            continue
        for intent, title in (
            ("decomposition", "Parts of the forecast"),
            ("state_sequence", "Hidden states over time"),
        ):
            panel = _chart(f"o_{other.name}"[:32], title, cast(ChartIntent, intent), other)
            if panel is not None and not any(p.id == panel.id for p in out):
                out.append(panel)
    return out


def _panels_regression(c: _Ctx) -> list[AnyPanel]:
    o, s = c.output, c.subject
    out: list[AnyPanel] = []
    if o is None:
        return out
    out.append(_value_kpi("predicted", f"Predicted {_noun(s)}"[:80], o))
    out += _metric_kpis(c.catalog, ("rmse", "mae", "r2", "coverage_90", "coverage_80"), 3)
    for p in (
        _chart("by_case", f"{s.label} per case"[:80], "range", o),
        _chart("fit", "Predicted and actual", "scatter", o),
        _chart("residuals", "Errors per case", "residuals", o),
    ):
        if p is not None:
            out.append(p)
    contributions = _out(c.catalog, "contributions")
    if contributions is not None:
        panel = _chart("contributions", "What drives the prediction", "bars", contributions)
        if panel is not None:
            out.append(panel)
    out += _pictures(c.catalog, ("ml.feature_importance",))
    return out


def _class_dim_of(o: CatalogOutput) -> str | None:
    return _class_dim(o.dims)


def _panels_classifier(c: _Ctx) -> list[AnyPanel]:
    o, s = c.output, c.subject
    out: list[AnyPanel] = []
    if o is None:
        return out
    cdim = _class_dim_of(o)
    if cdim is None or s.positive_label is not None:
        select = {cdim: s.positive_label} if cdim and s.positive_label else {}
        title = f"Chance of {s.positive_label}" if s.positive_label else "Chance of a positive"
        out.append(
            KpiPanel(
                id="chance",
                title=title[:80],
                source=OutputSource(variable=o.name, select=select),
                reduce=Reduce(how="select") if select else Reduce(),
                band="mean" if o.representation is not Representation.POINT else "none",
            )
        )
    if cdim is not None and "top_class" in o.reduces:
        out.append(
            KpiPanel(
                id="most_likely",
                title="Most likely result",
                source=OutputSource(variable=o.name),
                reduce=Reduce(how="top_class", over=[cdim]),
                band="none",
                compare="none",
                good="none",
            )
        )
    out += _metric_kpis(c.catalog, ("auc", "accuracy"), 2)
    for p in (
        _chart("chances", "Chance of each result", "probability_bars", o),
        _chart("calibration", "Predicted and observed chances", "calibration", o),
        _chart("confusion", "Right and wrong results", "confusion", o),
        ChartPanel(
            id="accuracy",
            title="How often the model was right",
            intent="accuracy_history",
            source=AccuracySource(metric="accuracy"),
        ),
    ):
        if p is not None:
            out.append(p)
    return out


def _panels_causal(c: _Ctx) -> list[AnyPanel]:
    out: list[AnyPanel] = _metric_kpis(
        c.catalog, ("effect_total", "att", "effect_relative", "p_effect_positive", "pre_mape"), 4
    )
    effect = _out(c.catalog, "effect")
    if effect is not None and effect.time_dim is not None:
        if not out and "sum" in effect.reduces:
            out.append(
                _value_kpi(
                    "effect_total",
                    "Total effect of the change",
                    effect,
                    reduce=Reduce(how="sum", over=[effect.time_dim]),
                )
            )
        panel = _chart("effect", "Effect of the change over time", "effect", effect)
        if panel is not None:
            out.append(panel)
    if c.output is not None and c.output.name != "effect":
        panel = _chart("outcome", f"{c.subject.label} over time"[:80], "fan", c.output)
        if panel is not None:
            out.append(panel)
    out += _pictures(c.catalog, ("causal.event_study", "causal.donor_weights"))
    return out


def _panels_field(c: _Ctx) -> list[AnyPanel]:
    out: list[AnyPanel] = []
    for o in [o for o in c.catalog.outputs if o.shape == "scalar"][:4]:
        out.append(_value_kpi(f"k_{o.name}"[:32], o.name.replace("_", " ").capitalize()[:80], o))
    grid = next((o for o in c.catalog.outputs if o.shape == "grid"), None)
    if grid is not None:
        out.append(
            ChartPanel(
                id="map",
                title=grid.name.replace("_", " ").capitalize()[:80],
                intent="field",
                source=OutputSource(variable=grid.name),
                band="none",
            )
        )
    areal = next((o for o in c.catalog.outputs if o.shape == "areal"), None)
    if areal is not None:
        panel = _chart(
            "areas", f"{areal.name.replace('_', ' ').capitalize()} by area"[:80], "area_bars", areal
        )
        if panel is not None:
            out.append(panel)
    timed = next((o for o in c.catalog.outputs if o.shape == "time"), None)
    if timed is not None:
        panel = _chart(
            "over_time", f"{timed.name.replace('_', ' ').capitalize()} over time"[:80], "fan", timed
        )
        if panel is not None:
            out.append(panel)
    if not out and c.output is not None:
        out += _panels_generic(c)
    return out


def _panels_image(c: _Ctx) -> list[AnyPanel]:
    out: list[AnyPanel] = _metric_kpis(c.catalog, ("accuracy", "auc"), 2)
    out += _pictures(c.catalog, ("ml.training_curves", "ml.misclassified", "ml.confusion_epochs"))
    return out


def _panels_generic(c: _Ctx) -> list[AnyPanel]:
    o = c.output
    if o is None:
        return []
    out: list[AnyPanel] = []
    kpi_reduce = Reduce(how="last") if "last" in o.reduces else Reduce()
    if o.shape != "grid":
        out.append(_value_kpi("value", c.subject.label, o, reduce=kpi_reduce))
    intent: ChartIntent | None = {
        "time": "fan",
        "categorical": "range",
        "classification": "probability_bars",
        "grid": "field",
        "areal": "area_bars",
        "scalar": None,
    }[o.shape]
    if intent is not None:
        panel = _chart("chart", c.subject.label, intent, o)
        if panel is not None:
            out.append(panel)
    return out


def _panels_mmm(c: _Ctx, *, tabs: Sequence[str] = (), whatif: bool = True) -> list[AnyPanel]:
    tabs = tabs or ("this_period", "trends", "scenarios", "history")
    out: list[AnyPanel] = [MmmPanel(id="tiles", title="This period", part="tiles")]
    o = c.output or _out(c.catalog, c.subject.variable)
    fan = ChartPanel(
        id="fan",
        title=f"{c.subject.label} over time"[:80],
        intent="fan",
        source=OutputSource(variable=o.name if o is not None else c.subject.variable),
        band=_band(o),
    )
    out.append(fan)
    out.append(MmmPanel(id="scorecard", title="Channels", part="scorecard"))
    out.append(MmmPanel(id="changes", title="What changed", part="changes"))
    if "trends" in tabs:
        out.append(MmmPanel(id="roi_trend", title="Return on media over time", part="roi_trend"))
    if "scenarios" in tabs and whatif:
        out.append(MmmPanel(id="scenarios", title="Scenarios", part="scenarios"))
    return out


class PresetDef(NamedTuple):
    """A preset as data: what it allows, its lead sentences and its default panels."""

    key: PresetKey
    title: str
    rules: PresetRules
    leads: Mapping[str, str]
    panels: Callable[[_Ctx], list[AnyPanel]]


PRESETS: Final[Mapping[PresetKey, PresetDef]] = {
    "mmm_planner": PresetDef(
        "mmm_planner",
        "Media planner",
        PRESET_RULES["mmm_planner"],
        LEADS["mmm_planner"],
        _panels_mmm,
    ),
    "forecast": PresetDef(
        "forecast", "Forecast", PRESET_RULES["forecast"], LEADS["forecast"], _panels_forecast
    ),
    "regression": PresetDef(
        "regression",
        "Prediction per case",
        PRESET_RULES["regression"],
        LEADS["regression"],
        _panels_regression,
    ),
    "classifier": PresetDef(
        "classifier",
        "Chance per case",
        PRESET_RULES["classifier"],
        LEADS["classifier"],
        _panels_classifier,
    ),
    "causal_impact": PresetDef(
        "causal_impact",
        "Effect of a change",
        PRESET_RULES["causal_impact"],
        LEADS["causal_impact"],
        _panels_causal,
    ),
    "field_map": PresetDef(
        "field_map", "Maps and values", PRESET_RULES["field_map"], LEADS["field_map"], _panels_field
    ),
    "image": PresetDef(
        "image", "Image model", PRESET_RULES["image"], LEADS["image"], _panels_image
    ),
    "generic": PresetDef(
        "generic", "Any model", PRESET_RULES["generic"], LEADS["generic"], _panels_generic
    ),
}
"""Every preset (keys = :data:`~mapi_spec.dashboard_v2.PresetKey`)."""

if set(PRESETS) != set(PRESET_RULES):  # pragma: no cover - import-time contract
    raise RuntimeError("PRESETS and PRESET_RULES disagree")


# ================================================================================== compile


def primary_output(catalog: SourceCatalog, preset: PresetKey) -> CatalogOutput | None:
    """The main output of a preset: a probability output for a classifier, a time output for
    a forecast, a grid for a map; else ``y_hat``, else the first output."""
    outs = catalog.outputs
    if not outs:
        return None
    pick: list[CatalogOutput] = []
    if preset == "classifier":
        pick = [o for o in outs if o.meaning == "class_probability" or o.shape == "classification"]
    elif preset in ("forecast", "mmm_planner", "causal_impact"):
        pick = [o for o in outs if o.time_dim is not None and o.name != "effect"]
    elif preset == "field_map":
        pick = [o for o in outs if o.shape in ("grid", "areal")]
    pick = pick or list(outs)
    return next((o for o in pick if o.name == "y_hat"), pick[0])


def _label_of(name: str) -> str:
    if name == "y_hat":
        return "Prediction"
    return (name.replace("_", " ").strip().capitalize() or "Result")[:60]


def _subject(preset: PresetKey, o: CatalogOutput | None) -> SubjectDef:
    if o is None:
        return SubjectDef(variable="y_hat", label="Result", unit="number")
    if preset == "classifier" or o.meaning == "class_probability":
        positive = o.classes[-1] if len(o.classes) == 2 else None
        label = f"Chance of {positive}" if positive else "Chance"
        return SubjectDef(
            variable=o.name,
            label=label[:60],
            unit="probability",
            decimals=2,
            positive_label=positive,
        )
    unit: TileUnit = "rate" if o.meaning == "rate" else "number"
    return SubjectDef(variable=o.name, label=_label_of(o.name), unit=unit, decimals=1)


def _period_of(preset: PresetKey, catalog: SourceCatalog) -> str:
    if preset in ("regression", "classifier", "image"):
        return "case"
    t = (catalog.time_dim or "").lower()
    return t if t in _PERIOD_WORDS else "period"


def _ensure(panels: list[AnyPanel], preset: PresetKey) -> list[AnyPanel]:
    rules = PRESET_RULES[preset]
    out: list[AnyPanel] = []
    seen: set[str] = set()
    kpis = 0
    for p in panels:
        if p.id in seen or p.kind not in rules.kinds:
            continue
        if isinstance(p, ChartPanel) and p.intent not in rules.intents:
            continue
        if p.kind == "kpi":
            if kpis >= 8:
                continue
            kpis += 1
        seen.add(p.id)
        out.append(p)
    if not out:
        out.append(
            NotePanel(
                id="about",
                title="About this dashboard",
                text="The results of the model show here when your analyst adds them.",
            )
        )
    return out[:24]


def compile_preset(
    preset: PresetKey,
    catalog: SourceCatalog,
    *,
    title: str,
    source: DashboardSource,
    channels: Sequence[ChannelDef] | None = None,
    subject: SubjectDef | None = None,
) -> DashboardSpecV2:
    """The starting draft of a preset on a model version: its default panels from what the
    catalog offers, the subject (the main output), and every input off. The
    ``mmm_planner`` preset needs the model's ``channels`` (from its response grid). Raises
    ``ValueError`` when it cannot be built."""
    o = primary_output(catalog, preset)
    subj = subject or _subject(preset, o)
    if subject is not None:
        o = _out(catalog, subject.variable) or o
    period = _period_of(preset, catalog)
    ctx = _Ctx(catalog, subj, o, period)
    mmm: MmmPlanner | None = None
    if preset == "mmm_planner":
        if not channels:
            raise ValueError("the media planner preset needs the model's channels")
        mmm = MmmPlanner(channels=list(channels))
    panels = _ensure(PRESETS[preset].panels(ctx), preset)
    refresh = RefreshPolicy(horizon=6) if preset == "forecast" else RefreshPolicy()
    return DashboardSpecV2(
        layout_version=2,
        preset=preset,
        title=title,
        period_label=period,
        source=source,
        subject=subj,
        panels=panels,
        mmm=mmm,
        refresh=refresh,
    )


# ================================================================================== upgrade


def _remap(variable: str, catalog: SourceCatalog, preset: PresetKey) -> tuple[str, str | None]:
    names = {o.name for o in catalog.outputs}
    if not names or variable in names:
        return variable, None
    o = primary_output(catalog, preset)
    assert o is not None
    return o.name, f"The KPI {variable!r} is now read from the model output {o.name!r}."


def _v1_preset(spec: DashboardSpec, catalog: SourceCatalog) -> PresetKey:
    if catalog.family == "mmm" or catalog.namespace == "mmm":
        return "mmm_planner"
    if catalog.family is None and catalog.namespace is None and not catalog.outputs:
        return "mmm_planner" if spec.whatif.enabled else "generic"
    preset = detect_preset(catalog)
    return "generic" if preset == "mmm_planner" else preset


def upgrade_v1(spec: DashboardSpec, catalog: SourceCatalog) -> tuple[DashboardSpecV2, list[str]]:
    """The layout 2 spec of a layout 1 spec, and plain notes on what changed.

    A media mix model (catalog family ``mmm``) becomes ``mmm_planner``: the same channels,
    tiles, what-ifs, actuals, refresh, limits and texts, and panels that follow the layout 1
    tabs (the subject keeps the layout 1 KPI variable, so the planner numbers stay the same).
    Any other model (a forecast whose channel was a placeholder) becomes the preset of
    its family without channels. The ranges of outcome panels change to predictive ranges
    (a note says so). Raises ``ValueError`` (or a pydantic ``ValidationError``) when the
    result is not a valid layout 2 spec; :func:`check_spec_v2` then tells what blocks it."""
    preset = _v1_preset(spec, catalog)
    notes: list[str] = []
    if preset == "mmm_planner":  # the planner reads the KPI as layout 1 does (same numbers)
        variable, o = spec.kpi.variable, primary_output(catalog, preset)
    else:
        variable, note = _remap(spec.kpi.variable, catalog, preset)
        o = _out(catalog, variable)
        if note:
            notes.append(note)
    subject = SubjectDef(
        variable=variable,
        label=spec.kpi.label,
        unit=spec.kpi.unit,
        currency=spec.kpi.currency,
        decimals=spec.kpi.decimals,
        value_per_unit=spec.kpi.value_per_unit if preset == "mmm_planner" else None,
    )
    actuals = ActualsInputsV2(**spec.actuals.model_dump())
    allowed = PRESET_RULES[preset].inputs
    if actuals.enabled and "actuals" not in allowed:
        actuals = ActualsInputsV2()
        notes.append(f"Uploads of actuals are not part of the {preset} preset: they are off.")
    limits = DashboardLimitsV2(exact_runs_per_day=spec.limits.exact_runs_per_day)
    refresh = spec.refresh
    if preset == "forecast" and refresh.horizon is None:
        refresh = refresh.model_copy(update={"horizon": 6})
    common = {
        "layout_version": 2,
        "preset": preset,
        "title": spec.title,
        "client_name": spec.client_name,
        "summary": spec.summary,
        "note": spec.note,
        "period_label": spec.period_label,
        "source": spec.source,
        "subject": subject,
        "actuals": actuals,
        "refresh": refresh,
        "limits": limits,
        "show_technical": spec.show_technical,
    }
    ctx = _Ctx(catalog, subject, o, spec.period_label)
    if preset == "mmm_planner":
        panels = _panels_mmm(ctx, tabs=spec.tabs, whatif=spec.whatif.enabled)
        mmm = MmmPlanner(channels=spec.channels, whatif=spec.whatif, tiles=spec.tiles)
        out = DashboardSpecV2.model_validate({**common, "panels": panels, "mmm": mmm})
    else:
        panels = _ensure(PRESETS[preset].panels(ctx), preset)
        out = DashboardSpecV2.model_validate({**common, "panels": panels})
        names = ", ".join(repr(c.name) for c in spec.channels)
        notes.append(
            f"The channels {names} were placeholders: the {preset} preset shows "
            f"{spec.kpi.label.lower()} without channels."
        )
        if spec.whatif.enabled:
            notes.append(
                "Channel what-ifs are not part of this preset. Turn on scenario rows to let "
                "clients try their own values."
            )
    if any(getattr(p, "band", None) == "outcome" for p in out.panels):
        notes.append(UPGRADE_NOTE_RANGES)
    return out, notes


# ==================================================================================== checks


def _err(
    pointer: str,
    message: str,
    *,
    code: ErrorCode = ErrorCode.BINDING_INVALID,
    action: str | None = None,
) -> Issue:
    return Issue(code=code, pointer=pointer, message=message, suggested_action=action)


def _warn(pointer: str, message: str, *, action: str | None = None) -> Issue:
    return Issue(
        code=ErrorCode.BINDING_INVALID,
        severity=Severity.WARNING,
        pointer=pointer,
        message=message,
        suggested_action=action,
    )


def _check_output_source(
    base: str,
    src: OutputSource,
    catalog: SourceCatalog,
    *,
    reduce: Reduce,
    band: str | None,
) -> tuple[list[Issue], CatalogOutput | None]:
    issues: list[Issue] = []
    o = _out(catalog, src.variable)
    if o is None:
        names = ", ".join(sorted(x.name for x in catalog.outputs)) or "none"
        return [
            _err(
                f"{base}/source/variable",
                f"{src.variable!r} is not an output of the model (outputs: {names})",
            )
        ], None
    for dim in src.select:
        if dim not in o.dims:
            issues.append(
                _err(
                    f"{base}/source/select/{dim}", f"{dim!r} is not a dim of the output {o.name!r}"
                )
            )
    how = reduce.how
    if how not in o.reduces:
        rules = REPRESENTATION_RULES[o.representation]
        if how in ("sum", "mean") and rules.aggregate == "none":
            issues.append(
                _err(
                    f"{base}/reduce",
                    f"the {o.representation.value} of {o.name!r} cannot be added up: {rules.note}",
                    action="show per-period values",
                )
            )
        elif how in ("last", "next") and o.time_dim is None:
            issues.append(_err(f"{base}/reduce", f"the output {o.name!r} has no time dim"))
        else:
            issues.append(_err(f"{base}/reduce", f"{how!r} is not possible on {o.name!r}"))
    elif how in ("sum", "mean") and o.representation is Representation.POINT:
        issues.append(
            Issue(
                code=ErrorCode.BINDING_INVALID,
                severity=Severity.INFO,
                pointer=f"{base}/reduce",
                message=f"the {how} of point predictions has no range: it is marked approximate",
            )
        )
    for dim in reduce.over:
        if dim not in o.dims:
            issues.append(_err(f"{base}/reduce/over", f"{dim!r} is not a dim of {o.name!r}"))
    if how == "next" and "forecast" not in catalog.score_modes:
        issues.append(
            _err(
                f"{base}/reduce",
                "the next period needs the forecast mode",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
            )
        )
    if band == "outcome" and not o.predictive_ok and o.representation is not Representation.POINT:
        issues.append(
            _warn(
                f"{base}/band",
                f"{o.name!r} has no range of single results: the panel shows "
                "the range of the average",
                action="set band to mean",
            )
        )
    return issues, o


def _check_panel(i: int, p: AnyPanel, spec: DashboardSpecV2, cat: SourceCatalog) -> list[Issue]:
    base = json_pointer("panels", i)
    issues: list[Issue] = []
    if isinstance(p, KpiPanel | ChartPanel):
        src = p.source
        if isinstance(src, OutputSource):
            found, o = _check_output_source(base, src, cat, reduce=p.reduce, band=p.band)
            issues += found
            if o is not None and isinstance(p, KpiPanel) and p.stat not in (*o.stats, "value"):
                issues.append(_err(f"{base}/stat", f"{o.name!r} has no {p.stat} (no range)"))
            if o is not None and isinstance(p, ChartPanel):
                if p.intent not in o.intents:
                    issues.append(
                        _err(
                            f"{base}/intent",
                            f"a {p.intent} chart does not fit the {o.shape} output {o.name!r}",
                        )
                    )
                if p.facet is not None and p.facet not in o.dims:
                    issues.append(_err(f"{base}/facet", f"{p.facet!r} is not a dim of {o.name!r}"))
        elif isinstance(src, MetricSource):
            if not any(m.key == src.key and m.labels == src.labels for m in cat.metrics):
                issues.append(
                    _err(
                        f"{base}/source/key",
                        f"the fit run logged no metric {src.key!r}"
                        + (f" with labels {src.labels}" if src.labels else ""),
                    )
                )
        elif isinstance(src, ScorecardSource):
            if src.check not in cat.scorecard:
                issues.append(
                    _err(f"{base}/source/check", f"the scorecard has no check {src.check!r}")
                )
        elif isinstance(src, ViewSource):
            issues += _check_view(base, src, spec, cat)
        elif isinstance(src, AccuracySource) and not spec.actuals.enabled:
            issues.append(
                Issue(
                    code=ErrorCode.BINDING_INVALID,
                    severity=Severity.INFO,
                    pointer=f"{base}/source",
                    message="accuracy shows after actuals are uploaded; uploads are off",
                )
            )
    elif isinstance(p, TablePanel):
        found, o = _check_output_source(base, p.source, cat, reduce=Reduce(), band=None)
        issues += found
        if o is not None:
            ok = {*o.dims, *o.stats}
            for j, col in enumerate(p.columns):
                if col not in ok:
                    issues.append(
                        _err(f"{base}/columns/{j}", f"{col!r} is not a column of {o.name!r}")
                    )
    elif isinstance(p, RecipePanel):
        if p.output is not None and _out(cat, p.output) is None:
            issues.append(_err(f"{base}/output", f"{p.output!r} is not an output of the model"))
        if p.is_picture:
            pic = next((x for x in cat.pictures if x.plot_id == p.plot_id), None)
            if pic is None:
                issues.append(
                    _err(
                        f"{base}/plot_id",
                        f"the model version has no shareable plot "
                        f"{p.plot_id!r} (only plots of platform recipes can be shown)",
                    )
                )
            elif pic.recipe != p.recipe:
                issues.append(_err(f"{base}/recipe", f"the plot {p.plot_id!r} is a {pic.recipe}"))
    return issues


def _check_view(
    base: str, src: ViewSource, spec: DashboardSpecV2, cat: SourceCatalog
) -> list[Issue]:
    if src.view == "forecast_total":
        o = _out(cat, spec.subject.variable)
        if "forecast" not in cat.score_modes:
            return [
                _err(
                    f"{base}/source/view",
                    "the forecast total needs the forecast mode",
                    code=ErrorCode.CAPABILITY_UNSUPPORTED,
                )
            ]
        if o is not None and o.representation is not Representation.DRAWS:
            return [
                _err(
                    f"{base}/source/view",
                    f"a total over the horizon needs draws; "
                    f"{o.name!r} has {o.representation.value}",
                    action="show per-period values",
                )
            ]
    if src.view == "counterfactual_delta" and "counterfactual" not in cat.score_modes:
        return [
            _err(
                f"{base}/source/view",
                "the model has no counterfactual mode",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
            )
        ]
    if src.view == "conditioning" and not cat.conditions_on_actuals:
        return [
            _err(
                f"{base}/source/view",
                "the model cannot condition a forecast on actuals",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
            )
        ]
    return []


def _check_inputs(spec: DashboardSpecV2, cat: SourceCatalog) -> list[Issue]:
    issues: list[Issue] = []
    rows = spec.scenario_rows
    fixed_rows = cat.row_policy == DimPolicy.FIXED
    if rows.enabled:
        mode = "forecast" if rows.horizon is not None else "predict"
        if mode not in cat.score_modes:
            issues.append(
                _err(
                    "/scenario_rows/enabled",
                    f"scenario rows need the {mode} mode",
                    code=ErrorCode.CAPABILITY_UNSUPPORTED,
                )
            )
        if cat.input_kind == "collection":
            issues.append(
                _err(
                    "/scenario_rows/enabled",
                    "the model reads images, not values",
                    code=ErrorCode.CAPABILITY_UNSUPPORTED,
                )
            )
        bound = {i.bind.dim for i in rows.inputs if isinstance(i.bind, DimBind)}
        if fixed_rows and rows.horizon is None and cat.row_dim not in bound | set(rows.fixed):
            issues.append(
                _err(
                    "/scenario_rows/enabled",
                    "the model's cases are fixed: a row "
                    "can only pick an existing case (add an input for it)",
                )
            )
        for j, name in enumerate(rows.outputs):
            if _out(cat, name) is None:
                issues.append(_err(f"/scenario_rows/outputs/{j}", f"{name!r} is not an output"))
    for j, idef in enumerate(rows.inputs):
        ptr = f"/scenario_rows/inputs/{j}"
        if isinstance(idef.bind, VarBind):
            ci = next((c for c in cat.inputs if c.var == idef.bind.var), None)
        else:
            ci = next((c for c in cat.inputs if c.dim == idef.bind.dim), None)
        if ci is None:
            if cat.inputs:
                issues.append(_err(f"{ptr}/bind", "the input binds nothing the model reads"))
            continue
        if idef.type in ("number", "integer") and idef.min is not None and idef.max is not None:
            if (ci.bound_min is not None and idef.min < ci.bound_min) or (
                ci.bound_max is not None and idef.max > ci.bound_max
            ):
                issues.append(
                    _err(
                        f"{ptr}/min",
                        f"the range is outside the bounds the model "
                        f"declares ({ci.bound_min}, {ci.bound_max})",
                    )
                )
            elif (
                ci.bound_min is None
                and ci.bound_max is None
                and (ci.profile_min is not None or ci.profile_max is not None)
            ):
                issues.append(
                    _warn(
                        f"{ptr}/min",
                        "the range comes from the fit data: check it",
                        action="confirm the range",
                    )
                )
        if idef.type == "choice" and ci.levels and ci.policy == DimPolicy.FIXED:
            known = set(ci.levels)
            for k, ch in enumerate(idef.choices):
                if ch not in known:
                    issues.append(
                        _err(
                            f"{ptr}/choices/{k}",
                            f"{ch!r} is not a fitted level (the dim takes no new levels)",
                        )
                    )
    batch = spec.batch
    if batch is not None and batch.enabled:
        if fixed_rows:
            issues.append(
                _err(
                    "/batch/enabled",
                    "the model's cases are fixed: it cannot score new cases",
                    code=ErrorCode.CAPABILITY_UNSUPPORTED,
                )
            )
        if "predict" not in cat.score_modes:
            issues.append(
                _err(
                    "/batch/enabled",
                    "batch scoring needs the predict mode",
                    code=ErrorCode.CAPABILITY_UNSUPPORTED,
                )
            )
        for j, name in enumerate(batch.outputs):
            if _out(cat, name) is None:
                issues.append(_err(f"/batch/outputs/{j}", f"{name!r} is not an output"))
    act = spec.actuals
    if act.enabled and act.match == "row" and cat.row_dim is None and cat.outputs:
        issues.append(_warn("/actuals/match", "the model has no row dim to match cases on"))
    if (
        act.enabled
        and act.match == "time"
        and spec.preset == "forecast"
        and ("forecast" not in cat.score_modes)
    ):
        issues.append(
            _err(
                "/actuals/enabled",
                "actuals need the forecast mode",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
            )
        )
    if (
        spec.mmm is not None
        and spec.mmm.whatif.enabled
        and cat.score_modes
        and ("counterfactual" not in cat.score_modes)
    ):
        issues.append(
            _err(
                "/mmm/whatif/enabled",
                "what-ifs need the counterfactual mode",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
            )
        )
    return issues


def check_spec_v2(spec: DashboardSpecV2, catalog: SourceCatalog) -> list[Issue]:
    """What the spec asks that the model version (its catalog) cannot give. Errors block a
    publish; warnings ask the analyst to check (a profile-derived range, a band that falls
    back to the average); infos explain (an approximate sum of point predictions)."""
    cat = catalog
    issues: list[Issue] = []
    if spec.preset != "mmm_planner" and cat.outputs and _out(cat, spec.subject.variable) is None:
        issues.append(
            _err("/subject/variable", f"{spec.subject.variable!r} is not an output of the model")
        )
    if spec.preset == "forecast" and "forecast" not in cat.score_modes:
        issues.append(
            _err(
                "/preset",
                "the forecast preset needs a model with a forecast mode",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
            )
        )
    if spec.preset == "image" and cat.input_kind != "collection" and cat.outputs:
        issues.append(_warn("/preset", "the image preset is for models of image collections"))
    for i, p in enumerate(spec.panels):
        issues += _check_panel(i, p, spec, cat)
    issues += _check_inputs(spec, cat)
    return issues[:200]
