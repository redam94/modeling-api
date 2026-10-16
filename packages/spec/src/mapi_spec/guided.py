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
``geo_mmm``        ``mmm.bayesian``            marketing mix by market (what-if, priors;
                                               mmm-framework's BayesianMMM)
``weekly_forecast````ts.hsgp_trend``           one weekly series, forecast ahead
``store_demand``   ``tabular.regressor``       demand per store and week (quantiles)
``customer_choice````choice.multinomial_logit``which product customers choose on each
                                               trip (a conditional logit with household
                                               effects, in ``plugins/tabular``)
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
from mapi_spec.coords import DataSummary
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity, parse_pointer
from mapi_spec.ingest import (
    DimMapping,
    IngestMapping,
    IngestPreset,
    VarMapping,
    WideVarMapping,
)
from mapi_spec.loader import dump_yaml
from mapi_spec.refs import DatasetRef
from mapi_spec.schema import DataSchema, DimSpec, Role, VarSpec
from mapi_spec.scorecard import SCORECARD_CHECKS, CheckThresholds
from mapi_spec.spec import API_VERSION, ModelSpec
from mapi_spec.views import ProfileView

__all__ = [
    "BUILDER_VALUES",
    "GUIDED_LABEL",
    "GUIDED_TEMPLATES",
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
    "draft_schema",
    "freq_plain",
    "get_template",
    "guided_lock_issues",
    "profiles_from_dataset",
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
    missing: Literal["forbid", "allow", "mask"] | None = Field(
        None,
        description="M3: missing-value policy of the variables a column answer becomes "
        "(default forbid; 'allow' for ragged panels such as shopping trips per household)",
        exclude_if=lambda v: v is None,
    )

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
_GEO_WORDS = ["geo", "geography", "market", "region", "country", "state", "dma", "city", "area",
              "territory", "province", "zone"]  # fmt: skip
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
_MMM_FIT = {"mode": "evaluate_then_refit", "tune": 1000, "draws": 1000, "chains": 4,
            "target_accept": 0.95, "seed": 20261015}  # fmt: skip
"""The geo MMM's fit: hierarchical adstock-saturation models sample slowly, and on two years
of weekly data a shorter fit leaves the scorecard's convergence checks failing (a Guided user
cannot change the fit settings), so it samples longer and more carefully."""

MMM_MODEL_ID: Final = "mmm.bayesian"
"""The MMM built-in (v0.5): mmm-framework's ``BayesianMMM`` (national, or hierarchical over
geos and products). mmm-framework is the only MMM engine."""
MMM_TEMPLATE_SETTINGS: Final[dict[str, JsonValue]] = {
    "adstock": {"kind": "geometric", "l_max": 8},
    "saturation": {"kind": "hill"},
    "trend": {"kind": "linear"},
    "seasonality": {"yearly_order": 3},
    "hierarchy": {"pool_over": ["geo"]},
}
"""The ``geo_mmm`` template's settings for :data:`MMM_MODEL_ID` (the plugin's settings schema
must accept them): geometric adstock, Hill saturation, a linear trend, three yearly Fourier
pairs and coefficients partially pooled over geos. The model owns scaling and carry-over, so
the template's pipeline only aggregates to weeks."""

GEO_MMM = GuidedTemplate(
    id="geo_mmm",
    version="2.0.0",
    title="Geo marketing mix",
    description_plain="Find out how much each marketing channel adds to your results in each "
    "market, and what happens if you move budget between channels.",
    family="mmm",
    plugin=f"{MMM_MODEL_ID}@^1",
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
        ],
        "model": {
            "use": f"{MMM_MODEL_ID}@^1",
            "settings": MMM_TEMPLATE_SETTINGS,
            "fit": _MMM_FIT,
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
    ui_levels={"/model/settings/hierarchy": "basic", "/model/settings/adstock": "advanced",
               "/model/settings/saturation": "advanced", "/model/settings/trend": "advanced",
               "/model/settings/seasonality": "advanced", "/model/fit": "advanced",
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
    scorecard_thresholds={"data.history": CheckThresholds(warn=2, fail=0.5)},  # years
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
    version="1.2.0",
    title="Customer choice",
    description_plain="Understand which product customers pick on each shopping trip, and how "
    "price and promotions shift the choice.",
    family="choice",
    plugin="choice.multinomial_logit@^1",
    questions=[
        GuidedQuestion(
            id="choice", prompt_plain="Which column says which product was chosen?",
            help_plain="Each value must name a product the same way as the price columns do "
            "(a column price_coke goes with the choice coke).",
            kind="column", role=Role.TARGET, accepts=["string"], missing="allow",
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
            id="occasion", prompt_plain="Which column numbers each customer's shopping trips?",
            help_plain="Number every customer's trips 1, 2, 3, ... in the order they happened.",
            kind="column", dim="trip", accepts=["integer"],
            keywords=["trip", "visit", "occasion", "basket", "purchase", "number"],
            dim_spec=DimSpec(kind="index", policy="extend"),
        ),
        GuidedQuestion(
            id="prices", prompt_plain="Which columns hold the price of each product?",
            kind="columns", role=Role.COVARIATE, wide_dim="product", as_var="price",
            accepts=["number", "integer"], keywords=["price", "cost"], missing="allow",
        ),
        GuidedQuestion(
            id="promos", prompt_plain="Which columns flag a promotion for each product?",
            kind="columns", role=Role.COVARIATE, wide_dim="product", as_var="promo",
            required=False, accepts=["number", "integer", "boolean"], missing="allow",
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
                     "vars": {"y": "{{choice}}"}},
            "split": {"kind": "last_per_group", "dim": "trip", "within": ["hh"], "last": 1,
                      "min_len": 3},
        },
        "model": {
            "use": "choice.multinomial_logit@^1",
            "settings": {"random_effects": ["hh"]},
            "fit": {"mode": "evaluate_then_refit", "draws": 100, "seed": 20261015},
        },
        "outputs": {"plots": ["core.data_overview", "core.posterior_forest"]},
        "evaluate": {"metrics": ["accuracy", "accuracy_naive", "accuracy_lift", "log_score",
                                 "brier"]},
        "execution": {"resource_class": "S", "timeout": "20m"},
    },
    scorecard_checks=["holdout.hit_rate"],
    tasks=["counterfactual"],
    ui_levels={"/model/settings": "advanced", "/model/fit": "advanced",
               "/execution": "advanced"},
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
    # M3 detection hints, left out of serialized profiles unless known
    monotonic: bool | None = Field(
        None,
        description="Values never decrease in file order (dates of a single series, trip "
        "numbers); repeated panels of dates are not monotonic",
        exclude_if=lambda v: v is None,
    )
    origin: Literal["column", "mff_variable", "dim", "variable"] | None = Field(
        None,
        description="Where the profile comes from: a table column, a VariableName of an MFF "
        "table, or a dim/variable of a stored dataset",
        exclude_if=lambda v: v is None,
    )

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
    # a stored dataset's dims answer dim questions only, its variables role questions only
    if (q.dim is not None and col.origin == "variable") or (
        q.role is not None and col.origin == "dim"
    ):
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
        if col.detected_freq:
            score += 0.05
            why.append(f"evenly spaced: {freq_plain(col.detected_freq)}")
    elif q.dim is not None and col.dtype in ("string", "integer") and col.n_distinct:
        if 2 <= col.n_distinct <= 1000 and col.n_distinct * 2 <= max(col.n_rows, 1):
            score += 0.2
            why.append(f"{col.n_distinct} repeated labels")
    ordered = q.dim_spec is not None and q.dim_spec.kind in ("time", "index")
    if q.dim is not None and ordered and col.monotonic:
        score += 0.05
        why.append("values in order")
    if q.role in (Role.SPEND, Role.TARGET, Role.MEDIA) and col.dtype in ("number", "integer"):
        if col.non_negative:
            score += 0.1
            why.append("never negative")
        elif col.min is not None and col.min < 0 and q.role is not Role.TARGET:
            score -= 0.2
            why.append("has negative values")
    if q.role is Role.TARGET and col.dtype == "string" and 2 <= (col.n_distinct or 0) <= 50:
        score += 0.2
        why.append(f"{col.n_distinct} distinct values")
    if q.role is not None and col.n_distinct == 1 and col.n_rows > 1:
        score -= 0.3
        why.append("always the same value")
    if col.n_rows and col.n_missing / col.n_rows > 0.5:
        score -= 0.3
        why.append("mostly empty")
    return max(0.0, min(1.0, score)), why


_FREQ_ANSWER = {"W": "weekly", "D": "daily", "B": "daily"}


_WEEKDAYS: Final = {"MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "THU": "Thursday",
                    "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday"}  # fmt: skip
_FREQ_WORDS: Final = {"D": "daily", "B": "every working day", "h": "hourly", "H": "hourly",
                      "MS": "monthly (on the 1st)", "M": "monthly (month ends)",
                      "ME": "monthly (month ends)", "QS": "quarterly", "Q": "quarterly",
                      "QE": "quarterly", "YS": "yearly", "Y": "yearly", "YE": "yearly",
                      "A": "yearly", "AS": "yearly"}  # fmt: skip


def freq_plain(freq: str) -> str:
    """A detected date frequency in words (``W-SUN`` -> ``weekly, on Sundays``), for the
    reasons role detection gives; unknown aliases are returned as they are."""
    if freq.startswith("W-") and freq[2:] in _WEEKDAYS:
        return f"weekly, on {_WEEKDAYS[freq[2:]]}s"
    if freq == "W":
        return "weekly"
    return _FREQ_WORDS.get(freq.split("-")[0], freq)


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


_KIND_DTYPE: Final[dict[str, ColumnKind]] = {
    "time": "date", "categorical": "string", "integer": "integer", "index": "integer",
    "float": "number",
}  # fmt: skip
_VAR_DTYPE: Final[dict[str, ColumnKind]] = {
    "float64": "number", "float32": "number", "int64": "integer", "int32": "integer",
    "bool": "boolean", "str": "string", "datetime64[ns]": "date",
}  # fmt: skip


def profiles_from_dataset(
    schema: DataSchema,
    summary: DataSummary | None = None,
    profile: ProfileView | None = None,
) -> list[ColumnProfile]:
    """Column profiles of a stored dataset version without ``columns.json`` (ingested
    before v0.4, or a zarr/netCDF upload): one per dim (``origin: dim``) and per variable
    (``origin: variable``), from its schema, summary and (optional) ``profile.json``.

    Pure JSON: the control plane uses it without reading the data.
    """
    coords = summary.coords if summary is not None else {}
    cells = 1
    for name in schema.dims:
        c = coords.get(name)
        cells *= c.size if c is not None else 1
    out: list[ColumnProfile] = []
    for name, dim in schema.dims.items():
        c = coords.get(name)
        dtype = _KIND_DTYPE.get(dim.kind, "string")
        out.append(
            ColumnProfile(
                name=name,
                dtype=dtype,
                n_rows=cells,
                n_distinct=c.size if c is not None else None,
                min_date=str(c.start)[:40]
                if c is not None and dtype == "date" and c.start
                else None,
                max_date=str(c.end)[:40] if c is not None and dtype == "date" and c.end else None,
                detected_freq=(dim.freq or (c.freq if c is not None else None) or None),
                monotonic=dim.kind in ("time", "integer", "index") or None,
                origin="dim",
                sample=[v for v in (c.values or [])[:5] if isinstance(v, str | int | float)]
                if c is not None
                else [],
            )
        )
    stats = profile.vars if profile is not None else {}
    for name, var in schema.vars.items():
        st = stats.get(name)
        out.append(
            ColumnProfile(
                name=name,
                dtype=_VAR_DTYPE.get(var.dtype, "string"),
                n_rows=st.size if st is not None and st.size else cells,
                n_missing=min(st.missing, st.size) if st is not None and st.size else 0,
                min=st.min if st is not None else None,
                max=st.max if st is not None else None,
                origin="variable",
            )
        )
    return out


_DRAFT_DTYPE: Final[dict[str, str]] = {
    "number": "float64", "integer": "float64", "boolean": "bool", "string": "str",
    "date": "datetime64[ns]",
}  # fmt: skip


def draft_schema(
    mapping: IngestMapping, columns: Sequence[ColumnProfile] | None = None
) -> DataSchema:
    """The ``DataSchema`` an ingest of ``mapping`` will produce (as far as a mapping tells:
    dtypes come from ``columns`` when given, numbers otherwise). Used to validate a draft
    statically before any data exists. Raises ``ValueError`` for a mapping without dims
    (a plain preset decides them from the data)."""
    if not mapping.dims:
        raise ValueError("the mapping maps no dims; the preset decides them from the data")
    by_name = {c.name: c for c in columns or []}
    dims: dict[str, DimSpec] = {name: dm.spec for name, dm in mapping.dims.items()}
    table = tuple(mapping.dims)
    vars_: dict[str, VarSpec] = {}
    for name, vm in mapping.vars.items():
        prof = by_name.get(vm.column)
        dtype = _DRAFT_DTYPE[prof.dtype] if prof is not None else "float64"
        vars_[name] = VarSpec(
            dims=table, dtype=dtype, role=vm.role, units=vm.units, missing=vm.missing,
            description=vm.description,
        )  # fmt: skip
    for name, w in mapping.wide.items():
        dims.setdefault(w.dim, w.dim_spec)
        kinds = {by_name[c].dtype for c in w.columns.values() if c in by_name}
        dtype = "bool" if kinds == {"boolean"} else "float64"
        vars_[name] = VarSpec(
            dims=(*table, w.dim), dtype=dtype, role=w.role, units=w.units, missing=w.missing,
            description=w.description,
        )  # fmt: skip
    return DataSchema(layout=mapping.layout, dims=dims, vars=vars_)


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


def channel_label(column: str, noise: Sequence[str] = ()) -> str:
    """A label from a wide column name (``TV_Spend_EUR`` → ``tv``): the name's words minus
    money and unit words and minus ``noise`` (the question's keywords, so ``price_coke``
    and ``promo_coke`` both become ``coke``)."""
    drop = _CHANNEL_NOISE | {t for k in noise for t in _tokens(k)}
    tokens = [t for t in _tokens(column) if t not in drop]
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
    format: Literal["csv", "parquet", "mff"] = Field(
        "csv",
        description="The upload's format; for 'mff' (M3) column answers name VariableName "
        "values, Period and the MFF key columns",
    )
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


def _wide_label_issues(wide: Mapping[str, WideVarMapping]) -> list[Issue]:
    """Wide variables sharing a dim must use the same labels (``price`` and ``promo`` along
    ``product``): the ingest refuses them otherwise, so say so in the draft already."""
    by_dim: dict[str, tuple[str, set[str]]] = {}
    out: list[Issue] = []
    for var, w in wide.items():
        labels = set(w.columns)
        if w.dim in by_dim and by_dim[w.dim][1] != labels:
            other, theirs = by_dim[w.dim]
            out.append(
                Issue(
                    code=ErrorCode.GUIDED_ANSWERS_INVALID,
                    severity=Severity.WARNING,
                    pointer=f"/mapping/wide/{var}",
                    message=f"{var!r} and {other!r} label {w.dim!r} differently "
                    f"({sorted(labels ^ theirs)[:5]}); pick one column per label for both",
                )
            )
        by_dim.setdefault(w.dim, (var, labels))
    return out


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
    taken: dict[str, tuple[str, bool]] = {}  # dataset name -> (question, shared wide dim)

    def claim(name: str, qid: str, column: str, *, shared: bool = False) -> str:
        """Reserve a dataset name; two answers may not become the same name (a shared
        wide dim such as ``product`` is the exception)."""
        other = taken.get(name)
        if other is not None and not (shared and other[1]):
            raise _bad(
                qid,
                f"column {column[:64]!r} would be named {name!r}, which the answer to "
                f"{other[0]!r} already uses; rename one of the columns",
            )
        taken[name] = (qid, shared)
        return name

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
                name = claim(safe_name(cols[0]), q.id, cols[0])
                spec = q.dim_spec or DimSpec(kind="categorical")
                prof = profiles.get(cols[0])
                if spec.kind == "time" and prof is not None and prof.detected_freq:
                    spec = spec.model_copy(update={"freq": prof.detected_freq})
                dims[name] = DimMapping(column=cols[0], spec=spec)
                values[q.id] = name
            elif q.wide_dim is not None and q.as_var is not None:
                labels = [channel_label(c, q.keywords) for c in cols]
                if len(set(labels)) != len(labels):
                    labels = [safe_name(c) for c in cols]
                claim(q.as_var, q.id, cols[0])
                claim(q.wide_dim, q.id, cols[0], shared=True)
                wide[q.as_var] = WideVarMapping(
                    dim=q.wide_dim,
                    columns=dict(zip(labels, cols, strict=True)),
                    role=q.role or Role.COVARIATE,
                    missing=q.missing or "forbid",
                )
                values[q.id] = q.as_var
            else:
                names = [claim(safe_name(c), q.id, c) for c in cols]
                for c, n in zip(cols, names, strict=True):
                    vars_[n] = VarMapping(
                        column=c, role=q.role or Role.COVARIATE, missing=q.missing or "forbid"
                    )
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
    issues.extend(_wide_label_issues(wide))
    if answers.format == "mff":
        mapping = IngestMapping(
            format="mff", preset=IngestPreset.MFF, dims=dims, vars=vars_, wide=wide
        )
    else:
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


# ============================================================================ persona lock

GUIDED_LABEL: Final = "guided"
"""``metadata.labels`` key naming the template a Guided spec was drafted from."""

_ABSENT: Final = object()


def _at(doc: Any, pointer: str) -> Any:
    node = doc
    for part in parse_pointer(pointer):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _ABSENT
    return node


def _normal(spec: ModelSpec) -> dict[str, Any]:
    out: dict[str, Any] = spec.model_dump(mode="json", by_alias=True)
    return out


def _reference(template: GuidedTemplate, name: str, project: str) -> ModelSpec:
    """The spec a template drafts from placeholder answers (only its fixed parts matter)."""
    answers: dict[str, JsonValue] = {}
    for q in template.questions:
        if q.kind == "column":
            answers[q.id] = f"__{q.id}"
        elif q.kind == "columns":
            answers[q.id] = [f"__{q.id}"] if q.required else []
        elif q.default is not None:
            answers[q.id] = q.default
        elif q.kind == "choice":
            answers[q.id] = q.choices[0].value
        elif q.kind == "number":
            answers[q.id] = q.minimum if q.minimum is not None else 1
        else:
            answers[q.id] = "x"
    req = GuidedAnswers(template_id=template.id, name=name, dataset="x", answers=answers)
    return ModelSpec.model_validate(build_draft(template, req, project=project).spec)


def _lock_issue(pointer: str, message: str) -> Issue:
    return Issue(
        code=ErrorCode.FORBIDDEN,
        pointer=pointer,
        message=message,
        suggested_action="Ask a project admin to make this change, or to unlock the project "
        "from Guided mode.",
    )


def guided_lock_issues(
    document: Mapping[str, Any], parent: Mapping[str, Any] | None = None
) -> list[Issue]:
    """Why a member below project admin may not store ``document`` in a Guided-locked
    project (empty: allowed). ``parent`` is the revision it replaces (``None``: a new spec).

    The policy (``Project.guided_locked``):

    * only Guided specs are stored: a valid ``ModelSpec`` whose ``metadata.labels.guided``
      names a built-in template and whose ``model.use`` is that template's plugin;
    * fields the template marks ``advanced`` or ``hidden`` (``GuidedTemplate.ui_levels``)
      keep their values: a revision may not change them from its parent, and a new spec
      must have what the template drafts wherever the template fixes the value (pointers
      whose template value has no ``{{placeholder}}``);
    * everything else - answers, ``basic`` settings, business priors and lift tests in
      ``calibration`` - may change. ``POST /v1/specs:validate`` and runs are unaffected.
    """
    try:
        spec = ModelSpec.model_validate(dict(document))
    except ValueError:
        return [_lock_issue("", "this project is locked to Guided mode: only valid Guided "
                                "specs can be stored")]  # fmt: skip
    label = spec.metadata.labels.get(GUIDED_LABEL)
    template = GUIDED_TEMPLATES.get(label or "")
    if template is None:
        return [
            _lock_issue(
                f"/metadata/labels/{GUIDED_LABEL}",
                "this project is locked to Guided mode: specs must come from a Guided "
                "template (metadata.labels.guided)",
            )
        ]
    issues: list[Issue] = []
    if spec.model.use.id != template.plugin.split("@", 1)[0]:
        issues.append(_lock_issue("/model/use", f"a {template.id} spec uses {template.plugin}"))
    doc = _normal(spec)
    base: dict[str, Any] | None = None
    if parent is not None:
        try:
            base = _normal(ModelSpec.model_validate(dict(parent)))
        except ValueError:
            base = None
    if base is None:
        base = _normal(_reference(template, spec.metadata.name, spec.metadata.project))
    for pointer, level in sorted(template.ui_levels.items()):
        if level == "basic":
            continue
        if parent is None and template_placeholders(_at(template.spec_template, pointer)):
            continue
        if _at(doc, pointer) != _at(base, pointer):
            issues.append(
                _lock_issue(
                    pointer,
                    f"{pointer} is an {level} setting of the {template.title!r} template; "
                    "in a Guided-locked project only project admins change it",
                )
            )
    return issues
