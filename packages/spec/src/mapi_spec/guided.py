"""Guided mode: templates, the question flow, role detection and drafts (plan §11; M3).

Guided mode is organised around tasks. The user picks a :class:`GuidedTemplate` ("What do
you want to learn?"), answers plain-language :class:`GuidedQuestion` s about their columns
("Which column is revenue?") - pre-filled by :func:`suggest_roles` from column profiles -
and gets a **draft**: an :class:`~mapi_spec.ingest.IngestMapping` for the upload plus a
``ModelSpec`` document (and its YAML, "View as YAML"), built by :func:`build_draft`.

Templates are data. Their ``spec_template`` is a ``ModelSpec`` document with
``{{placeholder}}`` strings, filled from answers, template defaults and lookups
(:func:`render_template`); every placeholder is covered by a question, a default, a lookup
or a value the builder always provides (:data:`BUILDER_VALUES`), which a test checks.

Built-in templates (:data:`GUIDED_TEMPLATES`):

=================  ==========================  =============================================
id                 plugin                      for
=================  ==========================  =============================================
``geo_mmm``        ``mmm.hierarchical_geo``    marketing mix by market (what-if, priors)
``weekly_forecast````ts.hsgp_trend``           one weekly series, forecast ahead
``store_demand``   ``tabular.regressor``       demand per store and week (quantiles)
``customer_choice````choice.multinomial_logit``which product customers choose; needs a
                                               built-in choice model (planned, not shipped)
=================  ==========================  =============================================
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import Field, JsonValue, model_validator

from mapi_spec._dto import RequestModel, ResponseModel, dto_examples
from mapi_spec._types import Slug
from mapi_spec.calibration import BusinessPriors, LiftTest
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity
from mapi_spec.ingest import (
    BAYESINSIGHT_PLUGIN,
    BayesInsightMetadata,
    BayesInsightModelDef,
    DimMapping,
    IngestMapping,
    IngestPreset,
    MffOptions,
    VarMapping,
    WideVarMapping,
    bayesinsight_var_ref,
)
from mapi_spec.loader import dump_yaml
from mapi_spec.refs import DatasetRef
from mapi_spec.schema import DimSpec, Role
from mapi_spec.scorecard import SCORECARD_CHECKS, CheckThresholds
from mapi_spec.spec import API_VERSION, ModelSpec

__all__ = [
    "BUILDER_VALUES",
    "GUIDED_TEMPLATES",
    "BayesInsightImport",
    "ColumnKind",
    "ColumnProfile",
    "DetectRolesRequest",
    "GuidedAnswers",
    "GuidedDraft",
    "GuidedQuestion",
    "GuidedTemplate",
    "QuestionChoice",
    "QuestionSuggestion",
    "RoleCandidate",
    "RoleSuggestions",
    "TemplateLookup",
    "UiLevel",
    "build_draft",
    "channel_label",
    "draft_from_bayesinsight",
    "get_template",
    "render_template",
    "safe_name",
    "suggest_roles",
    "template_placeholders",
]

UiLevel = Literal["basic", "advanced", "hidden"]
ColumnKind = Literal["number", "integer", "string", "date", "boolean"]
GuidedTask = Literal["whatif", "forecast", "counterfactual", "business_priors", "lift_tests"]

_PLACEHOLDER: Final = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")
BUILDER_VALUES: Final = frozenset({"name", "project", "dataset"})
"""Placeholders :func:`build_draft` always fills: the spec name, the project and the
``dataset://`` reference."""


# ===================================================================================== templates


class QuestionChoice(ResponseModel):
    value: str = Field(..., max_length=64)
    label: str = Field(..., max_length=200)


class GuidedQuestion(ResponseModel):
    """One plain-language question. Column questions name what the column becomes:

    * ``dim``: the column labels a dataset dim (``dim_spec`` describes it); the placeholder
      ``{{<id>}}`` is the dataset dim name;
    * ``role`` (``column``): the column is a data variable with that role; the placeholder is
      the dataset variable name;
    * ``role`` + ``wide_dim`` (``columns``): the columns become *one* variable ``as_var``
      along ``wide_dim`` (one label per column, e.g. spend per channel);
    * ``role`` without ``wide_dim`` (``columns``): each column is its own variable.
    """

    id: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,31}$")
    prompt_plain: str = Field(..., max_length=300)
    help_plain: str | None = Field(None, max_length=1000)
    kind: Literal["column", "columns", "choice", "number", "text"]
    required: bool = True
    role: Role | None = None
    dim: str | None = Field(None, description="Canonical dim this column labels")
    dim_spec: DimSpec | None = None
    wide_dim: str | None = None
    as_var: str | None = None
    accepts: list[ColumnKind] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list, max_length=64)
    choices: list[QuestionChoice] = Field(default_factory=list, max_length=32)
    default: JsonValue = None
    minimum: float | None = None
    maximum: float | None = None
    ui_level: UiLevel = "basic"

    @model_validator(mode="after")
    def _shape(self) -> GuidedQuestion:
        if self.kind in ("column", "columns"):
            if (self.dim is None) == (self.role is None):
                raise ValueError(f"{self.id}: a column question has exactly one of dim or role")
            if self.wide_dim is not None and (self.kind != "columns" or self.as_var is None):
                raise ValueError(f"{self.id}: wide columns need kind=columns and as_var")
        elif self.dim or self.role or self.wide_dim:
            raise ValueError(f"{self.id}: only column questions map columns")
        if self.kind == "choice" and not self.choices:
            raise ValueError(f"{self.id}: a choice question needs choices")
        return self


class TemplateLookup(ResponseModel):
    """A placeholder derived from an answer: ``map[answer]`` (e.g. frequency → season)."""

    source: str
    map: dict[str, JsonValue]


class GuidedTemplate(ResponseModel):
    """A guided task: questions, the spec they fill and what to check afterwards.

    ``ui_levels`` maps JSON pointers of the spec to ``x-ui-level`` hints (``hidden`` fields
    are kept but not shown; Expert edits round-trip). ``plugin_available`` is false while
    the template's built-in plugin is not shipped (``plugin_note`` says what is missing).
    """

    id: Slug
    version: str = Field("1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    title: str = Field(..., max_length=120)
    description_plain: str = Field(..., max_length=1000)
    family: Literal["mmm", "forecast", "tabular", "choice"]
    plugin: str = Field(..., description="UseRef of the built-in plugin (id@range)")
    plugin_available: bool = True
    plugin_note: str | None = Field(None, max_length=500)
    questions: list[GuidedQuestion] = Field(..., min_length=1, max_length=32)
    spec_template: dict[str, JsonValue]
    defaults: dict[str, JsonValue] = Field(default_factory=dict)
    lookups: dict[str, TemplateLookup] = Field(default_factory=dict)
    scorecard_checks: list[str] = Field(default_factory=list)
    scorecard_thresholds: dict[str, CheckThresholds] = Field(default_factory=dict)
    tasks: list[GuidedTask] = Field(default_factory=list)
    ui_levels: dict[str, UiLevel] = Field(default_factory=dict)

    model_config = dto_examples(
        {
            "id": "weekly_forecast",
            "version": "1.0.0",
            "title": "Weekly forecast",
            "description_plain": "Forecast one series a few weeks ahead.",
            "family": "forecast",
            "plugin": "ts.hsgp_trend@^1",
            "questions": [
                {
                    "id": "kpi",
                    "prompt_plain": "Which column do you want to forecast?",
                    "kind": "column",
                    "role": "target",
                    "accepts": ["number", "integer"],
                    "keywords": ["sales"],
                },
                {
                    "id": "date",
                    "prompt_plain": "Which column holds the date or week?",
                    "kind": "column",
                    "dim": "time",
                    "dim_spec": {"kind": "time", "policy": "extend"},
                    "accepts": ["date"],
                },
            ],
            "spec_template": {
                "apiVersion": "mapi/v1",
                "kind": "ModelSpec",
                "metadata": {"name": "{{name}}", "project": "{{project}}"},
                "data": {
                    "dataset": "{{dataset}}",
                    "bind": {"dims": {"t": "{{date}}"}, "vars": {"y": "{{kpi}}"}},
                },
                "model": {"use": "ts.hsgp_trend@^1", "settings": {"max_extension": 26}},
            },
            "scorecard_checks": ["convergence.rhat", "holdout.mape"],
            "tasks": ["forecast"],
            "ui_levels": {"/model/fit": "advanced"},
        }
    )

    @model_validator(mode="after")
    def _consistent(self) -> GuidedTemplate:
        ids = [q.id for q in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError("question ids must be unique")
        unknown = set(self.scorecard_checks) - set(SCORECARD_CHECKS)
        if unknown or set(self.scorecard_thresholds) - set(self.scorecard_checks):
            raise ValueError(f"unknown scorecard checks {sorted(unknown)}")
        for name, lk in self.lookups.items():
            if lk.source not in ids:
                raise ValueError(f"lookup {name!r} reads unknown question {lk.source!r}")
        missing = template_placeholders(self.spec_template) - (
            set(ids) | set(self.defaults) | set(self.lookups) | BUILDER_VALUES
        )
        if missing:
            raise ValueError(f"placeholders without a source: {sorted(missing)}")
        return self

    def question(self, qid: str) -> GuidedQuestion:
        for q in self.questions:
            if q.id == qid:
                return q
        raise KeyError(qid)


def template_placeholders(obj: Any) -> set[str]:
    """Every ``{{name}}`` in a template (keys and values)."""
    found: set[str] = set()
    if isinstance(obj, str):
        found.update(_PLACEHOLDER.findall(obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            found |= template_placeholders(k) | template_placeholders(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= template_placeholders(v)
    return found


def render_template(obj: Any, values: Mapping[str, Any]) -> Any:
    """Fill ``{{name}}`` placeholders: a string that is *only* a placeholder becomes the
    value itself (any JSON); placeholders inside longer strings are replaced by the value's
    text. Raises ``KeyError`` for a placeholder without a value."""
    if isinstance(obj, str):
        whole = _PLACEHOLDER.fullmatch(obj.strip())
        if whole:
            return copy.deepcopy(values[whole.group(1)])
        return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), obj)
    if isinstance(obj, dict):
        return {render_template(k, values): render_template(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render_template(v, values) for v in obj]
    return copy.deepcopy(obj)


# ========================================================================= built-in templates

_KPI_WORDS = ["revenue", "sales", "kpi", "conversions", "orders", "units", "signups",
              "turnover", "volume", "target"]  # fmt: skip
_DATE_WORDS = ["date", "week", "day", "period", "month", "time", "ds"]
_GEO_WORDS = ["geo", "market", "region", "country", "state", "dma", "city", "area"]
_SPEND_WORDS = ["spend", "cost", "budget", "investment", "media"]

_FREQ = GuidedQuestion(
    id="frequency",
    prompt_plain="How often is your data recorded?",
    kind="choice",
    choices=[
        QuestionChoice(value="weekly", label="Weekly"),
        QuestionChoice(value="daily", label="Daily"),
    ],
    default="weekly",
)
_HOLDOUT = GuidedQuestion(
    id="holdout_periods",
    prompt_plain="How many of the latest periods should we hold back to test the model?",
    help_plain="The model is fitted without them first, to measure its forecast error, then "
    "refitted on everything.",
    kind="number",
    default=8,
    minimum=1,
    maximum=104,
    ui_level="advanced",
)
_TIME_DIM = DimSpec(kind="time", policy="extend")
_FIT = {"mode": "evaluate_then_refit", "tune": 500, "draws": 500, "chains": 4, "seed": 20261015}

GEO_MMM = GuidedTemplate(
    id="geo_mmm",
    title="Geo marketing mix",
    description_plain="Find out how much each marketing channel adds to your results in each "
    "market, and what happens if you move budget between channels.",
    family="mmm",
    plugin="mmm.hierarchical_geo@^1",
    questions=[
        GuidedQuestion(
            id="kpi", prompt_plain="Which column is the result you want to explain?",
            help_plain="Usually revenue, sales or sign-ups.", kind="column", role=Role.TARGET,
            accepts=["number", "integer"], keywords=_KPI_WORDS,
        ),
        GuidedQuestion(
            id="date", prompt_plain="Which column holds the date or week?", kind="column",
            dim="time", dim_spec=_TIME_DIM, accepts=["date"], keywords=_DATE_WORDS,
        ),
        GuidedQuestion(
            id="geo", prompt_plain="Which column names the market or region?", kind="column",
            dim="geo", accepts=["string", "integer"], keywords=_GEO_WORDS,
            dim_spec=DimSpec(kind="categorical", policy="new_levels",
                             new_level_strategy="pooled"),
        ),
        GuidedQuestion(
            id="spend", prompt_plain="Which columns are media spend (one per channel)?",
            help_plain="Pick every column that holds money spent on a channel.",
            kind="columns", role=Role.SPEND, wide_dim="channel", as_var="spend",
            accepts=["number", "integer"], keywords=_SPEND_WORDS,
        ),
        GuidedQuestion(
            id="controls",
            prompt_plain="Which other columns might move the result (price, promotions)?",
            kind="columns", role=Role.CONTROL, required=False, accepts=["number", "integer"],
            keywords=["price", "promo", "promotion", "discount", "weather", "temperature",
                      "holiday", "competitor", "distribution"],
        ),
        _FREQ,
        _HOLDOUT,
    ],
    spec_template={
        "apiVersion": API_VERSION,
        "kind": "ModelSpec",
        "metadata": {"name": "{{name}}", "project": "{{project}}",
                     "labels": {"family": "mmm", "guided": "geo_mmm"}},
        "data": {
            "dataset": "{{dataset}}",
            "bind": {"dims": {"geo": "{{geo}}", "channel": "channel", "time": "{{date}}"},
                     "vars": {"y": "{{kpi}}", "spend": "spend"}},
            "split": {"kind": "time_holdout", "dim": "time", "last": "{{holdout_periods}}",
                      "after_step": "weekly"},
        },
        "pipeline": [
            {"id": "weekly", "use": "core.resample@^1",
             "with": {"dim": "time", "freq": "W-SUN", "how": "sum", "full_periods_only": True}},
            {"id": "scale_spend", "use": "core.mean_scale@^1",
             "with": {"vars": ["spend"], "over": ["time"], "unseen_levels": "pooled"}},
            {"id": "scale_y", "use": "core.mean_scale@^1",
             "with": {"vars": ["y"], "over": ["time"], "unseen_levels": "pooled"}},
            {"id": "lags", "use": "core.lag_tensor@^1",
             "with": {"vars": ["spend"], "along": "time", "within": ["geo"], "l_max": 6,
                      "pad": "first"}},
            {"id": "season", "use": "core.fourier@^1",
             "with": {"along": "time", "period": 52.18, "order": 3, "out": "fourier"}},
        ],
        "model": {
            "use": "mmm.hierarchical_geo@^1",
            "settings": {"pooling": "partial", "adstock": {"kind": "geometric", "l_max": 6},
                         "saturation": {"kind": "logistic"}},
            "fit": _FIT,
        },
        "outputs": {"derived": ["contributions", "response_curves", "marginal_cost"],
                    "plots": ["core.ppc_fan", "core.convergence", "mmm.decomposition",
                              "mmm.response_curves"],
                    "ui_draws": 300},
        "evaluate": {"metrics": ["mape", "coverage_90"]},
        "execution": {"resource_class": "M", "timeout": "45m"},
    },
    defaults={"holdout_periods": 8, "frequency": "weekly"},
    scorecard_checks=["convergence.rhat", "convergence.ess", "convergence.divergences",
                      "holdout.mape", "holdout.coverage_90", "plausibility.negative_roi",
                      "plausibility.roi_absurd", "plausibility.share_outlier",
                      "plausibility.baseline_share", "data.missing", "data.history",
                      "data.spend_variation"],
    tasks=["whatif", "forecast", "counterfactual", "business_priors", "lift_tests"],
    ui_levels={"/model/settings/pooling": "basic", "/model/settings/adstock": "advanced",
               "/model/settings/saturation": "advanced", "/model/fit": "advanced",
               "/pipeline": "hidden", "/execution": "advanced"},
)  # fmt: skip

WEEKLY_FORECAST = GuidedTemplate(
    id="weekly_forecast",
    title="Weekly forecast",
    description_plain="Forecast one series - sales, visits, demand - a few weeks or months "
    "ahead, with a range of likely values.",
    family="forecast",
    plugin="ts.hsgp_trend@^1",
    questions=[
        GuidedQuestion(
            id="kpi", prompt_plain="Which column do you want to forecast?", kind="column",
            role=Role.TARGET, accepts=["number", "integer"], keywords=_KPI_WORDS,
        ),
        GuidedQuestion(
            id="date", prompt_plain="Which column holds the date or week?", kind="column",
            dim="time", dim_spec=_TIME_DIM, accepts=["date"], keywords=_DATE_WORDS,
        ),
        GuidedQuestion(
            id="horizon", prompt_plain="How many periods ahead do you need to forecast?",
            kind="number", default=26, minimum=1, maximum=520,
        ),
        _FREQ,
        _HOLDOUT,
    ],
    spec_template={
        "apiVersion": API_VERSION,
        "kind": "ModelSpec",
        "metadata": {"name": "{{name}}", "project": "{{project}}",
                     "labels": {"family": "forecast", "guided": "weekly_forecast"}},
        "data": {
            "dataset": "{{dataset}}",
            "bind": {"dims": {"t": "{{date}}"}, "vars": {"y": "{{kpi}}"}},
            "split": {"kind": "time_holdout", "dim": "t", "last": "{{holdout_periods}}"},
        },
        "model": {
            "use": "ts.hsgp_trend@^1",
            "settings": {"max_extension": "{{horizon}}", "season_period": "{{season_period}}"},
            "fit": _FIT,
        },
        "outputs": {"plots": ["core.ppc_fan", "core.convergence"], "ui_draws": 300},
        "evaluate": {"metrics": ["mape", "coverage_90"]},
        "execution": {"resource_class": "S", "timeout": "30m"},
    },
    defaults={"holdout_periods": 8, "horizon": 26, "frequency": "weekly"},
    lookups={"season_period": TemplateLookup(source="frequency",
                                             map={"weekly": 52.18, "daily": 365.25})},
    scorecard_checks=["convergence.rhat", "convergence.ess", "convergence.divergences",
                      "holdout.mape", "holdout.coverage_90", "data.missing", "data.history"],
    scorecard_thresholds={"data.history": CheckThresholds(warn=104, fail=26)},
    tasks=["forecast"],
    ui_levels={"/model/settings/max_extension": "basic", "/model/fit": "advanced",
               "/execution": "advanced"},
)  # fmt: skip

STORE_DEMAND = GuidedTemplate(
    id="store_demand",
    title="Demand by store",
    description_plain="Predict how much each store will sell each week, with a low and a high "
    "estimate, from prices, promotions and the calendar.",
    family="tabular",
    plugin="tabular.regressor@^1",
    questions=[
        GuidedQuestion(
            id="kpi", prompt_plain="Which column is the quantity sold?", kind="column",
            role=Role.TARGET, accepts=["number", "integer"],
            keywords=["units", "quantity", "qty", "sales", "demand", "volume", "sold"],
        ),
        GuidedQuestion(
            id="date", prompt_plain="Which column holds the week?", kind="column", dim="time",
            dim_spec=_TIME_DIM, accepts=["date"], keywords=_DATE_WORDS,
        ),
        GuidedQuestion(
            id="store", prompt_plain="Which column names the store?", kind="column",
            dim="store", accepts=["string", "integer"],
            keywords=["store", "shop", "outlet", "branch", "location", "site"],
            dim_spec=DimSpec(kind="categorical", policy="new_levels",
                             new_level_strategy="pooled"),
        ),
        GuidedQuestion(
            id="drivers",
            prompt_plain="Which columns explain demand (price, promotions, stock)?",
            kind="columns", role=Role.COVARIATE, required=False, accepts=["number", "integer"],
            keywords=["price", "promo", "promotion", "discount", "stock", "display"],
        ),
        _HOLDOUT.model_copy(update={"default": 12}),
    ],
    spec_template={
        "apiVersion": API_VERSION,
        "kind": "ModelSpec",
        "metadata": {"name": "{{name}}", "project": "{{project}}",
                     "labels": {"family": "tabular", "guided": "store_demand"}},
        "data": {
            "dataset": "{{dataset}}",
            "bind": {"dims": {"store": "{{store}}", "time": "{{date}}"}, "vars": {"y": "{{kpi}}"}},
            "split": {"kind": "time_holdout", "dim": "time", "last": "{{holdout_periods}}"},
        },
        "pipeline": [
            {"id": "cal", "use": "core.calendar@^1",
             "with": {"along": "time", "features": ["week_of_year", "month"]}},
        ],
        "model": {
            "use": "tabular.regressor@^1",
            "settings": {"rows": ["store", "time"], "coord_features": {"store": "category"}},
            "fit": {"mode": "evaluate_then_refit", "seed": 7},
        },
        "outputs": {"plots": ["core.fit_vs_actual", "core.residuals_over_time",
                              "ml.feature_importance"]},
        "evaluate": {"metrics": ["mape", "coverage_80"]},
        "execution": {"resource_class": "S", "timeout": "20m"},
    },
    defaults={"holdout_periods": 12},
    scorecard_checks=["holdout.mape", "data.missing", "data.history"],
    tasks=["forecast"],
    ui_levels={"/model/settings": "advanced", "/pipeline": "hidden", "/execution": "advanced"},
)  # fmt: skip

CUSTOMER_CHOICE = GuidedTemplate(
    id="customer_choice",
    title="Customer choice",
    description_plain="Understand which product customers pick on each shopping trip, and how "
    "price and promotions shift the choice.",
    family="choice",
    plugin="choice.multinomial_logit@^1",
    plugin_available=False,
    plugin_note="Needs a built-in multinomial choice model (choice.multinomial_logit: "
    "alternative-specific utilities over price and promotion, household random effects, "
    "draws representation, predict and simulate modes). Planned; the template is published "
    "so the question flow and the data contract can be reviewed.",
    questions=[
        GuidedQuestion(
            id="choice", prompt_plain="Which column says which product was chosen?",
            kind="column", role=Role.TARGET, accepts=["string", "integer"],
            keywords=["choice", "chosen", "brand", "product", "purchase", "bought"],
        ),
        GuidedQuestion(
            id="customer", prompt_plain="Which column identifies the customer or household?",
            kind="column", dim="customer", accepts=["string", "integer"],
            keywords=["customer", "household", "hh", "panelist", "member", "user"],
            dim_spec=DimSpec(kind="categorical", policy="new_levels",
                             new_level_strategy="hyperprior"),
        ),
        GuidedQuestion(
            id="occasion", prompt_plain="Which column numbers or dates each shopping trip?",
            kind="column", dim="trip", accepts=["date", "integer"],
            keywords=["trip", "visit", "occasion", "date", "week", "basket"],
            dim_spec=DimSpec(kind="index", policy="extend"),
        ),
        GuidedQuestion(
            id="prices", prompt_plain="Which columns hold the price of each product?",
            kind="columns", role=Role.COVARIATE, wide_dim="product", as_var="price",
            accepts=["number"], keywords=["price", "cost"],
        ),
        GuidedQuestion(
            id="promos", prompt_plain="Which columns flag a promotion for each product?",
            kind="columns", role=Role.COVARIATE, wide_dim="product", as_var="promo",
            required=False, accepts=["number", "integer", "boolean"],
            keywords=["promo", "promotion", "display", "feature", "deal"],
        ),
    ],
    spec_template={
        "apiVersion": API_VERSION,
        "kind": "ModelSpec",
        "metadata": {"name": "{{name}}", "project": "{{project}}",
                     "labels": {"family": "choice", "guided": "customer_choice"}},
        "data": {
            "dataset": "{{dataset}}",
            "bind": {"dims": {"hh": "{{customer}}", "trip": "{{occasion}}",
                              "product": "product"},
                     "vars": {"y": "{{choice}}", "price": "price"}},
            "split": {"kind": "last_per_group", "dim": "trip", "within": ["hh"], "last": 1,
                      "min_len": 3},
        },
        "model": {
            "use": "choice.multinomial_logit@^1",
            "settings": {"random_effects": ["hh"]},
            "fit": _FIT,
        },
        "outputs": {"plots": ["core.convergence", "core.ppc"]},
        "evaluate": {"metrics": ["elpd_loo"]},
        "execution": {"resource_class": "M", "timeout": "45m"},
    },
    scorecard_checks=["convergence.rhat", "convergence.ess", "convergence.divergences",
                      "data.missing"],
    tasks=["counterfactual"],
    ui_levels={"/model/settings": "advanced", "/execution": "advanced"},
)  # fmt: skip

GUIDED_TEMPLATES: Final[dict[str, GuidedTemplate]] = {
    t.id: t for t in (GEO_MMM, WEEKLY_FORECAST, STORE_DEMAND, CUSTOMER_CHOICE)
}
"""The built-in guided templates, by id (``GET /v1/guided/templates``)."""


def get_template(template_id: str) -> GuidedTemplate:
    """A built-in template (``NOT_FOUND`` otherwise)."""
    try:
        return GUIDED_TEMPLATES[template_id]
    except KeyError:
        raise MapiError(
            f"no guided template {template_id[:64]!r}", code=ErrorCode.NOT_FOUND
        ) from None


# ============================================================================ role detection


class ColumnProfile(RequestModel):
    """What the browser (DuckDB-WASM/papaparse preview) or an ingest profile knows about a
    column. ``name`` and ``sample`` are user data: render them as text only."""

    name: str = Field(..., min_length=1, max_length=256)
    dtype: ColumnKind
    n_rows: int = Field(..., ge=0)
    n_missing: int = Field(0, ge=0)
    n_distinct: int | None = Field(None, ge=0)
    min: float | None = None
    max: float | None = None
    min_date: str | None = Field(None, max_length=40)
    max_date: str | None = Field(None, max_length=40)
    detected_freq: str | None = Field(None, max_length=16, examples=["W-SUN", "D", "MS"])
    sample: list[str | int | float | bool | None] = Field(default_factory=list, max_length=20)

    model_config = dto_examples(
        {"name": "revenue_eur", "dtype": "number", "n_rows": 1040, "n_distinct": 1012,
         "min": 1520.5, "max": 98312.0},
        {"name": "week", "dtype": "date", "n_rows": 1040, "n_distinct": 104,
         "min_date": "2024-01-07", "max_date": "2025-12-28", "detected_freq": "W-SUN"},
    )  # fmt: skip

    @model_validator(mode="after")
    def _counts(self) -> ColumnProfile:
        if self.n_missing > self.n_rows:
            raise ValueError("n_missing cannot exceed n_rows")
        for v in self.sample:
            if isinstance(v, str) and len(v) > 200:
                raise ValueError("sample strings are at most 200 characters")
        return self

    @property
    def non_negative(self) -> bool:
        return self.min is not None and self.min >= 0


class DetectRolesRequest(RequestModel):
    """``POST /v1/guided/detect-roles``: exactly one of ``dataset`` (a stored version:
    its schema and profile are used) or ``columns`` (profiled in the browser)."""

    template_id: Slug
    dataset: DatasetRef | None = None
    columns: list[ColumnProfile] | None = Field(None, min_length=1, max_length=2048)

    model_config = dto_examples(
        {
            "template_id": "geo_mmm",
            "columns": [
                *ColumnProfile.model_json_schema()["examples"],
                {"name": "market", "dtype": "string", "n_rows": 1040, "n_distinct": 10},
                {"name": "tv_spend", "dtype": "number", "n_rows": 1040, "min": 0, "max": 9e4},
            ],
        }
    )

    @model_validator(mode="after")
    def _one(self) -> DetectRolesRequest:
        if (self.dataset is None) == (self.columns is None):
            raise ValueError("give exactly one of 'dataset' or 'columns'")
        return self


class RoleCandidate(ResponseModel):
    column: str
    confidence: float = Field(..., ge=0, le=1)
    reason_plain: str = Field(..., max_length=300)


class QuestionSuggestion(ResponseModel):
    """Candidates for one question (best first) and the pre-filled answer."""

    question_id: str
    candidates: list[RoleCandidate] = Field(default_factory=list, max_length=20)
    answer: JsonValue = None


class RoleSuggestions(ResponseModel):
    template_id: str
    questions: list[QuestionSuggestion]
    unassigned: list[str] = Field(default_factory=list, description="Columns no answer uses")
    issues: list[Issue] = Field(default_factory=list)

    model_config = dto_examples(
        {
            "template_id": "geo_mmm",
            "questions": [
                {
                    "question_id": "kpi",
                    "candidates": [{"column": "revenue_eur", "confidence": 0.9,
                                    "reason_plain": "named like a revenue column; numbers"}],
                    "answer": "revenue_eur",
                },
                {
                    "question_id": "spend",
                    "candidates": [{"column": "tv_spend", "confidence": 0.8,
                                    "reason_plain": "named like spend; never negative"}],
                    "answer": ["tv_spend"],
                },
            ],
            "unassigned": [],
        }
    )  # fmt: skip


_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+|(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])")


def _tokens(name: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return [t for t in _TOKEN_SPLIT.split(spaced.lower()) if t]


def _score(q: GuidedQuestion, col: ColumnProfile) -> tuple[float, list[str]]:
    if q.accepts and col.dtype not in q.accepts:
        return 0.0, []
    score, why = 0.1, []
    tokens = _tokens(col.name)
    flat = "".join(tokens)
    exact = next((k for k in q.keywords if k in tokens), None)
    if exact is not None:
        score += 0.6
        why.append(f"named like a {exact} column")
    elif any(k in flat for k in q.keywords if len(k) >= 4):
        score += 0.4
        why.append(f"name mentions {next(k for k in q.keywords if len(k) >= 4 and k in flat)}")
    if q.dim_spec is not None and q.dim_spec.kind == "time" and col.dtype == "date":
        score += 0.3
        why.append("contains dates")
    elif q.dim is not None and col.dtype in ("string", "integer") and col.n_distinct:
        if 2 <= col.n_distinct <= 1000 and col.n_distinct * 2 <= max(col.n_rows, 1):
            score += 0.2
            why.append(f"{col.n_distinct} repeated labels")
    if q.role in (Role.SPEND, Role.TARGET, Role.MEDIA) and col.non_negative:
        score += 0.1
        why.append("never negative")
    if col.n_rows and col.n_missing / col.n_rows > 0.5:
        score -= 0.3
        why.append("mostly empty")
    return max(0.0, min(1.0, score)), why


_FREQ_ANSWER = {"W": "weekly", "D": "daily", "B": "daily"}


def suggest_roles(template: GuidedTemplate, columns: Sequence[ColumnProfile]) -> RoleSuggestions:
    """Rank columns for every column question and pre-fill answers (pure heuristics).

    Single-column questions take the best column at confidence ≥ 0.5 not used by an earlier
    question; multi-column questions take every remaining column at ≥ 0.5. ``frequency``
    is answered from the chosen date column's detected frequency.
    """
    used: set[str] = set()
    out: list[QuestionSuggestion] = []
    by_name = {c.name: c for c in columns}
    for q in template.questions:
        if q.kind not in ("column", "columns"):
            out.append(QuestionSuggestion(question_id=q.id, answer=q.default))
            continue
        ranked = []
        for col in columns:
            conf, why = _score(q, col)
            if conf > 0.1:
                ranked.append(
                    RoleCandidate(
                        column=col.name,
                        confidence=round(conf, 3),
                        reason_plain="; ".join(why) or "a possible match",
                    )
                )
        ranked.sort(key=lambda c: (-c.confidence, c.column))
        free = [c for c in ranked if c.column not in used and c.confidence >= 0.5]
        answer: JsonValue
        if q.kind == "column":
            answer = free[0].column if free else None
            if free:
                used.add(free[0].column)
        else:
            answer = [c.column for c in free]
            used.update(c.column for c in free)
        out.append(QuestionSuggestion(question_id=q.id, candidates=ranked[:20], answer=answer))
    for s in out:
        if s.question_id == "frequency":
            date_q = next(
                (x for x in out if x.question_id in ("date", "occasion") and x.answer), None
            )
            date_col = by_name.get(str(date_q.answer)) if date_q is not None else None
            freq = (date_col.detected_freq or "") if date_col is not None else ""
            guess = _FREQ_ANSWER.get(freq[:1].upper())
            if guess is not None:
                out[out.index(s)] = s.model_copy(update={"answer": guess})
    issues = [
        Issue(
            code=ErrorCode.GUIDED_ANSWERS_INVALID,
            severity=Severity.WARNING,
            pointer=f"/answers/{s.question_id}",
            message="no column looks like an answer to this question; please pick one",
        )
        for s, q in zip(out, template.questions, strict=True)
        if q.required and q.kind in ("column", "columns") and not s.answer
    ]
    return RoleSuggestions(
        template_id=template.id,
        questions=out,
        unassigned=[c.name for c in columns if c.name not in used],
        issues=issues,
    )


# ======================================================================================= drafts

_SAFE = re.compile(r"[^a-z0-9_]+")


def safe_name(column: str) -> str:
    """A dataset name for a column (``Revenue (EUR)`` → ``revenue_eur``)."""
    name = _SAFE.sub("_", column.strip().lower()).strip("_") or "column"
    if not name[0].isalpha():
        name = "c_" + name
    return name[:128]


_CHANNEL_NOISE = {"spend", "cost", "costs", "budget", "investment", "media", "eur", "usd",
                  "gbp", "amount", "total"}  # fmt: skip


def channel_label(column: str) -> str:
    """A channel label from a spend column name (``TV_Spend_EUR`` → ``tv``)."""
    tokens = [t for t in _tokens(column) if t not in _CHANNEL_NOISE]
    return "_".join(tokens) or safe_name(column)


class GuidedAnswers(RequestModel):
    """``POST /v1/guided/drafts``: answers to a template's questions.

    Column answers are *column names* of the upload (a list for ``columns`` questions).
    ``dataset`` names the dataset the upload will become (``dataset://<project>/<name>``);
    ``columns`` (optional) lets the draft use detected types and frequencies.
    """

    template_id: Slug
    name: Slug = Field(..., description="Name of the spec (and model) to create")
    dataset: Slug = Field(..., description="Name of the dataset the upload becomes")
    format: Literal["csv", "parquet"] = "csv"
    answers: dict[str, JsonValue] = Field(default_factory=dict, max_length=32)
    columns: list[ColumnProfile] | None = Field(None, max_length=2048)
    business_priors: BusinessPriors | None = None
    lift_tests: list[LiftTest] = Field(default_factory=list, max_length=500)

    model_config = dto_examples(
        {
            "template_id": "geo_mmm",
            "name": "eu-geo-mmm",
            "dataset": "eu_media_weekly",
            "format": "csv",
            "answers": {
                "kpi": "revenue_eur",
                "date": "week",
                "geo": "market",
                "spend": ["tv_spend", "search_spend", "social_spend"],
                "controls": ["avg_price"],
                "frequency": "weekly",
                "holdout_periods": 8,
            },
        }
    )


class GuidedDraft(ResponseModel):
    """The draft a template and its answers produce: how to ingest the upload, the spec,
    the spec as YAML ("View as YAML") and anything worth a second look."""

    template_id: str
    template_version: str
    dataset_ref: str
    mapping: IngestMapping
    spec: dict[str, JsonValue]
    yaml: str
    issues: list[Issue] = Field(default_factory=list)

    model_config = dto_examples(
        {
            "template_id": "geo_mmm",
            "template_version": "1.0.0",
            "dataset_ref": "dataset://growth/eu_media_weekly@latest",
            "mapping": IngestMapping.model_json_schema()["examples"][1],
            "spec": {"apiVersion": "mapi/v1", "kind": "ModelSpec"},
            "yaml": "apiVersion: mapi/v1\nkind: ModelSpec\n…",
            "issues": [],
        }
    )


def _bad(qid: str, message: str) -> MapiError:
    return MapiError(message, code=ErrorCode.GUIDED_ANSWERS_INVALID, pointer=f"/answers/{qid}")


def _columns_answer(q: GuidedQuestion, value: JsonValue) -> list[str]:
    if q.kind == "column":
        if not isinstance(value, str) or not value:
            raise _bad(q.id, "answer with one column name")
        return [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise _bad(q.id, "answer with a list of column names")
    if len(set(value)) != len(value):
        raise _bad(q.id, "a column is listed twice")
    return [str(v) for v in value]


def build_draft(
    template: GuidedTemplate,
    answers: GuidedAnswers,
    *,
    project: str,
) -> GuidedDraft:
    """Turn answers into an ingest mapping and a ``ModelSpec`` (pure; validated).

    Raises ``GUIDED_ANSWERS_INVALID`` for a missing required answer, a column used twice,
    a choice or number out of range, or a resulting spec that does not parse.
    """
    profiles = {c.name: c for c in answers.columns or []}
    if unknown := sorted(set(answers.answers) - {q.id for q in template.questions}):
        raise _bad(unknown[0], f"the template has no question {unknown[0]!r}")
    values: dict[str, Any] = dict(template.defaults)
    dims: dict[str, DimMapping] = {}
    vars_: dict[str, VarMapping] = {}
    wide: dict[str, WideVarMapping] = {}
    used: dict[str, str] = {}
    issues: list[Issue] = []
    for q in template.questions:
        value = answers.answers.get(q.id, q.default)
        if value is None or value == []:
            if q.required and q.id not in values:
                raise _bad(q.id, f"answer {q.prompt_plain!r}")
            if q.kind == "columns":
                values[q.id] = []
            continue
        if q.kind in ("column", "columns"):
            cols = _columns_answer(q, value)
            for c in cols:
                if c in used:
                    raise _bad(q.id, f"column {c!r} already answers {used[c]!r}")
                used[c] = q.id
                prof = profiles.get(c)
                if prof is not None and q.accepts and prof.dtype not in q.accepts:
                    issues.append(
                        Issue(
                            code=ErrorCode.GUIDED_ANSWERS_INVALID,
                            severity=Severity.WARNING,
                            pointer=f"/answers/{q.id}",
                            message=f"column {c!r} looks like {prof.dtype}, expected "
                            f"{' or '.join(q.accepts)}",
                        )
                    )
            if q.dim is not None:
                name = safe_name(cols[0])
                spec = q.dim_spec or DimSpec(kind="categorical")
                prof = profiles.get(cols[0])
                if spec.kind == "time" and prof is not None and prof.detected_freq:
                    spec = spec.model_copy(update={"freq": prof.detected_freq})
                dims[name] = DimMapping(column=cols[0], spec=spec)
                values[q.id] = name
            elif q.wide_dim is not None and q.as_var is not None:
                labels = [channel_label(c) for c in cols]
                if len(set(labels)) != len(labels):
                    labels = [safe_name(c) for c in cols]
                wide[q.as_var] = WideVarMapping(
                    dim=q.wide_dim,
                    columns=dict(zip(labels, cols, strict=True)),
                    role=q.role or Role.COVARIATE,
                )
                values[q.id] = q.as_var
            else:
                names = [safe_name(c) for c in cols]
                for c, n in zip(cols, names, strict=True):
                    vars_[n] = VarMapping(column=c, role=q.role or Role.COVARIATE)
                values[q.id] = names[0] if q.kind == "column" else names
        elif q.kind == "choice":
            if value not in {ch.value for ch in q.choices}:
                raise _bad(q.id, f"choose one of {[ch.value for ch in q.choices]}")
            values[q.id] = value
        elif q.kind == "number":
            if not isinstance(value, int | float) or isinstance(value, bool):
                raise _bad(q.id, "answer with a number")
            if (q.minimum is not None and value < q.minimum) or (
                q.maximum is not None and value > q.maximum
            ):
                raise _bad(q.id, f"answer between {q.minimum} and {q.maximum}")
            values[q.id] = value
        else:
            values[q.id] = str(value)
    for name, lk in template.lookups.items():
        src = values.get(lk.source)
        if str(src) not in lk.map:
            raise _bad(lk.source, f"no setting for {src!r}")
        values[name] = lk.map[str(src)]
    dataset_ref = f"dataset://{project}/{answers.dataset}@latest"
    values.update({"name": answers.name, "project": project, "dataset": dataset_ref})
    try:
        doc = render_template(template.spec_template, values)
    except KeyError as exc:
        raise _bad(str(exc.args[0]), f"answer {exc.args[0]!r}") from None
    if answers.business_priors is not None or answers.lift_tests:
        cal: dict[str, Any] = {}
        if answers.business_priors is not None:
            cal["business_priors"] = answers.business_priors.model_dump(mode="json")
        if answers.lift_tests:
            cal["lift_tests"] = [t.model_dump(mode="json") for t in answers.lift_tests]
        doc["calibration"] = cal
    mapping = IngestMapping(format=answers.format, dims=dims, vars=vars_, wide=wide)
    try:
        model_spec = ModelSpec.model_validate(doc)
    except ValueError as exc:
        raise MapiError(
            "the answers do not make a valid spec",
            code=ErrorCode.GUIDED_ANSWERS_INVALID,
            untrusted_text=str(exc)[:4000],
        ) from None
    if not template.plugin_available:
        issues.append(
            Issue(
                code=ErrorCode.REF_UNRESOLVED,
                severity=Severity.WARNING,
                pointer="/model/use",
                message=f"{template.plugin} is not available yet: {template.plugin_note}",
            )
        )
    return GuidedDraft(
        template_id=template.id,
        template_version=template.version,
        dataset_ref=dataset_ref,
        mapping=mapping,
        spec=model_spec.model_dump(mode="json", by_alias=True, exclude_defaults=True),
        yaml=dump_yaml(model_spec),
        issues=issues,
    )


# ============================================================================== BayesInsight


class BayesInsightImport(RequestModel):
    """``POST /v1/guided/import/bayesinsight``: a BayesInsight project as a draft (Q8: an
    ingest adapter plus the ``mmm.bayesinsight`` plugin, not a separate spec kind)."""

    name: Slug
    dataset: Slug = Field(..., description="Name of the dataset the MFF upload becomes")
    format: Literal["csv", "parquet", "mff"] = "mff"
    model_def: BayesInsightModelDef
    metadata: BayesInsightMetadata
    holdout_periods: int = Field(8, ge=1, le=104)

    model_config = dto_examples(
        {
            "name": "bi-import",
            "dataset": "bi_mff",
            "format": "mff",
            "model_def": {
                "variable_details": [
                    {"variable_name": "Sales_Total_Total_Total", "variable_type": "exog",
                     "likelihood": {"type": "Normal"}},
                    {"variable_name": "TV_Total_Total_Total", "variable_type": "media",
                     "adstock": "delayed", "media_transform": "hill"},
                    {"variable_name": "annual_seasonality", "variable_type": "season"},
                ]
            },
            "metadata": {"periodicity": "Weekly", "row_ids": ["Geography", "Product", "Period"],
                         "allowed_geos": ["US", "DE"], "allowed_products": ["Total"],
                         "allowed_outlets": ["Total"], "allowed_campaigns": ["Total"],
                         "allowed_creatives": ["Total"]},
        }
    )  # fmt: skip


def draft_from_bayesinsight(req: BayesInsightImport, *, project: str) -> GuidedDraft:
    """The draft of an imported BayesInsight model: an MFF ingest with the BayesInsight
    preset, and a spec for ``mmm.bayesinsight`` that carries ``model_def`` verbatim plus
    where each analytic variable lives in the dataset.

    ``IMPORT_UNSUPPORTED`` errors: no single ``exog`` (KPI) variable, or a variable name
    that does not match the metadata's row ids.
    """
    kpi = req.model_def.kpi
    if kpi is None:
        raise MapiError(
            "model_def.json needs exactly one 'exog' variable (the KPI)",
            code=ErrorCode.IMPORT_UNSUPPORTED,
            pointer="/model_def/variable_details",
        )
    mff = MffOptions(freq=req.metadata.freq)
    refs: dict[str, dict[str, Any]] = {}
    roles: dict[str, str] = {}
    issues: list[Issue] = []
    role_of = {"exog": Role.TARGET, "media": Role.SPEND, "control": Role.CONTROL}
    for i, v in enumerate(req.model_def.variable_details):
        if v.variable_type in ("season", "localtrend"):
            if v.variable_type == "localtrend":
                issues.append(
                    Issue(
                        code=ErrorCode.IMPORT_UNSUPPORTED,
                        severity=Severity.WARNING,
                        pointer=f"/model_def/variable_details/{i}",
                        message="local trends are kept in model_def; check that the plugin "
                        "version supports them",
                    )
                )
            continue
        try:
            ref = bayesinsight_var_ref(v.variable_name, req.metadata, mff)
        except ValueError as exc:
            raise MapiError(
                str(exc),
                code=ErrorCode.IMPORT_UNSUPPORTED,
                pointer=f"/model_def/variable_details/{i}/variable_name",
            ) from None
        refs[v.variable_name] = {"var": ref.variable, "sel": dict(ref.sel)}
        roles.setdefault(ref.variable, role_of[v.variable_type].value)
    mff = mff.model_copy(update={"roles": roles})
    dataset_ref = f"dataset://{project}/{req.dataset}@latest"
    doc: dict[str, Any] = {
        "apiVersion": API_VERSION,
        "kind": "ModelSpec",
        "metadata": {"name": req.name, "project": project,
                     "labels": {"family": "mmm", "imported": "bayesinsight"}},
        "data": {
            "dataset": dataset_ref,
            "split": {"kind": "time_holdout", "dim": mff.time_dim, "last": req.holdout_periods},
        },
        "model": {
            "use": BAYESINSIGHT_PLUGIN,
            "settings": {
                "model_def": req.model_def.model_dump(mode="json", by_alias=True,
                                                      exclude={"vof", "artifact", "fitted"}),
                "variables": refs,
                "kpi": refs[kpi.variable_name]["var"],
            },
            "fit": {"mode": "evaluate_then_refit"},
        },
        "evaluate": {"metrics": ["mape", "coverage_90"]},
        "execution": {"resource_class": "M", "timeout": "45m"},
    }  # fmt: skip
    spec = ModelSpec.model_validate(doc)
    mapping = IngestMapping(
        format=req.format, preset=IngestPreset.BAYESINSIGHT, mff=mff, bayesinsight=req.metadata
    )
    return GuidedDraft(
        template_id="bayesinsight_import",
        template_version="1.0.0",
        dataset_ref=dataset_ref,
        mapping=mapping,
        spec=spec.model_dump(mode="json", by_alias=True, exclude_defaults=True),
        yaml=dump_yaml(spec),
        issues=issues,
    )
