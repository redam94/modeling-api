"""The gallery of workflow templates (M3.5): ready-made workflows users start from.

==========================  ================================================================
id                          what it automates
==========================  ================================================================
``ts_readiness_arimax``     unit-root tests (ADF, KPSS, PP, Zivot-Andrews) -> differencing and
                            seasonal differencing chosen by branches -> ACF/PACF and an
                            information-criterion search of AR and MA orders -> an
                            expanding-window backtest with a gap and several horizons ->
                            ARIMAX vs naive benchmarks -> select -> governance -> report
``var_vecm_cointegration``  Johansen test -> VECM with the selected rank when the series are
                            tied together, else differences + lag search + VAR -> Granger
                            tests -> report
``mmm_governed``            driver collinearity check -> mmm-framework marketing mix model
                            with a holdout -> governance policy -> report + evidence pack
``forecast_bakeoff``        one expanding-window backtest for SARIMAX, ETS, Theta and the
                            smooth-trend model against naive benchmarks -> compare -> select
                            the winner -> refit it -> report
``bayes_workflow_audit``    any Bayesian model (``model`` parameter): fit with prior
                            predictive draws -> prior plot -> parameter summary and a
                            convergence gate -> predictive check, prior sensitivity and
                            simulated recovery -> pair, predictive and estimate plots -> report
``sci_image_model_check``   an image model (``sci.cmb_field``, ``sci.strong_lens``, …): fit ->
                            residual maps and a predictive image grid -> predictive check ->
                            report
==========================  ================================================================

Templates are complete :class:`~mapi_spec.workflow.WorkflowSpec` documents that pass
:func:`~mapi_spec.workflow.validate_workflow` (a test checks it);
:func:`instantiate_template` names a copy for a project.
"""

from __future__ import annotations

import copy
from typing import Any, Final, cast

from pydantic import Field, JsonValue

from mapi_spec._dto import ResponseModel, dto_examples
from mapi_spec._types import Slug
from mapi_spec.about import About, AboutReference, Difficulty, TypicalRuntime
from mapi_spec.guided import MMM_MODEL_ID, MMM_TEMPLATE_SETTINGS
from mapi_spec.workflow import WorkflowSpec

__all__ = [
    "WORKFLOW_TEMPLATES",
    "WorkflowTemplate",
    "get_workflow_template",
    "instantiate_template",
]


class WorkflowTemplate(ResponseModel):
    """A gallery template: its About card and the workflow document."""

    id: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version: str = Field("1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    about: About
    document: dict[str, JsonValue]
    tags: list[str] = Field(default_factory=list, max_length=16)

    model_config = dto_examples(
        {
            "id": "readiness",
            "about": About.model_json_schema()["examples"][0],
            "document": {
                "apiVersion": "mapi/v1",
                "kind": "Workflow",
                "metadata": {"name": "readiness", "project": "templates"},
                "inputs": {"data": {"type": "dataset"}},
                "nodes": [{"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}}],
            },
            "tags": ["forecast"],
        }
    )

    @property
    def spec(self) -> WorkflowSpec:
        return WorkflowSpec.model_validate(self.document)


def _doc(name: str, params: dict[str, Any], inputs: dict[str, Any], nodes: list[dict[str, Any]],
         outputs: dict[str, str]) -> dict[str, JsonValue]:  # fmt: skip
    doc: dict[str, Any] = {
        "apiVersion": "mapi/v1",
        "kind": "Workflow",
        "metadata": {"name": name, "project": "templates"},
        "params": params,
        "inputs": inputs,
        "nodes": nodes,
        "outputs": outputs,
    }
    return cast("dict[str, JsonValue]", doc)


def _p(name: str) -> dict[str, str]:
    return {"$param": name}


def _x(text: str) -> dict[str, str]:
    return {"$expr": text}


_DATA = {"data": {"type": "dataset", "description": "The weekly (or daily) series to model"}}
_D = "if(nodes.wanders.value, 1, 0)"
_SD = "if(nodes.seasonal.value, 1, 0)"
_SERIES = ["sdiff.data", "inputs.data"]
"""The series the unit-root tests see: seasonally differenced when the season says so."""
_ARIMAX_SETTINGS = {
    "order": _x(f"[nodes.order.best_p, {_D}, nodes.order.best_q]"),
    "seasonal_order": _x(f"[0, {_SD}, 0, params.season]"),
    "exog": _p("drivers"),
}

TS_READINESS_ARIMAX = WorkflowTemplate(
    id="ts_readiness_arimax",
    about=About(
        title="Time-series readiness to ARIMAX",
        summary_plain="Finds whether your series needs a difference and how many past values "
        "are important. Then it backtests a forecast with drivers against simple benchmarks "
        "and keeps the model only if it wins and passes governance.",
        use_when=["You want an automatic, fully tested forecast of one series with drivers",
                  "You want a record and an explanation of each decision about the model"],
        avoid_when=["You have many related series that affect each other (use VAR or VECM)",
                    "You have less than about two seasons of history"],
        data_needs=["One numeric series over time, at least two seasons long",
                    "Optional drivers that you know for the future (for example, prices and "
                    "holidays)"],
        assumptions=["Past patterns continue into the forecast horizon",
                     "The drivers have a linear effect on the series"],
        outputs=["Test verdicts and the decision about the difference",
                 "The selected AR and MA orders",
                 "Backtest errors for each horizon, compared with the benchmarks",
                 "A validated ARIMAX model", "A plain-language report"],
        difficulty=Difficulty.BEGINNER,
        typical_runtime=TypicalRuntime.MINUTES,
        example="Weekly orders: the tests say 'difference once' and AIC selects (2,1,1). "
        "The backtest is 25% better than seasonal naive, and governance passes.",
        related=["test.adf", "test.kpss", "search.arma_order", "ts.sarimax",
                 "split.expanding_window"],
        references=[AboutReference(title="Hyndman & Athanasopoulos, Forecasting: Principles "
                                   "and Practice", url="https://otexts.com/fpp3/")],
        fits={"goal": ["forecast", "check_data"], "data_shape": ["one_series"],
              "frequency": ["daily", "weekly", "monthly"], "history": ["medium", "long"]},
    ),
    document=_doc(
        "ts-readiness-arimax",
        params={
            "alpha": {"type": "number", "default": 0.05, "minimum": 0.001, "maximum": 0.2,
                      "description": "The significance level of the tests"},
            "season": {"type": "integer", "default": 52, "minimum": 2, "maximum": 400,
                       "description": "The length of a season in periods (52 for weekly data)"},
            "horizons": {"type": "array", "default": [1, 4, 8],
                         "description": "The forecast horizons that the backtest scores"},
            "gap": {"type": "integer", "default": 0, "minimum": 0, "maximum": 52,
                    "description": "The periods between the training data and the test "
                                   "data (for late data)"},
            "n_folds": {"type": "integer", "default": 6, "minimum": 2, "maximum": 50},
            "max_p": {"type": "integer", "default": 3, "minimum": 0, "maximum": 8},
            "max_q": {"type": "integer", "default": 3, "minimum": 0, "maximum": 8},
            "drivers": {"type": "array", "default": [],
                        "description": "The driver columns that you know for the future"},
        },
        inputs=_DATA,
        nodes=[
            # 1. The seasonal difference first (as nsdiffs before ndiffs) ...
            {"id": "seas", "use": "test.seasonal_strength", "inputs": {"data": "inputs.data"},
             "with": {"period": _p("season")}},
            {"id": "seasonal", "use": "branch.if", "label": "Seasonal difference?",
             "with": {"condition": "nodes.seas.seasonal",
                      "explain_then": "The series has a strong seasonal pattern. The "
                                      "workflow compared each value with the same period one "
                                      "season earlier (a seasonal difference).",
                      "explain_else": "The series has no strong seasonal pattern, so the "
                                      "workflow did not apply a seasonal difference."}},
            {"id": "sdiff", "use": "transform.seasonal_difference",
             "inputs": {"data": "inputs.data"}, "when": "seasonal.then",
             "with": {"period": _p("season")}},
            # 2. ... then the unit-root tests on the seasonally differenced series
            {"id": "adf", "use": "test.adf", "inputs": {"data": _SERIES},
             "with": {"alpha": _p("alpha")}, "label": "Does it wander? (ADF)"},
            {"id": "kpss", "use": "test.kpss", "inputs": {"data": _SERIES},
             "with": {"alpha": _p("alpha")}, "label": "Is it stable? (KPSS)"},
            {"id": "pp", "use": "test.pp", "inputs": {"data": _SERIES},
             "with": {"alpha": _p("alpha")}},
            {"id": "za", "use": "test.zivot_andrews", "inputs": {"data": _SERIES},
             "with": {"alpha": _p("alpha")}},
            # over-differencing guard: a first difference whose lag-1 autocorrelation is
            # below -0.5 removed a unit root that was not there (a non-invertible MA)
            {"id": "cand", "use": "transform.difference", "inputs": {"data": _SERIES},
             "label": "Trial difference"},
            {"id": "overdiff", "use": "test.acf_pacf", "inputs": {"data": "cand.data"},
             "with": {"nlags": 4, "max_order": 1}, "label": "Too many differences?"},
            {"id": "wanders", "use": "branch.if", "label": "Difference?",
             "with": {"condition": "(not nodes.adf.reject or nodes.kpss.reject) and not "
                                   "(nodes.pp.reject and nodes.za.reject) and "
                                   "nodes.overdiff.acf[1] > -0.5",
                      "explain_then": "The tests found that the series wanders and does not "
                                      "return to a steady level. The workflow applied a "
                                      "difference, so the model uses the change from one "
                                      "period to the next.",
                      "explain_else": "The tests found that the series returns to a steady "
                                      "level, or that a difference would go too far. The "
                                      "workflow used the series as it is."}},
            {"id": "diff", "use": "transform.difference",
             "inputs": {"data": _SERIES}, "when": "wanders.then"},
            {"id": "acf", "use": "test.acf_pacf",
             "inputs": {"data": ["diff.data", "sdiff.data", "inputs.data"]},
             "with": {"max_order": _p("max_p")}},
            # the orders of the model that is fitted (same d and seasonal D), searched only
            # before the backtest's first test window
            {"id": "order", "use": "search.arma_order",
             "inputs": {"data": "inputs.data", "split": "folds.plan"},
             "with": {"d": _x(_D), "D": _x(_SD), "period": _p("season"),
                      "max_p": _p("max_p"), "max_q": _p("max_q"),
                      "exog": _p("drivers"), "criterion": "aic"},
             "label": "AR and MA orders"},
            {"id": "folds", "use": "split.expanding_window", "inputs": {"data": "inputs.data"},
             "with": {"horizons": _p("horizons"), "gap": _p("gap"), "n_folds": _p("n_folds"),
                      "step": 4}},
            {"id": "backtest", "use": "model.backtest", "label": "ARIMAX",
             "inputs": {"data": "inputs.data", "split": "folds.plan"},
             "with": {"model": "ts.sarimax@^1", "settings": _ARIMAX_SETTINGS,
                      "metrics": ["mae", "rmse", "mape", "mase"]}},
            {"id": "naive", "use": "evaluate.baseline", "label": "Naive forecast",
             "inputs": {"data": "inputs.data", "split": "folds.plan"},
             "with": {"methods": ["naive", "seasonal_naive"], "season": _p("season")}},
            {"id": "compare", "use": "compare.models",
             "inputs": {"candidates": ["backtest.metrics", "naive.metrics"]},
             "with": {"metric": "mae"}},
            {"id": "fit", "use": "model.fit", "inputs": {"data": "inputs.data"},
             "with": {"model": "ts.sarimax@^1", "settings": _ARIMAX_SETTINGS,
                      "evaluate": {"metrics": ["mape", "coverage_90"]}}},
            {"id": "select", "use": "select.best",
             "inputs": {"table": "compare.table", "models": ["fit.model"]},
             "with": {"metric": "mae", "must_beat": ["naive"], "margin": 0.0}},
            {"id": "governance", "use": "governance.policy",
             "inputs": {"model": "select.model", "data": "inputs.data"}},
            {"id": "report", "use": "report.summary",
             "inputs": {"tests": ["adf.result", "kpss.result", "pp.result", "za.result",
                                  "seas.result", "acf.result"],
                        "metrics": ["backtest.metrics", "naive.metrics"],
                        "items": ["order.table", "compare.table"],
                        "model": "select.model"},
             "with": {"title": "Time-series readiness and ARIMAX"}},
        ],
        outputs={"model": "select.model", "report": "report.report",
                 "evidence": "governance.evidence"},
    ),
    tags=["forecast", "statsmodels", "governance"],
)  # fmt: skip

VAR_VECM = WorkflowTemplate(
    id="var_vecm_cointegration",
    about=About(
        title="Series that move together (VAR or VECM)",
        summary_plain="Tests whether several series stay tied together in the long run. "
        "Then it makes one model of all the series that keeps the long-run tie or uses "
        "their changes.",
        use_when=["Two to five series have an effect on each other (for example, prices, "
                  "sales and search interest)"],
        avoid_when=["A single series", "Less than about 50 periods"],
        data_needs=["Two to five numeric series over time at the same frequency. Each series "
                    "has one value for each period (a long table, or a wide sheet that you "
                    "ingest with the wide mapping)"],
        assumptions=["The relationships stay the same over the period",
                     "Effects come from past values"],
        outputs=["The cointegration rank", "A VECM or VAR model", "Granger test results",
                 "A report"],
        difficulty=Difficulty.EXPERT,
        typical_runtime=TypicalRuntime.SECONDS,
        example="Your price and the prices of two competitors share one long-run tie. A "
        "VECM with rank 1 shows that the gaps close in less than one month.",
        related=["test.johansen", "ts.vecm", "ts.var", "test.granger"],
        fits={"goal": ["compare_series", "forecast"], "data_shape": ["several_series"],
              "history": ["medium", "long"]},
    ),
    document=_doc(
        "var-vecm",
        params={
            "target": {"type": "string", "required": True,
                       "description": "The main result (one of the series)"},
            "driver": {"type": "string", "required": True,
                       "description": "A series that can possibly help to predict the target"},
            "maxlags": {"type": "integer", "default": 8, "minimum": 1, "maximum": 52},
        },
        inputs={"data": {"type": "dataset",
                         "description": "The series along one dimension (one value for each "
                         "series and period)"}},
        nodes=[
            {"id": "johansen", "use": "test.johansen", "inputs": {"data": "inputs.data"}},
            # 0 < rank < k: tied in the long run (VECM). rank = k: every series is
            # stationary (VAR on the levels). rank = 0: random walks (VAR on the changes).
            {"id": "tied", "use": "branch.if", "label": "Tied in the long run?",
             "with": {"condition": "nodes.johansen.rank > 0 and "
                                   "nodes.johansen.rank < len(nodes.johansen.trace_stats)",
                      "explain_then": "The test found that the series stay tied together in "
                                      "the long run. The workflow fitted a model that keeps "
                                      "that tie (VECM).",
                      "explain_else": "The test found no long-run tie between the series, so "
                                      "the workflow fitted a VAR and not a VECM."}},
            {"id": "rank", "use": "search.coint_rank", "inputs": {"data": "inputs.data"},
             "when": "tied.then"},
            {"id": "vecm", "use": "model.fit", "inputs": {"data": "inputs.data"},
             "when": "tied.then",
             "with": {"model": "ts.vecm@^1",
                      "settings": {"coint_rank": _x("min(max(nodes.rank.rank, 1), "
                                                    "len(nodes.johansen.trace_stats) - 1)"),
                                   "k_ar_diff": 1}}},
            {"id": "stationary", "use": "branch.if", "label": "Already stable?",
             "when": "tied.else",
             "with": {"condition": "nodes.johansen.rank >= len(nodes.johansen.trace_stats)",
                      "explain_then": "Each series already returns to a steady level, so the "
                                      "workflow fitted the VAR on the series as they are.",
                      "explain_else": "The series wander and have no long-run tie. The "
                                      "workflow applied a difference to the series before it "
                                      "fitted the VAR."}},
            {"id": "diff", "use": "transform.difference", "inputs": {"data": "inputs.data"},
             "when": "stationary.else"},
            {"id": "lags", "use": "search.var_lag_order",
             "inputs": {"data": ["diff.data", "inputs.data"]}, "when": "tied.else",
             "with": {"maxlags": _p("maxlags")}},
            {"id": "var", "use": "model.fit", "inputs": {"data": ["diff.data", "inputs.data"]},
             "when": "tied.else",
             "with": {"model": "ts.var@^1", "settings": {"lags": _x("nodes.lags.best_lag")}}},
            {"id": "granger", "use": "test.granger",
             "inputs": {"data": ["diff.data", "inputs.data"]},
             "with": {"y": _p("target"), "x": _p("driver"), "maxlag": _p("maxlags")}},
            {"id": "report", "use": "report.summary",
             "inputs": {"tests": ["johansen.result", "granger.result"],
                        "items": ["rank.table", "lags.table"],
                        "model": ["vecm.model", "var.model"]},
             "with": {"title": "Series that move together"}},
        ],
        outputs={"report": "report.report"},
    ),
    tags=["multivariate", "statsmodels"],
)  # fmt: skip

MMM_GOVERNED = WorkflowTemplate(
    id="mmm_governed",
    about=About(
        title="Marketing mix with governance",
        summary_plain="Does a check of your drivers and fits the marketing mix model. Then "
        "it runs the governance checks of your organisation and makes a report and an "
        "evidence pack for approval.",
        use_when=["You will use a marketing mix model to make budget decisions",
                  "The model risk team or the auditors need evidence"],
        avoid_when=["You only want a quick look at the data (use the guided template)"],
        data_needs=["Weekly sales and the spend for each channel (for each market, if you "
                    "have this data)"],
        assumptions=["Your organisation has a governance policy for marketing mix models"],
        outputs=["A fitted marketing mix model", "Governance evidence",
                 "A report and an evidence pack"],
        difficulty=Difficulty.INTERMEDIATE,
        typical_runtime=TypicalRuntime.LONG,
        example="The model passes the shuffled-result and fake-channel checks. Then it waits "
        "for the approval of a reviewer.",
        related=[MMM_MODEL_ID, "governance.policy", "null.noise_regressors", "test.vif"],
        fits={"goal": ["explain_marketing", "validate_model"], "data_shape": ["panel",
              "one_series"], "history": ["medium", "long"]},
    ),
    document=_doc(
        "mmm-governed",
        params={
            "drivers": {"type": "array", "required": True,
                        "description": "The spend and control columns to test for collinearity"},
            "holdout_weeks": {"type": "integer", "default": 8, "minimum": 1, "maximum": 52},
        },
        inputs={"data": {"type": "dataset"}},
        nodes=[
            {"id": "vif", "use": "test.vif", "inputs": {"data": "inputs.data"},
             "with": {"vars": _p("drivers")}},
            {"id": "holdout", "use": "split.holdout", "inputs": {"data": "inputs.data"},
             "with": {"split": {"kind": "time_holdout", "dim": "time",
                                "last": _p("holdout_weeks")}}},
            {"id": "fit", "use": "model.fit",
             "inputs": {"data": "inputs.data", "split": "holdout.plan"},
             "with": {"model": f"{MMM_MODEL_ID}@^1",
                      "settings": copy.deepcopy(MMM_TEMPLATE_SETTINGS),
                      "evaluate": {"metrics": ["mape", "coverage_90"]}}},
            {"id": "governance", "use": "governance.policy",
             "inputs": {"model": "fit.model", "data": "inputs.data"},
             "with": {"submit": True}},
            {"id": "report", "use": "report.summary",
             "inputs": {"tests": ["vif.result"], "metrics": ["fit.metrics"],
                        "model": "fit.model"},
             "with": {"title": "Marketing mix model"}},
            {"id": "pack", "use": "report.evidence_pack",
             "inputs": {"model": "fit.model", "evidence": "governance.evidence"}},
        ],
        outputs={"model": "fit.model", "evidence": "governance.evidence",
                 "pack": "pack.report"},
    ),
    tags=["mmm", "mmm-framework", "governance"],
)  # fmt: skip

_BT = {"inputs": {"data": "inputs.data", "split": "folds.plan"}}
FORECAST_BAKEOFF = WorkflowTemplate(
    id="forecast_bakeoff",
    about=About(
        title="Forecast bake-off",
        summary_plain="Backtests some forecast models on the same periods and compares them "
        "with simple benchmarks. Then it fits the winner again.",
        use_when=["You do not know which forecast method is best for your series"],
        avoid_when=["You already have a model that you trust"],
        data_needs=["One numeric series over time, at least two seasons long"],
        assumptions=["The backtest periods are similar to the future"],
        outputs=["Backtest errors for each model and horizon", "The winner, fitted again",
                 "A comparison report"],
        difficulty=Difficulty.BEGINNER,
        typical_runtime=TypicalRuntime.LONG,
        example="ETS wins with 7% error at 4 weeks. ARIMA has 8%, Theta 9% and seasonal naive "
        "12%.",
        related=["model.backtest", "compare.models", "select.best", "ts.ets", "ts.theta"],
        fits={"goal": ["forecast"], "data_shape": ["one_series"],
              "history": ["medium", "long"]},
    ),
    document=_doc(
        "forecast-bakeoff",
        params={
            "horizons": {"type": "array", "default": [1, 4, 8]},
            "season": {"type": "integer", "default": 52, "minimum": 2, "maximum": 400},
            "n_folds": {"type": "integer", "default": 6, "minimum": 2, "maximum": 50},
        },
        inputs={"data": {"type": "dataset"}},
        nodes=[
            {"id": "folds", "use": "split.expanding_window", "inputs": {"data": "inputs.data"},
             "with": {"horizons": _p("horizons"), "n_folds": _p("n_folds"), "step": 4}},
            {"id": "sarimax", "use": "model.backtest", "label": "ARIMA", **_BT,
             "with": {"model": "ts.sarimax@^1",
                      "settings": {"order": [1, 1, 1], "seasonal_order": [0, 1, 0, 52]}}},
            {"id": "ets", "use": "model.backtest", "label": "Exponential smoothing", **_BT,
             "with": {"model": "ts.ets@^1", "settings": {"trend": "add", "damped_trend": True}}},
            {"id": "theta", "use": "model.backtest", "label": "Theta", **_BT,
             "with": {"model": "ts.theta@^1"}},
            {"id": "smooth", "use": "model.backtest", "label": "Smooth trend", **_BT,
             "with": {"model": "ts.hsgp_trend@^1", "fit": {"draws": 200, "tune": 200}}},
            {"id": "naive", "use": "evaluate.baseline", "label": "Naive forecast", **_BT,
             "with": {"methods": ["naive", "seasonal_naive"], "season": _p("season")}},
            {"id": "compare", "use": "compare.models",
             "inputs": {"candidates": ["sarimax.metrics", "ets.metrics", "theta.metrics",
                                       "smooth.metrics", "naive.metrics"]},
             "with": {"metric": "mase"}},
            {"id": "best", "use": "select.best", "inputs": {"table": "compare.table"},
             "with": {"metric": "mase", "must_beat": ["naive"]}},
            {"id": "refit", "use": "model.fit", "inputs": {"data": "inputs.data"},
             "with": {"model": _x("nodes.best.selected_model")}},
            {"id": "report", "use": "report.summary",
             "inputs": {"items": ["compare.table"], "model": "refit.model"},
             "with": {"title": "Forecast bake-off"}},
        ],
        outputs={"model": "refit.model", "comparison": "compare.table",
                 "report": "report.report"},
    ),
    tags=["forecast", "comparison"],
)  # fmt: skip

# ================================================================ M5: Bayesian model checks

_FIT_PARAMS: dict[str, Any] = {
    "draws": {"type": "integer", "default": 1000, "minimum": 10, "maximum": 100000,
              "description": "The posterior draws for each chain"},
    "tune": {"type": "integer", "default": 1000, "minimum": 0, "maximum": 100000,
             "description": "The tuning steps for each chain (the sampler drops them)"},
    "chains": {"type": "integer", "default": 4, "minimum": 2, "maximum": 16,
               "description": "The number of chains (two or more, so that R-hat is "
                              "available)"},
    "seed": {"type": "integer", "default": 1, "minimum": 0,
             "description": "The random seed of the fit"},
}  # fmt: skip
_FIT: dict[str, Any] = {"draws": _p("draws"), "tune": _p("tune"), "chains": _p("chains"),
                        "seed": _p("seed")}  # fmt: skip
_MODEL_PARAMS: dict[str, Any] = {
    "settings": {"type": "object", "default": {},
                 "description": "The settings of the model (see its About card)"},
    "bind": {"type": "object", "default": {},
             "description": "The binding of the dataset names to the names of the model "
                            "(dims and vars), as in data.bind of a model spec"},
}  # fmt: skip


def _or_null(param: str) -> dict[str, str]:
    """A string parameter, or null when it is empty (the recipe then finds the value)."""
    return _x(f"if(params.{param} == '', null, params.{param})")


_VARS = _x("if(len(params.vars) > 0, params.vars, null)")
"""The ``vars`` parameter, or null (all the variables) when it is empty."""


BAYES_WORKFLOW_AUDIT = WorkflowTemplate(
    id="bayes_workflow_audit",
    about=About(
        title="Bayesian workflow audit",
        summary_plain="Fits a Bayesian model and does each check of a careful workflow: the "
        "starting assumptions, the sampler, the predictions, the effect of the priors and "
        "the recovery of known values. Then it writes one report with the plots.",
        use_when=["You want to trust a Bayesian model before you use its ranges",
                  "A reviewer asks for the standard checks of a Bayesian workflow",
                  "You changed a model and want to do all the checks again"],
        avoid_when=["The model gives only point estimates (the checks need draws)",
                    "You need a result in seconds (the recovery check fits the model many "
                    "times)"],
        data_needs=["A dataset that the model can fit",
                    "The model reference, its settings and, if the names are different, "
                    "the binding of the dataset to the model"],
        assumptions=["The model gives posterior draws and can predict the fit data",
                     "The fit has two or more chains, so that the workflow can measure R-hat"],
        outputs=["A fitted model version",
                 "A table of the parameters with R-hat and the effective sample size",
                 "The coverage of the 50% and 90% ranges, the PIT distance and a p-value",
                 "The prior sensitivity and recovery evidence",
                 "A report with the prior predictive, pair, predictive and estimate plots"],
        difficulty=Difficulty.INTERMEDIATE,
        typical_runtime=TypicalRuntime.LONG,
        example="Radon in 85 counties: R-hat 1.00, 89% of the homes in the 90% ranges, and "
        "priors two times wider move the floor effect by 3%. The audit passes.",
        related=["model.fit", "model.summary", "evaluate.ppc", "plot.render",
                 "governance.check", "sensitivity.prior", "recovery.simulated",
                 "tabular.hierarchical_glm"],
        references=[AboutReference(title="Gelman et al. (2020), Bayesian Workflow",
                                   url="https://arxiv.org/abs/2011.01808"),
                    AboutReference(title="Gabry et al. (2019), Visualization in Bayesian "
                                   "workflow", url="https://arxiv.org/abs/1709.01449")],
        expert_notes="The fit keeps prior_predictive.draws prior predictive draws for "
        "core.prior_predictive (set prior_predictive to {} for a model that cannot sample "
        "its priors, for example an ICAR prior). The branch 'converged' needs the largest R-hat "
        "(rank-normalised split R-hat over all the posterior values, up to 20 000) under "
        "rhat_max and the smallest bulk ESS at or over min_ess. Only then do evaluate.ppc, "
        "sensitivity.prior (refits with the prior widths times each of prior_scales) and "
        "recovery.simulated (n_sims refits on simulated targets, at the refit budget of the "
        "governance run) run. The branch 'trusted' needs ppc.ok and no fail or error from "
        "the two governance checks. A check that does not apply (skipped) does not block. "
        "The plots use on_failure: continue, so a recipe that cannot read a model gives a "
        "line in the report and does not stop the audit.",
        fits={"goal": ["validate_model"], "uncertainty": ["essential"],
              "experience": ["intermediate", "expert"]},
    ),
    document=_doc(
        "bayes-workflow-audit",
        params={
            "model": {"type": "string", "required": True,
                      "description": "The model to audit, for example "
                                     "tabular.hierarchical_glm@^1 or sci.heat_plate@^1"},
            **_MODEL_PARAMS,
            **_FIT_PARAMS,
            "prior_predictive": {"type": "object", "default": {"draws": 500},
                                 "description": "The prior predictive draws of the fit, as "
                                                "{draws: N}. Type {} for a model that cannot "
                                                "sample its priors (the workflow then does "
                                                "not draw the prior plot)"},
            "vars": {"type": "array", "default": [],
                     "description": "The parameters of the pair plot and the estimate "
                                    "plot (empty: the plots select them)"},
            "rhat_max": {"type": "number", "default": 1.01, "minimum": 1.0, "maximum": 1.5,
                         "description": "The largest R-hat that passes"},
            "min_ess": {"type": "number", "default": 400, "minimum": 0,
                        "description": "The smallest bulk effective sample size that "
                                       "passes (100 for each of 4 chains)"},
            "statistic": {"type": "string", "default": "sd",
                          "enum": ["mean", "sd", "max", "min"],
                          "description": "The statistic of the predictive p-value"},
            "prior_scales": {"type": "array", "default": [0.5, 2.0],
                             "description": "The prior widths that the sensitivity check "
                                            "tries, as multiples of the model priors"},
            "n_sims": {"type": "integer", "default": 20, "minimum": 5, "maximum": 100,
                       "description": "The simulated datasets of the recovery check (each "
                                      "one is a fit)"},
        },
        inputs={"data": {"type": "dataset", "description": "The dataset that the model fits"}},
        nodes=[
            {"id": "fit", "use": "model.fit", "label": "Fit with prior draws",
             "inputs": {"data": "inputs.data"},
             "with": {"model": _p("model"), "settings": _p("settings"), "bind": _p("bind"),
                      "fit": {**_FIT, "prior_predictive": _x(
                          "if(len(params.prior_predictive) > 0, params.prior_predictive, "
                          "null)")}}},
            {"id": "has_prior", "use": "branch.if", "label": "Prior draws?",
             "with": {"condition": "len(params.prior_predictive) > 0",
                      "explain_then": "The run asked for draws from the priors, so the "
                                      "workflow compared them with the data.",
                      "explain_else": "The run did not ask for draws from the priors, so "
                                      "the workflow did not draw the prior plot."}},
            {"id": "prior_plot", "use": "plot.render", "label": "Prior predictive check",
             "inputs": {"model": "fit.model"}, "when": "has_prior.then",
             "on_failure": "continue",
             "with": {"recipe": "core.prior_predictive", "source": "model"}},
            {"id": "summary", "use": "model.summary", "label": "Parameters and convergence",
             "inputs": {"model": "fit.model"}},
            {"id": "pair_plot", "use": "plot.render", "label": "Pair plot",
             "inputs": {"model": "fit.model"}, "on_failure": "continue",
             "with": {"recipe": "core.pair", "source": "model",
                      "settings": {"var_names": _VARS}}},
            {"id": "converged", "use": "branch.if", "label": "Did the sampler converge?",
             "with": {"condition": "coalesce(nodes.summary.max_rhat, 99) < params.rhat_max "
                                   "and coalesce(nodes.summary.min_ess, 0) >= params.min_ess",
                      "explain_then": "R-hat and the effective sample size are in their "
                                      "limits, so the chains agree. The workflow did the "
                                      "checks of the predictions and the priors.",
                      "explain_else": "R-hat is too large or the effective sample size is "
                                      "too small, so the chains do not agree. The workflow "
                                      "stopped the audit. Fit with more draws and tuning "
                                      "steps, or change the model."}},
            {"id": "ppc", "use": "evaluate.ppc", "label": "Predictions against the data",
             "inputs": {"model": "fit.model"}, "when": "converged.then",
             "on_failure": "continue", "with": {"statistic": _p("statistic")}},
            {"id": "ppc_plot", "use": "plot.render", "label": "Posterior predictive check",
             "inputs": {"model": "fit.model"}, "when": "converged.then",
             "on_failure": "continue",
             "with": {"recipe": "core.ppc", "source": "predictions"}},
            {"id": "sensitivity", "use": "governance.check", "label": "Prior sensitivity",
             "inputs": {"model": "fit.model", "data": "inputs.data"},
             "when": "converged.then",
             "with": {"check": "sensitivity.prior",
                      "params": {"scales": _p("prior_scales")}}},
            {"id": "recovery", "use": "governance.check", "label": "Recovery of known values",
             "inputs": {"model": "fit.model", "data": "inputs.data"},
             "when": "converged.then",
             "with": {"check": "recovery.simulated", "params": {"n_sims": _p("n_sims")}}},
            {"id": "trusted", "use": "branch.if", "label": "Can you trust the model?",
             "when": "converged.then",
             "with": {"condition": "coalesce(nodes.ppc.ok, false) and "
                                   "nodes.sensitivity.status not in ['fail', 'error'] and "
                                   "nodes.recovery.status not in ['fail', 'error']",
                      "explain_then": "No check failed. The model makes data like the "
                                      "observed data, and the prior and recovery checks "
                                      "did not fail. Read their warnings, then use the "
                                      "estimates.",
                      "explain_else": "One or more checks failed. Look at the predictive "
                                      "check, the prior sensitivity and the recovery "
                                      "evidence before you use the estimates."}},
            {"id": "estimates", "use": "plot.render", "label": "Estimates",
             "inputs": {"model": "fit.model"}, "when": "trusted.then",
             "on_failure": "continue",
             "with": {"recipe": "core.posterior_forest", "source": "model",
                      "settings": {"var_names": _VARS}}},
            {"id": "report", "use": "report.summary",
             "inputs": {"items": ["summary.table"], "metrics": ["ppc.metrics"],
                        "plots": ["prior_plot.plot", "pair_plot.plot", "ppc_plot.plot",
                                  "estimates.plot"],
                        "model": "fit.model"},
             "with": {"title": "Bayesian workflow audit"}},
        ],
        outputs={"model": "fit.model", "report": "report.report",
                 "summary": "summary.table", "ppc": "ppc.metrics",
                 "sensitivity": "sensitivity.evidence", "recovery": "recovery.evidence"},
    ),
    tags=["bayesian", "diagnostics", "governance"],
)  # fmt: skip

_IMAGE = {"var": _or_null("var"), "frame": _or_null("frame")}
SCI_IMAGE_MODEL_CHECK = WorkflowTemplate(
    id="sci_image_model_check",
    about=About(
        title="Image model check",
        summary_plain="Fits a model that predicts an image and compares its predicted images "
        "with the observed image. It shows the differences as maps and writes a report.",
        use_when=["Your model predicts a full image, for example a sky map, a lensed galaxy "
                  "or a pattern",
                  "You want to see where on the image the model does not agree with the data"],
        avoid_when=["The observed values are only at a few points (the residual maps are "
                    "then almost empty)",
                    "The model does not predict an image"],
        data_needs=["An observed image with two image dims, for example y and x",
                    "The model reference, its settings and, if the names are different, "
                    "the binding of the dataset to the model"],
        assumptions=["The model gives predictive draws of the image on the grid of the "
                     "observed image",
                     "The noise of the data is in the predictive draws or in a variable "
                     "named sigma"],
        outputs=["A fitted model version",
                 "A table of the parameters with R-hat and the effective sample size",
                 "Maps of the observed image, the mean prediction and the residuals",
                 "The observed image next to predicted images on one colour scale",
                 "The coverage of the 50% and 90% ranges and a report"],
        difficulty=Difficulty.INTERMEDIATE,
        typical_runtime=TypicalRuntime.MINUTES,
        example="A sky map: the normalised residuals look like noise with sd 1.02, 91% of "
        "the pixels are in the 90% ranges, and you cannot find the real map in the grid.",
        related=["model.fit", "plot.render", "evaluate.ppc", "model.summary",
                 "sci.cmb_field", "sci.strong_lens", "sci.turing_pattern"],
        references=[AboutReference(title="Gabry et al. (2019), Visualization in Bayesian "
                                   "workflow", url="https://arxiv.org/abs/1709.01449")],
        expert_notes="Both plots read the in-sample predictions of the model (source: "
        "predictions) with the observed target as observed_data. sci.image_residuals shows "
        "the observed image, the mean, the residual and (obs - mean) / sqrt(sd^2 + sigma^2). "
        "The recipe adds no sigma to posterior_predictive draws. var selects the image "
        "variable when the model has more than one, and frame selects the dim of the slider "
        "(for example run or t). The predictive check pools all the pixels. It needs 10 or "
        "more predictive draws, and it does not stop the workflow when it fails.",
        fits={"goal": ["validate_model", "map_fields", "fit_physics"],
              "data_shape": ["grid"]},
    ),
    document=_doc(
        "sci-image-model-check",
        params={
            "model": {"type": "string", "required": True,
                      "description": "The image model, for example sci.cmb_field@^1"},
            **_MODEL_PARAMS,
            **_FIT_PARAMS,
            "var": {"type": "string", "default": "",
                    "description": "The predicted image variable (empty: the plots find it)"},
            "frame": {"type": "string", "default": "",
                      "description": "The dim with one image for each level, for example "
                                     "run or t (empty: the plots find it)"},
            "n_draws": {"type": "integer", "default": 5, "minimum": 1, "maximum": 11,
                        "description": "The predicted images next to the observed image"},
            "shuffle": {"type": "boolean", "default": False,
                        "description": "Hide which panel is the observed image (the "
                                       "caption gives the answer)"},
            "statistic": {"type": "string", "default": "sd",
                          "enum": ["mean", "sd", "max", "min"],
                          "description": "The statistic of the predictive p-value"},
        },
        inputs={"data": {"type": "dataset",
                         "description": "The observed image (or images) and the inputs of "
                                        "the model"}},
        nodes=[
            {"id": "fit", "use": "model.fit", "inputs": {"data": "inputs.data"},
             "with": {"model": _p("model"), "settings": _p("settings"), "bind": _p("bind"),
                      "fit": _FIT}},
            {"id": "summary", "use": "model.summary", "label": "Parameters and convergence",
             "inputs": {"model": "fit.model"}},
            {"id": "residuals", "use": "plot.render", "label": "Residual maps",
             "inputs": {"model": "fit.model"},
             "with": {"recipe": "sci.image_residuals", "source": "predictions",
                      "settings": _IMAGE}},
            {"id": "grid", "use": "plot.render", "label": "Observed and predicted images",
             "inputs": {"model": "fit.model"},
             "with": {"recipe": "sci.ppc_image_grid", "source": "predictions",
                      "settings": {**_IMAGE, "n_draws": _p("n_draws"),
                                   "shuffle": _p("shuffle")}}},
            {"id": "ppc", "use": "evaluate.ppc", "label": "Predictions against the data",
             "inputs": {"model": "fit.model"}, "on_failure": "continue",
             "with": {"statistic": _p("statistic")}},
            {"id": "report", "use": "report.summary",
             "inputs": {"items": ["summary.table"], "metrics": ["ppc.metrics"],
                        "plots": ["residuals.plot", "grid.plot"], "model": "fit.model"},
             "with": {"title": "Image model check"}},
        ],
        outputs={"model": "fit.model", "report": "report.report",
                 "summary": "summary.table", "ppc": "ppc.metrics"},
    ),
    tags=["science", "image", "diagnostics"],
)  # fmt: skip

WORKFLOW_TEMPLATES: Final[dict[str, WorkflowTemplate]] = {
    t.id: t
    for t in (TS_READINESS_ARIMAX, VAR_VECM, MMM_GOVERNED, FORECAST_BAKEOFF,
              BAYES_WORKFLOW_AUDIT, SCI_IMAGE_MODEL_CHECK)
}  # fmt: skip
"""The gallery (``GET /v1/workflow-templates``)."""


def get_workflow_template(template_id: str) -> WorkflowTemplate:
    """A gallery template (``KeyError`` when unknown)."""
    return WORKFLOW_TEMPLATES[template_id]


def instantiate_template(
    template: WorkflowTemplate | str, *, name: Slug, project: Slug
) -> WorkflowSpec:
    """A copy of a template's workflow named ``name`` in ``project``."""
    t = WORKFLOW_TEMPLATES[template] if isinstance(template, str) else template
    doc = copy.deepcopy(t.document)
    meta = doc["metadata"]
    assert isinstance(meta, dict)
    meta["name"] = name
    meta["project"] = project
    labels = meta.setdefault("labels", {})
    assert isinstance(labels, dict)
    labels["workflow-template"] = t.id
    return WorkflowSpec.model_validate(doc)
