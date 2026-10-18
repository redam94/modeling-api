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
model          ``model.fit`` (any catalog model id), ``model.backtest``, ``model.forecast``,
               ``model.summary``
evaluate       ``evaluate.metrics``, ``evaluate.baseline``, ``evaluate.residuals``,
               ``evaluate.ppc``
compare        ``compare.models``, ``compare.diebold_mariano``, ``compare.stacking``
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
    "The series: variable names, or labels along the series dimension of the data (if "
    "empty, all the series of the target)"
)


_ALPHA = _num("The significance level", 0.05, 0, 0.5, exclusive=True)
_PER_SERIES = _bool("For panels, test every series (geo, product) separately", True)
_DATA_IN = {"data": PortSpec(type=PortType.DATASET, description="The data to use")}


def _f(t: ResultType, d: str) -> ResultField:
    return ResultField(type=t, description=d)


_TEST_FIELDS: Final[dict[str, ResultField]] = {
    "statistic": _f("number", "The test statistic"),
    "p_value": _f("number", "The p-value (for panels, the median of all series)"),
    "alpha": _f("number", "The significance level that the test used"),
    "reject": _f("boolean", "True if the test rejected the null hypothesis at alpha"),
    "decision": _f("string", "The decision label (see the About card of the test)"),
    "verdict_plain": _f("string", "The verdict in plain words"),
    "critical_values": _f("object", "The critical values for each level ('1%', '5%', '10%')"),
    "n_obs": _f("integer", "The number of observations that the test used"),
    "lags": _f("integer", "The number of lags that the test used"),
    "details": _f("object", "Fields that are specific to the test"),
    "per_series": _f("array", "The results for each series of a panel"),
    "reject_share": _f("number", "The share of series where the test rejected the null hypothesis"),
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
    "Tests whether a series returns to a stable level or wanders away. The result tells "
    "you whether to apply a difference before you forecast.",
    ["Before you fit a forecast model that needs a stable series",
     "With the KPSS test, which examines the same question from the opposite side"],
    ["The series is very short (less than about 30 periods)",
     "The series has one clear break (use Zivot-Andrews)"],
    ["One numeric series over time (or a panel, with a test for each series)"],
    ["Regular time steps", "The trend option that you select describes the trend"],
    ["The statistic, the p-value and a plain verdict",
     "The results for each series of a panel"],
    _I, _SEC,
    "Weekly sales give p = 0.57. The series wanders, so apply one difference.",
    related=["test.kpss", "test.pp", "transform.difference"],
    expert="Augmented Dickey-Fuller (statsmodels adfuller); H0: unit root. Lag length by "
    "AIC/BIC/t-stat; regression 'c', 'ct', 'ctt' or 'n'; MacKinnon p-values.",
    refs=[_SM, ("Dickey & Fuller (1979)", None)], fits=_TESTS_FIT,
), {"var": _var(), "regression": _enum("Deterministic terms", ["c", "ct", "ctt", "n"], "c"),
    "autolag": _enum("Lag selection", ["AIC", "BIC", "t-stat", "none"], "AIC"),
    "maxlag": _int("The largest lag to try (default: Schwert's rule)", None, 0, 100),
    "per_series": _PER_SERIES})  # fmt: skip

_test("test.kpss", _a(
    "Is the series stable? (KPSS)",
    "Tests whether a series stays near a stable level or trend. It is the opposite of the "
    "ADF test, so the two tests together give a better answer.",
    ["With ADF, to confirm whether the series needs a difference"],
    ["The series is very short"],
    ["One numeric series over time"],
    ["Regular time steps"],
    ["The statistic, the p-value and a plain verdict"],
    _I, _SEC,
    "KPSS rejects stability (p = 0.01) and ADF cannot reject a wandering series. Apply a "
    "difference.",
    related=["test.adf", "transform.difference"],
    expert="KPSS (statsmodels kpss); H0: level (or trend) stationarity; p-values are "
    "interpolated within [0.01, 0.1] and clipped outside.",
    refs=[_SM, ("Kwiatkowski, Phillips, Schmidt & Shin (1992)", None)], fits=_TESTS_FIT,
), {"var": _var(), "regression": _enum("Stable around a level or a trend", ["c", "ct"], "c"),
    "nlags": _enum("Bandwidth rule", ["auto", "legacy"], "auto"),
    "per_series": _PER_SERIES})  # fmt: skip

_test("test.pp", _a(
    "Does the series wander? (Phillips-Perron)",
    "Tests whether a series wanders, as ADF does. It gives better results when the errors "
    "are uneven or related over time.",
    ["You want a second test after ADF on a noisy series"],
    ["The series is very short"],
    ["One numeric series over time"],
    ["Regular time steps"],
    ["The statistic, the p-value and a plain verdict"],
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
    "Tests whether a series that seems to wander is stable with one sudden jump. For "
    "example, a price change or a new product can cause a jump.",
    ["The series has one clear shift"],
    ["There are several breaks", "The series is short"],
    ["One numeric series over time (at least about 50 periods)"],
    ["At most one break, at an unknown date"],
    ["The statistic, the p-value, the likely date of the break and a plain verdict"],
    _X, _SEC,
    "Sales jump after a relaunch in March 2023. If the test permits that break, the "
    "series is stable.",
    related=["test.adf", "test.chow"],
    expert="Zivot-Andrews (statsmodels zivot_andrews); H0: unit root without a break; "
    "break in intercept, trend or both.",
    refs=[_SM, ("Zivot & Andrews (1992)", None)], fits=_TESTS_FIT,
), {"var": _var(), "regression": _enum("Break in", ["c", "t", "ct"], "c"),
    "trim": _num("The share at each end that cannot be a break date", 0.15, 0.05, 0.3),
    "per_series": _PER_SERIES},
    {"break_index": _f("integer", "The position of the most likely break"),
     "break_at": _f("string", "The date of the most likely break")})  # fmt: skip

_test("test.johansen", _a(
    "Are the series tied together? (Johansen)",
    "Tests whether several wandering series stay tied together in the long run. It also "
    "finds the number of these ties.",
    ["Before you make one model of several series that have a trend"],
    ["The series do not wander (they need no difference)", "Only one series"],
    ["Two or more numeric series over time"],
    ["Each series wanders on its own", "Enough history (50 or more periods)"],
    ["The number of long-run ties (rank) and the test table"],
    _X, _SEC,
    "The prices of two close competitors share one long-run tie (rank 1).",
    related=["ts.vecm", "search.coint_rank", "test.engle_granger"],
    expert="Johansen trace and maximum-eigenvalue tests (statsmodels coint_johansen); rank "
    "from sequential tests at the critical values for the chosen level.",
    refs=[_SM, ("Johansen (1991)", None)],
    fits={"goal": ["compare_series"], "data_shape": ["several_series"]},
), {"vars": _SYSTEM_VARS,
    "det_order": _int("Deterministic terms: -1 none, 0 constant, 1 trend", 0, -1, 1),
    "k_ar_diff": _int("Lagged differences", 1, 0, 24),
    "method": _enum("Test statistic", ["trace", "maxeig"], "trace"),
    "alpha": _enum("The level for the rank (critical values are available at 10%, 5% and 1%)",
                   ["0.1", "0.05", "0.01"], "0.05")},
    {"rank": _f("integer", "The selected cointegration rank"),
     "trace_stats": _f("array", "The trace statistics for each rank"),
     "max_eig_stats": _f("array", "The maximum-eigenvalue statistics for each rank")},)  # fmt: skip

_test("test.engle_granger", _a(
    "Are two series tied together? (Engle-Granger)",
    "Tests whether two wandering series stay tied together in the long run, so that the "
    "gap between them closes again.",
    ["Before you make an error-correction model of one result and one driver"],
    ["More than two series (use Johansen)"],
    ["Two numeric series over time"],
    ["Both series wander", "A maximum of one long-run relationship"],
    ["The statistic, the p-value and a plain verdict"],
    _X, _SEC,
    "Your price and the price of a competitor are tied (p = 0.02).",
    related=["test.johansen", "ts.ecm"],
    expert="Engle-Granger two-step test (statsmodels coint); H0: no cointegration.",
    refs=[_SM, ("Engle & Granger (1987)", None)],
    fits={"goal": ["compare_series"], "data_shape": ["several_series"]},
), {"y": _var("The first series"), "x": _var("The second series"),
    "trend": _enum("Deterministic terms", ["c", "ct", "ctt", "n"], "c")},
    required=["y", "x"])  # fmt: skip

_RESID_IN = {
    "data": PortSpec(type=PortType.DATASET, required=False,
                     description="A series to test (if you do not give a model)"),
    "model": PortSpec(type=PortType.MODEL_VERSION, required=False,
                      description="Test the residuals of the model instead"),
}  # fmt: skip

_test("test.ljung_box", _a(
    "Is there a pattern left? (Ljung-Box)",
    "Tests whether the errors of a model are still related to their own past. If they are, "
    "the model does not find all of the structure.",
    ["On the errors of a forecast model, after you fit it"],
    ["On the raw series (use ACF/PACF to look for structure)"],
    ["The errors of a model (residuals) or a series"],
    ["Regular time steps"],
    ["The statistic and the p-value for each lag, and a plain verdict"],
    _I, _SEC,
    "The errors of an ARIMA model show no pattern up to lag 10 (p = 0.42). This is good.",
    related=["test.acf_pacf", "evaluate.residuals", "residuals.whiteness"],
    expert="Ljung-Box Q (statsmodels acorr_ljungbox); H0: no autocorrelation up to the lag; "
    "model_df adjusts the degrees of freedom for fitted ARMA terms.",
    refs=[_SM, ("Ljung & Box (1978)", None)], fits={"goal": ["validate_model", "forecast"]},
), {"var": _var("The series to test (default: the residuals of the model input)"),
    "lags": _int("The number of lags to test", 10, 1, 200),
    "model_df": _int("The number of fitted ARMA terms to subtract (default: from the model)",
                     None, 0, 50)},
    inputs=_RESID_IN)  # fmt: skip

_test("test.jarque_bera", _a(
    "Bell-shaped errors? (Jarque-Bera)",
    "Tests whether values, usually the errors of a model, follow a bell curve. If not, the "
    "ranges that use this assumption can be wrong.",
    ["You want to test the errors of a model before you trust its ranges"],
    ["Small samples (the test is not reliable with less than about 50 values)"],
    ["The errors of a model (residuals) or a series"],
    ["The values are independent"],
    ["The statistic, the p-value, the skewness and the tail weight"],
    _I, _NOW,
    "The errors are skewed (p < 0.001). A small number of misses are very large.",
    related=["evaluate.residuals"],
    expert="Jarque-Bera on skewness and kurtosis (statsmodels jarque_bera); H0: normality.",
    refs=[_SM], fits={"goal": ["validate_model"]},
), {"var": _var("The series to test (default: the residuals of the model input)")},
    {"skew": _f("number", "Skewness"), "kurtosis": _f("number", "Kurtosis")},
    inputs=_RESID_IN)  # fmt: skip

_test("test.breusch_pagan", _a(
    "Even-sized errors? (Breusch-Pagan)",
    "Tests whether the errors of a model are larger at some times or for some inputs. Then "
    "the ranges of the model are too narrow in some places.",
    ["You want to test the errors of a regression model"],
    ["Models without inputs"],
    ["A fitted model with inputs"],
    ["The spread of the errors changes linearly with the inputs"],
    ["The statistic, the p-value and a plain verdict"],
    _X, _NOW,
    "The errors increase with spend (p = 0.003). The ranges are too narrow for weeks with "
    "a large spend.",
    related=["test.white", "evaluate.residuals"],
    expert="Breusch-Pagan LM test (statsmodels het_breuschpagan); H0: homoskedasticity.",
    refs=[_SM], fits={"goal": ["validate_model"]},
), {"robust": _bool("Use the studentised version of Koenker", True)},
    inputs={"model": PortSpec(type=PortType.MODEL_VERSION)})  # fmt: skip

_test("test.white", _a(
    "Even-sized errors? (White)",
    "Tests whether the errors of a model are larger in some places. It is a more general "
    "test than Breusch-Pagan.",
    ["You think that the size of the errors has a curved relation to the inputs"],
    ["Models with many inputs and little data (the test uses all the combinations of the "
     "inputs)"],
    ["A fitted model with inputs"],
    ["Enough observations for the squared and cross terms"],
    ["The statistic, the p-value and a plain verdict"],
    _X, _NOW,
    "The White test also finds uneven errors (p = 0.01).",
    related=["test.breusch_pagan"],
    expert="White's test (statsmodels het_white); H0: homoskedasticity.",
    refs=[_SM], fits={"goal": ["validate_model"]},
), inputs={"model": PortSpec(type=PortType.MODEL_VERSION)})  # fmt: skip

_test("test.arch_lm", _a(
    "Do volatile periods cluster? (ARCH-LM)",
    "Tests whether calm periods and turbulent periods occur in groups. If they do, the "
    "ranges must become wider after a shock.",
    ["Financial or demand series with bursts of volatility"],
    ["Series without visible bursts"],
    ["The errors of a model (residuals) or a series"],
    ["Regular time steps"],
    ["The statistic, the p-value and a plain verdict"],
    _X, _NOW,
    "The errors after promotions are larger for several weeks (p = 0.02).",
    related=["test.ljung_box"],
    expert="Engle's ARCH LM test (statsmodels het_arch); H0: no ARCH effects.",
    refs=[_SM, ("Engle (1982)", None)], fits={"goal": ["validate_model"]},
), {"var": _var("The series to test (default: the residuals of the model input)"),
    "nlags": _int("Lags", 4, 1, 50)}, inputs=_RESID_IN)  # fmt: skip

_test("test.granger", _a(
    "Does one series help predict another? (Granger)",
    "Tests whether the past values of a driver help to predict a result better than the "
    "past values of the result alone.",
    ["You must decide which series to include in a VAR",
     "You want to make sure that a leading indicator really leads"],
    ["You want to prove cause and effect (the test shows only help with the prediction)"],
    ["Two numeric series over time"],
    ["Both series are stable (if not, apply a difference first)"],
    ["A p-value that is corrected for the number of lags that the test tried",
     "The p-value for each lag"],
    _I, _SEC,
    "Search interest helps to predict sales two weeks later (p = 0.004).",
    related=["ts.var", "test.adf"],
    expert="Granger causality F-tests for lags 1..maxlag (statsmodels "
    "grangercausalitytests, ssr F test); H0: the driver's lags have zero coefficients. "
    "The verdict uses the smallest p-value Holm-corrected for the maxlag lags tried (an "
    "uncorrected minimum about doubles the false-positive rate), or the F test at one "
    "prespecified `lag`; the per-lag p-values are descriptive.",
    refs=[_SM, ("Granger (1969)", None),
          ("Holm (1979), A simple sequentially rejective multiple test procedure", None)],
    fits={"goal": ["compare_series", "explain_drivers"], "data_shape": ["several_series"]},
), {"y": _var("The result"), "x": _var("The candidate driver"),
    "maxlag": _int("The largest lag to test", 4, 1, 52),
    "lag": _int("Test only this lag (no correction is necessary)", None, 1, 52)},
    {"min_p_value": _f("number", "The smallest uncorrected p-value of all the lags"),
     "best_lag": _f("integer", "The lag with the smallest p-value"),
     "p_values": _f("object", "The uncorrected p-value for each lag (descriptive)"),
     "correction": _f("string", "holm (lags 1..maxlag) or none (one lag)")},
    required=["y", "x"])  # fmt: skip

_test("test.acf_pacf", _a(
    "How far back does the past matter? (ACF/PACF)",
    "Measures how strongly each past period is related to the present. It also suggests "
    "the number of past values for a forecast model.",
    ["You must select the orders of an ARIMA model",
     "You want a quick look at the seasonality"],
    ["The series still wanders (apply a difference first)"],
    ["One stable numeric series over time"],
    ["Regular time steps"],
    ["The significant lags, the suggested AR and MA orders and the correlation values"],
    _I, _SEC,
    "The partial correlations stop after lag 2 and the plain correlations decrease slowly. "
    "Try an AR(2).",
    related=["search.arma_order", "test.ljung_box"],
    expert="Sample ACF/PACF (statsmodels acf, pacf with Yule-Walker-MLE) with Bartlett / "
    "1.96/sqrt(n) bands; suggested p = last significant PACF lag before the cut-off, q from "
    "the ACF.",
    refs=[_SM, ("Box, Jenkins, Reinsel & Ljung, Time Series Analysis", None)],
    fits=_TESTS_FIT,
), {"var": _var(), "nlags": _int("The number of lags to calculate", 24, 1, 200),
    "max_order": _int("The largest order to suggest", 5, 0, 24)},
    {"significant_acf_lags": _f("array", "The lags that have a significant autocorrelation"),
     "significant_pacf_lags": _f("array", "The lags that have a significant partial "
                                    "autocorrelation"),
     "suggested_p": _f("integer", "The suggested AR order"),
     "suggested_q": _f("integer", "The suggested MA order"),
     "acf": _f("array", "Autocorrelations"),
     "pacf": _f("array", "Partial autocorrelations")})  # fmt: skip

_test("test.seasonal_strength", _a(
    "How strong is the season?",
    "Measures how much of the changes in a series come from a seasonal pattern that "
    "repeats. The scale is from 0 (none) to 1 (all).",
    ["You must decide whether to model the seasonality or to apply a seasonal difference"],
    ["Less than two full seasons of data"],
    ["One numeric series over time and the season length"],
    ["The season has a fixed length"],
    ["The seasonal strength, the trend strength and a plain verdict"],
    _B, _SEC,
    "Weekly ice-cream sales have a seasonal strength of 0.85. Model the season.",
    related=["transform.seasonal_difference", "core.fourier"],
    expert="STL decomposition; F_s = max(0, 1 - Var(R) / Var(S + R)) (Wang, Smith & "
    "Hyndman); 'seasonal' when F_s >= threshold.",
    refs=[("Hyndman & Athanasopoulos, FPP3 section 4.3", "https://otexts.com/fpp3/")],
    fits=_TESTS_FIT,
), {"var": _var(), "period": _int("The season length in periods (default: from the frequency)",
                                   None, 2, 400),
    "threshold": _num("The strength that the test counts as seasonal", 0.64, 0, 1)},
    {"strength": _f("number", "The seasonal strength, from 0 to 1"),
     "trend_strength": _f("number", "The trend strength, from 0 to 1"),
     "period": _f("integer", "The season length that the test used"),
     "seasonal": _f("boolean", "True if the strength is at or above the threshold")})  # fmt: skip

_test("test.vif", _a(
    "Are drivers too alike? (VIF)",
    "Tests whether some drivers change almost together. Then it is difficult to find "
    "their separate effects.",
    ["Before a regression or marketing mix model with many drivers"],
    ["You only predict and the separate effects are not important"],
    ["The driver columns"],
    ["The effects are approximately linear"],
    ["The VIF for each driver, the largest VIF and the drivers above the limit"],
    _I, _NOW,
    "TV spend and radio spend have a VIF of 14. You cannot reliably separate their effects.",
    related=["mmm.bayesian", "plausibility.signs"],
    expert="Variance inflation factor 1/(1 - R_j^2) per regressor (statsmodels "
    "variance_inflation_factor) with a constant added.",
    refs=[_SM], fits={"goal": ["check_data", "explain_marketing", "explain_drivers"]},
), {"vars": _vars("The drivers"), "limit": _num("The VIF limit for a driver", 10, 1)},
    {"max_vif": _f("number", "The largest VIF"),
     "table": _f("object", "The VIF for each driver"),
     "high_vif_vars": _f("array", "The drivers above the limit")},
    required=["vars"])  # fmt: skip

_test("test.cusum", _a(
    "Did the relationship change? (CUSUM)",
    "Tests whether the relationship between the result and the drivers of a model stayed "
    "the same over time. You do not have to guess when it changed.",
    ["You think that something changed, but you do not know when"],
    ["You know the date (use the Chow test)"],
    ["A fitted regression model over time"],
    ["The errors are independent"],
    ["Whether the statistic went across the stability bounds, and where"],
    _X, _NOW,
    "The cumulative errors go across the bound in 2022. The relationship changed.",
    related=["test.chow", "stability.rolling_params"],
    expert="OLS-residual CUSUM test (statsmodels breaks_cusumolsresid); H0: parameter "
    "stability.",
    refs=[_SM, ("Ploberger & Kramer (1992)", None)], fits={"goal": ["validate_model"]},
), {}, {"break_detected": _f("boolean", "True if the statistic went across the bounds"),
        "break_at": _f("string", "The position of the peak of the statistic")},
    inputs={"model": PortSpec(type=PortType.MODEL_VERSION)})  # fmt: skip

_test("test.chow", _a(
    "Did it change on this date? (Chow)",
    "Tests whether the relationship between a result and its drivers is different before "
    "and after a date that you select.",
    ["You know a date when something changed (a relaunch, a new competitor)"],
    ["You do not know the date (use CUSUM)"],
    ["A result, its drivers and a candidate date"],
    ["The errors have the same spread before and after the date"],
    ["The statistic, the p-value and a plain verdict"],
    _I, _NOW,
    "The price effect is different after the 2023 relaunch (p = 0.01).",
    related=["test.cusum", "test.zivot_andrews"],
    expert="Chow F test comparing pooled and split OLS fits at the break point.",
    refs=[("Chow (1960)", None)], fits={"goal": ["validate_model", "explain_drivers"]},
), {"y": _var("The result"), "x": _vars("The drivers"),
    "break_at": {"type": ["string", "integer"],
                 "description": "The date or position of the break"}},
    required=["y", "x", "break_at"])  # fmt: skip

# =============================================================================== transforms

_DATA_OUT = {"data": PortType.DATASET}
_TRANSFORM_FIT = {"goal": ["check_data", "forecast"]}


def _transform(id: str, about: About, params: Mapping[str, dict[str, Any]],
               required: Sequence[str] = (), fields: Mapping[str, ResultField] | None = None
               ) -> None:  # fmt: skip
    _node(id, NodeKind.TRANSFORM, about, params=params, required=required, outputs=_DATA_OUT,
          fields={"vars": _f("array", "The variables that the node wrote"),
                  **(fields or {})})  # fmt: skip


_transform("transform.difference", _a(
    "Change from the previous period",
    "Replaces each value with its change from the previous period. This changes a "
    "wandering series into a stable series.",
    ["A unit-root test shows that the series wanders"],
    ["The series is already stable (a difference adds noise)"],
    ["A numeric series over time"],
    ["You lose the first periods (one for each difference)"],
    ["The series after the difference (the forecasts go back to the original scale "
     "automatically)"],
    _I, _NOW,
    "Sales of 100, 104, 103 become changes of +4 and -1.",
    related=_UNIT_ROOT_REL, fits=_TRANSFORM_FIT,
    expert="First differences of order d along the time dim, per series; the last d levels "
    "are kept as state to integrate forecasts.",
), {"vars": _vars("The variables for the difference (default: the target)"),
    "order": _int("The number of differences", 1, 1, 2),
    "dim": {**_NAME, "default": "time"}})  # fmt: skip

_transform("transform.seasonal_difference", _a(
    "Change from the same period last season",
    "Replaces each value with its change from the same period one season earlier. This "
    "removes a strong pattern that repeats.",
    ["Strong seasonality that you do not want a seasonal model to learn"],
    ["Less than two seasons of history"],
    ["A numeric series over time and the season length"],
    ["You lose the first season of data"],
    ["The series after the seasonal difference"],
    _I, _NOW,
    "The sales of week 53 minus the sales of week 1 one year earlier.",
    related=["test.seasonal_strength", "transform.difference"], fits=_TRANSFORM_FIT,
), {"vars": _vars("The variables for the difference (default: the target)"),
    "period": _int("The season length in periods", None, 2, 400),
    "order": _int("The number of differences", 1, 1, 2), "dim": {**_NAME, "default": "time"}},
    required=["period"])  # fmt: skip

_transform("transform.log", _a(
    "Logarithm",
    "Calculates the logarithm of positive values. This changes growth into steady steps, "
    "and you can compare large series with small series.",
    ["The values increase by percentages", "The spread increases with the level"],
    ["The values can be zero or negative"],
    ["Positive numeric columns"],
    ["All values are positive"],
    ["The same columns on a log scale"],
    _B, _NOW,
    "Sales of 10, 100 and 1000 become 2.3, 4.6 and 6.9.",
    related=["transform.box_cox", "core.log1p"], fits=_TRANSFORM_FIT,
), {"vars": _vars("The variables (default: the target)")})  # fmt: skip

_transform("transform.box_cox", _a(
    "Stabilise the spread",
    "Bends a positive series so that its changes have about the same size at high and low "
    "levels. It finds the best bend from the data.",
    ["The spread increases with the level and a log is too strong or too weak"],
    ["The values can be zero or negative"],
    ["Positive numeric columns"],
    ["All values are positive"],
    ["The transformed columns and the selected bend (lambda)"],
    _I, _NOW,
    "Lambda 0.3 makes the spread of monthly sales approximately constant.",
    related=["transform.log"], fits=_TRANSFORM_FIT,
    expert="Box-Cox with lambda by maximum likelihood (scipy.stats.boxcox) or fixed.",
), {"vars": _vars("The variables (default: the target)"),
    "lmbda": _num("A fixed lambda (default: an estimate from the data)", None, -2, 2)},
    fields={"lmbda": _f("number", "The lambda that the node used")})  # fmt: skip

_transform("transform.steps", _a(
    "Preprocessing steps",
    "Runs catalog preprocessing steps on the data. For example, the steps can make weekly "
    "totals, scale values or add calendar features.",
    ["The data needs the same preparation as a model spec gives"],
    ["A model node runs the same steps (put them in its pipeline)"],
    ["A dataset"],
    ["Stateful steps learn from the data that you give here"],
    ["The prepared dataset"],
    _I, _SEC,
    "Weekly totals, then a log of spend, before the tests.",
    related=["core.resample", "core.log1p"], fits=_TRANSFORM_FIT,
), {"steps": {"type": "array", "minItems": 1, "maxItems": 64,
              "description": "Pipeline steps, as in a ModelSpec ({id, use, with})",
              "items": {"type": "object", "required": ["id", "use"],
                        "properties": {"id": {"type": "string"}, "use": {"type": "string"},
                                       "with": {"type": "object"}}}}},
    required=["steps"])  # fmt: skip

_transform("transform.select", _a(
    "Keep some variables or dates",
    "Keeps only the variables, markets or dates that you select. For example, you can test "
    "one series or a recent period.",
    ["Later nodes must see only part of the data"],
    ["The workflow needs all of the data"],
    ["A dataset"],
    ["Later nodes do not need the data that this node removes"],
    ["A smaller dataset"],
    _B, _NOW,
    "Keep only y and price for the market DE.",
    related=["core.filter_coords"], fits=_TRANSFORM_FIT,
), {"vars": _vars("The variables to keep"),
    "sel": {"type": "object", "description": "dim -> labels to keep",
            "additionalProperties": {"type": "array", "maxItems": 10000}},
    "start": {"type": ["string", "integer"], "description": "The first time label to keep"},
    "end": {"type": ["string", "integer"],
            "description": "The last time label to keep"}})  # fmt: skip

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
    "n_folds": _f("integer", "The number of folds"),
    "folds": _f("array", "The fold windows (positions) or groups"),
}


def _split(id: str, model: type[Any], about: About) -> None:
    _node(id, NodeKind.SPLIT, about, schema=_plan_schema(model), outputs=_SPLIT_OUT,
          fields=_SPLIT_FIELDS)  # fmt: skip


_split("split.holdout", HoldoutPlan, _a(
    "One test period",
    "Keeps one part of the data, usually the latest periods, to test the model.",
    ["You want one quick, honest test"],
    ["You want to know how stable the error is (use a rolling backtest)"],
    ["A dataset with an ordered or group dimension"],
    ["The part that you keep for the test is similar to the future that is important to you"],
    ["A plan with one fold"],
    _B, _NOW, "Hold out the last 8 weeks.",
    related=["core.time_holdout", "split.expanding_window"], fits=_SPLIT_FIT,
))  # fmt: skip
_split("split.rolling_origin", RollingOriginPlan, _a(
    "Rolling backtest (fixed window)",
    "Repeats the test at several points in time. Each time, the model trains on the same "
    "length of recent history, so you can see if the error stays the same.",
    ["Old history is possibly not relevant now",
     "You want errors at several forecast horizons"],
    ["Little history (each fold needs a full window)"],
    ["A dataset with a time dimension"],
    ["A window with a fixed length is enough for the model to learn"],
    ["A plan with several folds and horizons"],
    _I, _NOW,
    "Train on 104 weeks and test 1, 4 and 8 weeks ahead. Move 4 weeks and do this 10 "
    "times.",
    related=["split.expanding_window", "model.backtest"], fits=_SPLIT_FIT,
))  # fmt: skip
_split("split.expanding_window", ExpandingWindowPlan, _a(
    "Rolling backtest (growing history)",
    "Repeats the test at several points in time. Each time, the model trains on all the "
    "history up to that point, as in a real forecast.",
    ["You forecast regularly and want a realistic estimate of the error",
     "You need a gap between the training data and the test data because the data comes "
     "late"],
    ["Very short series"],
    ["A dataset with a time dimension"],
    ["More history is never a problem"],
    ["A plan with several folds and horizons"],
    _I, _NOW,
    "Six origins 4 weeks apart, a 1-week gap, horizons 1 to 8.",
    related=["split.rolling_origin", "core.as_of", "model.backtest"], fits=_SPLIT_FIT,
))  # fmt: skip
_split("split.blocked_kfold", BlockedKFoldPlan, _a(
    "Blocked cross-validation",
    "Cuts the time line into some blocks and uses each block one time as the test. A "
    "buffer around each block stops data leaks from adjacent periods.",
    ["Models that explain, where the future is not the only important thing",
     "Short series, where rolling backtests leave too little data"],
    ["Forecasts only (a model that trains on the future is not realistic)"],
    ["A dataset with a time dimension"],
    ["The buffer is long enough, so that adjacent periods are not related"],
    ["A plan with k folds"],
    _X, _NOW, "Five blocks with a 4-week buffer on each side.",
    related=["split.purged_kfold"], fits={"goal": ["validate_model", "explain_drivers"]},
    refs=[("Bergmeir & Benitez (2012), On the use of cross-validation for time series "
           "predictor evaluation", None)],
))  # fmt: skip
_split("split.purged_kfold", PurgedKFoldPlan, _a(
    "Purged cross-validation",
    "Works like blocked cross-validation, but removes the periods immediately before and "
    "after each test block. Use it for results that cover several periods.",
    ["The results come from windows that overlap the test block"],
    ["Plain forecasts"],
    ["A dataset with a time dimension"],
    ["Purge and embargo cover the overlap"],
    ["A plan with k folds"],
    _X, _NOW, "Five blocks, purge 2 periods before and embargo 4 after each test block.",
    related=["split.blocked_kfold"], fits={"goal": ["validate_model"]},
    refs=[("Lopez de Prado (2018), Advances in Financial Machine Learning, ch. 7", None)],
))  # fmt: skip
_split("split.geo_holdout", GeoHoldoutPlan, _a(
    "Hold out markets",
    "Keeps whole markets or stores out of the training data and tests the model on them. "
    "You can also test only their latest periods.",
    ["You will apply the model to new markets",
     "You want to read the result of a geo experiment"],
    ["You only predict for markets that the model knows"],
    ["A dataset with a group dimension, for example geo"],
    ["The held-out markets are similar to the other markets"],
    ["A plan with one or more folds of held-out groups"],
    _I, _NOW, "Hold out 2 random markets, 5 times with different draws.",
    related=["core.leave_group_out"], fits={"goal": ["validate_model", "explain_marketing"],
                                             "data_shape": ["panel"]},
))  # fmt: skip
_split("split.nested", NestedPlan, _a(
    "Tuning inside a backtest",
    "Runs a second, inner backtest in each training window, so that you can tune settings "
    "without data from the outer test periods. The search.grid node tunes on the inner "
    "folds and scores on the outer folds.",
    ["You tune settings (orders, penalties) and want an honest final error"],
    ["You do not tune settings"],
    ["A dataset with a time dimension and enough history for both levels"],
    ["The inner folds fit in each outer training window"],
    ["A plan with inner folds in each fold"],
    _X, _NOW, "Outer: expanding window, 4 origins. Inner: expanding window, 3 origins.",
    related=["search.grid", "split.expanding_window"], fits={"goal": ["validate_model"]},
))  # fmt: skip

# =================================================================================== search

_SEARCH_FIT = {"goal": ["forecast"], "data_shape": ["one_series", "several_series"]}
_IC = _enum("Information criterion", ["aic", "bic", "hqic", "aicc"], "aic")

_node("search.arma_order", NodeKind.SEARCH, _a(
    "Find the AR and MA orders",
    "Tries a grid of past-value (AR) and past-error (MA) orders for a forecast model. It "
    "selects the order with the best balance of fit and simplicity.",
    ["Before you fit an ARIMA or ARIMAX model"],
    ["The series still wanders (apply a difference first)", "Very short series"],
    ["One stable numeric series over time (optional drivers)"],
    ["The best order is in the grid"],
    ["The best orders and the full grid of scores"],
    _I, _SEC,
    "Out of p = 0..4 and q = 0..4, ARMA(2,1) has the lowest AIC.",
    related=["test.acf_pacf", "ts.sarimax", "search.sarima_order"],
    expert="Grid search of SARIMAX(p,d,q)(0,D,0)s fits by AIC/BIC/HQIC/AICc (statsmodels "
    "arma_order_select_ic style), with the differencing of the model that will be fitted; "
    "failed fits are skipped and reported. With a split plan, only the periods before its "
    "first test window are searched, so a backtest on the plan stays out of sample.",
    refs=[_SM], fits=_SEARCH_FIT,
), params={"var": _var("The series (default: the target)"),
           "max_p": _int("Largest AR order", 4, 0, 12), "max_q": _int("Largest MA order", 4, 0, 12),
           "d": _int("The differences in the model", 0, 0, 2),
           "D": _int("The seasonal differences in the model", 0, 0, 1),
           "period": _int("Season length (with D = 1)", None, 2, 400),
           "exog": _vars("The drivers to include in each fit", 0), "criterion": _IC,
           "trend": _enum("Trend term", ["n", "c", "t", "ct"], "c")},
   inputs={"data": PortSpec(type=PortType.DATASET, description="The data to use"),
           "split": PortSpec(type=PortType.SPLIT_PLAN, required=False,
                             description="Search only the periods before the first test "
                             "window of the plan")},
   outputs={"table": PortType.TABLE},
   fields={"best_p": _f("integer", "The best AR order"),
           "best_q": _f("integer", "The best MA order"),
           "best_order": _f("array", "[p, d, q]"),
           "best_score": _f("number", "The criterion value of the best order"),
           "n_failed": _f("integer", "The number of fits that failed")})  # fmt: skip

_node("search.sarima_order", NodeKind.SEARCH, _a(
    "Find seasonal orders",
    "Adds seasonal past values and errors to the order search, for series with a pattern "
    "that repeats.",
    ["The series has a clear season that a difference alone does not remove"],
    ["No seasonality (use the plain order search)"],
    ["One stable numeric series over time and the season length"],
    ["The best orders are in the grid"],
    ["The best non-seasonal and seasonal orders and the grid of scores"],
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
           "best_score": _f("number", "The criterion value of the best order")})  # fmt: skip

_node("search.var_lag_order", NodeKind.SEARCH, _a(
    "How many lags for series together",
    "Selects the number of past periods for a model of several related series.",
    ["Before you fit a VAR or VECM"],
    ["One series (use the AR/MA order search)"],
    ["Two or more stable numeric series over time"],
    ["The best lag is in the range"],
    ["The selected lag for each criterion"],
    _I, _SEC, "AIC selects 3 lags and BIC selects 1.",
    related=["ts.var", "ts.vecm"], refs=[_SM],
    fits={"goal": ["compare_series"], "data_shape": ["several_series"]},
), params={"vars": _SYSTEM_VARS, "maxlags": _int("Largest lag", 8, 1, 52),
           "criterion": _IC},
   outputs={"table": PortType.TABLE},
   fields={"best_lag": _f("integer", "The lag for the selected criterion"),
           "by_criterion": _f("object",
                              "The lag for each criterion (aic, bic, hqic, fpe)")})  # fmt: skip

_node("search.coint_rank", NodeKind.SEARCH, _a(
    "How many long-run ties",
    "Finds the number of long-run relationships that tie a set of wandering series "
    "together. The error-correction model needs this number.",
    ["Before you fit a VECM"],
    ["The series do not wander"],
    ["Two or more wandering numeric series"],
    ["The assumptions of the Johansen test (enough history, the correct trend terms)"],
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
    "Fits a model with each combination of the settings that you list. It scores each "
    "combination on a backtest and selects the best.",
    ["You want to tune some settings of a catalog model"],
    ["Large grids on slow models (each point is a full fit for each fold)"],
    ["A dataset, a split plan and the settings to try"],
    ["The backtest error is similar to the error in real use"],
    ["The best settings and a table of scores"],
    _X, _LONG, "Try 3 saturation shapes x 2 carry-over lengths. Keep the lowest MAPE.",
    related=["split.nested", "model.backtest"], fits={"goal": ["forecast", "validate_model"]},
    expert="Every grid point is backtested on the plan and the best score wins. With a "
    "split.nested plan this is nested cross-validation: each outer fold picks its point on "
    "its own inner folds (inside its training window), refits it on the outer training "
    "window and scores it on the outer test window; best_score pools those outer "
    "forecasts (the honest estimate of the tuned model, free of the selection bias of "
    "scoring the winner on the folds that chose it) and best_settings is the point the "
    "outer folds chose most often.",
), params={"model": {"type": "string", "description": "Catalog model ref (id@range)"},
           "settings": {"type": "object", "description": "The base settings"},
           "grid": {"type": "object", "description": "JSON pointer into settings -> values",
                    "additionalProperties": {"type": "array", "minItems": 1, "maxItems": 50}},
           "metric": {"type": "string", "default": "rmse"},
           "direction": _enum("Better when", ["min", "max"], "min"),
           "max_points": _int("The maximum number of grid points", 50, 1, 500)},
   required=["model", "grid"],
   inputs={"data": PortSpec(type=PortType.DATASET),
           "split": PortSpec(type=PortType.SPLIT_PLAN)},
   outputs={"table": PortType.TABLE, "best": PortType.MODEL_VERSION},
   fields={"best_settings": _f("object", "The winning settings"),
           "best_score": _f("number", "The score of the best settings (nested plans: the "
                            "outer-fold score of the points that the inner folds selected)"),
           "n_points": _f("integer", "The number of points that the node tried"),
           "nested": _f("boolean",
                        "True if the node tuned on inner folds and scored on outer folds"),
           "outer_scores": _f("array", "Nested plans: the score of each outer fold"),
           "selected_per_fold": _f("array",
                                   "Nested plans: the point that each outer fold selected")},
   runs_in="child_run")  # fmt: skip

# ==================================================================================== model

_MODEL_REF = {"type": "string",
              "description": "Catalog model ref (id@range), for example ts.sarimax@^1",
              "pattern": r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+(@\S+)?$"}  # fmt: skip
_MODEL_FIT = {"goal": ["forecast", "explain_drivers", "explain_marketing"]}

_node("model.fit", NodeKind.MODEL, _a(
    "Fit a model",
    "Fits a model from the catalog to the data and saves it as a model version. It can "
    "also score the model on a test period.",
    ["Each workflow that ends with a model"],
    ["You only need checks of the data"],
    ["A dataset and the settings of the model"],
    ["The assumptions of the model (see its About card)"],
    ["A model version with its scorecard and metrics"],
    _B, _MIN, "Fit ts.sarimax@^1 with the order from the search node.",
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
    "Fits a model again on each fold of a split plan and measures its forecast error at "
    "each horizon. This shows how reliable the model is.",
    ["Before you trust a forecast model", "You want to compare models fairly"],
    ["Slow models with many folds (each fold is a fit)"],
    ["A dataset, a split plan and the model settings"],
    ["The folds are similar to the real use of the model"],
    ["Metrics for each fold and horizon, and forecasts for each fold"],
    _I, _LONG, "Ten folds of ARIMAX: MAPE 6% at 1 week, 11% at 8 weeks.",
    related=["split.expanding_window", "evaluate.baseline", "compare.models"],
    fits={"goal": ["forecast", "validate_model"]},
), params={"model": _MODEL_REF, "settings": {"type": "object"}, "fit": {"type": "object"},
           "pipeline": {"type": "array", "maxItems": 64}, "bind": {"type": "object"},
           "metrics": {"type": "array", "items": {"type": "string"},
                       "default": ["mae", "rmse", "mape", "mase"]},
           "refit": _bool("Fit again on each fold (if false, fit one time and roll forward)",
                          True)},
   required=["model"],
   inputs={"data": PortSpec(type=PortType.DATASET), "split": PortSpec(type=PortType.SPLIT_PLAN)},
   outputs={"metrics": PortType.METRICS, "table": PortType.TABLE},
   fields={"metrics": _f("object", "The average metrics"),
           "by_horizon": _f("object", "The metrics for each horizon"),
           "n_folds": _f("integer", "The number of folds that the node scored")},
   runs_in="child_run")  # fmt: skip

_node("model.forecast", NodeKind.MODEL, _a(
    "Forecast with a model",
    "Uses a fitted model version to forecast the future periods. It uses future drivers if "
    "the model needs them.",
    ["You want the forecast after you select a model"],
    ["The model cannot forecast (look at its score modes)"],
    ["A model version and, for models with drivers, the future values of the drivers"],
    ["The future drivers are the same as the plan"],
    ["A forecast dataset and a fan chart"],
    _B, _SEC, "Forecast 12 weeks with planned prices.",
    related=["model.fit", "ts.sarimax"], fits={"goal": ["forecast"]},
), params={"horizon": _int("The number of periods ahead", 12, 1, 1000),
           "future_covariates": _enum("What to do with missing future drivers",
                                      ["carry_last", "seasonal_naive", "zero", "error"],
                                      "error"),
           "summary": {"type": "array", "items": {"type": "string"},
                       "default": ["mean", "hdi_90"]}},
   inputs={"model": PortSpec(type=PortType.MODEL_VERSION),
           "data": PortSpec(type=PortType.DATASET, required=False,
                            description="The future drivers")},
   outputs={"forecast": PortType.DATASET, "plot": PortType.PLOT},
   fields={"total": _f("number", "The total of the forecast over the horizon")},
   runs_in="child_run")  # fmt: skip

# ================================================================================= evaluate

_METRIC_FIELDS = {
    "mae": _f("number", "Mean absolute error"), "rmse": _f("number", "Root mean squared error"),
    "mape": _f("number", "Mean absolute percentage error"),
    "smape": _f("number", "Symmetric MAPE"), "mase": _f("number", "Mean absolute scaled error"),
    "coverage_90": _f("number", "The share of actual values in the 90% range"),
    "crps": _f("number", "Continuous ranked probability score"),
    "by_horizon": _f("object", "The metrics for each horizon"),
}  # fmt: skip

_node("evaluate.metrics", NodeKind.EVALUATE, _a(
    "Measure forecast error",
    "Measures the difference between the predictions of a model and the actual values on "
    "the test data. It gives several measures that are easy to read.",
    ["After you fit or backtest a model"],
    ["No data was kept for the test"],
    ["A model version (and a test period) or backtest results"],
    ["The test data is similar to the future use"],
    ["MAE, RMSE, MAPE, MASE and range coverage, for each horizon"],
    _B, _SEC, "MAPE 8%, and 88% of the actual values are in the 90% range.",
    related=["evaluate.baseline", "holdout.accuracy"],
    fits={"goal": ["validate_model", "forecast"]},
    expert="MASE scales by the in-sample seasonal-naive MAE (Hyndman & Koehler 2006); CRPS "
    "from draws or quantiles.",
), params={"metrics": {"type": "array", "items": {"type": "string", "enum": sorted(_METRIC_FIELDS)},
                       "default": ["mae", "rmse", "mape", "mase", "coverage_90"]},
           "season": _int("The season length for MASE (default: from the frequency)", None, 1,
                          400)},
   inputs={"model": PortSpec(type=PortType.MODEL_VERSION, required=False),
           "backtest": PortSpec(type=PortType.METRICS, required=False)},
   outputs={"metrics": PortType.METRICS}, fields=_METRIC_FIELDS)  # fmt: skip

_node("evaluate.baseline", NodeKind.EVALUATE, _a(
    "Simple benchmark forecasts",
    "Scores very simple forecasts, for example 'same as last week' or 'same as last year', "
    "on the same test periods. You can then make sure that a model is better than them.",
    ["All forecast workflows (a model must be better than these benchmarks to be useful)"],
    ["There is no time order"],
    ["A dataset and the same split plan that the model used"],
    ["The node scores the benchmark on the same folds"],
    ["The metrics of each benchmark for each fold and horizon"],
    _B, _SEC, "Seasonal naive has a MAPE of 14%. The ARIMAX model has 9%.",
    related=["baseline.beats_naive", "compare.models"],
    fits={"goal": ["validate_model", "forecast"]},
), params={"methods": {"type": "array", "minItems": 1,
                       "items": {"type": "string", "enum": ["naive", "seasonal_naive", "mean",
                                                            "drift"]},
                       "default": ["naive", "seasonal_naive"]},
           "season": _int("The season length (default: from the frequency)", None, 1, 400),
           "var": _var("The series (default: the target)")},
   inputs={"data": PortSpec(type=PortType.DATASET), "split": PortSpec(type=PortType.SPLIT_PLAN)},
   outputs={"metrics": PortType.METRICS},
   fields={"by_method": _f("object", "The metrics for each benchmark"),
           "best_method": _f("string", "The strongest benchmark")})  # fmt: skip

_node("evaluate.residuals", NodeKind.EVALUATE, _a(
    "Test the model's errors",
    "Tests the errors of a model for patterns that remain, uneven size and shapes that are "
    "not a bell curve. It does all these tests at the same time.",
    ["After you fit a time-series model or a regression model"],
    ["Models without residuals (classifiers)"],
    ["A model version"],
    ["The fit gives residuals"],
    ["Ljung-Box, normality and spread tests with plain verdicts, and the ACF of the errors"],
    _I, _SEC, "No pattern remains, the tails are slightly heavy and the spread is even.",
    related=["test.ljung_box", "test.jarque_bera", "residuals.whiteness"],
    fits={"goal": ["validate_model"]},
), params={"lags": _int("Ljung-Box lags", 10, 1, 200), "alpha": _ALPHA},
   inputs={"model": PortSpec(type=PortType.MODEL_VERSION)},
   outputs={"table": PortType.TABLE, "plot": PortType.PLOT},
   fields={"white_noise": _f("boolean", "No pattern remains in the errors"),
           "normal": _f("boolean", "Bell-shaped errors"),
           "equal_spread": _f("boolean", "Even-sized errors")})  # fmt: skip

# ================================================================================== compare

_node("compare.models", NodeKind.COMPARE, _a(
    "Compare models side by side",
    "Puts the scores of several models and benchmarks in one table. You can see which one "
    "is best on each measure.",
    ["You fitted or backtested several candidates"],
    ["The models have tests on different periods (then the comparison is not fair)"],
    ["Metrics from two or more model or benchmark nodes"],
    ["All candidates have scores on the same folds"],
    ["A comparison table and a rank order for each metric"],
    _B, _NOW, "ARIMAX 9% MAPE, ETS 11%, seasonal naive 14%.",
    related=["select.best", "compare.diebold_mariano"],
    fits={"goal": ["validate_model", "forecast"]},
), params={"metric": {"type": "string", "default": "rmse"},
           "direction": _enum("Better when", ["min", "max"], "min")},
   inputs={"candidates": PortSpec(type=PortType.METRICS, multiple=True)},
   outputs={"table": PortType.TABLE},
   fields={"ranking": _f("array", "The candidates, with the best first"),
           "best": _f("string", "The best candidate (node id)")},
   runs_in="orchestrator")  # fmt: skip

_node("compare.diebold_mariano", NodeKind.COMPARE, _a(
    "Is one forecast really better?",
    "Tests whether the forecasts of one model are really more accurate than the forecasts "
    "of another model, or whether the difference is possibly luck.",
    ["Two models are close and you need to know if the gap is real"],
    ["Very few test periods"],
    ["Backtest errors of two models on the same folds"],
    ["The differences between the errors are stable over time"],
    ["The statistic, the p-value and the better model"],
    _X, _NOW, "ARIMAX is better than ETS, but p = 0.31. The difference is possibly luck.",
    related=["compare.models"],
    expert="Diebold-Mariano test with the Harvey-Leybourne-Newbold small-sample correction "
    "on squared or absolute loss, on one h-step error per backtest fold. Folds `step` "
    "periods apart share test periods only up to ceil(h / step) - 1 folds, which is the "
    "number of autocovariances in the long-run variance (h - 1 for consecutive origins); "
    "the step is read from the folds.",
    refs=[("Diebold & Mariano (1995)", None),
          ("Harvey, Leybourne & Newbold (1997)", None)], fits={"goal": ["validate_model"]},
), params={"loss": _enum("Loss", ["squared", "absolute"], "squared"), "horizon": _int("h", 1, 1),
           "step": _int("The periods between fold origins (default: from the folds)", None, 1),
           "alpha": _ALPHA},
   inputs={"a": PortSpec(type=PortType.METRICS), "b": PortSpec(type=PortType.METRICS)},
   outputs={"result": PortType.TEST_RESULT}, fields=_TEST_FIELDS)  # fmt: skip

# =================================================================================== select

_node("select.best", NodeKind.SELECT, _a(
    "Select the best model",
    "Selects the model with the best score from a comparison. It can also make sure that "
    "the model is better than the simple benchmarks.",
    ["The end of a bake-off between candidates"],
    ["Only one candidate"],
    ["A comparison table"],
    ["The selected metric is the important metric for the decision"],
    ["The model version that won, and the reason"],
    _B, _NOW, "Select the lowest RMSE, but only if it is 5% better than seasonal naive.",
    related=["compare.models", "baseline.beats_naive"],
    fits={"goal": ["validate_model", "forecast"]},
), params={"metric": {"type": "string", "default": "rmse"},
           "direction": _enum("Better when", ["min", "max"], "min"),
           "must_beat": {"type": "array", "items": {"type": "string"},
                         "description": "The benchmark ids that the winner must be better than"},
           "margin": _num("The necessary relative improvement over must_beat", 0.0, 0, 1)},
   inputs={"table": PortSpec(type=PortType.TABLE),
           "models": PortSpec(type=PortType.MODEL_VERSION, multiple=True, required=False)},
   outputs={"model": PortType.MODEL_VERSION},
   fields={"selected": _f("string", "The winner (node id)"),
           "selected_model": _f("string", "The catalog model ref of the winner, if the "
                                "winner is a model node"),
           "score": _f("number", "The score of the winner"),
           "beats_baselines": _f("boolean", "True if the winner is better than the benchmarks"),
           "reason_plain": _f("string", "Why the node selected the winner")},
   runs_in="orchestrator")  # fmt: skip

_node("select.threshold", NodeKind.SELECT, _a(
    "Keep models that pass a limit",
    "Keeps only the models with a score that passes a limit that you set, for example an "
    "error below 10%.",
    ["You need all models that are good enough, not only the best"],
    ["You need one winner only"],
    ["A comparison table"],
    ["The limit shows what is acceptable"],
    ["The models that pass"],
    _B, _NOW, "Keep all models with a MAPE below 10%.",
    related=["select.best"], fits={"goal": ["validate_model"]},
), params={"metric": {"type": "string"}, "max": _num("Upper limit"), "min": _num("Lower limit")},
   required=["metric"],
   inputs={"table": PortSpec(type=PortType.TABLE)},
   outputs={"table": PortType.TABLE},
   fields={"passed": _f("array", "The candidates that pass"),
           "n_passed": _f("integer", "The number of candidates that pass")},
   runs_in="orchestrator")  # fmt: skip

# =============================================================================== governance

_node("governance.policy", NodeKind.GOVERNANCE, _a(
    "Run the governance checks",
    "Runs on a model all the checks that the policy of your organisation needs, and records "
    "the evidence. For example, the model must be better than random noise and a simple "
    "benchmark.",
    ["Before you validate and promote a model"],
    ["Exploratory work that nobody will use"],
    ["A model version and the data that you fitted it on"],
    ["The policy agrees with the model family"],
    ["Evidence for each check and an overall pass or fail"],
    _I, _LONG, "Seven checks pass and the placebo check fails, so the model is not validated.",
    related=["null.permutation_target", "baseline.beats_naive", "governance.check"],
    fits={"goal": ["validate_model"]},
), params={"policy": {"type": "string",
                      "description": "The policy id (default: the policy that is in force)"},
           "submit": _bool("Submit the result for approval when the checks pass", False)},
   inputs={"model": PortSpec(type=PortType.MODEL_VERSION),
           "data": PortSpec(type=PortType.DATASET, required=False)},
   outputs={"evidence": PortType.EVIDENCE},
   fields={"passed": _f("boolean", "True if all the blocking checks passed"),
           "failed_checks": _f("array", "The checks that failed"),
           "status": _f("string", "checks_passed or checks_failed")},
   runs_in="child_run", cacheable=False)  # fmt: skip

_node("governance.check", NodeKind.GOVERNANCE, _a(
    "Run one governance check",
    "Runs one check from the governance catalog on a model, for example the shuffled-result "
    "test.",
    ["You want to try a check before you add it to a policy"],
    ["Formal validation (run the full policy)"],
    ["A model version and its data"],
    ["The check applies to the model family"],
    ["Evidence for the check, and its result: pass, warn or fail"],
    _I, _MIN, "The noise-channel check gives the fake channel a 2% share. The check passes.",
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
    "Writes a short report of the workflow results: the test verdicts, the decisions, the "
    "selected model and how good it is.",
    ["At the end of a workflow that you share with other persons"],
    ["You only need the raw numbers"],
    ["The results of earlier nodes"],
    ["Earlier nodes were successful, or the workflow skipped them for a reason"],
    ["A report with text, tables and plots"],
    _B, _NOW, "Sales wander, so the workflow applied a difference. ARIMAX(2,1,1) was 30% "
    "better than seasonal naive.",
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
    "Puts the governance evidence, decisions and approvals of a model into one document "
    "for auditors, as HTML and JSON.",
    ["An audit, a model risk review or an approval"],
    ["Before the governance checks run"],
    ["A model version with governance evidence"],
    ["The server stores evidence for each check"],
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
    "Uses earlier results to decide the path of the workflow. For example: 'apply a "
    "difference only if the tests show that the series wanders'.",
    ["A step must run only under a condition"],
    ["All steps must always run"],
    ["The results of earlier nodes (and workflow parameters)"],
    ["The condition refers only to earlier nodes"],
    ["The path that the workflow took (then or else) and the values that decided it"],
    _I, _NOW,
    "nodes.adf.p_value >= params.alpha or nodes.kpss.reject decides whether to apply a "
    "difference.",
    related=["test.adf", "transform.difference"], fits={"goal": ["forecast", "check_data"]},
    expert="Conditions use the safe expression language (mapi_spec.expr): comparisons, "
    "and/or/not, arithmetic, a few functions; no code is executed.",
), params={"condition": {"type": "string", "maxLength": 2000,
                         "description": "The condition in the workflow expression language"},
           "explain_then": {"type": "string", "maxLength": 300,
                            "description": "What it means when the condition is true, in plain "
                            "words for the run report (for example, 'The tests found that "
                            "the series wanders, so the workflow applied a difference.')"},
           "explain_else": {"type": "string", "maxLength": 300,
                            "description": "What it means when the condition is false, in plain "
                            "words"}},
   required=["condition"], inputs={},
   fields={"value": _f("boolean", "The value of the condition"),
           "arm": _f("string", "then or else")},
   runs_in="orchestrator")  # fmt: skip

_node("subworkflow.run", NodeKind.SUBWORKFLOW, _a(
    "Run another workflow",
    "Runs a saved workflow as one step of this workflow. You write common sequences, for "
    "example data readiness checks, one time and use them again.",
    ["Several workflows use the same sequence of steps"],
    ["Only this workflow uses the steps"],
    ["A saved workflow revision and its inputs"],
    ["The inputs and outputs of the other workflow agree with the connections here"],
    ["The outputs of the other workflow"],
    _I, _MIN, "Use 'time-series readiness' again before three different model workflows.",
    related=["workflow.ts_readiness_arimax"], fits={"goal": ["forecast", "validate_model"]},
), params={"workflow": {"type": "string", "pattern": r"^workflow://\S+$",
                        "description": "workflow://<project>/<name>@<revision>"},
           "params": {"type": "object",
                      "description": "The parameters to give to the other workflow"}},
   required=["workflow"], inputs={}, runs_in="child_run")  # fmt: skip


# ============================================================== M5: nodes on posterior draws

_node("model.summary", NodeKind.MODEL, _a(
    "Summarise the parameters",
    "Gives the estimate, the spread and the credible range of each parameter of a fitted "
    "Bayesian model, with checks that the sampler converged.",
    ["You want to branch on a parameter, for example when its range excludes zero",
     "You must stop when the sampler did not converge (R-hat, effective sample size)"],
    ["Models that give only point estimates (the node needs draws)"],
    ["A model version with posterior draws"],
    ["The draws come from two or more chains (else R-hat is not available)"],
    ["A table and, for each parameter, the mean, sd, HDI bounds, R-hat and bulk ESS"],
    _I, _SEC, "slope: mean 0.42, 94% range 0.18 to 0.66, R-hat 1.00. The range excludes zero.",
    related=["model.fit", "governance.check", "evaluate.ppc"],
    fits={"goal": ["validate_model", "explain_drivers"]},
    expert="Rank-normalised split R-hat (the larger of bulk and tail) and bulk ESS (Vehtari "
    "et al. 2021); the HDI is the narrowest interval with hdi_prob of the draws (0.94 gives "
    "the 3% and 97% bounds). Vector parameters are flattened to name[label] (labels of "
    "several dims joined by ', '), at most 200 values; read them with "
    "nodes.<id>.params['beta[north]'].hdi_low. n_divergent is null when the model stores no "
    "sample_stats.",
    refs=[("Vehtari, Gelman, Simpson, Carpenter & Bürkner (2021), Rank-normalization, "
           "folding, and localization: an improved R-hat", None)],
), params={"vars": _vars("The posterior variables (default: all of them)"),
           "hdi_prob": _num("The probability inside the HDI", 0.94, 0.5, 0.999)},
   inputs={"model": PortSpec(type=PortType.MODEL_VERSION)},
   outputs={"table": PortType.TABLE},
   fields={"params": _f("object", "For each parameter: mean, sd, hdi_low, hdi_high, rhat "
                                  "and ess_bulk"),
           "max_rhat": _f("number", "The largest R-hat (null if it cannot be measured)"),
           "min_ess": _f("number", "The smallest bulk effective sample size"),
           "n_divergent": _f("integer", "The number of divergent transitions (null if "
                                        "unknown)"),
           "n_params": _f("integer", "The number of summarised values"),
           "truncated": _f("boolean", "True if the node kept only the first 200 values")},
   )  # fmt: skip

_node("evaluate.ppc", NodeKind.EVALUATE, _a(
    "Check the predictions against the data",
    "Simulates new data from a fitted Bayesian model and compares it with the observed data. "
    "It shows if the model can make data that looks like the real data.",
    ["After each Bayesian fit, before you trust its ranges",
     "You must stop a workflow when the model does not agree with the data"],
    ["Models that give only point estimates or summaries (the node needs draws)"],
    ["A model version that predicts with draws"],
    ["The check uses the fit data, so it is optimistic (a model that overfits can pass)"],
    ["The coverage of the 50% and 90% ranges, the PIT distance, a p-value and a verdict"],
    _I, _SEC, "88% of the values are in the 90% range and p = 0.41 for the mean: the "
    "check passes.",
    related=["model.fit", "model.summary", "core.ppc"],
    fits={"goal": ["validate_model"]},
    expert="Posterior predictive draws come from the score path (predict, output kind "
    "predictive) on the fit data. coverage_p is the share of observations inside the "
    "central p interval of their predictive draws; pit_ks is the Kolmogorov-Smirnov "
    "distance of the PIT values P(y_rep < y) + 0.5 P(y_rep = y) from uniform, with the "
    "limit 1.36 / sqrt(n) unless pit_ks_max is set; the p-value is P(T(y_rep) >= T(y)) for "
    "T = statistic. ok needs both coverages within coverage_tolerance of nominal, the "
    "p-value in [p_min, 1 - p_min] and pit_ks under its limit.",
    refs=[("Gelman, Meng & Stern (1996), Posterior predictive assessment of model fitness",
           None), ("Gabry et al. (2019), Visualization in Bayesian workflow", None)],
), params={"statistic": _enum("The statistic of the p-value", ["mean", "sd", "max", "min"],
                              "mean"),
           "n_draws": _int("The number of predictive draws to use", 400, 10, 4000),
           "p_min": _num("The smallest p-value (and 1 - p_min the largest) that passes",
                         0.025, 0, 0.5),
           "coverage_tolerance": _num("The largest gap between the coverage and its level "
                                      "that passes", 0.1, 0, 0.5),
           "pit_ks_max": _num("The largest PIT distance that passes (default: 1.36 / "
                              "sqrt(n))", None, 0, 1)},
   inputs={"model": PortSpec(type=PortType.MODEL_VERSION)},
   outputs={"metrics": PortType.METRICS},
   fields={"coverage_50": _f("number", "The share of observed values in the 50% range"),
           "coverage_90": _f("number", "The share of observed values in the 90% range"),
           "pit_ks": _f("number", "The distance of the PIT values from uniform (0 is best)"),
           "p_values": _f("object", "The p-value of the statistic for each target"),
           "ok": _f("boolean", "True if all the checks pass"),
           "verdict_plain": _f("string", "The verdict in plain words")})  # fmt: skip

_node("compare.stacking", NodeKind.COMPARE, _a(
    "Weigh models by their predictions",
    "Gives each candidate model a weight from how well it predicts each data point that it "
    "did not see. A mix of the models with these weights predicts best.",
    ["You fitted several Bayesian models of the same data and none is clearly best",
     "You want to average the predictions of the models instead of selecting one"],
    ["The models used different data (the scores are not comparable)"],
    ["Metrics of two or more model.fit nodes with evaluate: {metrics: [elpd_loo]}"],
    ["All candidates are fitted on the same data points in the same order"],
    ["A weight for each candidate, the best candidate and a table"],
    _X, _SEC, "Weights: bym2 0.71, icar 0.29, iid 0.00.",
    related=["compare.models", "select.best", "model.fit"],
    fits={"goal": ["validate_model"]},
    expert="Stacking maximises sum_i log sum_k w_k exp(elpd_loo_ik) over the simplex (Yao, "
    "Vehtari, Simpson & Gelman 2018), from the pointwise PSIS-LOO values that model.fit "
    "stores for data of at most 10 000 points. pseudo_bma (always reported) is pseudo-BMA+: "
    "softmax weights averaged over a Bayesian bootstrap of the pointwise elpd. Without "
    "pointwise values the weights are pseudo-BMA from elpd ± se (method pseudo_bma_se) or "
    "from elpd alone (pseudo_bma), and note_plain says so.",
    refs=[("Yao, Vehtari, Simpson & Gelman (2018), Using stacking to average Bayesian "
           "predictive distributions", None)],
), params={"metric": _enum("The elpd estimate to use", ["elpd_loo", "elpd_waic"], "elpd_loo"),
           "method": _enum("The weights to give", ["stacking", "pseudo_bma_plus"], "stacking"),
           "n_bootstrap": _int("Bootstrap draws for pseudo-BMA+", 1000, 100, 10000)},
   inputs={"candidates": PortSpec(type=PortType.METRICS, multiple=True)},
   outputs={"table": PortType.TABLE},
   fields={"weights": _f("object", "The weight of each candidate (they sum to 1)"),
           "pseudo_bma": _f("object", "The pseudo-BMA+ weight of each candidate"),
           "best": _f("string", "The candidate with the largest weight"),
           "method": _f("string", "stacking, pseudo_bma_plus, pseudo_bma_se or pseudo_bma"),
           "note_plain": _f("string", "Why the node did not use stacking (null if it did)")},
   )  # fmt: skip


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
        "Tests a property of the data or of the errors of a model. It gives a yes/no verdict "
        "with its evidence.",
        ["You must make a decision from the data (for example, about a difference or lags)"],
        ["You only want a chart (use the Data Explorer)"],
        ["A dataset or a model version"],
        ["The assumptions of each test (see its card)"],
        ["The statistic, the p-value, the decision and a plain verdict"],
        _I,
        _SEC,
        "ADF shows that the series wanders.",
        related=["test.adf"],
    ),
    NodeKind.TRANSFORM: _a(
        "Data processing",
        "Changes the data before later steps use it. For example, it calculates differences "
        "or logs.",
        ["You must prepare the data before a test or a model"],
        ["The data is ready as it is"],
        ["A dataset"],
        ["The workflow reverses the transforms on forecasts where necessary"],
        ["A new dataset"],
        _B,
        _NOW,
        "Apply one difference to sales.",
        related=["transform.difference"],
    ),
    NodeKind.BRANCH: _a(
        "Decision",
        "Uses earlier results to select the path of the workflow.",
        ["A step must run only when a condition is true"],
        ["All steps always run"],
        ["The results of earlier nodes"],
        ["Conditions read only earlier results"],
        ["The path that the workflow took"],
        _I,
        _NOW,
        "Apply a difference only when the series wanders.",
        related=["branch.if"],
    ),
    NodeKind.SPLIT: _a(
        "Train/test plan",
        "Decides which periods or groups the model learns from, and which periods or groups "
        "test the model. It can do this many times.",
        ["Each model that you want to test honestly"],
        ["You do not test the model"],
        ["A dataset"],
        ["The test periods are similar to the real use of the model"],
        ["A split plan"],
        _I,
        _NOW,
        "Six expanding-window folds.",
        related=["split.expanding_window"],
    ),
    NodeKind.SEARCH: _a(
        "Search",
        "Tries many settings and selects the best setting by a score.",
        ["You want to select orders or settings from the data"],
        ["The settings are fixed before the run"],
        ["A dataset (and a split plan for backtest searches)"],
        ["The best setting is in the search range"],
        ["The best settings and a table of scores"],
        _I,
        _SEC,
        "Find ARMA orders by AIC.",
        related=["search.arma_order"],
    ),
    NodeKind.MODEL: _a(
        "Model",
        "Fits, backtests or forecasts with any model from the catalog.",
        ["The workflow must make a model or a forecast"],
        ["Only checks of the data"],
        ["A dataset and model settings"],
        ["The assumptions of the model"],
        ["Model versions, metrics and forecasts"],
        _B,
        _MIN,
        "Fit an ARIMAX model.",
        related=["model.fit"],
    ),
    NodeKind.EVALUATE: _a(
        "Evaluation",
        "Measures how good a model or a simple benchmark is on test data.",
        ["After you fit or backtest a model"],
        ["No data was kept for the test"],
        ["Model versions or backtests"],
        ["The test data is similar to real use"],
        ["Metrics"],
        _B,
        _SEC,
        "Score the seasonal-naive benchmark.",
        related=["evaluate.metrics"],
    ),
    NodeKind.COMPARE: _a(
        "Comparison",
        "Puts the scores of several candidates side by side, or tests whether one candidate "
        "is really better.",
        ["Several candidate models"],
        ["One candidate"],
        ["Metrics of the candidates"],
        ["The same folds for each candidate"],
        ["A comparison table or a test"],
        _B,
        _NOW,
        "ARIMAX vs ETS vs naive.",
        related=["compare.models"],
    ),
    NodeKind.SELECT: _a(
        "Selection",
        "Selects the winner, or all candidates that pass a limit.",
        ["The end of a bake-off"],
        ["One candidate"],
        ["A comparison table"],
        ["The metric agrees with the decision"],
        ["The selected model(s)"],
        _B,
        _NOW,
        "The lowest RMSE wins.",
        related=["select.best"],
    ),
    NodeKind.GOVERNANCE: _a(
        "Governance",
        "Runs the checks that a model must pass before you can validate it. It stores the "
        "evidence.",
        ["Before you validate or promote a model"],
        ["Experiments that you will not keep"],
        ["A model version and its data"],
        ["A policy covers the model family"],
        ["Evidence and a pass or fail"],
        _I,
        _LONG,
        "Run the tenant policy.",
        related=["governance.policy"],
    ),
    NodeKind.REPORT: _a(
        "Report",
        "Changes the results of the workflow into a report that is easy to read, or into an "
        "evidence pack.",
        ["You want to share the results"],
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
        ["You want to use a common sequence again"],
        ["Steps that you use one time only"],
        ["A saved workflow revision"],
        ["The inputs and outputs agree"],
        ["The outputs of the other workflow"],
        _I,
        _MIN,
        "Reuse the readiness checks.",
        related=["subworkflow.run"],
    ),
}
"""Cards of the node kinds (``GET /v1/catalog/workflow-nodes`` groups by them)."""
