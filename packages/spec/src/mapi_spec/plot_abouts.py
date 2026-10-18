"""About cards of the built-in plot recipes: the core, MMM, ML and science packs.

The text is written for people who read the charts, not for people who make them: the summary
says what the chart shows for them, the lists say when to open it, what the model or run must
produce, what the chart takes for granted and what is on it, and the example reads one chart
the way a careful reader would. ``expert_notes`` keeps the computations and the settings that
change them apart.

The recipes in ``mapi_plots`` take their card from here by id
(``mapi_plots.recipes._base.recipe(about=...)``); ``related`` names only other plot recipes.
"""

from __future__ import annotations

from typing import Final

from mapi_spec.about import About
from mapi_spec.abouts import _B, _I, _NOW, _SEC, _X, _a

__all__ = ["PLOT_ABOUTS"]

_ARVIZ = ("ArviZ documentation", "https://python.arviz.org/en/stable/")
_GABRY = (
    "Gabry et al. (2019), Visualization in Bayesian workflow",
    "https://arxiv.org/abs/1709.01449",
)
_VEHTARI_RHAT = (
    "Vehtari et al. (2021), Rank-normalization, folding, and localization: an improved R-hat",
    "https://arxiv.org/abs/1903.08008",
)
_VEHTARI_LOO = (
    "Vehtari, Gelman and Gabry (2017), Practical Bayesian model evaluation using "
    "leave-one-out cross-validation and WAIC",
    "https://arxiv.org/abs/1507.04544",
)
_BDA3 = ("Gelman et al. (2013), Bayesian Data Analysis, 3rd edition", None)
_MMM_DOCS = ("mmm-framework documentation", "https://redam94.github.io/mmm-framework/")

_DRAWS_OR_SUMMARY = (
    "Predictions with draws (posterior_predictive, predictions or draws group), or a score "
    "summary with interval statistics"
)

PLOT_ABOUTS: Final[dict[str, About]] = {
    # ============================================================ core: predictions
    "core.ppc_fan": _a(
        "Fit and forecast",
        "Shows the model's estimate over time as a line with shaded ranges, and the actual "
        "values as dots. A dashed line marks where the training data ends and the forecast "
        "starts.",
        [
            "You want to see how well the model follows the history",
            "You want to show a forecast with its range to other people",
            "You want to check the forecast against values that the model did not see",
        ],
        [
            "Your data has no time or order dimension",
            "You want to compare single predictions with actual values cell by cell (use "
            "Predicted vs actual)",
        ],
        [
            _DRAWS_OR_SUMMARY,
            "Optional: the actual values (observed_data or observed group)",
            "A time-like dimension, such as week or date",
        ],
        [
            "Draws are added up over the dimensions that the chart does not show, such as markets",
            "A score summary cannot be added up, so it must have one value for each point in "
            "time (or each panel)",
            "The training data ends where the actual values stop, unless the run says otherwise",
        ],
        [
            "A line for the median (or mean) estimate",
            "Shaded 50% and 90% bands",
            "Dots for the actual values",
            "A dashed line where the training data ends",
            "One panel for each market or product when the data has a small group dimension",
            "A caption with the share of later actual values inside the 90% band",
        ],
        _B,
        _NOW,
        "The 90% band covers 18 of the 20 actual weeks and the line goes through the dots: "
        "the model fits the history well. After the dashed line, three weeks in a row are "
        "above the band. The model probably missed a change in level at that time.",
        related=["core.fit_vs_actual", "core.residuals_over_time", "core.ppc", "sci.ensemble"],
        expert="Bands are per-time quantiles of the draws (equal-tailed, at `levels`, default "
        "[0.5, 0.9]) after summing the draws over the hidden dims; joint draws make totals "
        "valid. A score summary uses its hdi_*/eti_*/q* statistics and cannot be summed. "
        "`point` picks median or mean. `split_at` sets the dashed line (default: the tree attr "
        "`split_at`, else the first x after the last observation). `facet` and `max_facets` "
        "pick the panel dim; the y axes are independent per panel. The caption's coverage "
        "counts only observations after the split, in the widest band.",
        refs=[_GABRY],
    ),
    "core.fit_vs_actual": _a(
        "Predicted vs actual",
        "Puts each prediction of the model next to the actual value in a scatter plot. Dots "
        "on the dashed diagonal line are perfect predictions.",
        [
            "You want a quick view of how accurate the model is",
            "You want to find the cells where the model is far wrong",
            "Your data has no time dimension",
        ],
        [
            "You want to see when the errors happen (use Residuals over time)",
            "You need to see the range of the predictions (this chart shows only the middle value)",
        ],
        [
            _DRAWS_OR_SUMMARY,
            "The actual values (observed_data or observed group)",
        ],
        [
            "Each dot uses the median (or mean) prediction only",
            "Cells with no actual value or no prediction are left out",
            "R² and MAPE in the caption are for the cells on the chart",
        ],
        [
            "A dot for each cell: the actual value across, the prediction up",
            "A dashed diagonal line for perfect predictions",
            "Colours for each market or product when there is a group dimension",
            "A caption with R² and the typical error in percent (MAPE)",
        ],
        _B,
        _NOW,
        "Most dots are near the diagonal and the caption says R² = 0.85. The dots for "
        "'south' are all below the line, so the model predicts too low for that market.",
        related=["core.residuals_over_time", "core.ppc_fan", "core.ppc"],
        expert="Point = median or mean of the draws (or the summary's point statistic). With "
        "draws, hidden dims are summed; with a score summary, the extra dims are kept as cells. "
        "R² = 1 − SS_res / SS_tot on the plotted cells (not a held-out score). MAPE skips cells "
        "where the actual value is zero. The chart is the same for train and test cells: "
        "filter the input to judge the forecast.",
    ),
    "core.residuals_over_time": _a(
        "Residuals over time",
        "Shows the error of the model (actual minus predicted) for each point in time. The "
        "errors should stay near the zero line with no pattern.",
        [
            "You want to find structure that the model does not capture",
            "You suspect a trend, a season or a one-time event that the model misses",
        ],
        [
            "You need a first view of the fit (start with Fit and forecast)",
            "You have very few time points",
        ],
        [
            _DRAWS_OR_SUMMARY,
            "The actual values (observed_data or observed group)",
        ],
        [
            "The residual uses the median (or mean) prediction only",
            "Errors from the noise of the data look random. A pattern shows a missing part in "
            "the model",
        ],
        [
            "A line with points for the residual at each time",
            "A zero line",
            "One colour for each market or product when there is a group dimension",
            "A caption that says if the model is too high or too low on average",
        ],
        _I,
        _NOW,
        "The residuals stay between −10 and +10 until June, and then they are all above zero. "
        "The model under-predicts from June, so it probably misses a change in the level or a "
        "new driver.",
        related=["core.fit_vs_actual", "core.ppc_fan", "core.ppc"],
        expert="Residual = observed − point (median or mean), on the same cells as Predicted "
        "vs actual. The caption reports an average bias when |mean residual| > 5% of the mean "
        "|observed|. Look for runs on one side of zero (bias over time), waves (a missing "
        "season), a funnel (spread that grows with the level) and single spikes (events or "
        "data errors).",
    ),
    "core.decomposition": _a(
        "Decomposition",
        "Splits the outcome into the parts that the model adds together, such as a baseline "
        "and each driver. It shows each part over time and its total share.",
        [
            "You want to know how much each part of the model explains",
            "You want to explain the result to people who do not read models",
        ],
        [
            "Your model does not add its parts together",
            "Some parts are negative and large (a stacked area is then hard to read)",
        ],
        ["A contributions group with one variable or label for each part, over time"],
        [
            "The parts add up to the outcome on the scale that the model uses",
            "Draws (or values) are added up over markets and other hidden dimensions",
        ],
        [
            "Stacked areas of each part over time",
            "Bars of the total contribution of each part with a 90% range",
            "A caption with the share of the top parts, and the parts that can be zero",
        ],
        _B,
        _NOW,
        "The baseline gives 85% of revenue, TV 10% and search 5%. The 90% range of search "
        "includes zero, so the model is not sure that search adds anything.",
        related=["mmm.decomposition", "core.counterfactual"],
        expert="Components are the labels of a component dim (`component`, default the first "
        "of component/channel/contribution/term/effect) plus whole variables without it. The "
        "area uses the mean over draws per time; the bar uses the mean of each draw's total "
        "with an HDI at `hdi_prob` (default 0.9). Shares are of the summed means. "
        "`max_components` caps the parts (default 20).",
    ),
    "core.counterfactual": _a(
        "Scenario vs baseline",
        "Compares a 'what if' scenario with the baseline over time. It also shows how sure "
        "the model is about the total difference.",
        [
            "You changed an input, such as spend or price, and want to see the effect",
            "You want the probability that the scenario does better",
        ],
        [
            "You compare more than one scenario (use Decision outcome)",
            "The scenario is far outside the data that the model saw",
        ],
        [
            "A scenario score: scenario draws (or summary) and baseline draws (or summary)",
            "Optional: delta draws (scenario minus baseline) for the total difference",
        ],
        [
            "The model gives correct answers for the changed inputs",
            "Scenario and baseline draws come from the same model draws, so you can subtract them",
        ],
        [
            "Two lines with 90% bands: the scenario and the baseline",
            "A histogram of the total change, with its mean and a 90% range",
            "A caption with the change and the probability of an increase",
        ],
        _B,
        _NOW,
        "The scenario line is above the baseline every week. The histogram of the total "
        "change is from 40 to 80, all above zero. The caption says that the probability of "
        "an increase is 100%.",
        related=["core.decision_distribution", "mmm.budget_plans", "core.decomposition"],
        expert="Bands are equal-tailed quantiles of the draws summed over hidden dims; summary "
        "inputs use their interval statistics. The total change sums the delta draws over all "
        "dims (or scenario − baseline when both have draws); the histogram uses `bins` "
        "(default 30) and the range is an HDI at `hdi_prob`. When only summaries exist, the "
        "chart shows the difference of the means with no uncertainty.",
    ),
    "core.decision_distribution": _a(
        "Decision outcome",
        "Shows the range of results for each option of a decision. It also gives the "
        "probability that each option is the best.",
        [
            "You choose between a few options, such as budget splits",
            "You want to see the risk of each option, not only its average",
        ],
        [
            "The result has more than one value for each option (sum it first)",
            "You have no draws, only averages",
        ],
        [
            "A decision group with draws of one result for each option (an option dimension)",
        ],
        [
            "Higher is better, unless you set higher_is_better to false",
            "All options use the same draws, so you can compare them draw by draw",
        ],
        [
            "Smooth curves of the results of each option",
            "A dot and a 90% range for each option",
            "Bars of the probability that each option is the best",
            "A caption that names the best option",
        ],
        _I,
        _NOW,
        "'shift_tv' has the highest curve and a 64% probability to be the best. Its range "
        "overlaps with 'cut_search', so the choice is not certain. 'keep' is best in only 8% "
        "of the draws.",
        related=["core.counterfactual", "mmm.budget_plans"],
        expert="Needs sample draws (chain/draw or sample). Densities are KDEs on a shared grid "
        "from the 0.5th to the 99.5th percentile; ranges are HDIs at `hdi_prob`. P(best) is the "
        "share of draws where the option has the highest (or lowest, with `higher_is_better` "
        "false) value, which needs the options to share draws. `candidate` names the option dim "
        "(default: the only extra dim); `max_candidates` caps it at 12.",
    ),
    # ============================================================ core: diagnostics
    "core.prior_predictive": _a(
        "Prior predictive check",
        "Shows data that the model can make before it sees your data, next to your data. Use "
        "it to check that your starting assumptions give values of the right size.",
        [
            "You set the starting beliefs of a Bayesian model and want to check them",
            "The fit fails or is slow and you suspect the starting beliefs",
        ],
        [
            "The model is not Bayesian",
            "You want to judge the fitted model (use Posterior predictive check)",
        ],
        [
            "A prior_predictive group (draws of the outcome before fitting)",
            "Optional: the actual values (observed_data group) with the same variable name",
        ],
        [
            "The starting beliefs should allow your data, but they do not need to match it",
            "Values far outside anything possible show beliefs that are too wide",
        ],
        [
            "Thin grey curves: the shape of the data in some draws",
            "A thick blue curve for all draws together",
            "A dark curve for your data",
            "A band over time when the data has a time dimension",
            "A caption with the share of your values inside the 90% range",
        ],
        _I,
        _NOW,
        "The grey curves spread from −100 to 300 and your weekly revenue is between 80 and "
        "130. 100% of your values are inside the 90% range, so the beliefs allow the data. "
        "If the curves went to 10 000, the beliefs would be much too wide.",
        related=["core.ppc", "mmm.prior_beliefs", "core.ppc_fan"],
        expert="Densities are KDEs of `num_draws` (default 50) evenly spaced prior predictive "
        "draws plus the pooled draws. Over time: quantile bands at `levels` of the mean over "
        "the other dims. The caption counts observations inside the per-cell 90% "
        "equal-tailed interval; under 50% suggests priors too narrow or on the wrong scale, "
        "and a median width over 100 × the data range suggests priors too wide. The observed "
        "variable must have the same name as the predicted one.",
        refs=[_GABRY],
    ),
    "core.convergence": _a(
        "Sampler convergence",
        "Checks that the fitting method found stable answers for each model parameter. Red "
        "points and a 'check' status show the parameters that you must not trust yet.",
        [
            "You fitted a Bayesian model and want to know if you can use it",
            "Results change from run to run",
        ],
        [
            "The model is not fitted with draws (such as a least-squares model)",
            "You want to know if the model is correct (a converged model can still be wrong)",
        ],
        [
            "A fitted Bayesian model with draws in chains (the posterior group)",
            "Optional: sample_stats with the divergence flags",
        ],
        [
            "Two or more chains make the R-hat check meaningful",
            "The default limits are R-hat at most 1.01 and 100 effective draws for each chain",
        ],
        [
            "A dot for R-hat of each parameter with a red limit line",
            "A dot for the effective sample size of each parameter with a limit line",
            "A table of the worst parameters",
            "Trace lines and rank plots of the worst parameters",
            "A caption with the number of problems and divergent transitions",
        ],
        _X,
        _SEC,
        "beta[tv] has R-hat 1.8 and its two trace lines stay at different levels: the chains "
        "disagree. The rank plot is not flat. Run the sampler longer or change the model "
        "before you use beta[tv]. The caption also reports 3 divergent transitions.",
        related=["core.posterior_forest", "core.ppc"],
        expert="Rank-normalised split R-hat (max of bulk and folded), bulk and tail ESS, per "
        "parameter element. A row is 'check' when R-hat > `rhat_threshold` (default 1.01) or "
        "min(bulk, tail ESS) < `ess_per_chain` × chains (default 100 per chain). Sorted by "
        "R-hat; `max_parameters` (40) dots, `table_rows` (15) table rows, `max_traces` (6) "
        "traces and rank histograms (`bins`, default 20; flat = well mixed). `max_elements` "
        "(2000) caps the elements checked. Divergences come from sample_stats.diverging.",
        refs=[_VEHTARI_RHAT, _ARVIZ],
    ),
    "core.posterior_forest": _a(
        "Posterior forest",
        "Shows the estimate and the range of each model parameter as a row with a dot and two "
        "lines. Rows whose range does not include zero have a clear direction.",
        [
            "You want to read the fitted values of the parameters",
            "You want to see which effects are clearly positive or negative",
        ],
        [
            "The parameters have no clear meaning on their own (for example, many latent values)",
            "The fit has not converged (check Sampler convergence first)",
        ],
        ["A fitted Bayesian model with draws (the posterior group)"],
        [
            "The draws are converged",
            "The parameters are on a scale that you can read; the chart does not change it",
        ],
        [
            "A dot for the median (or mean) of each parameter",
            "A thick bar for the middle 50% range",
            "A thin line for the 94% range",
            "A dashed zero line for variables whose ranges cross zero",
            "One panel with its own scale for each variable",
        ],
        _I,
        _NOW,
        "beta[tv] is 0.5 with a 94% range from 0.1 to 0.9: the effect is clearly positive. "
        "beta[search] goes from −0.2 to 0.6, so its direction is not certain.",
        related=["core.convergence", "core.prior_predictive", "mmm.prior_beliefs"],
        expert="The thin line is the HDI at `hdi_prob` (default 0.94, the ArviZ default); the "
        "thick bar is the central (equal-tailed) interval at `inner_prob` (default 0.5). "
        "`point` is median or mean. Elements are listed in model order up to `max_rows` (60); "
        "`var_names` picks variables. The caption counts intervals that exclude zero, which "
        "only matters for parameters where zero means 'no effect'.",
        refs=[_ARVIZ],
    ),
    "core.ppc": _a(
        "Posterior predictive check",
        "Compares data that the fitted model makes with your actual data. It shows if the "
        "ranges of the model are too narrow, too wide or shifted.",
        [
            "You want to check if the model's ranges are honest",
            "You want to see if the model gets the shape of the data right, such as its tails",
        ],
        [
            "You want to see the fit over time (use Fit and forecast)",
            "You have no draws, only averages",
        ],
        [
            "Predictions with draws (the posterior_predictive group)",
            "The actual values (observed_data group)",
        ],
        [
            "Each actual value is compared with the draws for the same cell",
            "A well-calibrated model puts about 90% of the values inside its 90% range",
        ],
        [
            "Thin curves for the shape of some predicted datasets and a dark curve for your data",
            "A PIT histogram with a grey band for the expected count (flat = calibrated)",
            "A coverage line against the ideal diagonal",
            "A caption with the coverage of the 90% range and warnings",
        ],
        _X,
        _SEC,
        "The coverage line is below the diagonal and only 70% of the values are in the 90% "
        "range. The PIT histogram is high at both ends (a U shape). The ranges of the model "
        "are too narrow, so the model is too sure.",
        related=["core.ppc_fan", "core.prior_predictive", "core.fit_vs_actual"],
        expert="PIT = P(ŷ < y) + ½ P(ŷ = y) per observed cell over the matched draws. The PIT "
        "histogram uses `bins` (default 20) and a ±1.96 SD binomial band. Coverage is the share "
        "of observations inside central equal-tailed intervals at `levels` (0.1 to 0.95). "
        "Caption rules: mean PIT < 0.4 (over-predicts) or > 0.6 (under-predicts); > 20% of PIT "
        "values in the outer 10% (too narrow); < 2% with n ≥ 50 (too wide). PIT values are "
        "not independent across time, so treat the band as a guide.",
        refs=[_GABRY, _BDA3],
    ),
    "core.model_comparison": _a(
        "Model comparison",
        "Ranks two or more models by how well they are expected to predict new data. It "
        "shows how far each model is from the best one and how sure that difference is.",
        [
            "You fitted more than one model to the same data and want to pick one",
            "You want to know if a more complex model is worth it",
        ],
        [
            "The models use different data or a different outcome",
            "You have only one model",
        ],
        [
            "A comparison group (for example from PSIS-LOO), or the log_likelihood group of "
            "each model",
        ],
        [
            "All models predict the same data points",
            "Differences smaller than about 4, or smaller than twice their standard error, "
            "are not important",
        ],
        [
            "A dot and a ±1 standard error line for the score (elpd) of each model",
            "A dot and a ±1 standard error line for the difference to the best model",
            "A caption that names the best model and judges the gap",
        ],
        _X,
        _SEC,
        "'unbiased' is best. 'biased' is 35 ± 6 elpd worse. That is more than twice the "
        "standard error, so the difference is clear. Use 'unbiased'.",
        related=["core.ppc", "core.fit_vs_actual"],
        expert="With `ic='auto'` a precomputed comparison group (elpd/elpd_loo/elpd_waic/"
        "elpd_kfold, se, dse, weight on a `model_dim`) is used as is; otherwise WAIC is "
        "computed from each child's log_likelihood (`var` picks the variable) and dse = "
        "sqrt(n · var(pointwise difference)) to the best model. Verdict on the runner-up: "
        "|Δelpd| < 4 is very small; > 2 dse is clear; else smaller than its uncertainty. "
        "Prefer PSIS-LOO over WAIC when it is available.",
        refs=[_VEHTARI_LOO],
    ),
    "core.data_overview": _a(
        "Data overview",
        "Shows each column of your data over time, where values are missing and how much data "
        "each group has. Use it before you fit a model.",
        [
            "You uploaded a dataset and want a first look",
            "You want to find gaps, spikes or groups with little data",
        ],
        [
            "Your data has no numeric columns",
            "You want a plot of model results",
        ],
        [
            "A dataset, or the observed_data, data or constant_data group of a run",
        ],
        [
            "Values are added up over the dimensions that the chart does not show (this does "
            "not suit prices or rates)",
            "The first numeric columns are shown, up to 8",
        ],
        [
            "One panel for each column with a line for each group",
            "A heatmap of the share of missing values over time",
            "Bars of the number of values in each group",
            "A caption with the column that has most missing values",
        ],
        _B,
        _NOW,
        "The revenue panel has a gap in week 4 for 'north'. The heatmap shows that search "
        "spend is missing for the first four weeks in 'east'. Fix or fill these values before "
        "you fit the model.",
        related=["core.ppc_fan", "core.prior_predictive"],
        expert="The time dim is detected (or `along`); the colour dim is a small categorical "
        "dim (`facet`, at most `max_levels` levels). Other dims are summed with min_count=1. "
        "The heatmap is the mean of isnull() over the other dims. Group sizes count non-null "
        "values of the first variable. `vars` and `max_vars` pick columns; above `max_rows` "
        "the x values are thinned evenly.",
    ),
    # ============================================================ mmm pack
    "mmm.response_curves": _a(
        "Response curves",
        "Shows how much outcome each channel brings for each level of spend. A curve that "
        "becomes flat shows that more spend in that channel adds little.",
        [
            "You want to know which channels are near saturation",
            "You plan the next budget and want to see the effect of more or less spend",
        ],
        [
            "You need the cost of the next unit of outcome (use Marginal cost)",
            "You want to read spend far above anything in the history",
        ],
        [
            "A response_curves group: draws of the response for each channel and spend level",
            "Optional: the current spend of each channel",
        ],
        [
            "Other channels and the baseline stay as they are",
            "The curve is reliable only near spend levels that the history contains",
        ],
        [
            "A line for the median response of each channel",
            "A shaded 90% band for each channel",
            "A diamond at the current spend of each channel",
            "A caption that names the channel nearest to saturation",
        ],
        _B,
        _NOW,
        "The TV curve rises fast and becomes flat after 60. TV's diamond is at 40, on the "
        "flatter part. Search is still steep at 25. More money in search probably returns "
        "more than the same money in TV.",
        related=["mmm.marginal_cost", "mmm.budget_plans", "mmm.decomposition"],
        expert="Median and HDI (`hdi_prob`, default 0.9) per channel and spend level; extra "
        "dims (such as geo) are summed. The spend axis is the grid coordinate or a `spend_var` "
        "over (channel, grid). The caption compares the slope of the median curve at current "
        "spend with its steepest slope (np.gradient). `facet` gives one panel per channel.",
        refs=[_MMM_DOCS],
    ),
    "mmm.marginal_cost": _a(
        "Marginal cost",
        "Shows what the next unit of outcome costs in each channel at each level of spend. It "
        "can also show the return of the next unit of spend.",
        [
            "You want to move budget to where the next unit of outcome is cheapest",
            "You want to find the spend level where a channel stops paying back",
        ],
        [
            "You want total returns, not the return of the next unit (use Response curves)",
            "The response curves are very flat, so the cost is very large and uncertain",
        ],
        [
            "A response_curves group: draws of the response for each channel and spend level",
            "Optional: the current spend of each channel",
        ],
        [
            "The cost comes from the slope of each response curve between spend levels",
            "Very high costs are cut at the 98th percentile so that the chart stays readable",
        ],
        [
            "A line for the median cost (or return) of each channel",
            "A shaded 90% band for each channel",
            "A diamond at the current spend of each channel",
            "A caption that names the cheapest channel at current spend",
        ],
        _I,
        _NOW,
        "At current spend, the next unit of revenue costs 0.8 in search and 2.5 in TV. The "
        "TV cost rises fast after 50. Move budget from TV to search until the two costs are "
        "close.",
        related=["mmm.response_curves", "mmm.budget_plans"],
        expert="Per draw: slope = np.gradient(response, spend). metric='cost' plots 1/slope "
        "(NaN where the slope is ≤ 0); metric='roi' plots the slope. Values above the "
        "`clip_quantile` (default 0.98) of all finite values are capped, so the top of a band "
        "can be flat because of the cap. Median and HDI at `hdi_prob`. The value at current "
        "spend is interpolated on the median curve, not per draw.",
        refs=[_MMM_DOCS],
    ),
    "mmm.budget_plans": _a(
        "Budget plan comparison",
        "Compares budget plans side by side: how each plan splits the spend and what outcome "
        "each plan is expected to bring. It also shows the change from a reference plan.",
        [
            "You have a few budget plans and must pick one",
            "You want the probability that a new plan beats the current plan",
        ],
        [
            "You have only one plan (use Scenario vs baseline)",
            "The plans use spend far outside the history",
        ],
        [
            "A plans group: spend for each plan and channel, and draws of the outcome for each "
            "plan",
        ],
        [
            "All plans use the same model draws, so you can compare them draw by draw",
            "The first plan is the reference, unless you select a different one",
        ],
        [
            "Grouped bars of the spend in each channel for each plan",
            "A dot and a 90% range of the outcome of each plan",
            "A dot and a 90% range of the change from the reference plan",
            "A caption with the best plan and its probability to be better",
        ],
        _B,
        _NOW,
        "plan_b moves 10 from search to TV. It is expected to bring 25 more revenue than "
        "'current' (90% range 5 to 45), with a 99% probability to be better. plan_c is lower "
        "than 'current'.",
        related=["mmm.response_curves", "mmm.marginal_cost", "core.decision_distribution"],
        expert="Needs outcome draws. Outcome extra dims are summed. Ranges are HDIs at "
        "`hdi_prob` of the outcome and of (plan − reference) per draw; P(better) = share of "
        "draws with a positive difference. `reference` sets the reference plan; "
        "`spend_var`, `outcome_var`, `plan` and `channel` rename the inputs.",
        refs=[_MMM_DOCS],
    ),
    "mmm.decomposition": _a(
        "Contribution by component",
        "Splits sales (or another KPI) into the baseline and the part from each marketing "
        "channel. It shows each part over time and its total share.",
        [
            "You want to know how much each channel added in the period",
            "You report the effect of marketing to people who do not read models",
        ],
        [
            "You want the effect of future spend (use Response curves)",
            "The model has not converged",
        ],
        [
            "A contributions group split by channel, with draws, plus baseline variables",
        ],
        [
            "The parts add up to the KPI on the scale that the model uses",
            "Markets and other hidden dimensions are added up",
        ],
        [
            "Stacked areas of the baseline and each channel over time",
            "Bars of the total contribution of each part with a 90% range",
            "A caption with the share of the top parts",
        ],
        _B,
        _NOW,
        "The baseline gives 84% of revenue, TV 11% and search 5%. TV's range is narrow, so "
        "its share is clear. The range of search includes zero, so do not report its share as "
        "a fact.",
        related=["core.decomposition", "mmm.response_curves", "core.counterfactual"],
        expert="Same computation as core.decomposition with `component` default 'channel'; "
        "variables without the channel dim (baseline, controls) become their own parts. Bars "
        "use the mean of each draw's total and an HDI at `hdi_prob`. Shares of a "
        "multiplicative (log-scale) model depend on the order of the parts, so read them as a "
        "guide.",
        refs=[_MMM_DOCS],
    ),
    "mmm.prior_beliefs": _a(
        "Your beliefs and the priors",
        "Compares the range that you gave for each channel with the values that the model "
        "allows before it sees your sales. Use it to check that the model understood your "
        "beliefs.",
        [
            "You entered beliefs about ROI or share for each channel",
            "You want to check the beliefs before you fit the model",
        ],
        [
            "You did not enter beliefs (the chart then has nothing to compare)",
            "You want the fitted results (use Contribution by component)",
        ],
        [
            "A prior_beliefs group: draws of ROI or share for each channel, and the ranges "
            "that you gave",
        ],
        [
            "Your range is a belief with a level of certainty, not a hard limit",
            "The share of the model's values inside your range should be close to the "
            "certainty that you gave",
        ],
        [
            "A light band for the range that you gave",
            "A thin line for 90% of the model's starting values",
            "A dark bar for the middle 50% and a tick at the median",
            "One panel for each quantity (ROI, share)",
            "A caption with the share of values inside your range for each channel",
        ],
        _B,
        _NOW,
        "You said TV's ROI is between 1.5 and 4.0 with 80% certainty. Only 60% of the model's "
        "values are in that range and the median is 2.5. The beliefs were translated too "
        "wide. Narrow the range or check the units of spend.",
        related=["core.prior_predictive", "core.posterior_forest", "mmm.response_curves"],
        expert="Per channel, the stated quantity (roi or contribution_share, from "
        "`stated_quantity`) gives 5/25/50/75/95% quantiles of the prior draws; "
        "`baseline_share` with range attrs adds a baseline row. Coverage = share of prior "
        "draws inside [stated_low, stated_high], compared with `stated_credibility`. The "
        "`height` setting here is the height of one channel row (default 26).",
        refs=[_MMM_DOCS],
    ),
    # ============================================================ ml pack
    "ml.feature_importance": _a(
        "Feature importance",
        "Ranks the inputs that a tabular model uses most. For a linear model it shows the "
        "size and the direction of each input's effect.",
        [
            "You want to know which inputs drive the predictions",
            "You want to find inputs that the model ignores",
        ],
        [
            "You want cause and effect (importance shows use, not cause)",
            "Inputs are strongly related, so the model can use either one",
        ],
        [
            "A feature_importance group from a tree model, or a coefficients group from a "
            "linear model",
        ],
        [
            "Tree importances are impurity based and favour inputs with many values",
            "Coefficients are on the model's scale, so compare them only when the inputs have "
            "the same scale",
        ],
        [
            "Bars for each input, largest first (at most 20)",
            "Red bars for negative coefficients and blue bars for positive ones",
            "A range line when there are several models (such as one for each quantile)",
            "A caption that names the top input and its share",
        ],
        _I,
        _NOW,
        "price has 42% of the total importance and the week of the year has 21%. "
        "promo_lagged[2] has 7%, so the second week after a promotion adds little. For the "
        "linear model, the price coefficient is −1.8: a higher price lowers the prediction.",
        related=["core.fit_vs_actual", "core.posterior_forest"],
        expert="`source='auto'` uses the feature_importance group, else coefficients; groups "
        "are found at the root, under model/ or anywhere in the tree. Coefficients are ranked "
        "by |value|. Extra dims (for example one model per quantile) give the mean as the bar "
        "and min to max as the range line. `top` (default 20) caps the bars; `feature_dim` "
        "names the feature dim. Permutation importance or SHAP is more reliable than impurity "
        "importance.",
    ),
    # ============================================================ science pack
    "sci.field_map": _a(
        "Field map",
        "Shows a map of a value that the model estimates on a grid, such as a density over an "
        "area. One panel shows the average and another panel shows where the model is not "
        "sure.",
        [
            "The model gives a value for each cell of a 2D grid (an image or a map)",
            "You want to see where the data fixes the value and where it does not",
        ],
        [
            "The output has no grid (use Profiles with bands or Ensemble of draws)",
            "The output is a direction and a length (use Vector field)",
        ],
        [
            "Draws of a gridded output in the posterior, predictions or posterior_predictive group",
            "Two grid dimensions, from the output's grid declaration or the settings",
        ],
        [
            "Each cell is summarised on its own; the chart does not show how cells vary together",
            "Large grids are averaged in blocks of cells so that the chart stays small",
        ],
        [
            "A heatmap of the mean in each cell",
            "A heatmap of the SD (the uncertainty) in each cell",
            "Optional panels: median, mean ÷ SD, 5% and 95% values, and a few single draws",
            "A slider when the output has a frame dimension, such as time",
        ],
        _I,
        _SEC,
        "The mean panel of a lensed galaxy shows a bright arc at the top left. The SD panel is "
        "dark there and bright at the edges of the image. The arc is clear, and the edges are "
        "not fixed by the data.",
        related=["sci.vector_field", "sci.profile_bands", "sci.ensemble"],
        expert="Statistics per cell over up to `max_draws` (400) evenly thinned draws: mean, "
        "median, sd (ddof 1), z = mean/sd, q05, q95 and `n_draws` single draws (`stats`, "
        "default mean and sd). `log` draws log10 first. Rasters are block-averaged to stay "
        "under `max_cells` per panel. `colormap`, `origin` and `log` default to the grid "
        "declaration. `frame` adds a slider (up to `max_frames`); `select` fixes other dims.",
    ),
    "sci.vector_field": _a(
        "Vector field",
        "Shows a field of arrows, such as forces or flows, on a grid. Colours show the length "
        "of the arrow in each cell and a second panel shows where the length is not certain.",
        [
            "The model gives a direction and a length for each cell of a 2D grid",
            "You want to see the pattern of a flow or a force",
        ],
        [
            "The output has one value for each cell (use Field map)",
            "The directions change a lot between draws (the mean arrows then look too short)",
        ],
        [
            "Draws of a gridded output with a component dimension of length 2 (the x and y parts)",
        ],
        [
            "The two components use the same units, so the length makes sense",
            "Arrows show the mean vector; colours show the mean length over the draws",
        ],
        [
            "A heatmap of the mean length with arrows of the mean direction",
            "A heatmap of the SD of the length",
            "Optional panels: the 5% and 95% values of the length",
            "A slider when the output has a frame dimension",
        ],
        _I,
        _SEC,
        "The arrows of a cell's traction force point inwards at the edges of the cell. The "
        "colours are bright at the two ends, where the forces are largest. The SD panel is "
        "high in the centre, so the forces there are not certain.",
        related=["sci.field_map", "sci.profile_bands"],
        expert="Length = hypot(u, v) per draw; panels are its mean, sd, q05 or q95 (`stats`). "
        "Arrows are the mean (u, v), block-averaged to about `arrows` (20) along the longer "
        "side and scaled so the longest is 0.9 of the arrow spacing. The mean length is at "
        "least the length of the mean vector; they differ when directions vary. `component` "
        "names the length-2 dim (default: the grid's, else the only one).",
    ),
    "sci.profile_bands": _a(
        "Profiles with bands",
        "Shows the model's estimate along one dimension, such as distance, with shaded "
        "ranges. It draws one curve for each level of a second dimension, such as time.",
        [
            "The model gives a curve, such as a profile over space at several times",
            "You want to compare the curves with the actual values",
        ],
        [
            "The output is a 2D map that you want to see as a picture (use Field map)",
            "You want to see single possible paths (use Ensemble of draws)",
        ],
        [
            "Draws of an output in the predictions, posterior_predictive or posterior group",
            "Optional: the actual values (observed_data group)",
        ],
        [
            "Other dimensions have one level, or you select one",
            "At most 12 curves are shown, evenly spaced",
        ],
        [
            "A line for the median of each curve",
            "Shaded 50% and 90% bands for each curve",
            "Dots for the actual values",
            "A slider that shows one curve with its bands at a time",
            "A caption with the share of actual values inside the 90% band",
        ],
        _B,
        _NOW,
        "Each line is the population along the river on one day. The front moves right and "
        "the bands become wider on later days. 88% of the dots are in the 90% band, so the "
        "model follows the spread of the population.",
        related=["sci.ensemble", "sci.field_map", "core.ppc_fan"],
        expert="Median or mean and equal-tailed quantile bands at `levels` (default 0.5, 0.9; "
        "at most 3) over up to `max_draws` draws. `along` defaults to the grid's x; `by` to "
        "the grid's frame or y. `max_curves` (12) picks evenly spaced levels of `by`. The "
        "slider's 'All' step shows the lines without bands. Coverage uses the widest band. "
        "`log` sets a log y axis.",
    ),
    "sci.ensemble": _a(
        "Ensemble of draws",
        "Shows many possible paths from the model as thin lines, with a shaded range and the "
        "actual values. Use it to see what single outcomes can look like, not only the "
        "average.",
        [
            "You want to see the shapes that the model allows, such as peaks and dips",
            "You want to explain uncertainty to people with single example paths",
        ],
        [
            "You want a clean summary for a report (use Fit and forecast)",
            "The output has more than one dimension that you cannot fix to one level",
        ],
        [
            "Draws of an output in the predictions, posterior_predictive or posterior group",
            "Optional: the actual values (observed_data group)",
        ],
        [
            "The thin lines are a random choice of draws (the seed is fixed)",
            "The band uses all the draws, up to 2000",
        ],
        [
            "Thin lines for 40 single draws",
            "A shaded 90% band",
            "A thick line for the median",
            "Dots for the actual values",
            "A caption with the share of actual values inside the band",
        ],
        _B,
        _NOW,
        "Most thin lines rise to a peak in week 10, but some peak in week 14. The band is wide "
        "around the peak. The dots are inside the band, so the model allows the actual peak, "
        "but it is not sure of the timing.",
        related=["sci.profile_bands", "core.ppc_fan", "core.ppc"],
        expert="`n_draws` (40) lines chosen without replacement with `seed`; bands are "
        "equal-tailed quantiles at `levels` (default [0.9]) over up to `max_draws` (2000) "
        "draws. `point` is median, mean or none. `along` defaults to the time-like dim, else "
        "the grid's x. Other dims must have one level or be fixed with `select`. The median "
        "line is not itself a possible path.",
    ),
    "sci.area_map": _a(
        "Area map",
        "Shows a map of areas, such as districts, coloured by the model's estimate for each "
        "area. Other panels show the uncertainty and how likely it is that the value is above "
        "a limit.",
        [
            "The model gives one value for each area of a map, such as a disease risk",
            "You want to find the areas where the risk is clearly high",
            "You want to compare the smoothed estimate with the raw observed values",
        ],
        [
            "The output is a value on a regular grid (use Field map)",
            "You need a base map with roads and place names (the chart draws only the outlines)",
        ],
        [
            "Draws of a per-area output in the posterior, predictions or posterior_predictive "
            "group (a summary with mean and sd also works)",
            "The outlines of the areas as ring_x(area, vertex) and ring_y(area, vertex)",
            "Optional: observed values for each area and a neighbour matrix adjacency(area, area_)",
        ],
        [
            "The outlines use the same units on both axes, such as metres or degrees",
            "Holes inside an area are filled",
            "Large outlines use fewer points so that the chart stays small",
        ],
        [
            "One map panel each for the observed values, the mean and the SD",
            "A panel with the probability that the value is above the threshold",
            "Crosses on the areas where that probability is above 95%",
            "Optional grey lines between neighbouring areas",
        ],
        _I,
        _SEC,
        "The observed panel of lip cancer in Scotland shows extreme values in small "
        "districts. The mean panel is smoother. The probability panel is dark red in the north, "
        "and crosses mark the districts where the risk is above 1 with 95% probability.",
        related=["sci.field_map", "core.posterior_forest"],
        expert="Statistics per area over up to `max_draws` (2000) draws: mean, median, sd "
        "(ddof 1) and P(value > `threshold`) (`stats`; default observed, mean, sd and "
        "exceedance). `threshold` defaults to 1 for a non-negative output (a relative risk) "
        "and to 0 otherwise. Fills use `classes` (9) equal steps of the colour scale over "
        "the 0.5 to 99.5% range; the colour bar and the hover show the exact value. "
        "`log_scale` colours log10 of the value panels. Areas match the outlines by label "
        "when both have labels, else by position. Polygons are drawn as filled scatter "
        "shapes on equal-aspect axes, not a geographic projection.",
    ),
    "sci.phylogeny": _a(
        "Phylogeny with tip estimates",
        "Shows a family tree of species next to the model's estimate for each species. Close "
        "relatives sit side by side, so you can see if related species have similar values.",
        [
            "The model gives one value for each species (or other tip) of a tree",
            "You want to see how the estimate follows the tree",
            "Some species have no observed value and you want to see their estimate",
        ],
        [
            "The groups are not related by a tree (use Posterior forest)",
            "The tree has thousands of tips and you need to see every one of them",
        ],
        [
            "Draws of a per-tip output in the predictions, posterior_predictive or posterior group",
            "The tree as parent(node) and branch_length(node), with optional is_tip(node) and "
            "tip_label(node)",
            "Optional: the observed trait for each tip (observed_data)",
        ],
        [
            "Each node has one parent; a root has none",
            "Tips match the output by label, else in the order of the nodes",
            "With more tips than the limit, the chart shows evenly spaced tips",
        ],
        [
            "The tree with the branch lengths along the x axis",
            "A dot at each tip coloured by the estimate",
            "A strip with the estimate and its 90% interval for each tip",
            "Open circles for the observed values",
        ],
        _I,
        _SEC,
        "The tree of 16 primates puts the large apes together at the top. Their dots in the "
        "strip are all on the right. Three species have no observed body mass, and their "
        "intervals are a little wider than those of their relatives.",
        related=["core.posterior_forest", "sci.profile_bands"],
        expert="The layout is rectangular: x is the sum of branch lengths from the root, y "
        "the order of the tips in a depth-first walk; a parent sits midway between its outer "
        "children. `parent` holds integer positions (or node labels); -1, NaN or the node "
        "itself marks a root, and several roots join at a common origin. The strip shows the "
        "mean or median (`point`) and the equal-tailed `interval` (0.9). `max_tips` (400) "
        "keeps evenly spaced tips and their ancestors.",
    ),
    "sci.rose": _a(
        "Rose of angles",
        "Shows directions or times of day on a circle. Bars show how often each direction "
        "occurs, with the range that the model expects for each sector.",
        [
            "The data are angles, such as wind directions, hours of the day or days of the year",
            "You want to check that the model gets the main directions right",
            "The model gives a density around the circle and you want to see its shape",
        ],
        [
            "The values are not on a circle (use Posterior predictive check)",
            "You need the exact numbers for each direction (use a table)",
        ],
        [
            "Observed angles (observed_data) and draws of simulated angles "
            "(posterior_predictive), or a density over an angle dimension with draws",
            "The units of the angles: radians, degrees, hours or days",
        ],
        [
            "One turn is 2π radians, 360 degrees, 24 hours or 365.25 days (or `period`)",
            "Sectors have equal widths",
            "A density curve is in units per one unit of angle",
        ],
        [
            "Bars for the observed count (or share) in each sector",
            "A thick line in each sector for the 90% range of simulated data, and the median",
            "Or a median curve with dotted 90% limits",
            "A slider when the data have a frame dimension, such as season",
        ],
        _B,
        _NOW,
        "The rose of wind directions has a long bar to the south-west. The model's range "
        "covers it, but the bars to the north-east end above the range: the model gives too "
        "few winds from the north-east.",
        related=["sci.profile_bands", "core.ppc"],
        expert="Histogram mode: the observed angles and each of up to `max_draws` (1000) "
        "predictive draws are counted in `bins` (24) equal sectors; the lines are equal-tailed "
        "`interval` (0.9) quantiles of the counts per sector. `scale` share divides by the "
        "number of angles (the default with a `frame`). Density mode (an `angle_dim` such as "
        "angle, hour or direction): quantiles per angle; observed angles are scaled to a "
        "density per unit. `clockwise` defaults to true (zero at the top) except for radians.",
    ),
}
