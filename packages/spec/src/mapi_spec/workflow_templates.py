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
``sci_phylo_signal``        sci.phylo_regression with iid, Brownian, Pagel's lambda and OU
                            residuals -> elpd_loo ranking, stacking weights, select -> the
                            winner's slope of ``focus`` (model.summary) -> branch on whether
                            its HDI excludes zero (then a predictive check) -> sci.phylogeny
                            -> report
``sci_calibrate_then_design``
                            sci.heat_plate fitted with one calibration run held out
                            (evaluate_then_refit) -> parameter summary, sci.image_residuals ->
                            branch on the holdout RMSE and R-hat -> model.score (predict) of
                            the new heater layout -> sci.field_map -> report
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

# ============================================================== M5: ladders of science models


def _fit_params(seed: int) -> dict[str, Any]:
    """The MCMC settings that every rung of a ladder shares (one value for each rung, so the
    rungs are comparable)."""
    return {
        "draws": {"type": "integer", "default": 1000, "minimum": 50, "maximum": 20000,
                  "description": "The posterior draws to keep in each chain"},
        "tune": {"type": "integer", "default": 1000, "minimum": 50, "maximum": 20000,
                 "description": "The warm-up steps of each chain (the sampler discards them)"},
        "chains": {"type": "integer", "default": 4, "minimum": 2, "maximum": 16,
                   "description": "The number of chains (two or more, for R-hat)"},
        "target_accept": {"type": "number", "default": 0.9, "minimum": 0.6, "maximum": 0.99,
                          "description": "The target acceptance rate of the sampler. A "
                          "higher value gives smaller steps and fewer divergences"},
        "engine": {"type": "string", "default": "pymc", "enum": ["pymc", "nutpie"],
                   "description": "The sampler (nutpie is faster on large data)"},
        "seed": {"type": "integer", "default": seed, "minimum": 0, "maximum": 2**31 - 1,
                 "description": "The random seed of each fit"},
    }  # fmt: skip


_LADDER_FIT = {"mode": "train_only", "draws": _p("draws"), "tune": _p("tune"),
               "chains": _p("chains"), "target_accept": _p("target_accept"),
               "engine": _p("engine"), "seed": _p("seed")}  # fmt: skip
"""Each rung fits on all the data: PSIS-LOO gives the honest score, so no holdout is lost."""
_ELPD = {"metrics": ["elpd_loo"]}

_AREAL = "sci.areal_bym2@^1"
_AREAL_RUNGS = (("iid", "Independent areas"), ("icar", "Neighbours share risk (ICAR)"),
                ("bym2", "Neighbours and noise (BYM2)"))  # fmt: skip
_B1 = "nodes.effect.params.b1"
_PRIOR_RUNGS = (("wide", "Wider priors", "params.prior_scale"),
                ("narrow", "Narrower priors", "1 / params.prior_scale"))  # fmt: skip

SCI_SPATIAL_LADDER = WorkflowTemplate(
    id="sci_spatial_ladder",
    about=About(
        title="Disease map: which spatial model?",
        summary_plain="Fits three models of disease counts on a map, from independent areas "
        "to areas that share their risk with their neighbours. Then it compares how well "
        "each model predicts, keeps the best one and tells you if the effect of your "
        "covariate stays clear.",
        use_when=["You have counts and expected counts for each area of a map, and one "
                  "covariate",
                  "You must know if the neighbours of an area change the answer",
                  "You need a map of the risk with evidence that the fit is good"],
        avoid_when=["You have the location of each case, not counts for each area",
                    "The areas have no neighbours (all of them are islands)",
                    "You need a fast answer (the workflow fits the model three times. When "
                    "the effect is clear, it fits the best model two more times)"],
        data_needs=["A count of cases and an expected count for each area",
                    "One covariate for each area, for example a percentage",
                    "An adjacency matrix of the areas and the outline of each area "
                    "(python -m mapi_plugin_science.export e18 makes this dataset from the "
                    "Scottish lip cancer data)"],
        assumptions=["All three models use the same areas in the same order",
                     "The covariate has the same effect in every area",
                     "Leave-one-area-out scores (PSIS-LOO) are reliable for these data. The "
                     "comparison table shows a warning when they are not"],
        outputs=["A table that compares the three models (elpd_loo) and their stacking "
                 "weights",
                 "The best model, with the range of the covariate effect and a decision: "
                 "clear or not clear",
                 "Convergence evidence for the best model",
                 "When the effect is clear, the range of the effect with wider and narrower "
                 "priors",
                 "A map of the relative risk and a report with the plot"],
        difficulty=Difficulty.EXPERT,
        typical_runtime=TypicalRuntime.MINUTES,
        example="On the 56 districts of Scotland, the effect of outdoor work on the log risk "
        "falls from 0.67 (independent areas) to 0.40 (BYM2) per 10 points when the areas "
        "share their risk. The workflow shows which model predicts best and if the effect "
        "stays clear.",
        related=["sci.areal_bym2", "compare.stacking", "model.summary", "plot.render",
                 "select.best"],
        references=[AboutReference(title="Riebler, Sørbye, Simpson & Rue (2016), An "
                                   "intuitive Bayesian spatial model for disease mapping "
                                   "that accounts for scaling", url=None),
                    AboutReference(title="Yao, Vehtari, Simpson & Gelman (2018), Using "
                                   "stacking to average Bayesian predictive distributions",
                                   url=None)],
        fits={"goal": ["validate_model", "explain_drivers"], "data_shape": ["table"],
              "frequency": ["not_time"], "uncertainty": ["essential"]},
        expert_notes="Poisson(E_i exp(b0 + b1 x_i + u_i)) with u iid, ICAR or BYM2 (scaled "
        "ICAR plus iid with mixing share rho). Each rung stores the pointwise PSIS-LOO "
        "values, so compare.stacking solves the stacking weights over the same areas. The "
        "branch reads the HDI of b1 on the rung that select.best keeps. When it excludes "
        "zero, that rung is fitted again with the prior sds of b0 and b1 and the "
        "HalfNormal scale of sigma multiplied and divided by prior_scale.",
    ),
    document=_doc(
        "sci-spatial-ladder",
        params={
            "bind": {"type": "object",
                     "default": {"dims": {"area": "district", "area_": "district_"},
                                 "vars": {"covariate": "aff"}},
                     "description": "The names in your dataset of the area dims and of the "
                     "covariate (the default fits the e18 export)"},
            "islands": {"type": "string", "default": "link_nearest",
                        "enum": ["link_nearest", "isolate", "error"],
                        "description": "What to do with areas that have no neighbours. "
                        "link_nearest links each one to the area with the closest border"},
            "hdi_prob": {"type": "number", "default": 0.94, "minimum": 0.5, "maximum": 0.999,
                         "description": "The probability inside the range of each parameter"},
            "prior_scale": {"type": "number", "default": 2.0, "minimum": 1.1, "maximum": 10,
                            "description": "When the effect is clear, the workflow fits the "
                            "best model again with the prior widths multiplied and divided "
                            "by this value"},
            **_fit_params(18),
        },
        inputs={"data": {"type": "dataset",
                         "description": "Counts, expected counts, a covariate, the adjacency "
                         "matrix and the outlines of the areas"}},
        nodes=[
            *({"id": kind, "use": "model.fit", "label": label,
               "inputs": {"data": "inputs.data"},
               "with": {"model": _AREAL, "bind": _p("bind"),
                        "settings": {"kind": kind, "islands": _p("islands")},
                        "fit": _LADDER_FIT, "evaluate": _ELPD}}
              for kind, label in _AREAL_RUNGS),
            {"id": "compare", "use": "compare.models",
             "inputs": {"candidates": ["iid.metrics", "icar.metrics", "bym2.metrics"]},
             "with": {"metric": "elpd_loo", "direction": "max"}},
            {"id": "weights", "use": "compare.stacking", "label": "Stacking weights",
             "inputs": {"candidates": ["iid.metrics", "icar.metrics", "bym2.metrics"]}},
            {"id": "best", "use": "select.best",
             "inputs": {"table": "compare.table",
                        "models": ["iid.model", "icar.model", "bym2.model"]},
             "with": {"metric": "elpd_loo", "direction": "max"}},
            {"id": "effect", "use": "model.summary", "label": "Covariate effect",
             "inputs": {"model": "best.model"},
             "with": {"vars": ["b0", "b1", "sigma"], "hdi_prob": _p("hdi_prob")}},
            {"id": "clear", "use": "branch.if", "label": "Is the effect clear?",
             "with": {"condition": f"{_B1}.hdi_low > 0 or {_B1}.hdi_high < 0",
                      "explain_then": "The range of the covariate effect in the best model "
                                      "does not include zero. The covariate has a clear "
                                      "effect on the risk. The workflow fits the best model "
                                      "again with wider and narrower priors.",
                      "explain_else": "The range of the covariate effect in the best model "
                                      "includes zero. When the areas share their risk, the "
                                      "data does not show a clear effect of the covariate."}},
            {"id": "converged", "use": "governance.check", "label": "Did the chains agree?",
             "inputs": {"model": "best.model", "data": "inputs.data"},
             "with": {"check": "convergence"}},
            # a clear effect must come from the data, not from the priors: refit the best
            # rung with the prior widths of b0, b1 and sigma multiplied and divided by
            # prior_scale (governance.check sensitivity.prior cannot find these widths in
            # the settings of sci.areal_bym2, so the workflow writes them out)
            *({"id": rid, "use": "model.fit", "label": label, "when": "clear.then",
               "inputs": {"data": "inputs.data"},
               "with": {"model": _AREAL, "bind": _p("bind"),
                        "settings": {"kind": _x("nodes.best.selected"),
                                     "islands": _p("islands"), "intercept_sd": _x(width),
                                     "slope_sd": _x(width), "sigma_scale": _x(width)},
                        "fit": _LADDER_FIT}}
              for rid, label, width in _PRIOR_RUNGS),
            *({"id": f"effect_{rid}", "use": "model.summary", "label": f"{label}: effect",
               "inputs": {"model": f"{rid}.model"}, "when": "clear.then",
               "with": {"vars": ["b1"], "hdi_prob": _p("hdi_prob")}}
              for rid, label, _w in _PRIOR_RUNGS),
            {"id": "map", "use": "plot.render", "label": "Relative risk map",
             "inputs": {"model": "best.model"},
             "with": {"recipe": "sci.area_map", "source": "model",
                      "settings": {"var": "rr", "stats": ["mean", "sd", "exceedance"],
                                   "threshold": 1.0},
                      "title": "Relative risk of each area (best model)"}},
            {"id": "report", "use": "report.summary",
             "inputs": {"items": ["compare.table", "weights.table", "effect.table",
                                  "effect_wide.table", "effect_narrow.table"],
                        "metrics": ["iid.metrics", "icar.metrics", "bym2.metrics"],
                        "model": "best.model", "plots": ["map.plot"]},
             "with": {"title": "Disease map: which spatial model?"}},
        ],
        outputs={"model": "best.model", "comparison": "compare.table",
                 "weights": "weights.table", "effect": "effect.table", "map": "map.plot",
                 "convergence": "converged.evidence",
                 "report": "report.report"},
    ),
    tags=["science", "spatial", "comparison", "governance"],
)  # fmt: skip

_NETWORK = "sci.network_latent_space@^1"
_NETWORK_RUNGS = (("covariates", "Pair covariates only"),
                  ("sociality", "Covariates and activity"),
                  ("latent_space", "Covariates, activity and a hidden map"))  # fmt: skip
_NET_FITS = [f"{k}.metrics" for k, _ in _NETWORK_RUNGS]
_NET_MODELS = [f"{k}.model" for k, _ in _NETWORK_RUNGS]

SCI_NETWORK_LADDER = WorkflowTemplate(
    id="sci_network_ladder",
    about=About(
        title="Network ties: which model?",
        summary_plain="Fits three models of the ties in a network, from shared traits only "
        "to a hidden map of the group. Then it compares how well each model predicts, keeps "
        "the best one and simulates new networks to compare them with your network.",
        use_when=["You have a table of yes or no ties between the pairs of a group, for "
                  "example who works with whom",
                  "You want to know if shared traits explain the ties, or if the activity of "
                  "each node and a hidden map are necessary",
                  "You need simulated networks to do a check of the model fit"],
        avoid_when=["The ties have a direction or are counts",
                    "The network has many thousands of nodes (each model looks at every pair)",
                    "You need a fast answer (the workflow fits the model three times)"],
        data_needs=["A square table with 1 for a tie, 0 for no tie and an empty cell when "
                    "you do not know",
                    "Values for each pair, for example 1 when the two nodes are in the same "
                    "office",
                    "Optional: a group code for each node, for colors in the plots "
                    "(python -m mapi_plugin_science.export e21 makes this dataset from the "
                    "law firm data)"],
        assumptions=["All three models use the same pairs in the same order",
                     "Each pair has a tie or not, and the two directions are the same",
                     "Leave-one-pair-out scores (PSIS-LOO) are reliable for these data. The "
                     "comparison table shows a warning when they are not"],
        outputs=["A table that compares the three models (elpd_loo) and their stacking "
                 "weights",
                 "The best model, with the range of each pair effect",
                 "A plot of the network with the tie probabilities of the best model",
                 "Statistics of simulated networks (density, transitivity and more) with "
                 "a plot",
                 "A report with the plots"],
        difficulty=Difficulty.EXPERT,
        typical_runtime=TypicalRuntime.MINUTES,
        example="On the coworker network of 71 lawyers, the hidden map predicts best "
        "(elpd_loo -761.6, against -763.4 with activity and -894.2 with covariates only). "
        "The simulated networks show if the model makes as many triangles as the real "
        "network.",
        related=["sci.network_latent_space", "compare.stacking", "model.score",
                 "plot.render", "model.summary"],
        references=[AboutReference(title="Hoff, Raftery & Handcock (2002), Latent space "
                                   "approaches to social network analysis", url=None),
                    AboutReference(title="Lazega (2001), The Collegial Phenomenon", url=None)],
        fits={"goal": ["validate_model", "explain_drivers"], "data_shape": ["table"],
              "frequency": ["not_time"], "uncertainty": ["essential"]},
        expert_notes="Bernoulli-logit dyad models: covariates (alpha + x beta), sociality "
        "(+ s_i + s_j) and latent_space (- ||z_i - z_j||, Procrustes-aligned). The "
        "log-likelihood is over the observed pairs i < j, so the pointwise PSIS-LOO values "
        "of the rungs line up for stacking. model.score in simulate mode draws whole graphs "
        "from the best rung and returns graph_stat(statistic) for each draw.",
    ),
    document=_doc(
        "sci-network-ladder",
        params={
            "bind": {"type": "object",
                     "default": {"dims": {"node": "lawyer", "node_": "lawyer_",
                                          "cov": "covariate"},
                                 "vars": {"tie": "cowork", "dyad_cov": "homophily",
                                          "node_group": "office"}},
                     "description": "The names in your dataset of the node dims, the tie "
                     "table, the pair values and the group code (the default fits the e21 "
                     "export)"},
            "node_groups": {"type": "boolean", "default": True,
                            "description": "The dataset has a group code for each node "
                            "(set false and remove node_group from bind when it has not)"},
            "latent_dims": {"type": "integer", "default": 2, "minimum": 1, "maximum": 3,
                            "description": "The dimensions of the hidden map"},
            "n_sims": {"type": "integer", "default": 500, "minimum": 10, "maximum": 4000,
                       "description": "The number of networks to simulate from the best "
                       "model"},
            "hdi_prob": {"type": "number", "default": 0.94, "minimum": 0.5, "maximum": 0.999,
                         "description": "The probability inside the range of each parameter"},
            **_fit_params(21),
        },
        inputs={"data": {"type": "dataset",
                         "description": "A symmetric table of ties, the pair values and "
                         "(optional) a group code for each node"}},
        nodes=[
            *({"id": kind, "use": "model.fit", "label": label,
               "inputs": {"data": "inputs.data"},
               "with": {"model": _NETWORK, "bind": _p("bind"),
                        "settings": {"kind": kind, "node_groups": _p("node_groups"),
                                     "latent_dims": _p("latent_dims")},
                        "fit": _LADDER_FIT, "evaluate": _ELPD}}
              for kind, label in _NETWORK_RUNGS),
            {"id": "compare", "use": "compare.models", "inputs": {"candidates": _NET_FITS},
             "with": {"metric": "elpd_loo", "direction": "max"}},
            {"id": "weights", "use": "compare.stacking", "label": "Stacking weights",
             "inputs": {"candidates": _NET_FITS}},
            {"id": "best", "use": "select.best",
             "inputs": {"table": "compare.table", "models": _NET_MODELS},
             "with": {"metric": "elpd_loo", "direction": "max"}},
            {"id": "effects", "use": "model.summary", "label": "Pair effects",
             "inputs": {"model": "best.model"},
             "with": {"vars": ["alpha", "beta"], "hdi_prob": _p("hdi_prob")}},
            {"id": "graph", "use": "plot.render", "label": "Network of the best model",
             "inputs": {"model": "best.model"},
             "with": {"recipe": "net.graph", "source": "predictions",
                      "settings": {"var": "tie_prob", "panels": "both"},
                      "title": "Tie probabilities of the best model"}},
            {"id": "simulate", "use": "model.score", "label": "Simulate networks",
             "inputs": {"model": "best.model"},
             "with": {"mode": "simulate", "group": "draws",
                      "request": {"output": {"vars": ["graph_stat"],
                                             "draws": _p("n_sims")},
                                  "seed": _p("seed")}}},
            {"id": "ppc", "use": "plot.render", "label": "Statistics of simulated networks",
             "inputs": {"data": "simulate.scores"},
             "with": {"recipe": "core.ridge", "source": "data",
                      "settings": {"var": "graph_stat", "dim": "statistic",
                                   "select": {"statistic": ["density", "transitivity"]}},
                      "title": "Density and transitivity of the simulated networks"}},
            {"id": "report", "use": "report.summary",
             "inputs": {"items": ["compare.table", "weights.table", "effects.table"],
                        "metrics": _NET_FITS, "model": "best.model",
                        "plots": ["graph.plot", "ppc.plot"]},
             "with": {"title": "Network ties: which model?"}},
        ],
        outputs={"model": "best.model", "comparison": "compare.table",
                 "weights": "weights.table", "effects": "effects.table",
                 "graph": "graph.plot", "simulations": "simulate.scores",
                 "ppc": "ppc.plot", "report": "report.report"},
    ),
    tags=["science", "network", "comparison"],
)  # fmt: skip

# ============================================================================ science (M5)

_FIT_PARAMS: dict[str, Any] = {
    "draws": {"type": "integer", "default": 1000, "minimum": 10, "maximum": 20000,
              "description": "The posterior draws that each chain keeps"},
    "tune": {"type": "integer", "default": 1000, "minimum": 10, "maximum": 20000,
             "description": "The tuning steps of each chain (the sampler drops them)"},
    "chains": {"type": "integer", "default": 4, "minimum": 1, "maximum": 16,
               "description": "The number of chains (two or more give R-hat)"},
    "seed": {"type": "integer", "default": 2017, "minimum": 0, "maximum": 2**31 - 1,
             "description": "The random seed of the sampler"},
    "bind": {"type": "object", "default": {},
             "description": "The names of the model (dims and vars) mapped to the names in "
                            "your dataset, when they are different. The workflow finds the "
                            "target itself when your dataset has one target."},
}  # fmt: skip
_FIT = {"draws": _p("draws"), "tune": _p("tune"), "chains": _p("chains"), "seed": _p("seed")}

PHYLO_MODEL = "sci.phylo_regression@^1"
_PHYLO_KINDS: tuple[tuple[str, str], ...] = (
    ("iid", "No tree (independent species)"),
    ("brownian", "Brownian motion on the tree"),
    ("lambda", "Pagel's lambda"),
    ("ou", "Ornstein-Uhlenbeck"),
)
_PHYLO_FITS = [
    {"id": kind, "use": "model.fit", "label": label, "inputs": {"data": "inputs.data"},
     "with": {"model": PHYLO_MODEL,
              "settings": {"kind": kind, "predictors": _p("predictors")},
              "fit": _FIT, "bind": _p("bind"), "evaluate": {"metrics": ["elpd_loo"]}}}
    for kind, label in _PHYLO_KINDS
]  # fmt: skip
_PHYLO_METRICS = [f"{kind}.metrics" for kind, _ in _PHYLO_KINDS]

SCI_PHYLO_SIGNAL = WorkflowTemplate(
    id="sci_phylo_signal",
    about=About(
        title="Trait regression across species, with and without the tree",
        summary_plain="Fits the same regression of a species trait four times: once with "
        "species as independent points and three times with a family tree that relates "
        "them. Then it keeps the version that predicts best and tells you if the effect "
        "that you ask about is clearly different from zero.",
        use_when=["You compare a trait across species (or other groups with a known tree) "
                  "and close relatives are similar",
                  "You want to know if an effect stays clear when you include the tree in "
                  "the model"],
        avoid_when=["You have no tree of the species",
                    "Fewer than about 20 species (the four versions then predict about "
                    "equally well)"],
        data_needs=["One row for each species: the trait to explain and one or more "
                    "predictors, all positive (the model takes logs)",
                    "The tree in the same dataset: parent(node), branch_length(node), "
                    "is_tip(node) and tip_label(node), with tip labels equal to the species "
                    "labels"],
        assumptions=["The predictors have a linear effect on the log of the trait",
                     "The residuals of related species are correlated as the selected "
                     "version says (Brownian motion, Pagel's lambda or Ornstein-Uhlenbeck)"],
        outputs=["The leave-one-out score (elpd_loo) of the four versions and their stacking "
                 "weights",
                 "The selected model and its slopes, with R-hat and ESS",
                 "The decision: is the range of the effect clear of zero?",
                 "A plot of the tree with the trait of each species", "A report"],
        difficulty=Difficulty.EXPERT,
        typical_runtime=TypicalRuntime.MINUTES,
        example="Brain size of 301 primates: Pagel's lambda wins (elpd 130, against 15 "
        "without the tree). The slope of group size is 0.03, with the 94% range -0.01 to "
        "0.07. Thus the effect is not clear of zero when the model includes the tree.",
        related=["sci.phylo_regression", "compare.models", "compare.stacking",
                 "model.summary", "evaluate.ppc"],
        references=[AboutReference(title="Felsenstein (1985), Phylogenies and the "
                                         "comparative method"),
                    AboutReference(title="Pagel (1999), Inferring the historical patterns of "
                                         "biological evolution")],
        expert_notes="Four sci.phylo_regression fits that differ only in the residual "
        "kind (iid, brownian, lambda, ou) are ranked by PSIS-LOO on the conditional "
        "leave-one-out density of each species. select.best keeps the highest elpd_loo, and "
        "compare.stacking gives the stacking weights. model.summary reads the slope that "
        "focus names from the winner. The branch tests if its HDI excludes zero.",
        fits={"goal": ["explain_drivers", "validate_model"], "data_shape": ["table"],
              "frequency": ["not_time"], "uncertainty": ["essential", "nice"],
              "experience": ["expert"]},
    ),
    document=_doc(
        "sci-phylo-signal",
        params={
            "predictors": {"type": "array", "default": ["body", "group_size"],
                           "description": "The predictor variables on the species dim"},
            "focus": {"type": "string", "default": "group_size",
                      "description": "The predictor whose slope the decision tests (one of "
                                     "the predictors)"},
            "hdi_prob": {"type": "number", "default": 0.94, "minimum": 0.5, "maximum": 0.999,
                         "description": "The probability inside the range of the slope"},
            **_FIT_PARAMS,
        },
        inputs={"data": {"type": "dataset",
                         "description": "Species traits and the tree (parent, branch_length, "
                                        "is_tip and tip_label on the node dim)"}},
        nodes=[
            *_PHYLO_FITS,
            {"id": "compare", "use": "compare.models",
             "inputs": {"candidates": _PHYLO_METRICS},
             "with": {"metric": "elpd_loo", "direction": "max"}},
            {"id": "weights", "use": "compare.stacking",
             "inputs": {"candidates": _PHYLO_METRICS}},
            {"id": "best", "use": "select.best",
             "inputs": {"table": "compare.table",
                        "models": [f"{kind}.model" for kind, _ in _PHYLO_KINDS]},
             "with": {"metric": "elpd_loo", "direction": "max"}},
            {"id": "slope", "use": "model.summary", "label": "Slopes of the winner",
             "inputs": {"model": "best.model"},
             "with": {"vars": ["slope"], "focus": _p("focus"), "hdi_prob": _p("hdi_prob")}},
            {"id": "clear", "use": "branch.if", "label": "Is the effect clear of zero?",
             "with": {"condition": "nodes.slope.focus.excludes_zero",
                      "explain_then": "The range of the slope does not include zero. The "
                                      "data show an effect of the predictor when the model "
                                      "includes the tree.",
                      "explain_else": "The range of the slope includes zero. The data do "
                                      "not show a clear effect of the predictor when the "
                                      "model includes the tree."}},
            # a clear effect is reported only with a check that the winner agrees with the data
            {"id": "ppc", "use": "evaluate.ppc", "label": "Does the winner agree with the data?",
             "when": "clear.then", "inputs": {"model": "best.model"}},
            {"id": "tree", "use": "plot.render", "label": "Trait on the tree",
             "inputs": {"model": "best.model"},
             "with": {"recipe": "sci.phylogeny"}},
            {"id": "report", "use": "report.summary",
             "inputs": {"items": ["compare.table", "weights.table", "slope.table"],
                        "metrics": ["ppc.metrics"], "model": "best.model",
                        "plots": ["tree.plot"]},
             "with": {"title": "Trait regression across species"}},
        ],
        outputs={"model": "best.model", "comparison": "compare.table",
                 "weights": "weights.table", "slopes": "slope.table", "tree": "tree.plot",
                 "report": "report.report"},
    ),
    tags=["science", "bayesian", "comparison", "phylogeny"],
)  # fmt: skip

HEAT_MODEL = "sci.heat_plate@^1"
# the split and the scores use your dataset's names: the bound names, else the model's
_RUN = "coalesce(params.bind.dims.run, 'run')"
_Y = "coalesce(params.bind.dims.y, 'y')"
_X = "coalesce(params.bind.dims.x, 'x')"
SCI_CALIBRATE_THEN_DESIGN = WorkflowTemplate(
    id="sci_calibrate_then_design",
    about=About(
        title="Calibrate a heated plate, then test a heater layout",
        summary_plain="Learns how a plate conducts and loses heat from some test runs with "
        "thermocouples. Then it predicts the temperature of each point of the plate for a "
        "new heater layout, but only if the calibration predicts a test run that it did not "
        "see well enough.",
        use_when=["You measured a plate (or a similar flat part) with one heater layout for "
                  "each run and some thermocouples",
                  "You want to try a new heater layout on the computer before you build it"],
        avoid_when=["The heat does not reach a steady state in your runs",
                    "Fewer than three calibration runs (one run is held out for the test)"],
        data_needs=["The calibration runs: heater_power(run, y, x) in W and "
                    "temperature(run, y, x) in degC on a regular grid, with no value where "
                    "there is no thermocouple",
                    "Optional: a second dataset with heater_power(run, y, x) of the new "
                    "layouts on the same grid (without one, the workflow predicts the "
                    "calibration runs)"],
        assumptions=["The plate is thin and reaches a steady state (the 2-D steady heat "
                     "equation)",
                     "The material and the losses are the same in each run",
                     "The temperature rise is proportional to the heater power"],
        outputs=["The conductivity k, the face and edge losses, the heater efficiency and "
                 "the spread length, with R-hat and ESS",
                 "The error on the calibration run that the fit did not see",
                 "The decision: is the calibration good enough?",
                 "The predicted temperature of each point for the new layouts, as a map",
                 "A report"],
        difficulty=Difficulty.EXPERT,
        typical_runtime=TypicalRuntime.MINUTES,
        example="Three 10 W test runs give k = 59 W/m/K and a spread length of 13 cm. The "
        "run that the fit did not see has an error of 0.12 degC. Thus the workflow "
        "predicts the new layout: the hottest point is 33 degC.",
        related=["sci.heat_plate", "split.holdout", "model.summary", "model.score",
                 "plot.render"],
        expert_notes="sci.heat_plate solves A(theta) u = eta q on a finite-volume grid. "
        "split.holdout leaves one random run out (leave_group_out, n = 1). model.fit fits "
        "the other runs, scores the run held out and fits all the runs again "
        "(evaluate_then_refit). The branch needs the holdout RMSE at or below max_error and "
        "max R-hat at or below max_rhat. Then model.score predicts the expected field of the "
        "layout (posterior draws of the field, without sensor noise).",
        fits={"goal": ["fit_physics", "map_fields", "validate_model"],
              "data_shape": ["grid"], "frequency": ["not_time"],
              "uncertainty": ["essential", "nice"], "experience": ["expert"]},
    ),
    document=_doc(
        "sci-calibrate-then-design",
        params={
            "thickness_mm": {"type": "number", "default": 5.0, "minimum": 0.01,
                             "maximum": 1000,
                             "description": "The thickness of the plate in mm"},
            "room_temperature": {"type": "number", "default": 20.0, "minimum": -100,
                                 "maximum": 1000,
                                 "description": "The temperature of the room in degC"},
            "max_error": {"type": "number", "default": 0.5, "minimum": 0,
                          "description": "The largest RMSE in degC on the run held out "
                                         "that the design step accepts"},
            "max_rhat": {"type": "number", "default": 1.01, "minimum": 1, "maximum": 2,
                         "description": "The largest R-hat that the design step accepts"},
            **_FIT_PARAMS,
            "seed": {**_FIT_PARAMS["seed"], "default": 51},
        },
        inputs={"data": {"type": "dataset",
                         "description": "The calibration runs: heater power and the "
                                        "thermocouple readings on the grid"},
                "layout": {"type": "dataset", "required": False,
                           "description": "The new heater layouts to predict (the heater "
                                          "power of each new run on the same grid)"}},
        nodes=[
            {"id": "holdout", "use": "split.holdout", "inputs": {"data": "inputs.data"},
             "with": {"split": {"kind": "leave_group_out", "dim": _x(_RUN), "n": 1,
                                "seed": _p("seed")}}},
            {"id": "fit", "use": "model.fit", "label": "Calibrate",
             "inputs": {"data": "inputs.data", "split": "holdout.plan"},
             "with": {"model": HEAT_MODEL,
                      "settings": {"thickness_mm": _p("thickness_mm"),
                                   "room_temperature": _p("room_temperature")},
                      "fit": {**_FIT, "mode": "evaluate_then_refit", "target_accept": 0.9},
                      "bind": _p("bind"),
                      "evaluate": {"metrics": ["rmse", "mae", "coverage_90"]}}},
            {"id": "plate", "use": "model.summary", "label": "Plate parameters",
             "inputs": {"model": "fit.model"},
             "with": {"vars": ["k", "h", "h_edge", "eta", "sigma", "spread_length"]}},
            {"id": "residuals", "use": "plot.render", "label": "Calibration residuals",
             "inputs": {"model": "fit.model"},
             "with": {"recipe": "sci.image_residuals"}},
            {"id": "good", "use": "branch.if", "label": "Is the calibration good enough?",
             "with": {"condition": "nodes.fit.metrics.rmse <= params.max_error and "
                                   "nodes.plate.max_rhat <= params.max_rhat",
                      "explain_then": "The calibration predicted the run that it did not "
                                      "see within the error limit, and the sampler "
                                      "converged. The workflow predicted the new layout.",
                      "explain_else": "The calibration did not predict the run that it did "
                                      "not see within the error limit, or the sampler did "
                                      "not converge. The workflow did not predict the new "
                                      "layout. Add calibration runs or thermocouples."}},
            {"id": "design", "use": "model.score", "label": "Predict the layout",
             "when": "good.then",
             "inputs": {"model": "fit.model", "data": ["inputs.layout", "inputs.data"]},
             "with": {"mode": "predict"}},
            {"id": "field", "use": "plot.render", "label": "Field of the layout",
             "when": "good.then", "inputs": {"data": "design.scores"},
             "with": {"recipe": "sci.field_map", "source": "data",
                      "settings": {"var": "field", "y": _x(_Y), "x": _x(_X),
                                   "frame": _x(_RUN), "origin": "lower",
                                   "colormap": "magma"}}},
            {"id": "report", "use": "report.summary",
             "inputs": {"items": ["plate.table"], "metrics": ["fit.metrics"],
                        "model": "fit.model", "plots": ["residuals.plot", "field.plot"]},
             "with": {"title": "Calibrate a heated plate, then test a heater layout"}},
        ],
        outputs={"model": "fit.model", "parameters": "plate.table",
                 "residuals": "residuals.plot", "design": "design.scores",
                 "field": "field.plot", "report": "report.report"},
    ),
    tags=["science", "bayesian", "physics", "design"],
)  # fmt: skip

WORKFLOW_TEMPLATES: Final[dict[str, WorkflowTemplate]] = {
    t.id: t
    for t in (
        TS_READINESS_ARIMAX,
        VAR_VECM,
        MMM_GOVERNED,
        FORECAST_BAKEOFF,
        SCI_SPATIAL_LADDER,
        SCI_NETWORK_LADDER,
        SCI_PHYLO_SIGNAL,
        SCI_CALIBRATE_THEN_DESIGN,
    )
}
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
