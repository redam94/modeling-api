"""The workflow node catalog: node kinds, typed ports and every node implementation (M3.5).

A workflow node names an implementation by id (``use: test.adf``). Each
:class:`NodeImplementation` declares its kind, typed input and output ports, a JSON Schema
for its parameters (``with``), the result fields branch conditions may read
(``nodes.<id>.<field>``), where it runs, and an :class:`~mapi_spec.about.About`.

=============  ===============================================================================
kind           implementations
=============  ===============================================================================
test           ``test.adf``, ``kpss``, ``pp``, ``zivot_andrews``, ``johansen``,
               ``engle_granger``, ``ljung_box``, ``jarque_bera``, ``breusch_pagan``, ``white``,
               ``arch_lm``, ``granger``, ``acf_pacf``, ``seasonal_strength``, ``vif``,
               ``cusum``, ``chow``
transform      ``transform.difference``, ``seasonal_difference``, ``log``, ``box_cox``,
               ``steps`` (catalog preprocessing steps), ``select``
split          ``split.holdout``, ``rolling_origin``, ``expanding_window``, ``blocked_kfold``,
               ``purged_kfold``, ``geo_holdout``, ``nested`` (:mod:`mapi_spec.splitplan`)
search         ``search.arma_order``, ``sarima_order``, ``var_lag_order``, ``coint_rank``,
               ``grid``
model          ``model.fit`` (any catalog model id), ``model.backtest``, ``model.forecast``
evaluate       ``evaluate.metrics``, ``evaluate.baseline``, ``evaluate.residuals``
compare        ``compare.models``, ``compare.diebold_mariano``
select         ``select.best``, ``select.threshold``
governance     ``governance.policy``, ``governance.check``
report         ``report.summary``, ``report.evidence_pack``
branch         ``branch.if`` (a condition in :mod:`mapi_spec.expr`)
subworkflow    ``subworkflow.run`` (another saved workflow)
=============  ===============================================================================

``runs_in`` says who executes a node: ``orchestrator`` nodes (branches, selections) work on
JSON results in the control plane with the safe expression language; ``sandbox`` nodes run
in a ``workflow_node`` run; ``child_run`` nodes start ordinary runs (fits, governance
refits) as children of the workflow run. User data is only ever decoded in the sandbox.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from mapi_spec._dto import ResponseModel, dto_examples
from mapi_spec.about import About, AboutReference, Difficulty, TypicalRuntime
from mapi_spec.capabilities import check_json_schema
from mapi_spec.splitplan import (
    BlockedKFoldPlan,
    ExpandingWindowPlan,
    GeoHoldoutPlan,
    HoldoutPlan,
    NestedPlan,
    PurgedKFoldPlan,
    RollingOriginPlan,
)

__all__ = [
    "BRANCH_ARMS",
    "NODE_IMPLEMENTATIONS",
    "NODE_KIND_ABOUTS",
    "SUBWORKFLOW_NODE",
    "NodeImplementation",
    "NodeKind",
    "PortSpec",
    "PortType",
    "ResultField",
    "RunsIn",
    "get_node",
]


class PortType(StrEnum):
    """What flows along a workflow edge."""

    DATASET = "dataset"
    """A dataset (a ``DatasetVersion`` or a derived dataset written by a node)."""
    TEST_RESULT = "test_result"
    """A :class:`~mapi_spec.stattests.StatTestResult`."""
    SPLIT_PLAN = "split_plan"
    """A :data:`~mapi_spec.splitplan.SplitPlan` (and, once resolved, its folds)."""
    MODEL_VERSION = "model_version"
    """A fitted model version (``model://…``)."""
    METRICS = "metrics"
    """Named metric values (overall, per fold and per horizon)."""
    TABLE = "table"
    """A small table (search grids, comparisons, check results)."""
    PLOT = "plot"
    """A ``PlotSpec``."""
    REPORT = "report"
    """A plain-language report (sections with text, tables and plots)."""
    EVIDENCE = "evidence"
    """Governance check results (:class:`~mapi_spec.governance.CheckResult` list)."""


class NodeKind(StrEnum):
    TEST = "test"
    TRANSFORM = "transform"
    BRANCH = "branch"
    SPLIT = "split"
    SEARCH = "search"
    MODEL = "model"
    EVALUATE = "evaluate"
    COMPARE = "compare"
    SELECT = "select"
    GOVERNANCE = "governance"
    REPORT = "report"
    SUBWORKFLOW = "subworkflow"


RunsIn = Literal["orchestrator", "sandbox", "child_run"]
BRANCH_ARMS: Final = ("then", "else")
"""The two control outputs of a ``branch`` node (``when: <branch id>.then``)."""
SUBWORKFLOW_NODE: Final = "subworkflow.run"
SINK_KINDS: Final = frozenset(
    {NodeKind.REPORT, NodeKind.GOVERNANCE, NodeKind.SELECT, NodeKind.MODEL, NodeKind.SUBWORKFLOW}
)
"""Kinds whose outputs may legitimately go unused (the workflow's results)."""


class PortSpec(ResponseModel):
    """An input port: its type, whether it must be bound and whether it takes several."""

    type: PortType
    required: bool = True
    multiple: bool = Field(False, description="Accepts a list of bindings (e.g. models)")
    description: str = Field("", max_length=300)


ResultType = Literal["number", "integer", "boolean", "string", "object", "array"]


class ResultField(ResponseModel):
    """A field of a node's JSON result that conditions may read (``nodes.<id>.<field>``)."""

    type: ResultType
    description: str = Field(..., max_length=300)


class NodeImplementation(ResponseModel):
    """One node implementation of the catalog (``GET /v1/catalog/workflow-nodes``)."""

    id: str = Field(..., pattern=r"^[a-z]+\.[a-z0-9_]+$")
    version: str = Field("1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    kind: NodeKind
    inputs: dict[str, PortSpec] = Field(default_factory=dict)
    outputs: dict[str, PortType] = Field(default_factory=dict)
    params_schema: dict[str, JsonValue] = Field(default_factory=dict)
    result_fields: dict[str, ResultField] = Field(default_factory=dict)
    runs_in: RunsIn = "sandbox"
    cacheable: bool = Field(True, description="Same inputs and params give the same result")
    about: About

    model_config = dto_examples(
        {
            "id": "test.adf",
            "version": "1.0.0",
            "kind": "test",
            "inputs": {"data": {"type": "dataset", "description": "The data to work on"}},
            "outputs": {"result": "test_result"},
            "params_schema": {"type": "object", "properties": {"alpha": {"type": "number"}}},
            "result_fields": {"p_value": {"type": "number", "description": "The p-value"}},
            "about": About.model_json_schema()["examples"][0],
        }
    )

    @field_validator("params_schema")
    @classmethod
    def _schema(cls, v: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return check_json_schema(v)

    @model_validator(mode="after")
    def _kind(self) -> NodeImplementation:
        if self.id.split(".", 1)[0] != self.kind.value and self.kind is not NodeKind.BRANCH:
            raise ValueError(f"{self.id}: the id's prefix must be its kind")
        return self

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


# ================================================================================ helpers


def _a(
    title: str,
    summary: str,
    use: Sequence[str],
    avoid: Sequence[str],
    needs: Sequence[str],
    assumes: Sequence[str],
    outputs: Sequence[str],
    difficulty: Difficulty,
    runtime: TypicalRuntime,
    example: str,
    *,
    related: Sequence[str] = (),
    expert: str | None = None,
    refs: Sequence[tuple[str, str | None]] = (),
    fits: Mapping[str, Sequence[str]] | None = None,
) -> About:
    return About(
        title=title, summary_plain=summary, use_when=list(use), avoid_when=list(avoid),
        data_needs=list(needs), assumptions=list(assumes), outputs=list(outputs),
        difficulty=difficulty, typical_runtime=runtime, example=example,
        related=list(related), expert_notes=expert,
        references=[AboutReference(title=t, url=u) for t, u in refs],
        fits={k: list(v) for k, v in (fits or {}).items()},
    )  # fmt: skip


_B, _I, _X = Difficulty.BEGINNER, Difficulty.INTERMEDIATE, Difficulty.EXPERT
_NOW, _SEC, _MIN, _LONG = (
    TypicalRuntime.INSTANT,
    TypicalRuntime.SECONDS,
    TypicalRuntime.MINUTES,
    TypicalRuntime.LONG,
)
_SM = ("statsmodels documentation", "https://www.statsmodels.org/stable/")


def _schema(props: Mapping[str, dict[str, Any]], required: Sequence[str] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(props),
    }
    if required:
        out["required"] = list(required)
    return out


def _num(desc: str, default: float | None = None, lo: float | None = None,
         hi: float | None = None, *, exclusive: bool = False) -> dict[str, Any]:  # fmt: skip
    s: dict[str, Any] = {"type": "number", "description": desc}
    if default is not None:
        s["default"] = default
    if lo is not None:
        s["exclusiveMinimum" if exclusive else "minimum"] = lo
    if hi is not None:
        s["exclusiveMaximum" if exclusive else "maximum"] = hi
    return s


def _int(desc: str, default: int | None = None, lo: int | None = 0,
         hi: int | None = None) -> dict[str, Any]:  # fmt: skip
    s: dict[str, Any] = {"type": "integer", "description": desc}
    if default is not None:
        s["default"] = default
    if lo is not None:
        s["minimum"] = lo
    if hi is not None:
        s["maximum"] = hi
    return s


def _enum(desc: str, values: Sequence[str], default: str | None = None) -> dict[str, Any]:
    s: dict[str, Any] = {"type": "string", "enum": list(values), "description": desc}
    if default is not None:
        s["default"] = default
    return s


def _bool(desc: str, default: bool) -> dict[str, Any]:
    return {"type": "boolean", "description": desc, "default": default}


_NAME = {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_.\-]{0,127}$"}


def _var(desc: str = "The variable to test (default: the dataset's target)") -> dict[str, Any]:
    return {**_NAME, "description": desc}


def _vars(desc: str, min_items: int = 1) -> dict[str, Any]:
    return {
        "type": "array",
        "items": _NAME,
        "minItems": min_items,
        "maxItems": 256,
        "description": desc,
    }


_SYSTEM_VARS: Final = _vars(
    "The series: variable names, or labels along the data's series dimension (every series "
    "of the target when omitted)"
)


_ALPHA = _num("Significance level", 0.05, 0, 0.5, exclusive=True)
_PER_SERIES = _bool("For panels, test every series (geo, product) separately", True)
_DATA_IN = {"data": PortSpec(type=PortType.DATASET, description="The data to work on")}


def _f(t: ResultType, d: str) -> ResultField:
    return ResultField(type=t, description=d)


_TEST_FIELDS: Final[dict[str, ResultField]] = {
    "statistic": _f("number", "The test statistic"),
    "p_value": _f("number", "The p-value (median over series for panels)"),
    "alpha": _f("number", "The significance level used"),
    "reject": _f("boolean", "Whether the null hypothesis was rejected at alpha"),
    "decision": _f("string", "The decision label (see the test's About)"),
    "verdict_plain": _f("string", "The verdict in plain words"),
    "critical_values": _f("object", "Critical values by level ('1%', '5%', '10%')"),
    "n_obs": _f("integer", "Observations used"),
    "lags": _f("integer", "Lags used"),
    "details": _f("object", "Test-specific fields"),
    "per_series": _f("array", "Per-series results for panels"),
    "reject_share": _f("number", "Share of series where the null was rejected"),
}
_TEST_OUT = {"result": PortType.TEST_RESULT}

_NODES: list[NodeImplementation] = []


def _node(
    id: str,
    kind: NodeKind,
    about: About,
    *,
    params: Mapping[str, dict[str, Any]] | None = None,
    required: Sequence[str] = (),
    schema: dict[str, Any] | None = None,
    inputs: Mapping[str, PortSpec] | None = None,
    outputs: Mapping[str, PortType] | None = None,
    fields: Mapping[str, ResultField] | None = None,
    runs_in: RunsIn = "sandbox",
    cacheable: bool = True,
) -> None:
    _NODES.append(
        NodeImplementation(
            id=id,
            kind=kind,
            inputs=dict(inputs if inputs is not None else _DATA_IN),
            outputs=dict(outputs or {}),
            params_schema=schema if schema is not None else _schema(params or {}, required),
            result_fields=dict(fields or {}),
            runs_in=runs_in,
            cacheable=cacheable,
            about=about,
        )
    )


def _test(
    id: str, about: About, params: Mapping[str, dict[str, Any]] | None = None,
    extra: Mapping[str, ResultField] | None = None, *, required: Sequence[str] = (),
    inputs: Mapping[str, PortSpec] | None = None,
) -> None:  # fmt: skip
    base: dict[str, dict[str, Any]] = {"alpha": _ALPHA}
    _node(
        id, NodeKind.TEST, about, params={**base, **(params or {})}, required=required,
        inputs=inputs, outputs=_TEST_OUT, fields={**_TEST_FIELDS, **(extra or {})},
    )  # fmt: skip


_TESTS_FIT = {"goal": ["check_data", "forecast"], "data_shape": ["one_series", "several_series"]}
_UNIT_ROOT_REL = ["test.adf", "test.kpss", "test.pp", "transform.difference"]

# ==================================================================================== tests

_test("test.adf", _a(
    "Does the series wander? (ADF)",
    "Checks whether a series keeps returning to a stable level or wanders off, which "
    "decides whether it should be differenced before forecasting.",
    ["Before fitting a forecasting model that needs a stable series",
     "Together with the KPSS test, which looks at the same question from the other side"],
    ["The series is very short (under about 30 periods)",
     "There is an obvious one-off break (use Zivot-Andrews)"],
    ["One numeric series over time (or a panel, tested per series)"],
    ["Regular time steps", "Any trend is described by the chosen trend option"],
    ["Statistic, p-value and a plain verdict", "Per-series results for panels"],
    _I, _SEC,
    "Weekly sales give p = 0.57: the series wanders, so difference it once.",
    related=["test.kpss", "test.pp", "transform.difference"],
    expert="Augmented Dickey-Fuller (statsmodels adfuller); H0: unit root. Lag length by "
    "AIC/BIC/t-stat; regression 'c', 'ct', 'ctt' or 'n'; MacKinnon p-values.",
    refs=[_SM, ("Dickey & Fuller (1979)", None)], fits=_TESTS_FIT,
), {"var": _var(), "regression": _enum("Deterministic terms", ["c", "ct", "ctt", "n"], "c"),
    "autolag": _enum("Lag selection", ["AIC", "BIC", "t-stat", "none"], "AIC"),
    "maxlag": _int("Largest lag tried (default: Schwert's rule)", None, 0, 100),
    "per_series": _PER_SERIES})  # fmt: skip

_test("test.kpss", _a(
    "Is the series stable? (KPSS)",
    "Checks whether a series varies around a stable level or trend; it is the mirror image "
    "of the ADF test, so the two together give a firmer answer.",
    ["Alongside ADF to confirm whether differencing is needed"],
    ["The series is very short"],
    ["One numeric series over time"],
    ["Regular time steps"],
    ["Statistic, p-value and a plain verdict"],
    _I, _SEC,
    "KPSS rejects stability (p = 0.01) while ADF cannot reject a wandering series: "
    "difference it.",
    related=["test.adf", "transform.difference"],
    expert="KPSS (statsmodels kpss); H0: level (or trend) stationarity; p-values are "
    "interpolated within [0.01, 0.1] and clipped outside.",
    refs=[_SM, ("Kwiatkowski, Phillips, Schmidt & Shin (1992)", None)], fits=_TESTS_FIT,
), {"var": _var(), "regression": _enum("Stationary around a level or a trend", ["c", "ct"], "c"),
    "nlags": _enum("Bandwidth rule", ["auto", "legacy"], "auto"),
    "per_series": _PER_SERIES})  # fmt: skip

_test("test.pp", _a(
    "Does the series wander? (Phillips-Perron)",
    "Checks, like ADF, whether a series wanders off, but copes better with errors that are "
    "uneven or related over time.",
    ["You want a second opinion to ADF on a noisy series"],
    ["The series is very short"],
    ["One numeric series over time"],
    ["Regular time steps"],
    ["Statistic, p-value and a plain verdict"],
    _X, _SEC,
    "PP and ADF agree that monthly demand wanders.",
    related=["test.adf", "test.kpss"],
    expert="Phillips-Perron Z-tau test (arch.unitroot.PhillipsPerron or an equivalent "
    "Newey-West corrected regression); H0: unit root.",
    refs=[("Phillips & Perron (1988)", None)], fits=_TESTS_FIT,
), {"var": _var(), "trend": _enum("Deterministic terms", ["n", "c", "ct"], "c"),
    "lags": _int("Newey-West lags (default: automatic)", None, 0, 100),
    "per_series": _PER_SERIES})  # fmt: skip

_test("test.zivot_andrews", _a(
    "Wandering or one break? (Zivot-Andrews)",
    "Checks whether a series that looks like it wanders is really stable with one sudden "
    "jump, such as a price change or a new product launch.",
    ["The series has an obvious one-off shift"],
    ["There are several breaks", "The series is short"],
    ["One numeric series over time (at least about 50 periods)"],
    ["At most one break, at an unknown date"],
    ["Statistic, p-value, the likely break date and a plain verdict"],
    _X, _SEC,
    "Sales jump after a relaunch in March 2023; with that break allowed the series is "
    "stable.",
    related=["test.adf", "test.chow"],
    expert="Zivot-Andrews (statsmodels zivot_andrews); H0: unit root without a break; "
    "break in intercept, trend or both.",
    refs=[_SM, ("Zivot & Andrews (1992)", None)], fits=_TESTS_FIT,
), {"var": _var(), "regression": _enum("Break in", ["c", "t", "ct"], "c"),
    "trim": _num("Share of each end excluded as a break date", 0.15, 0.05, 0.3),
    "per_series": _PER_SERIES},
    {"break_index": _f("integer", "Position of the most likely break"),
     "break_at": _f("string", "Date of the most likely break")})  # fmt: skip

_test("test.johansen", _a(
    "Are the series tied together? (Johansen)",
    "Checks whether several wandering series stay tied together in the long run, and how "
    "many such ties there are.",
    ["Before modelling several trending series together"],
    ["The series do not wander (no differencing needed)", "Only one series"],
    ["Two or more numeric series over time"],
    ["Each series wanders on its own", "Enough history (50+ periods)"],
    ["The number of long-run ties (rank) and the test table"],
    _X, _SEC,
    "Prices of two close competitors share one long-run tie (rank 1).",
    related=["ts.vecm", "search.coint_rank", "test.engle_granger"],
    expert="Johansen trace and maximum-eigenvalue tests (statsmodels coint_johansen); rank "
    "from sequential tests at the critical values for the chosen level.",
    refs=[_SM, ("Johansen (1991)", None)],
    fits={"goal": ["compare_series"], "data_shape": ["several_series"]},
), {"vars": _SYSTEM_VARS,
    "det_order": _int("Deterministic terms: -1 none, 0 constant, 1 trend", 0, -1, 1),
    "k_ar_diff": _int("Lagged differences", 1, 0, 24),
    "method": _enum("Test statistic", ["trace", "maxeig"], "trace"),
    "alpha": _enum("Level for the rank (critical values exist at 10%, 5%, 1%)",
                   ["0.1", "0.05", "0.01"], "0.05")},
    {"rank": _f("integer", "The selected cointegration rank"),
     "trace_stats": _f("array", "Trace statistics per rank"),
     "max_eig_stats": _f("array", "Maximum-eigenvalue statistics per rank")},)  # fmt: skip

_test("test.engle_granger", _a(
    "Are two series tied together? (Engle-Granger)",
    "Checks whether two wandering series stay tied together in the long run, so their gap "
    "keeps closing.",
    ["Before an error-correction model of one result and one driver"],
    ["More than two series (use Johansen)"],
    ["Two numeric series over time"],
    ["Both series wander", "One long-run relationship at most"],
    ["Statistic, p-value and a plain verdict"],
    _X, _SEC,
    "Your price and a competitor's price are tied (p = 0.02).",
    related=["test.johansen", "ts.ecm"],
    expert="Engle-Granger two-step test (statsmodels coint); H0: no cointegration.",
    refs=[_SM, ("Engle & Granger (1987)", None)],
    fits={"goal": ["compare_series"], "data_shape": ["several_series"]},
), {"y": _var("The first series"), "x": _var("The second series"),
    "trend": _enum("Deterministic terms", ["c", "ct", "ctt", "n"], "c")},
    required=["y", "x"])  # fmt: skip

_RESID_IN = {
    "data": PortSpec(type=PortType.DATASET, required=False,
                     description="A series to test (when no model is given)"),
    "model": PortSpec(type=PortType.MODEL_VERSION, required=False,
                      description="Test the model's residuals instead"),
}  # fmt: skip

_test("test.ljung_box", _a(
    "Is there a pattern left? (Ljung-Box)",
    "Checks whether the errors a model leaves behind are still related to their own past, "
    "which would mean the model misses some structure.",
    ["After fitting a forecasting model, on its errors"],
    ["On the raw series (use ACF/PACF to look for structure)"],
    ["Model errors (residuals) or a series"],
    ["Regular time steps"],
    ["Statistic and p-value per lag, a plain verdict"],
    _I, _SEC,
    "Errors of an ARIMA model show no pattern up to lag 10 (p = 0.42): good.",
    related=["test.acf_pacf", "evaluate.residuals", "residuals.whiteness"],
    expert="Ljung-Box Q (statsmodels acorr_ljungbox); H0: no autocorrelation up to the lag; "
    "model_df adjusts the degrees of freedom for fitted ARMA terms.",
    refs=[_SM, ("Ljung & Box (1978)", None)], fits={"goal": ["validate_model", "forecast"]},
), {"var": _var("Series to test (default: residuals of the model input)"),
    "lags": _int("Lags tested", 10, 1, 200),
    "model_df": _int("Fitted ARMA terms to subtract (default: from the model)", None, 0, 50)},
    inputs=_RESID_IN)  # fmt: skip

_test("test.jarque_bera", _a(
    "Bell-shaped errors? (Jarque-Bera)",
    "Checks whether values, usually a model's errors, follow a bell curve; if not, ranges "
    "built on that assumption may be off.",
    ["Checking a model's errors before trusting its ranges"],
    ["Small samples (the test is unreliable below about 50 values)"],
    ["Model errors (residuals) or a series"],
    ["Values are independent"],
    ["Statistic, p-value, skewness and tail weight"],
    _I, _NOW,
    "Errors are skewed (p < 0.001): a few very large misses.",
    related=["evaluate.residuals"],
    expert="Jarque-Bera on skewness and kurtosis (statsmodels jarque_bera); H0: normality.",
    refs=[_SM], fits={"goal": ["validate_model"]},
), {"var": _var("Series to test (default: residuals of the model input)")},
    {"skew": _f("number", "Skewness"), "kurtosis": _f("number", "Kurtosis")},
    inputs=_RESID_IN)  # fmt: skip

_test("test.breusch_pagan", _a(
    "Even-sized errors? (Breusch-Pagan)",
    "Checks whether a model's errors are bigger at some times or for some inputs than "
    "others, which makes its ranges too narrow in places.",
    ["Checking a regression-type model's errors"],
    ["Models without inputs"],
    ["A fitted model with inputs"],
    ["The error spread changes linearly with the inputs"],
    ["Statistic, p-value and a plain verdict"],
    _X, _NOW,
    "Errors grow with spend (p = 0.003): ranges are too narrow for big weeks.",
    related=["test.white", "evaluate.residuals"],
    expert="Breusch-Pagan LM test (statsmodels het_breuschpagan); H0: homoskedasticity.",
    refs=[_SM], fits={"goal": ["validate_model"]},
), {"robust": _bool("Koenker's studentised version", True)},
    inputs={"model": PortSpec(type=PortType.MODEL_VERSION)})  # fmt: skip

_test("test.white", _a(
    "Even-sized errors? (White)",
    "Checks, more generally than Breusch-Pagan, whether a model's errors are bigger in some "
    "places than others.",
    ["You suspect the error size depends on inputs in a curved way"],
    ["Models with many inputs and little data (the test uses all their combinations)"],
    ["A fitted model with inputs"],
    ["Enough observations for the squared and cross terms"],
    ["Statistic, p-value and a plain verdict"],
    _X, _NOW,
    "White's test also flags uneven errors (p = 0.01).",
    related=["test.breusch_pagan"],
    expert="White's test (statsmodels het_white); H0: homoskedasticity.",
    refs=[_SM], fits={"goal": ["validate_model"]},
), inputs={"model": PortSpec(type=PortType.MODEL_VERSION)})  # fmt: skip

_test("test.arch_lm", _a(
    "Do volatile periods cluster? (ARCH-LM)",
    "Checks whether calm and turbulent periods come in clusters, in which case ranges "
    "should widen after a shock.",
    ["Financial or demand series with bursts of volatility"],
    ["Series without visible bursts"],
    ["Model errors (residuals) or a series"],
    ["Regular time steps"],
    ["Statistic, p-value and a plain verdict"],
    _X, _NOW,
    "Errors after promotions are larger for several weeks (p = 0.02).",
    related=["test.ljung_box"],
    expert="Engle's ARCH LM test (statsmodels het_arch); H0: no ARCH effects.",
    refs=[_SM, ("Engle (1982)", None)], fits={"goal": ["validate_model"]},
), {"var": _var("Series to test (default: residuals of the model input)"),
    "nlags": _int("Lags", 4, 1, 50)}, inputs=_RESID_IN)  # fmt: skip

_test("test.granger", _a(
    "Does one series help predict another? (Granger)",
    "Checks whether past values of a driver help predict a result beyond what the result's "
    "own past already tells you.",
    ["Deciding which series to include in a VAR",
     "Checking that a leading indicator really leads"],
    ["Proving cause and effect (it only shows predictive help)"],
    ["Two numeric series over time"],
    ["Both series are stable (difference them first if not)"],
    ["p-value per lag and the smallest p-value"],
    _I, _SEC,
    "Search interest helps predict sales two weeks later (p = 0.004).",
    related=["ts.var", "test.adf"],
    expert="Granger causality F-tests for lags 1..maxlag (statsmodels "
    "grangercausalitytests, ssr F test); H0: the driver's lags have zero coefficients.",
    refs=[_SM, ("Granger (1969)", None)],
    fits={"goal": ["compare_series", "explain_drivers"], "data_shape": ["several_series"]},
), {"y": _var("The result"), "x": _var("The candidate driver"),
    "maxlag": _int("Largest lag tested", 4, 1, 52)},
    {"min_p_value": _f("number", "Smallest p-value over the lags"),
     "best_lag": _f("integer", "Lag with the smallest p-value"),
     "p_values": _f("object", "p-value by lag")},
    required=["y", "x"])  # fmt: skip

_test("test.acf_pacf", _a(
    "How far back does the past matter? (ACF/PACF)",
    "Measures how strongly each past period is related to the present and suggests how "
    "many past values a forecasting model should use.",
    ["Choosing the orders of an ARIMA model", "Seeing seasonality at a glance"],
    ["The series still wanders (difference it first)"],
    ["One stable numeric series over time"],
    ["Regular time steps"],
    ["Significant lags, suggested AR and MA orders, the correlation values"],
    _I, _SEC,
    "Partial correlations cut off after lag 2 and plain ones fade slowly: try an AR(2).",
    related=["search.arma_order", "test.ljung_box"],
    expert="Sample ACF/PACF (statsmodels acf, pacf with Yule-Walker-MLE) with Bartlett / "
    "1.96/sqrt(n) bands; suggested p = last significant PACF lag before the cut-off, q from "
    "the ACF.",
    refs=[_SM, ("Box, Jenkins, Reinsel & Ljung, Time Series Analysis", None)],
    fits=_TESTS_FIT,
), {"var": _var(), "nlags": _int("Lags computed", 24, 1, 200),
    "max_order": _int("Largest order suggested", 5, 0, 24)},
    {"significant_acf_lags": _f("array", "Lags whose autocorrelation is significant"),
     "significant_pacf_lags": _f("array", "Lags whose partial autocorrelation is significant"),
     "suggested_p": _f("integer", "Suggested AR order"),
     "suggested_q": _f("integer", "Suggested MA order"),
     "acf": _f("array", "Autocorrelations"),
     "pacf": _f("array", "Partial autocorrelations")})  # fmt: skip

_test("test.seasonal_strength", _a(
    "How strong is the season?",
    "Measures how much of a series' ups and downs come from a repeating seasonal pattern, "
    "on a scale from 0 (none) to 1 (all).",
    ["Deciding whether to model seasonality or seasonally difference"],
    ["Less than two full seasons of data"],
    ["One numeric series over time and the season length"],
    ["The season has a fixed length"],
    ["Seasonal strength, trend strength and a plain verdict"],
    _B, _SEC,
    "Weekly ice-cream sales have seasonal strength 0.85: model the season.",
    related=["transform.seasonal_difference", "core.fourier"],
    expert="STL decomposition; F_s = max(0, 1 - Var(R) / Var(S + R)) (Wang, Smith & "
    "Hyndman); 'seasonal' when F_s >= threshold.",
    refs=[("Hyndman & Athanasopoulos, FPP3 section 4.3", "https://otexts.com/fpp3/")],
    fits=_TESTS_FIT,
), {"var": _var(), "period": _int("Season length in periods (default: from the frequency)",
                                   None, 2, 400),
    "threshold": _num("Strength counted as seasonal", 0.64, 0, 1)},
    {"strength": _f("number", "Seasonal strength between 0 and 1"),
     "trend_strength": _f("number", "Trend strength between 0 and 1"),
     "period": _f("integer", "Season length used"),
     "seasonal": _f("boolean", "Strength reaches the threshold")})  # fmt: skip

_test("test.vif", _a(
    "Are drivers too alike? (VIF)",
    "Checks whether some drivers move almost together, which makes it hard to tell their "
    "separate effects apart.",
    ["Before a regression or marketing mix model with many drivers"],
    ["You only predict and do not care about separate effects"],
    ["The driver columns"],
    ["Effects are roughly linear"],
    ["VIF per driver, the largest one and the drivers above the limit"],
    _I, _NOW,
    "TV and radio spend have VIF 14: their effects cannot be separated reliably.",
    related=["mmm.bayesian", "plausibility.signs"],
    expert="Variance inflation factor 1/(1 - R_j^2) per regressor (statsmodels "
    "variance_inflation_factor) with a constant added.",
    refs=[_SM], fits={"goal": ["check_data", "explain_marketing", "explain_drivers"]},
), {"vars": _vars("The drivers"), "limit": _num("VIF above which a driver is flagged", 10, 1)},
    {"max_vif": _f("number", "The largest VIF"),
     "table": _f("object", "VIF by driver"),
     "high_vif_vars": _f("array", "Drivers above the limit")},
    required=["vars"])  # fmt: skip

_test("test.cusum", _a(
    "Did the relationship change? (CUSUM)",
    "Checks whether a model's relationship between result and drivers stayed the same "
    "over time, without having to guess when it might have changed.",
    ["You suspect something changed but not when"],
    ["You know the date (use the Chow test)"],
    ["A fitted regression-type model over time"],
    ["Errors are independent"],
    ["Whether the stability bounds were crossed and where"],
    _X, _NOW,
    "The cumulative errors cross the bound in 2022: the relationship shifted.",
    related=["test.chow", "stability.rolling_params"],
    expert="OLS-residual CUSUM test (statsmodels breaks_cusumolsresid); H0: parameter "
    "stability.",
    refs=[_SM, ("Ploberger & Kramer (1992)", None)], fits={"goal": ["validate_model"]},
), {}, {"break_detected": _f("boolean", "The bounds were crossed"),
        "break_at": _f("string", "Where the statistic peaks")},
    inputs={"model": PortSpec(type=PortType.MODEL_VERSION)})  # fmt: skip

_test("test.chow", _a(
    "Did it change on this date? (Chow)",
    "Checks whether the relationship between a result and its drivers is different before "
    "and after a date you choose.",
    ["You know a date when something changed (a relaunch, a new competitor)"],
    ["You do not know the date (use CUSUM)"],
    ["A result, its drivers and a candidate date"],
    ["Errors have the same spread before and after"],
    ["Statistic, p-value and a plain verdict"],
    _I, _NOW,
    "The price effect differs after the 2023 relaunch (p = 0.01).",
    related=["test.cusum", "test.zivot_andrews"],
    expert="Chow F test comparing pooled and split OLS fits at the break point.",
    refs=[("Chow (1960)", None)], fits={"goal": ["validate_model", "explain_drivers"]},
), {"y": _var("The result"), "x": _vars("The drivers"),
    "break_at": {"type": ["string", "integer"], "description": "Break date or position"}},
    required=["y", "x", "break_at"])  # fmt: skip

# =============================================================================== transforms

_DATA_OUT = {"data": PortType.DATASET}
_TRANSFORM_FIT = {"goal": ["check_data", "forecast"]}


def _transform(id: str, about: About, params: Mapping[str, dict[str, Any]],
               required: Sequence[str] = (), fields: Mapping[str, ResultField] | None = None
               ) -> None:  # fmt: skip
    _node(id, NodeKind.TRANSFORM, about, params=params, required=required, outputs=_DATA_OUT,
          fields={"vars": _f("array", "Variables written"), **(fields or {})})  # fmt: skip


_transform("transform.difference", _a(
    "Change from the previous period",
    "Replaces each value by its change since the previous period, which turns a wandering "
    "series into a stable one.",
    ["A unit-root test says the series wanders"],
    ["The series is already stable (differencing adds noise)"],
    ["A numeric series over time"],
    ["The first periods are lost (one per difference)"],
    ["The differenced series (forecasts are added back up automatically)"],
    _I, _NOW,
    "Sales of 100, 104, 103 become changes of +4 and -1.",
    related=_UNIT_ROOT_REL, fits=_TRANSFORM_FIT,
    expert="First differences of order d along the time dim, per series; the last d levels "
    "are kept as state to integrate forecasts.",
), {"vars": _vars("Variables to difference (default: the target)"),
    "order": _int("How many times", 1, 1, 2), "dim": {**_NAME, "default": "time"}})  # fmt: skip

_transform("transform.seasonal_difference", _a(
    "Change from the same period last season",
    "Replaces each value by its change since the same period one season earlier, which "
    "removes a strong repeating pattern.",
    ["Strong seasonality that a seasonal model should not have to learn"],
    ["Less than two seasons of history"],
    ["A numeric series over time and the season length"],
    ["The first season of data is lost"],
    ["The seasonally differenced series"],
    _I, _NOW,
    "Week 53's sales minus week 1's sales a year earlier.",
    related=["test.seasonal_strength", "transform.difference"], fits=_TRANSFORM_FIT,
), {"vars": _vars("Variables to difference (default: the target)"),
    "period": _int("Season length in periods", None, 2, 400),
    "order": _int("How many times", 1, 1, 2), "dim": {**_NAME, "default": "time"}},
    required=["period"])  # fmt: skip

_transform("transform.log", _a(
    "Logarithm",
    "Takes the logarithm of positive values, which turns growth into steady steps and "
    "makes big and small series comparable.",
    ["Values grow by percentages", "The spread grows with the level"],
    ["Values can be zero or negative"],
    ["Positive numeric columns"],
    ["All values are positive"],
    ["The same columns on a log scale"],
    _B, _NOW,
    "Sales of 10, 100 and 1000 become 2.3, 4.6 and 6.9.",
    related=["transform.box_cox", "core.log1p"], fits=_TRANSFORM_FIT,
), {"vars": _vars("Variables (default: the target)")})  # fmt: skip

_transform("transform.box_cox", _a(
    "Stabilise the spread",
    "Bends a positive series so its ups and downs are about the same size at high and low "
    "levels, choosing the best bend from the data.",
    ["The spread grows with the level and a log is too strong or too weak"],
    ["Values can be zero or negative"],
    ["Positive numeric columns"],
    ["All values are positive"],
    ["The transformed columns and the chosen bend (lambda)"],
    _I, _NOW,
    "Lambda 0.3 makes monthly sales' spread roughly constant.",
    related=["transform.log"], fits=_TRANSFORM_FIT,
    expert="Box-Cox with lambda by maximum likelihood (scipy.stats.boxcox) or fixed.",
), {"vars": _vars("Variables (default: the target)"),
    "lmbda": _num("Fixed lambda (default: estimated)", None, -2, 2)},
    fields={"lmbda": _f("number", "The lambda used")})  # fmt: skip

_transform("transform.steps", _a(
    "Preprocessing steps",
    "Runs catalog preprocessing steps, such as weekly totals, scaling or calendar features, "
    "on the data.",
    ["The data needs the same preparation as a model spec would apply"],
    ["A model node will run the same steps itself (put them in its pipeline)"],
    ["A dataset"],
    ["Stateful steps learn on the data given here"],
    ["The prepared dataset"],
    _I, _SEC,
    "Weekly totals then a log of spend before the tests.",
    related=["core.resample", "core.log1p"], fits=_TRANSFORM_FIT,
), {"steps": {"type": "array", "minItems": 1, "maxItems": 64,
              "description": "Pipeline steps, as in a ModelSpec ({id, use, with})",
              "items": {"type": "object", "required": ["id", "use"],
                        "properties": {"id": {"type": "string"}, "use": {"type": "string"},
                                       "with": {"type": "object"}}}}},
    required=["steps"])  # fmt: skip

_transform("transform.select", _a(
    "Keep some variables or dates",
    "Keeps only the variables, markets or dates you choose, for example to test one series "
    "or a recent period.",
    ["Later nodes should only see part of the data"],
    ["Everything is needed"],
    ["A dataset"],
    ["What is dropped is not needed downstream"],
    ["A smaller dataset"],
    _B, _NOW,
    "Keep only y and price for the market DE.",
    related=["core.filter_coords"], fits=_TRANSFORM_FIT,
), {"vars": _vars("Variables to keep"),
    "sel": {"type": "object", "description": "dim -> labels to keep",
            "additionalProperties": {"type": "array", "maxItems": 10000}},
    "start": {"type": ["string", "integer"], "description": "First time label kept"},
    "end": {"type": ["string", "integer"], "description": "Last time label kept"}})  # fmt: skip

# =================================================================================== splits


def _plan_schema(model: type[Any]) -> dict[str, Any]:
    schema: dict[str, Any] = model.model_json_schema()
    schema["properties"].pop("kind", None)
    if "required" in schema:
        schema["required"] = [r for r in schema["required"] if r != "kind"]
        if not schema["required"]:
            del schema["required"]
    schema.pop("title", None)
    schema.pop("description", None)
    return schema


_SPLIT_FIT = {"goal": ["forecast", "validate_model"]}
_SPLIT_OUT = {"plan": PortType.SPLIT_PLAN}
_SPLIT_FIELDS = {
    "n_folds": _f("integer", "Number of folds"),
    "folds": _f("array", "Fold windows (positions) or groups"),
}


def _split(id: str, model: type[Any], about: About) -> None:
    _node(id, NodeKind.SPLIT, about, schema=_plan_schema(model), outputs=_SPLIT_OUT,
          fields=_SPLIT_FIELDS)  # fmt: skip


_split("split.holdout", HoldoutPlan, _a(
    "One test period",
    "Holds back one part of the data, usually the latest periods, to test the model on.",
    ["A quick, single honest test"],
    ["You want to know how stable the error is (use a rolling backtest)"],
    ["A dataset with an ordered or group dimension"],
    ["The held-back part is like the future you care about"],
    ["A plan with one fold"],
    _B, _NOW, "Hold out the last 8 weeks.",
    related=["core.time_holdout", "split.expanding_window"], fits=_SPLIT_FIT,
))  # fmt: skip
_split("split.rolling_origin", RollingOriginPlan, _a(
    "Rolling backtest (fixed window)",
    "Repeats the test at several points in time, each time training on the same length of "
    "recent history, to see how the error holds up.",
    ["Old history may no longer be relevant", "You want errors at several forecast horizons"],
    ["Little history (each fold needs a full window)"],
    ["A dataset with a time dimension"],
    ["A fixed-length window is enough to learn from"],
    ["A plan with several folds and horizons"],
    _I, _NOW,
    "Train on 104 weeks, test 1, 4 and 8 weeks ahead, move 4 weeks, repeat 10 times.",
    related=["split.expanding_window", "model.backtest"], fits=_SPLIT_FIT,
))  # fmt: skip
_split("split.expanding_window", ExpandingWindowPlan, _a(
    "Rolling backtest (growing history)",
    "Repeats the test at several points in time, each time training on all history up to "
    "that point, the way a forecast is really made.",
    ["You forecast regularly and want a realistic error estimate",
     "You need a gap between training and test because data arrives late"],
    ["Very short series"],
    ["A dataset with a time dimension"],
    ["More history never hurts"],
    ["A plan with several folds and horizons"],
    _I, _NOW,
    "Six origins 4 weeks apart, a 1-week gap, horizons 1 to 8.",
    related=["split.rolling_origin", "core.as_of", "model.backtest"], fits=_SPLIT_FIT,
))  # fmt: skip
_split("split.blocked_kfold", BlockedKFoldPlan, _a(
    "Blocked cross-validation",
    "Cuts the time line into a few blocks and uses each block once as the test, leaving a "
    "buffer around it so neighbouring periods do not leak.",
    ["Explanatory models where the future is not the only thing that matters",
     "Short series where rolling backtests leave too little data"],
    ["Pure forecasting (training on the future is unrealistic)"],
    ["A dataset with a time dimension"],
    ["The buffer is long enough for neighbouring periods to be unrelated"],
    ["A plan with k folds"],
    _X, _NOW, "Five blocks with a 4-week buffer on each side.",
    related=["split.purged_kfold"], fits={"goal": ["validate_model", "explain_drivers"]},
    refs=[("Bergmeir & Benitez (2012), On the use of cross-validation for time series "
           "predictor evaluation", None)],
))  # fmt: skip
_split("split.purged_kfold", PurgedKFoldPlan, _a(
    "Purged cross-validation",
    "Like blocked cross-validation, but drops periods just before and just after each test "
    "block, for results that span several periods.",
    ["Results are computed over windows that overlap the test block"],
    ["Plain forecasting"],
    ["A dataset with a time dimension"],
    ["Purge and embargo cover the overlap"],
    ["A plan with k folds"],
    _X, _NOW, "Five blocks, purge 2 periods before and embargo 4 after each test block.",
    related=["split.blocked_kfold"], fits={"goal": ["validate_model"]},
    refs=[("Lopez de Prado (2018), Advances in Financial Machine Learning, ch. 7", None)],
))  # fmt: skip
_split("split.geo_holdout", GeoHoldoutPlan, _a(
    "Hold out markets",
    "Leaves whole markets or stores out of training and tests the model on them, "
    "optionally only over their latest periods.",
    ["You will apply the model to new markets", "Reading out a geo experiment"],
    ["You only predict for markets the model has seen"],
    ["A dataset with a group dimension such as geo"],
    ["Held-out markets resemble the others"],
    ["A plan with one or more folds of held-out groups"],
    _I, _NOW, "Hold out 2 random markets, 5 times with different draws.",
    related=["core.leave_group_out"], fits={"goal": ["validate_model", "explain_marketing"],
                                             "data_shape": ["panel"]},
))  # fmt: skip
_split("split.nested", NestedPlan, _a(
    "Tuning inside a backtest",
    "Runs a second, inner backtest inside each training window, so settings are tuned "
    "without peeking at the outer test periods.",
    ["You tune settings (orders, penalties) and want an honest final error"],
    ["You do not tune anything"],
    ["A dataset with a time dimension and enough history for both levels"],
    ["Inner folds fit inside every outer training window"],
    ["A plan whose folds carry inner folds"],
    _X, _NOW, "Outer: expanding window, 4 origins; inner: expanding window, 3 origins.",
    related=["search.grid", "split.expanding_window"], fits={"goal": ["validate_model"]},
))  # fmt: skip

# =================================================================================== search

_SEARCH_FIT = {"goal": ["forecast"], "data_shape": ["one_series", "several_series"]}
_IC = _enum("Information criterion", ["aic", "bic", "hqic", "aicc"], "aic")

_node("search.arma_order", NodeKind.SEARCH, _a(
    "Find the AR and MA orders",
    "Tries a grid of past-value (AR) and past-error (MA) orders for a forecasting model and "
    "picks the one with the best balance of fit and simplicity.",
    ["Before fitting an ARIMA or ARIMAX model"],
    ["The series still wanders (difference first)", "Very short series"],
    ["One stable numeric series over time (optional drivers)"],
    ["The best order is within the grid"],
    ["The best orders, the full grid of scores"],
    _I, _SEC,
    "Out of p = 0..4 and q = 0..4, ARMA(2,1) has the lowest AIC.",
    related=["test.acf_pacf", "ts.sarimax", "search.sarima_order"],
    expert="Grid search of SARIMAX(p,d,q) fits by AIC/BIC/HQIC/AICc (statsmodels "
    "arma_order_select_ic style); failed fits are skipped and reported.",
    refs=[_SM], fits=_SEARCH_FIT,
), params={"var": _var("The series (default: the target)"),
           "max_p": _int("Largest AR order", 4, 0, 12), "max_q": _int("Largest MA order", 4, 0, 12),
           "d": _int("Differences inside the model", 0, 0, 2),
           "exog": _vars("Drivers included in every fit", 0), "criterion": _IC,
           "trend": _enum("Trend term", ["n", "c", "t", "ct"], "c")},
   outputs={"table": PortType.TABLE},
   fields={"best_p": _f("integer", "Best AR order"), "best_q": _f("integer", "Best MA order"),
           "best_order": _f("array", "[p, d, q]"), "best_score": _f("number", "Its criterion"),
           "n_failed": _f("integer", "Fits that failed")})  # fmt: skip

_node("search.sarima_order", NodeKind.SEARCH, _a(
    "Find seasonal orders",
    "Extends the order search to seasonal past values and errors, for series with a "
    "repeating pattern.",
    ["The series has a clear season that differencing alone does not remove"],
    ["No seasonality (use the plain order search)"],
    ["One stable numeric series over time and the season length"],
    ["The best orders are within the grid"],
    ["Best non-seasonal and seasonal orders, the grid of scores"],
    _X, _MIN,
    "SARIMA(1,0,1)(1,1,0)[52] wins on weekly sales.",
    related=["search.arma_order", "ts.sarimax"], refs=[_SM], fits=_SEARCH_FIT,
), params={"var": _var("The series (default: the target)"),
           "max_p": _int("Largest AR order", 2, 0, 6), "max_q": _int("Largest MA order", 2, 0, 6),
           "max_P": _int("Largest seasonal AR order", 1, 0, 3),
           "max_Q": _int("Largest seasonal MA order", 1, 0, 3),
           "d": _int("Differences", 0, 0, 2), "D": _int("Seasonal differences", 0, 0, 1),
           "period": _int("Season length", None, 2, 400), "exog": _vars("Drivers", 0),
           "criterion": _IC},
   required=["period"], outputs={"table": PortType.TABLE},
   fields={"best_order": _f("array", "[p, d, q]"),
           "best_seasonal_order": _f("array", "[P, D, Q, s]"),
           "best_score": _f("number", "Its criterion")})  # fmt: skip

_node("search.var_lag_order", NodeKind.SEARCH, _a(
    "How many lags for series together",
    "Picks how many past periods a model of several related series should use.",
    ["Before fitting a VAR or VECM"],
    ["One series (use the AR/MA order search)"],
    ["Two or more stable numeric series over time"],
    ["The best lag is within the range"],
    ["The selected lag by each criterion"],
    _I, _SEC, "AIC picks 3 lags, BIC 1.",
    related=["ts.var", "ts.vecm"], refs=[_SM],
    fits={"goal": ["compare_series"], "data_shape": ["several_series"]},
), params={"vars": _SYSTEM_VARS, "maxlags": _int("Largest lag", 8, 1, 52),
           "criterion": _IC},
   outputs={"table": PortType.TABLE},
   fields={"best_lag": _f("integer", "Lag by the chosen criterion"),
           "by_criterion": _f("object", "Lag by aic, bic, hqic, fpe")})  # fmt: skip

_node("search.coint_rank", NodeKind.SEARCH, _a(
    "How many long-run ties",
    "Decides how many long-run relationships tie a set of wandering series together, which "
    "the error-correction model needs.",
    ["Before fitting a VECM"],
    ["The series do not wander"],
    ["Two or more wandering numeric series"],
    ["Johansen's assumptions (enough history, the right trend terms)"],
    ["The selected rank and the test table"],
    _X, _SEC, "Rank 1 for three related prices.",
    related=["test.johansen", "ts.vecm"], refs=[_SM],
    fits={"goal": ["compare_series"], "data_shape": ["several_series"]},
), params={"vars": _SYSTEM_VARS,
           "det_order": _int("-1 none, 0 constant, 1 trend", 0, -1, 1),
           "k_ar_diff": _int("Lagged differences", 1, 0, 24),
           "method": _enum("Test", ["trace", "maxeig"], "trace"),
           "signif": _enum("Level", ["0.1", "0.05", "0.01"], "0.05")},
   outputs={"table": PortType.TABLE},
   fields={"rank": _f("integer", "The selected rank")})  # fmt: skip

_node("search.grid", NodeKind.SEARCH, _a(
    "Try settings on a backtest",
    "Fits a model with each combination of settings you list, scores each on a backtest, "
    "and picks the best.",
    ["Tuning a few settings of any catalog model"],
    ["Large grids on slow models (each point is a full fit per fold)"],
    ["A dataset, a split plan and the settings to try"],
    ["The backtest error reflects real use"],
    ["The best settings and a table of scores"],
    _X, _LONG, "Try 3 saturation shapes x 2 carry-over lengths, keep the lowest MAPE.",
    related=["split.nested", "model.backtest"], fits={"goal": ["forecast", "validate_model"]},
), params={"model": {"type": "string", "description": "Catalog model ref (id@range)"},
           "settings": {"type": "object", "description": "Base settings"},
           "grid": {"type": "object", "description": "JSON pointer into settings -> values",
                    "additionalProperties": {"type": "array", "minItems": 1, "maxItems": 50}},
           "metric": {"type": "string", "default": "rmse"},
           "direction": _enum("Better when", ["min", "max"], "min"),
           "max_points": _int("Grid points allowed", 50, 1, 500)},
   required=["model", "grid"],
   inputs={"data": PortSpec(type=PortType.DATASET),
           "split": PortSpec(type=PortType.SPLIT_PLAN)},
   outputs={"table": PortType.TABLE, "best": PortType.MODEL_VERSION},
   fields={"best_settings": _f("object", "The winning settings"),
           "best_score": _f("number", "Its score"), "n_points": _f("integer", "Points tried")},
   runs_in="child_run")  # fmt: skip

# ==================================================================================== model

_MODEL_REF = {"type": "string", "description": "Catalog model ref (id@range), e.g. ts.sarimax@^1",
              "pattern": r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+(@\S+)?$"}  # fmt: skip
_MODEL_FIT = {"goal": ["forecast", "explain_drivers", "explain_marketing"]}

_node("model.fit", NodeKind.MODEL, _a(
    "Fit a model",
    "Fits any model from the catalog to the data, optionally scoring it on a test period, "
    "and saves it as a model version.",
    ["Every workflow that ends in a model"],
    ["You only need data checks"],
    ["A dataset and the model's settings"],
    ["The model's own assumptions (see its About)"],
    ["A model version with its scorecard and metrics"],
    _B, _MIN, "Fit ts.sarimax@^1 with order from the search node.",
    related=["model.backtest", "ts.sarimax", "mmm.bayesian"], fits=_MODEL_FIT,
), params={"model": _MODEL_REF,
           "settings": {"type": "object", "description": "Model settings"},
           "fit": {"type": "object", "description": "Fit configuration (FitConfig)"},
           "pipeline": {"type": "array", "maxItems": 64, "description": "Preprocessing steps"},
           "bind": {"type": "object", "description": "Binding to canonical names"},
           "evaluate": {"type": "object", "description": "Evaluation block (metrics)"}},
   required=["model"],
   inputs={"data": PortSpec(type=PortType.DATASET),
           "split": PortSpec(type=PortType.SPLIT_PLAN, required=False,
                             description="A holdout plan (its split becomes data.split)")},
   outputs={"model": PortType.MODEL_VERSION, "metrics": PortType.METRICS},
   fields={"model_version": _f("string", "model://… of the new version"),
           "metrics": _f("object", "Holdout metrics"),
           "scorecard": _f("string", "pass, warn or fail")},
   runs_in="child_run")  # fmt: skip

_node("model.backtest", NodeKind.MODEL, _a(
    "Backtest a model",
    "Refits a model on every fold of a split plan and measures its forecast error at each "
    "horizon, so you see how reliable it really is.",
    ["Before trusting a forecasting model", "Comparing models fairly"],
    ["Slow models with many folds (each fold is a fit)"],
    ["A dataset, a split plan and the model settings"],
    ["Folds resemble how the model will be used"],
    ["Metrics per fold and horizon, forecasts per fold"],
    _I, _LONG, "Ten folds of ARIMAX: MAPE 6% at 1 week, 11% at 8 weeks.",
    related=["split.expanding_window", "evaluate.baseline", "compare.models"],
    fits={"goal": ["forecast", "validate_model"]},
), params={"model": _MODEL_REF, "settings": {"type": "object"}, "fit": {"type": "object"},
           "pipeline": {"type": "array", "maxItems": 64}, "bind": {"type": "object"},
           "metrics": {"type": "array", "items": {"type": "string"},
                       "default": ["mae", "rmse", "mape", "mase"]},
           "refit": _bool("Refit on every fold (else fit once and roll forward)", True)},
   required=["model"],
   inputs={"data": PortSpec(type=PortType.DATASET), "split": PortSpec(type=PortType.SPLIT_PLAN)},
   outputs={"metrics": PortType.METRICS, "table": PortType.TABLE},
   fields={"metrics": _f("object", "Average metrics"),
           "by_horizon": _f("object", "Metrics per horizon"),
           "n_folds": _f("integer", "Folds scored")},
   runs_in="child_run")  # fmt: skip

_node("model.forecast", NodeKind.MODEL, _a(
    "Forecast with a model",
    "Uses a fitted model version to forecast ahead, with future drivers if it needs them.",
    ["Producing the forecast after a model was selected"],
    ["The model cannot forecast (check its score modes)"],
    ["A model version and, for driver models, future driver values"],
    ["Future drivers are as planned"],
    ["A forecast dataset and a fan chart"],
    _B, _SEC, "Forecast 12 weeks with planned prices.",
    related=["model.fit", "ts.sarimax"], fits={"goal": ["forecast"]},
), params={"horizon": _int("Periods ahead", 12, 1, 1000),
           "future_covariates": _enum("Missing future drivers", ["carry_last", "seasonal_naive",
                                                                 "zero", "error"], "error"),
           "summary": {"type": "array", "items": {"type": "string"},
                       "default": ["mean", "hdi_90"]}},
   inputs={"model": PortSpec(type=PortType.MODEL_VERSION),
           "data": PortSpec(type=PortType.DATASET, required=False,
                            description="Future drivers")},
   outputs={"forecast": PortType.DATASET, "plot": PortType.PLOT},
   fields={"total": _f("number", "Forecast total over the horizon")},
   runs_in="child_run")  # fmt: skip

# ================================================================================= evaluate

_METRIC_FIELDS = {
    "mae": _f("number", "Mean absolute error"), "rmse": _f("number", "Root mean squared error"),
    "mape": _f("number", "Mean absolute percentage error"),
    "smape": _f("number", "Symmetric MAPE"), "mase": _f("number", "Mean absolute scaled error"),
    "coverage_90": _f("number", "Share inside the 90% range"),
    "crps": _f("number", "Continuous ranked probability score"),
    "by_horizon": _f("object", "Metrics per horizon"),
}  # fmt: skip

_node("evaluate.metrics", NodeKind.EVALUATE, _a(
    "Measure forecast error",
    "Measures how far a model's predictions were from what actually happened on the test "
    "data, in several easy-to-read ways.",
    ["After fitting or backtesting a model"],
    ["Nothing was held out for testing"],
    ["A model version (and a test period) or backtest results"],
    ["The test data resembles future use"],
    ["MAE, RMSE, MAPE, MASE, range coverage, per horizon"],
    _B, _SEC, "MAPE 8% and 88% of actual values inside the 90% range.",
    related=["evaluate.baseline", "holdout.accuracy"],
    fits={"goal": ["validate_model", "forecast"]},
    expert="MASE scales by the in-sample seasonal-naive MAE (Hyndman & Koehler 2006); CRPS "
    "from draws or quantiles.",
), params={"metrics": {"type": "array", "items": {"type": "string", "enum": sorted(_METRIC_FIELDS)},
                       "default": ["mae", "rmse", "mape", "mase", "coverage_90"]},
           "season": _int("Season length for MASE (default: from the frequency)", None, 1, 400)},
   inputs={"model": PortSpec(type=PortType.MODEL_VERSION, required=False),
           "backtest": PortSpec(type=PortType.METRICS, required=False)},
   outputs={"metrics": PortType.METRICS}, fields=_METRIC_FIELDS)  # fmt: skip

_node("evaluate.baseline", NodeKind.EVALUATE, _a(
    "Simple benchmark forecasts",
    "Scores very simple forecasts, such as 'same as last week' or 'same as last year', on "
    "the same test periods, so you can check a model beats them.",
    ["Any forecasting workflow (a model must beat these to be worth it)"],
    ["There is no time order"],
    ["A dataset and the same split plan the model used"],
    ["The benchmark is evaluated on exactly the same folds"],
    ["Metrics of each benchmark per fold and horizon"],
    _B, _SEC, "Seasonal naive has MAPE 14%; the ARIMAX model 9%.",
    related=["baseline.beats_naive", "compare.models"],
    fits={"goal": ["validate_model", "forecast"]},
), params={"methods": {"type": "array", "minItems": 1,
                       "items": {"type": "string", "enum": ["naive", "seasonal_naive", "mean",
                                                            "drift"]},
                       "default": ["naive", "seasonal_naive"]},
           "season": _int("Season length (default: from the frequency)", None, 1, 400),
           "var": _var("The series (default: the target)")},
   inputs={"data": PortSpec(type=PortType.DATASET), "split": PortSpec(type=PortType.SPLIT_PLAN)},
   outputs={"metrics": PortType.METRICS},
   fields={"by_method": _f("object", "Metrics per benchmark"),
           "best_method": _f("string", "The strongest benchmark")})  # fmt: skip

_node("evaluate.residuals", NodeKind.EVALUATE, _a(
    "Check the model's errors",
    "Checks the errors a model leaves behind for leftover patterns, uneven size and "
    "non-bell shapes, all in one go.",
    ["After fitting any time-series or regression model"],
    ["Models without residuals (classifiers)"],
    ["A model version"],
    ["Residuals are available from the fit"],
    ["Ljung-Box, normality and spread checks with plain verdicts, ACF of errors"],
    _I, _SEC, "No leftover pattern, slightly heavy tails, even spread.",
    related=["test.ljung_box", "test.jarque_bera", "residuals.whiteness"],
    fits={"goal": ["validate_model"]},
), params={"lags": _int("Ljung-Box lags", 10, 1, 200), "alpha": _ALPHA},
   inputs={"model": PortSpec(type=PortType.MODEL_VERSION)},
   outputs={"table": PortType.TABLE, "plot": PortType.PLOT},
   fields={"white_noise": _f("boolean", "No leftover pattern"),
           "normal": _f("boolean", "Bell-shaped errors"),
           "equal_spread": _f("boolean", "Even-sized errors")})  # fmt: skip

# ================================================================================== compare

_node("compare.models", NodeKind.COMPARE, _a(
    "Compare models side by side",
    "Puts the scores of several models and benchmarks in one table so you can see which "
    "does best on each measure.",
    ["You fitted or backtested several candidates"],
    ["Models were tested on different periods (the comparison would be unfair)"],
    ["Metrics from two or more model or benchmark nodes"],
    ["Everything was scored on the same folds"],
    ["A comparison table and a ranking per metric"],
    _B, _NOW, "ARIMAX 9% MAPE, ETS 11%, seasonal naive 14%.",
    related=["select.best", "compare.diebold_mariano"],
    fits={"goal": ["validate_model", "forecast"]},
), params={"metric": {"type": "string", "default": "rmse"},
           "direction": _enum("Better when", ["min", "max"], "min")},
   inputs={"candidates": PortSpec(type=PortType.METRICS, multiple=True)},
   outputs={"table": PortType.TABLE},
   fields={"ranking": _f("array", "Candidates best first"),
           "best": _f("string", "The best candidate (node id)")},
   runs_in="orchestrator")  # fmt: skip

_node("compare.diebold_mariano", NodeKind.COMPARE, _a(
    "Is one forecast really better?",
    "Tests whether one model's forecasts are truly more accurate than another's or whether "
    "the difference could be luck.",
    ["Two models are close and you need to know if the gap is real"],
    ["Very few test periods"],
    ["Backtest errors of two models on the same folds"],
    ["Error differences are stable over time"],
    ["Statistic, p-value and which model is better"],
    _X, _NOW, "ARIMAX beats ETS but p = 0.31: the difference may be luck.",
    related=["compare.models"],
    expert="Diebold-Mariano test with the Harvey-Leybourne-Newbold small-sample correction "
    "on squared or absolute loss.",
    refs=[("Diebold & Mariano (1995)", None)], fits={"goal": ["validate_model"]},
), params={"loss": _enum("Loss", ["squared", "absolute"], "squared"), "horizon": _int("h", 1, 1),
           "alpha": _ALPHA},
   inputs={"a": PortSpec(type=PortType.METRICS), "b": PortSpec(type=PortType.METRICS)},
   outputs={"result": PortType.TEST_RESULT}, fields=_TEST_FIELDS)  # fmt: skip

# =================================================================================== select

_node("select.best", NodeKind.SELECT, _a(
    "Pick the best model",
    "Picks the model with the best score from a comparison, and can insist that it beats "
    "the simple benchmarks.",
    ["The end of a bake-off between candidates"],
    ["Only one candidate"],
    ["A comparison table"],
    ["The chosen metric is the one that matters for the decision"],
    ["The winning model version and why it won"],
    _B, _NOW, "Pick the lowest RMSE, but only if it beats seasonal naive by 5%.",
    related=["compare.models", "baseline.beats_naive"],
    fits={"goal": ["validate_model", "forecast"]},
), params={"metric": {"type": "string", "default": "rmse"},
           "direction": _enum("Better when", ["min", "max"], "min"),
           "must_beat": {"type": "array", "items": {"type": "string"},
                         "description": "Benchmark ids the winner must beat"},
           "margin": _num("Required relative improvement over must_beat", 0.0, 0, 1)},
   inputs={"table": PortSpec(type=PortType.TABLE),
           "models": PortSpec(type=PortType.MODEL_VERSION, multiple=True, required=False)},
   outputs={"model": PortType.MODEL_VERSION},
   fields={"selected": _f("string", "The winner (node id)"),
           "selected_model": _f("string", "The winner's catalog model ref, when it is a "
                                "model node"),
           "score": _f("number", "Its score"), "beats_baselines": _f("boolean", "It beats them"),
           "reason_plain": _f("string", "Why it was picked")},
   runs_in="orchestrator")  # fmt: skip

_node("select.threshold", NodeKind.SELECT, _a(
    "Keep models that pass a bar",
    "Keeps only the models whose score passes a limit you set, for example an error below "
    "10%.",
    ["You need every model that is good enough, not just the best"],
    ["You need exactly one winner"],
    ["A comparison table"],
    ["The limit reflects what is acceptable"],
    ["The passing models"],
    _B, _NOW, "Keep every model with MAPE under 10%.",
    related=["select.best"], fits={"goal": ["validate_model"]},
), params={"metric": {"type": "string"}, "max": _num("Upper limit"), "min": _num("Lower limit")},
   required=["metric"],
   inputs={"table": PortSpec(type=PortType.TABLE)},
   outputs={"table": PortType.TABLE},
   fields={"passed": _f("array", "Passing candidates"), "n_passed": _f("integer", "How many")},
   runs_in="orchestrator")  # fmt: skip

# =============================================================================== governance

_node("governance.policy", NodeKind.GOVERNANCE, _a(
    "Run the governance checks",
    "Runs every check your organisation's policy requires on a model, such as beating "
    "random noise and a simple benchmark, and records the evidence.",
    ["Before a model may be validated and promoted"],
    ["Exploratory work that will not be used"],
    ["A model version and the data it was fitted on"],
    ["The policy matches the model's family"],
    ["Evidence for every check and an overall pass or fail"],
    _I, _LONG, "Seven checks pass, the placebo check fails: the model is not validated.",
    related=["null.permutation_target", "baseline.beats_naive", "governance.check"],
    fits={"goal": ["validate_model"]},
), params={"policy": {"type": "string", "description": "Policy id (default: the one in force)"},
           "submit": _bool("Submit the result for approval when the checks pass", False)},
   inputs={"model": PortSpec(type=PortType.MODEL_VERSION),
           "data": PortSpec(type=PortType.DATASET, required=False)},
   outputs={"evidence": PortType.EVIDENCE},
   fields={"passed": _f("boolean", "Every blocking check passed"),
           "failed_checks": _f("array", "Checks that failed"),
           "status": _f("string", "checks_passed or checks_failed")},
   runs_in="child_run", cacheable=False)  # fmt: skip

_node("governance.check", NodeKind.GOVERNANCE, _a(
    "Run one governance check",
    "Runs a single check from the governance catalog on a model, for example the shuffled "
    "result test.",
    ["Trying a check before adding it to a policy"],
    ["Formal validation (run the whole policy)"],
    ["A model version and its data"],
    ["The check applies to the model's family"],
    ["Evidence for the check and its pass, warn or fail"],
    _I, _MIN, "The noise-channel check gives the fake channel a 2% share: pass.",
    related=["governance.policy"], fits={"goal": ["validate_model"]},
), params={"check": {"type": "string", "description": "Check id (governance catalog)"},
           "params": {"type": "object", "description": "Check parameters"},
           "warn": _num("Warn threshold"), "fail": _num("Fail threshold")},
   required=["check"],
   inputs={"model": PortSpec(type=PortType.MODEL_VERSION),
           "data": PortSpec(type=PortType.DATASET, required=False)},
   outputs={"evidence": PortType.EVIDENCE},
   fields={"status": _f("string", "pass, warn, fail or error"),
           "value": _f("number", "The measured value")},
   runs_in="child_run")  # fmt: skip

# =================================================================================== report

_node("report.summary", NodeKind.REPORT, _a(
    "Plain-language report",
    "Writes a short report of what the workflow found: test verdicts, the decisions taken, "
    "the chosen model and how well it does.",
    ["At the end of any workflow you share with others"],
    ["You only need the raw numbers"],
    ["Results of earlier nodes"],
    ["Earlier nodes succeeded or were skipped for a reason"],
    ["A report with text, tables and plots"],
    _B, _NOW, "Sales wander, so they were differenced; ARIMAX(2,1,1) beat seasonal naive by 30%.",
    related=["report.evidence_pack"], fits={"goal": ["validate_model", "forecast", "check_data"]},
), params={"title": {"type": "string", "maxLength": 200},
           "sections": {"type": "array", "items": {"type": "string"},
                        "description": "Node ids to include (default: all)"}},
   inputs={"items": PortSpec(type=PortType.TABLE, multiple=True, required=False),
           "tests": PortSpec(type=PortType.TEST_RESULT, multiple=True, required=False),
           "metrics": PortSpec(type=PortType.METRICS, multiple=True, required=False),
           "model": PortSpec(type=PortType.MODEL_VERSION, required=False)},
   outputs={"report": PortType.REPORT}, runs_in="orchestrator")  # fmt: skip

_node("report.evidence_pack", NodeKind.REPORT, _a(
    "Evidence pack",
    "Bundles a model's governance evidence, decisions and approvals into one document for "
    "auditors, as HTML and JSON.",
    ["Audit, model risk review or sign-off"],
    ["Before the governance checks have run"],
    ["A model version with governance evidence"],
    ["Evidence is stored for every check"],
    ["An evidence pack (HTML and JSON)"],
    _B, _SEC, "A pack with 9 checks, 2 approvals and the lineage of model v7.",
    related=["governance.policy"], fits={"goal": ["validate_model"]},
), params={"format": _enum("Format", ["html", "json"], "html")},
   inputs={"model": PortSpec(type=PortType.MODEL_VERSION),
           "evidence": PortSpec(type=PortType.EVIDENCE, required=False)},
   outputs={"report": PortType.REPORT}, runs_in="child_run")  # fmt: skip

# ========================================================================== branch, subflow

_node("branch.if", NodeKind.BRANCH, _a(
    "Choose a path",
    "Decides which way the workflow goes from earlier results, for example 'difference the "
    "series only if the tests say it wanders'.",
    ["A step should only run under a condition"],
    ["Every step should always run"],
    ["Results of earlier nodes (and workflow parameters)"],
    ["The condition refers only to earlier nodes"],
    ["The path taken (then or else) and the values that decided it"],
    _I, _NOW,
    "nodes.adf.p_value >= params.alpha or nodes.kpss.reject decides whether to difference.",
    related=["test.adf", "transform.difference"], fits={"goal": ["forecast", "check_data"]},
    expert="Conditions use the safe expression language (mapi_spec.expr): comparisons, "
    "and/or/not, arithmetic, a few functions; no code is executed.",
), params={"condition": {"type": "string", "maxLength": 2000,
                         "description": "Condition in the workflow expression language"},
           "explain_then": {"type": "string", "maxLength": 300,
                            "description": "What it means when the condition holds, in plain "
                            "words for the run report (e.g. 'The tests found the series "
                            "wanders, so it was differenced.')"},
           "explain_else": {"type": "string", "maxLength": 300,
                            "description": "What it means when the condition does not hold, "
                            "in plain words"}},
   required=["condition"], inputs={},
   fields={"value": _f("boolean", "The condition's value"),
           "arm": _f("string", "then or else")},
   runs_in="orchestrator")  # fmt: skip

_node("subworkflow.run", NodeKind.SUBWORKFLOW, _a(
    "Run another workflow",
    "Runs a saved workflow as one step of this one, so common sequences such as data "
    "readiness checks are written once and reused.",
    ["A sequence of steps is shared by several workflows"],
    ["The steps are only used here"],
    ["A saved workflow revision and its inputs"],
    ["The other workflow's inputs and outputs match how it is wired here"],
    ["The other workflow's outputs"],
    _I, _MIN, "Reuse 'time-series readiness' before three different model workflows.",
    related=["workflow.ts_readiness_arimax"], fits={"goal": ["forecast", "validate_model"]},
), params={"workflow": {"type": "string", "pattern": r"^workflow://\S+$",
                        "description": "workflow://<project>/<name>@<revision>"},
           "params": {"type": "object", "description": "Parameters passed through"}},
   required=["workflow"], inputs={}, runs_in="child_run")  # fmt: skip


NODE_IMPLEMENTATIONS: Final[dict[str, NodeImplementation]] = {n.id: n for n in _NODES}
"""Every node implementation by id. Subworkflow ports come from the referenced workflow."""

if len(NODE_IMPLEMENTATIONS) != len(_NODES):  # pragma: no cover - authoring guard
    raise RuntimeError("duplicate node implementation ids")


def get_node(node_id: str) -> NodeImplementation | None:
    """The implementation ``node_id`` (an ``@version`` suffix is ignored), or ``None``."""
    return NODE_IMPLEMENTATIONS.get(node_id.split("@", 1)[0])


# ============================================================================ node kind cards

NODE_KIND_ABOUTS: Final[dict[NodeKind, About]] = {
    NodeKind.TEST: _a(
        "Statistical test",
        "Checks a property of the data or of a model's errors and gives "
        "a yes/no verdict with its evidence.",
        ["You need to decide something from the data (difference or not, which lags)"],
        ["You only want a chart (use the Data Explorer)"],
        ["A dataset or a model version"],
        ["Each test's own assumptions (see its card)"],
        ["Statistic, p-value, decision and a plain verdict"],
        _I,
        _SEC,
        "ADF says the series wanders.",
        related=["test.adf"],
    ),
    NodeKind.TRANSFORM: _a(
        "Data processing",
        "Changes the data before later steps use it, for example taking differences or logs.",
        ["Data must be prepared before a test or a model"],
        ["The data is ready as it is"],
        ["A dataset"],
        ["Transforms are undone on forecasts where needed"],
        ["A new dataset"],
        _B,
        _NOW,
        "Difference sales once.",
        related=["transform.difference"],
    ),
    NodeKind.BRANCH: _a(
        "Decision",
        "Chooses which path the workflow takes from earlier results.",
        ["A step should only run when a condition holds"],
        ["Everything always runs"],
        ["Results of earlier nodes"],
        ["Conditions only read earlier results"],
        ["The path taken"],
        _I,
        _NOW,
        "Difference only when the series wanders.",
        related=["branch.if"],
    ),
    NodeKind.SPLIT: _a(
        "Train/test plan",
        "Decides which periods or groups the model learns from and which "
        "it is tested on, possibly many times over.",
        ["Every model you want to test honestly"],
        ["You do not test the model"],
        ["A dataset"],
        ["Test periods resemble how the model will be used"],
        ["A split plan"],
        _I,
        _NOW,
        "Six expanding-window folds.",
        related=["split.expanding_window"],
    ),
    NodeKind.SEARCH: _a(
        "Search",
        "Tries many settings and picks the best by a score.",
        ["Choosing orders or settings from the data"],
        ["Settings are fixed in advance"],
        ["A dataset (and a split plan for backtest searches)"],
        ["The best setting is in the search range"],
        ["Best settings and a table of scores"],
        _I,
        _SEC,
        "Find ARMA orders by AIC.",
        related=["search.arma_order"],
    ),
    NodeKind.MODEL: _a(
        "Model",
        "Fits, backtests or forecasts with any model from the catalog.",
        ["The workflow should produce a model or a forecast"],
        ["Only data checks"],
        ["A dataset and model settings"],
        ["The model's own assumptions"],
        ["Model versions, metrics, forecasts"],
        _B,
        _MIN,
        "Fit an ARIMAX model.",
        related=["model.fit"],
    ),
    NodeKind.EVALUATE: _a(
        "Evaluation",
        "Measures how good a model or a simple benchmark is on test data.",
        ["After fitting or backtesting"],
        ["Nothing was held out"],
        ["Model versions or backtests"],
        ["Test data resembles real use"],
        ["Metrics"],
        _B,
        _SEC,
        "Score the seasonal-naive benchmark.",
        related=["evaluate.metrics"],
    ),
    NodeKind.COMPARE: _a(
        "Comparison",
        "Puts several candidates' scores side by side, or tests whether one is truly better.",
        ["Several candidate models"],
        ["One candidate"],
        ["Metrics of the candidates"],
        ["Same folds for every candidate"],
        ["A comparison table or a test"],
        _B,
        _NOW,
        "ARIMAX vs ETS vs naive.",
        related=["compare.models"],
    ),
    NodeKind.SELECT: _a(
        "Selection",
        "Picks the winner, or every candidate that passes a bar.",
        ["The end of a bake-off"],
        ["One candidate"],
        ["A comparison table"],
        ["The metric reflects the decision"],
        ["The selected model(s)"],
        _B,
        _NOW,
        "Lowest RMSE wins.",
        related=["select.best"],
    ),
    NodeKind.GOVERNANCE: _a(
        "Governance",
        "Runs the checks a model must pass before it can be validated, and stores the evidence.",
        ["Before validating or promoting a model"],
        ["Throw-away experiments"],
        ["A model version and its data"],
        ["A policy covers the model's family"],
        ["Evidence and a pass or fail"],
        _I,
        _LONG,
        "Run the tenant policy.",
        related=["governance.policy"],
    ),
    NodeKind.REPORT: _a(
        "Report",
        "Turns the workflow's results into a readable report or an evidence pack.",
        ["Sharing results"],
        ["Nobody reads the result"],
        ["Earlier results"],
        ["Earlier nodes finished"],
        ["A report"],
        _B,
        _NOW,
        "A one-page summary.",
        related=["report.summary"],
    ),
    NodeKind.SUBWORKFLOW: _a(
        "Sub-workflow",
        "Runs another saved workflow as one step.",
        ["Reusing a common sequence"],
        ["One-off steps"],
        ["A saved workflow revision"],
        ["Inputs and outputs match"],
        ["The other workflow's outputs"],
        _I,
        _MIN,
        "Reuse the readiness checks.",
        related=["subworkflow.run"],
    ),
}
"""Cards of the node kinds (``GET /v1/catalog/workflow-nodes`` groups by them)."""
