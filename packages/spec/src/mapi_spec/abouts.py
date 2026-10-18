"""About cards of the built-in catalog (M3.5): steps, splits, models and guided templates.

The text is written for people who do not do statistics for a living: the summary says what
the item does for them, the lists say when it fits, what it needs and what comes out, and
``expert_notes`` keeps the maths apart. Workflow nodes and governance checks keep their
cards next to their registries (:mod:`mapi_spec.workflow_nodes`,
:mod:`mapi_spec.governance`); :func:`builtin_about` looks up any of them by id.

Built-in steps and models take their card from here when their class does not set one
(``mapi_sdk.steps.Step.about``, ``mapi_sdk.models.CustomModel.about``). Models that are built
in other v0.5 components (mmm-framework and statsmodels) are listed too, so their plugins and
the chooser agree on the words: :data:`PLANNED_MODEL_IDS`.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping, Sequence
from typing import Final

from mapi_spec.about import About, AboutKind, AboutReference, Difficulty, TypicalRuntime

__all__ = [
    "MODEL_ABOUTS",
    "PLANNED_MODEL_IDS",
    "SCIENCE_MODEL_IDS",
    "SPLIT_ABOUTS",
    "STEP_ABOUTS",
    "SURVIVING_MODEL_IDS",
    "TEMPLATE_ABOUTS",
    "builtin_about",
    "builtin_about_kind",
    "builtin_abouts",
]

_B, _I, _X = Difficulty.BEGINNER, Difficulty.INTERMEDIATE, Difficulty.EXPERT
_NOW, _SEC, _MIN, _LONG = (
    TypicalRuntime.INSTANT,
    TypicalRuntime.SECONDS,
    TypicalRuntime.MINUTES,
    TypicalRuntime.LONG,
)


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
        title=title,
        summary_plain=summary,
        use_when=list(use),
        avoid_when=list(avoid),
        data_needs=list(needs),
        assumptions=list(assumes),
        outputs=list(outputs),
        difficulty=difficulty,
        typical_runtime=runtime,
        example=example,
        related=list(related),
        expert_notes=expert,
        references=[AboutReference(title=t, url=u) for t, u in refs],
        fits={k: list(v) for k, v in (fits or {}).items()},
    )


_PREP = {"goal": ["check_data"]}

# ===================================================================================== steps

STEP_ABOUTS: Final[dict[str, About]] = {
    "core.resample": _a(
        "Weekly or monthly totals",
        "Adds the daily (or hourly) numbers together into one number for each week or month. "
        "Each row then covers the same length of time.",
        [
            "Your data is daily, but you plan and report by week or month",
            "A model needs weekly data",
        ],
        [
            "Your data already has one row for each week or month",
            "You cannot add the numbers together, and an average hides the important information",
        ],
        ["A date column with a regular interval"],
        [
            "You can add spend and sales together. Prices and rates get an average (select this "
            "for each column)",
            "When the step needs full periods, it removes an incomplete first or last period",
        ],
        ["The same columns with one row for each week or month"],
        _B,
        _NOW,
        "If the daily TV spend is 100 every day, the week that ends on Sunday gets 700.",
        related=["core.count_events", "core.calendar"],
        expert="Group-by on the time coordinate at the target frequency (pandas offset alias). "
        "`how` for each variable (sum, mean, last). `full_periods_only` removes partial bins.",
        fits=_PREP,
    ),
    "core.count_events": _a(
        "Count events per period",
        "Changes a list of individual events, such as orders or sign-ups, into counts for each "
        "day, week or month.",
        ["Your dataset has one row for each event and you need a time series"],
        ["Your data is already a series of totals for each period"],
        ["A column with the time of each event"],
        ["Each row is one event. The step ignores events outside the dates that you select"],
        ["A count for each period (and for each group if you select one), and optional sums"],
        _B,
        _NOW,
        "Three orders on Monday and one order on Tuesday become 3 and 1.",
        related=["core.resample"],
        fits=_PREP,
    ),
    "core.filter_coords": _a(
        "Keep some markets, products or dates",
        "Keeps only the markets, products or date range that you select. It removes the rest "
        "before you fit the model.",
        [
            "You do not want to model some markets or products",
            "The early history is not reliable and you want to remove it",
        ],
        [
            "You want the model to learn from all the data",
            "You want to test on markets that the model did not see (use a split instead)",
        ],
        ["A dimension such as market, product or date"],
        ["The model and the steps after this step do not need the removed data"],
        ["The same data with fewer markets, products or dates"],
        _B,
        _NOW,
        "Keep only the markets DE and FR, or only the weeks from 2023 and after.",
        related=["core.leave_group_out"],
        fits=_PREP,
    ),
    "core.to_long": _a(
        "One row per observation",
        "Changes a table where the rows of each group are side by side into one row for each "
        "observation. It removes the empty padding.",
        [
            "A model works on individual observations, such as shopping trips",
            "Groups have very different numbers of rows",
        ],
        ["All groups have the same number of rows and the model needs a grid"],
        ["A group dimension (customer) and a position dimension (trip number)"],
        ["Missing values or a mask mark the padding cells"],
        ["The same values with one row for each observation and a group index"],
        _I,
        _NOW,
        "Household 1 with 3 trips and household 2 with 1 trip become 4 rows.",
        related=["core.to_padded"],
        fits=_PREP,
    ),
    "core.to_padded": _a(
        "Put groups side by side",
        "Puts the rows of each group side by side in a grid. It fills short groups with gaps "
        "and marks the gaps as missing.",
        ["A model needs a grid of groups by position, such as households by trip"],
        ["Your data is already a grid"],
        ["A group column and an order in each group"],
        ["The gaps have a mark, so the model can ignore them"],
        ["A grid of groups by position, and a mask of the real cells"],
        _I,
        _NOW,
        "Households with 3 trips and 1 trip become a 2 by 3 grid with two empty cells.",
        related=["core.to_long"],
        fits=_PREP,
    ),
    "core.stack": _a(
        "Combine dimensions",
        "Combines two or more dimensions, such as market and product, into one list of cells.",
        ["A model works on a flat list of cells, not on a grid"],
        ["The model can use the grid directly"],
        ["Two or more dimensions to combine"],
        [
            "The step can remove empty combinations. It keeps a record of them, so you can put "
            "them back"
        ],
        ["One combined dimension"],
        _I,
        _NOW,
        "3 markets by 4 products become 12 market-product cells.",
        related=["core.unstack"],
        fits=_PREP,
    ),
    "core.unstack": _a(
        "Split a combined dimension",
        "Divides a combined dimension back into its parts. For example, market-product cells "
        "become markets and products again.",
        ["You stacked dimensions in an earlier step and you need the grid again"],
        ["No step stacked the dimensions"],
        ["A dimension that a stack step made"],
        ["The parts of each combined label are known"],
        ["The original dimensions"],
        _I,
        _NOW,
        "12 market-product cells become 3 markets by 4 products.",
        related=["core.stack"],
        fits=_PREP,
    ),
    "core.formula": _a(
        "New column from a formula",
        "Makes a new column from a simple formula of existing columns, such as price times units.",
        [
            "You need a derived number, for example revenue = price x units",
            "A ratio or a difference of two columns explains the result better",
        ],
        ["A built-in step already does it (for example scale, log or lag)"],
        ["The columns that the formula uses"],
        [
            "The formula uses only the permitted operations (arithmetic and some functions). "
            "No code runs"
        ],
        ["One new column"],
        _I,
        _NOW,
        "revenue = price * units adds a revenue column.",
        expert="The step compiles a Lark grammar to an allow-listed AST of xarray operations "
        "and evaluates it in the sandbox. This is a usability feature, not a security boundary.",
        fits=_PREP,
    ),
    "core.adstock": _a(
        "Carry-over of advertising",
        "Spreads the advertising effect of each period over the next periods. People remember "
        "an ad for some time after they see it.",
        [
            "You model media with a simple (non-Bayesian) model and you need carry-over",
            "You want to see how a fixed carry-over rate changes spend series",
        ],
        ["The model already calculates carry-over itself (the Bayesian MMM does this)"],
        ["Media spend or exposure over time"],
        ["You set the carry-over rate. The model does not learn it from the data"],
        ["Media columns that include the remaining effect of past periods"],
        _I,
        _NOW,
        "With a 50% weekly carry-over, 100 spent this week counts as 100, then 50, then 25.",
        related=["core.saturation", "mmm.bayesian"],
        expert="Geometric (alpha), delayed (theta) or Weibull kernels up to l_max lags, with "
        "optional normalisation to a sum of 1. The step applies them to each group along time.",
        fits={"goal": ["explain_marketing", "check_data"]},
    ),
    "core.saturation": _a(
        "Diminishing returns",
        "Bends spend so that each extra unit of money adds a little less than the unit before. "
        "This is what occurs when a channel is saturated.",
        ["You model media with a simple model and you need diminishing returns"],
        ["The model learns saturation itself (the Bayesian MMM does this)"],
        ["Media spend or exposure"],
        ["You set the shape and its settings. The model does not learn them from the data"],
        ["Media columns on a curved scale"],
        _I,
        _NOW,
        "With half-saturation at 1000, a spend of 1000 gives half of the maximum effect.",
        related=["core.adstock", "mmm.bayesian"],
        expert="Logistic (lam), Hill (slope, half) or Michaelis-Menten curves, applied element "
        "by element.",
        fits={"goal": ["explain_marketing", "check_data"]},
    ),
    "core.fx_convert": _a(
        "Convert currencies",
        "Converts money columns from local currencies into one currency. It uses exchange rates "
        "for each day or week.",
        ["Markets report in different currencies"],
        ["All values are already in one currency"],
        ["Money columns and a table of exchange rates over time"],
        ["The exchange-rate table covers all dates in the data"],
        ["Money columns in the currency that you select"],
        _B,
        _NOW,
        "1000 GBP on a day when 1 GBP = 1.17 EUR becomes 1170 EUR.",
        related=["core.resample"],
        fits=_PREP,
    ),
    "core.mean_scale": _a(
        "Put series on a common scale",
        "Divides each series by its average, so that you can compare big and small markets on "
        "the same scale.",
        [
            "Markets or channels are very different in size",
            "A model works better when each series has an average of 1",
        ],
        ["The model scales the data itself"],
        ["Numeric columns"],
        ["The step learns the average from the training data only and uses it again for new data"],
        [
            "The same columns with an average of 1 (the server converts the results back "
            "automatically)"
        ],
        _B,
        _NOW,
        "A market that sells 5000 a week on average becomes 1.0. A week with 6000 becomes 1.2.",
        related=["core.standardize", "core.max_abs_scale"],
        fits=_PREP,
    ),
    "core.max_abs_scale": _a(
        "Scale to between -1 and 1",
        "Divides each series by its largest absolute value, so all values are between -1 and 1.",
        ["A model needs values in a fixed range"],
        ["Your data has extreme outliers (they set the scale)"],
        ["Numeric columns"],
        ["The step learns the largest value from the training data only"],
        ["The same columns between -1 and 1"],
        _B,
        _NOW,
        "Values 50, 100 and -20 become 0.5, 1.0 and -0.2.",
        related=["core.mean_scale", "core.clip"],
        fits=_PREP,
    ),
    "core.standardize": _a(
        "Centre and scale",
        "Moves each series so that its average is zero, and stretches it so that its typical "
        "spread is one.",
        ["You compare the size of effects across inputs that have different units"],
        ["Zero must keep its meaning (a spend of zero must stay zero)"],
        ["Numeric columns"],
        ["The step learns the average and the spread from the training data only"],
        ["The same columns with average 0 and spread 1"],
        _B,
        _NOW,
        "If temperatures have an average of 15 and a spread of 5, 20 degrees becomes 1.0.",
        related=["core.mean_scale"],
        fits=_PREP,
    ),
    "core.log1p": _a(
        "Compress large values",
        "Takes the logarithm of one plus each value. This makes very large numbers smaller and "
        "changes growth rates into steady steps.",
        ["Values cover several orders of magnitude", "Effects work in percentages"],
        ["Values can be negative"],
        ["Numeric columns with values of zero or more"],
        ["The server converts the results back. Averages on the original scale are approximate"],
        ["The same columns on a log scale"],
        _B,
        _NOW,
        "Sales of 9, 99 and 999 become about 2.3, 4.6 and 6.9.",
        related=["core.logit"],
        fits=_PREP,
    ),
    "core.logit": _a(
        "Unbounded scale for shares",
        "Changes shares between 0 and 1, such as conversion rates, into numbers without limits. "
        "Then a model cannot predict impossible values.",
        ["You model a rate or a share"],
        ["Values are not between 0 and 1"],
        ["Columns with shares that are strictly between 0 and 1"],
        ["The step moves exact values of 0 and 1 a small distance inwards"],
        ["The same columns on an unbounded scale"],
        _I,
        _NOW,
        "A 50% share becomes 0. A 90% share becomes about 2.2.",
        related=["core.log1p"],
        fits=_PREP,
    ),
    "core.impute": _a(
        "Fill gaps",
        "Fills missing values with a sensible substitute, such as the average, zero or the "
        "previous value.",
        ["Some values are missing and the model cannot use data with gaps"],
        [
            "Many values are missing (correct the source instead)",
            "A missing value has a meaning (add a missing-value flag)",
        ],
        ["The columns with gaps"],
        ["The step learns the fill values from the training data only"],
        ["The same columns without gaps"],
        _B,
        _NOW,
        "The step fills a missing week of spend in a market with the average of that market.",
        related=["core.missing_indicator", "core.replace_sentinel"],
        fits=_PREP,
    ),
    "core.replace_sentinel": _a(
        "Placeholder numbers to missing",
        "Marks placeholder numbers, such as -99 or 9999, as missing. Some systems use these "
        "numbers for 'unknown'.",
        ["Your source uses codes such as -99 for missing values"],
        ["All numbers are real measurements"],
        ["Columns with placeholder codes"],
        ["The codes are never real values"],
        ["The same columns with gaps in place of the codes"],
        _B,
        _NOW,
        "A temperature of -99 becomes a missing value.",
        related=["core.impute"],
        fits=_PREP,
    ),
    "core.clip": _a(
        "Cap extreme values",
        "Changes values below or above the limits that you select to the nearest limit. This "
        "controls outliers.",
        [
            "Some extreme values have too much effect on the model",
            "Values outside a range are errors",
        ],
        ["The extreme values are real and important"],
        ["Numeric columns and a lower limit, an upper limit or both"],
        ["Values beyond the limits do not have more information"],
        ["The same columns in the limits"],
        _B,
        _NOW,
        "With an upper limit of 100, a value of 250 becomes 100.",
        related=["core.max_abs_scale"],
        fits=_PREP,
    ),
    "core.fourier": _a(
        "Repeating seasonal patterns",
        "Adds smooth wave-shaped columns. With these columns, a model can learn a repeating "
        "pattern, such as a pattern for each year or each week.",
        [
            "Your result increases and decreases with the season",
            "You want seasonality without one column for each month",
        ],
        ["You have less than one full season of history"],
        ["A regular date column"],
        ["The pattern repeats with the same length in each cycle"],
        ["Pairs of wave columns (more pairs give a more detailed shape)"],
        _I,
        _NOW,
        "Three yearly wave pairs on weekly data show a summer peak and a winter dip.",
        related=["core.calendar", "core.holidays"],
        expert="sin/cos(2 pi k t / period), k = 1..order. The origin and the period are stored "
        "state, so forecasts continue the same phase.",
        fits=_PREP,
    ),
    "core.trend": _a(
        "Steady drift over time",
        "Adds a column that counts up over time, so a model can show a steady increase or "
        "decrease.",
        ["Your result increases or decreases steadily"],
        ["The trend changes direction frequently (use a flexible trend model)"],
        ["A regular date column"],
        ["The drift continues at the same rate into the future"],
        ["A trend column (optionally curved)"],
        _B,
        _NOW,
        "Week 1 gets 0, week 52 gets 1, and week 104 gets 2.",
        related=["core.fourier", "ts.hsgp_trend"],
        fits=_PREP,
    ),
    "core.calendar": _a(
        "Calendar facts",
        "Adds calendar facts, such as the month, the week of the year, or if a day is on a "
        "weekend.",
        ["Behaviour changes with the day of the week or the month"],
        ["Your data is not over time"],
        ["A date column"],
        ["The same calendar effect occurs every year"],
        ["One column for each calendar feature"],
        _B,
        _NOW,
        "2024-06-01 gets month 6 and weekend = yes.",
        related=["core.holidays", "core.fourier"],
        fits=_PREP,
    ),
    "core.holidays": _a(
        "Holiday flags",
        "Marks the periods that contain public holidays. It uses a holiday table that you supply.",
        ["Holidays change your sales"],
        ["Holidays do not change your result"],
        ["A date column and a holiday table (for each country or market)"],
        ["The holiday table covers the full period, and also the forecast"],
        ["A holiday flag or a count for each period"],
        _B,
        _NOW,
        "The step flags the week that contains 25 December in each market that has this holiday.",
        related=["core.calendar"],
        fits=_PREP,
    ),
    "core.lag": _a(
        "Earlier values as columns",
        "Adds the values from one or more earlier periods as extra columns, such as the sales "
        "of last week.",
        ["The value of the last period helps to predict this period"],
        ["You forecast many periods ahead and the lagged value is not known"],
        ["A time dimension and the columns to lag"],
        ["Earlier rows exist. The first rows get a fill value"],
        ["One new column for each lag"],
        _I,
        _NOW,
        "A one-week lag of sales puts the 120 of last week next to the 130 of this week.",
        related=["core.lag_tensor"],
        fits=_PREP,
    ),
    "core.lag_tensor": _a(
        "Recent history per series",
        "Keeps the last periods of each series side by side. Models use them to calculate "
        "carry-over effects themselves.",
        ["A model calculates its carry-over from a window of past spend"],
        ["The model takes plain series and uses the history internally"],
        ["A time dimension and the columns that need history"],
        ["The window is sufficiently long for the effect to decrease to zero"],
        ["A new dimension with the last periods of each series"],
        _X,
        _NOW,
        "With a 6-week window, each week has the spend of the 6 weeks before it.",
        related=["core.lag", "core.adstock"],
        fits=_PREP,
    ),
    "core.one_hot": _a(
        "Categories to yes/no columns",
        "Changes a category, such as region, into one yes/no column for each value. Then models "
        "that need numbers can use it.",
        ["A model needs numbers but you have categories"],
        ["A category has hundreds of values (give them numbers instead)"],
        ["Category columns"],
        [
            "The categories in the training data are the important categories. New categories "
            "follow a rule"
        ],
        ["One 0/1 column for each category value"],
        _B,
        _NOW,
        "Region north/south becomes columns north = 1/0 and south = 1/0.",
        related=["core.encode_levels"],
        fits=_PREP,
    ),
    "core.encode_levels": _a(
        "Number each category",
        "Gives each category value a number. Many models use this number to find a separate "
        "effect for each group.",
        ["A model needs group numbers (one effect for each store or market)"],
        ["The model reads category labels itself"],
        ["Category columns or dimensions"],
        ["The step gives the numbers on the training data. New values follow a rule"],
        ["An integer code for each category value"],
        _I,
        _NOW,
        "Stores A, B, C become 0, 1, 2.",
        related=["core.one_hot"],
        fits=_PREP,
    ),
    "core.missing_indicator": _a(
        "Flag missing values",
        "Adds a yes/no column that marks where a value was missing. Then a model can tell real "
        "values from filled values.",
        ["The fact that a value is missing gives you information"],
        ["There are no gaps"],
        ["Columns that can have gaps"],
        ["Use this step before you fill the gaps"],
        ["One 0/1 column for each input column"],
        _B,
        _NOW,
        "A missing price gets a flag of 1 next to the filled price.",
        related=["core.impute"],
        fits=_PREP,
    ),
}
"""Cards of the built-in preprocessing steps (``mapi-steps``)."""

# ==================================================================================== splits

SPLIT_ABOUTS: Final[dict[str, About]] = {
    "core.time_holdout": _a(
        "Test on the latest periods",
        "Keeps back the most recent periods and fits the model on the rest. Then it measures "
        "how well the model predicts the periods that it did not see.",
        ["You forecast, or you want an honest error for recent periods"],
        ["Your data does not have a time order"],
        ["An ordered dimension such as date"],
        ["The recent past is similar to the near future"],
        ["A training part and a test part, and holdout error metrics"],
        _B,
        _NOW,
        "With 104 weeks, the model learns on the first 96 weeks and the test uses the last 8 "
        "weeks.",
        related=["core.as_of", "split.rolling_origin"],
        fits={"goal": ["forecast", "validate_model"]},
    ),
    "core.as_of": _a(
        "Fit as of a past date",
        "Fits the model as if today is a past date. Then it measures the predictions for the "
        "periods after that date.",
        [
            "You want to do a past decision point again",
            "Data comes with a delay (use a gap)",
        ],
        ["Your data does not have a time order"],
        ["An ordered dimension and a cut-off date"],
        ["The cut-off leaves sufficient history for the fit"],
        ["A training part up to the date and a test part after it"],
        _B,
        _NOW,
        "As of 2024-06-30 with a 2-week gap, the test uses July from week 3.",
        related=["core.time_holdout", "split.expanding_window"],
        fits={"goal": ["forecast", "validate_model"]},
    ),
    "core.leave_group_out": _a(
        "Test on unseen markets",
        "Removes full markets or stores from the fit and measures how well the model predicts "
        "them.",
        ["You will use the model for new markets or stores"],
        ["You only predict for markets that the model saw"],
        ["A group dimension with several levels"],
        ["The removed groups are similar to the other groups"],
        ["A training part and the held-out groups"],
        _I,
        _NOW,
        "Fit on 8 markets and test on the other 2.",
        related=["split.geo_holdout"],
        fits={"goal": ["predict_values", "validate_model"], "data_shape": ["panel"]},
    ),
    "core.last_per_group": _a(
        "Test each group on its latest rows",
        "Keeps back the last observations of each customer or store and tests the model on them.",
        ["Groups have different lengths of history, such as shopping trips for each household"],
        ["Your data is one series"],
        ["A group dimension and an order in each group"],
        ["Each group has sufficient earlier rows to learn from"],
        ["Training rows and the held-out last rows of each group"],
        _I,
        _NOW,
        "The test uses the last trip of each household that has a minimum of 3 trips.",
        related=["core.time_holdout"],
        fits={"goal": ["understand_choices", "validate_model"], "data_shape": ["choices"]},
    ),
    "core.mask_block": _a(
        "Hide random patches",
        "Hides random blocks of the data from the fit and measures how well the model fills "
        "them in.",
        ["You want to test how the model fills gaps, not how it forecasts"],
        ["You need a forecast error (hold out the latest periods instead)"],
        ["One or more dimensions to cut blocks from"],
        ["The model accepts missing target values"],
        ["The data with hidden cells, and the positions of these cells"],
        _X,
        _NOW,
        "Hide 10% of the market-week cells in blocks of 4 weeks.",
        related=["core.time_holdout"],
        fits={"goal": ["validate_model"]},
    ),
}
"""Cards of the five built-in train/test splits (``mapi_steps.splits``)."""

# ==================================================================================== models

_TS_REF = ("statsmodels: time series analysis", "https://www.statsmodels.org/stable/tsa.html")

MODEL_ABOUTS: Final[dict[str, About]] = {
    # ---------------------------------------------------------------- surviving built-ins
    "tabular.regressor": _a(
        "Demand by store and week",
        "Predicts a number for each store, product and week from prices, promotions and the "
        "calendar. It gives a low and a high estimate.",
        [
            "You have many stores or products and you want a forecast for each",
            "Prices, promotions or stock explain demand",
        ],
        [
            "You need to add predictions into totals with a range (you cannot add ranges)",
            "You have one short series",
        ],
        ["A table of the number to predict and its drivers, for each store and week"],
        [
            "You know the future values of the drivers when you forecast",
            "Stores that the model did not see behave like the average store",
        ],
        ["A median forecast with low and high values for each cell", "Feature importance"],
        _B,
        _SEC,
        "Predict the units of next week for 300 stores from planned prices and promotions.",
        related=["core.calendar", "core.one_hot"],
        expert="Gradient-boosted quantile regression (xgboost or sklearn) on stacked rows. "
        "Coordinates become categorical features. Direct multi-horizon forecasting. "
        "Quantile representation (no cross-cell aggregation).",
        fits={
            "goal": ["predict_values", "forecast"],
            "data_shape": ["panel", "table"],
            "drivers": ["yes"],
            "frequency": ["daily", "weekly", "monthly", "not_time"],
        },
    ),
    "tabular.hierarchical_glm": _a(
        "Group effects with sharing",
        "Estimates a separate baseline for each group, such as a county or a store. Small "
        "groups get help from the data of the other groups.",
        [
            "Groups have very different quantities of data",
            "You want to predict for groups that the model did not see",
        ],
        ["You have one series over time"],
        ["One row for each observation with a group label and predictors"],
        [
            "Groups are sufficiently similar to share information",
            "Effects are additive on the scale of the model",
        ],
        ["Effects for each group with ranges", "Predictions for new groups"],
        _I,
        _MIN,
        "Radon levels for each house. If a county has few houses, its baseline moves towards "
        "the state average.",
        related=["core.encode_levels"],
        expert="Bayesian hierarchical GLM (PyMC, NUTS). Partially pooled group intercepts and "
        "optional slopes. Group-level covariates in the mean. The model draws new levels from "
        "the fitted hyperprior.",
        fits={
            "goal": ["explain_drivers", "predict_values"],
            "data_shape": ["table", "panel"],
            "uncertainty": ["essential", "nice"],
            "frequency": ["not_time"],
        },
    ),
    "choice.multinomial_logit": _a(
        "Customer choice",
        "Explains which product a customer selects on each shopping trip, and how price and "
        "promotions change that choice.",
        [
            "You have purchase records with the price of each product on offer",
            "You want to test a price change before you make it",
        ],
        ["You only have total sales, not individual choices"],
        [
            "One row for each trip: the product that the customer selected, and the price and "
            "promotion of each product"
        ],
        [
            "Customers select the option that they like best for the given price and promotion",
            "Differences in taste between households are stable over time",
        ],
        [
            "How sensitive the choices are to price and promotion",
            "Predicted choice shares",
            "What-if shares for new prices",
        ],
        _I,
        _MIN,
        "A 10% decrease in the price of Brand A increases its share from 30% to 36%.",
        related=["core.last_per_group"],
        expert="Conditional (McFadden) logit over alternative-specific covariates with "
        "household random effects. Bayesian-bootstrap draws over households.",
        refs=[("McFadden (1974), Conditional logit analysis of qualitative choice behavior", None)],
        fits={
            "goal": ["understand_choices"],
            "data_shape": ["choices"],
            "uncertainty": ["essential", "nice"],
        },
    ),
    "ts.gaussian_hmm": _a(
        "Hidden states over time",
        "Finds a small number of hidden states, such as calm periods and busy periods, that a "
        "series changes between. It tells you the probable state of each period.",
        ["Your series seems to change between distinct modes"],
        ["Behaviour changes smoothly, not in jumps", "You have very short sequences"],
        ["One or more sequences of measurements over time"],
        [
            "Each state has its own typical level and spread",
            "The probability of a change of state depends only on the current state",
        ],
        ["The probable state of each period", "How frequently the states change", "Forecasts"],
        _X,
        _MIN,
        "The track of an animal divides into rest phases and travel phases.",
        related=["ts.markov_switching"],
        expert="K-state Gaussian HMM. The forward algorithm in PyMC marginalises the states. "
        "The terminal state is stored for forecast and filter. Ordering fixes label switching.",
        fits={
            "goal": ["find_regimes"],
            "data_shape": ["one_series", "panel"],
            "uncertainty": ["essential", "nice"],
        },
    ),
    "ts.hsgp_trend": _a(
        "Smooth trend forecast",
        "Forecasts one regular series with a smooth, flexible trend and a repeating seasonal "
        "pattern. It gives a range of probable values.",
        [
            "You forecast one weekly or daily series with a trend that bends over time",
            "You want an honest range around the forecast",
        ],
        [
            "You have many related series (forecast them together or for each store)",
            "You need a forecast further ahead than the model permits without a new fit",
        ],
        ["One number over time at a regular frequency"],
        ["The trend changes gradually", "The seasonal pattern repeats"],
        ["A forecast with a range", "Trend and seasonal parts"],
        _B,
        _MIN,
        "Forecast weekly website visits 26 weeks ahead with a 90% range.",
        related=["core.fourier", "ts.ets", "ts.sarimax"],
        expert="Hilbert-space Gaussian process (HSGP) trend with optional linear drift and "
        "Fourier seasonality (PyMC, NUTS). Forecasts are limited to max_extension periods.",
        refs=[
            (
                "Riutort-Mayol et al. (2022), Practical Hilbert space approximate Bayesian "
                "Gaussian processes",
                "https://arxiv.org/abs/2004.11408",
            )
        ],
        fits={
            "goal": ["forecast"],
            "data_shape": ["one_series"],
            "frequency": ["daily", "weekly", "monthly"],
            "history": ["medium", "long"],
            "uncertainty": ["essential", "nice"],
        },
    ),
    # --------------------------------------------------------------- mmm-framework (v0.5)
    "mmm.bayesian": _a(
        "Marketing mix model",
        "Measures how much each marketing channel adds to your sales, in total or for each "
        "market. It also shows what occurs if you move budget between channels.",
        [
            "You have two or more years of weekly sales and spend for each channel",
            "You want to know the return of each channel and plan budgets",
            "You have results for each market or product and you want one model for all of them",
        ],
        [
            "Spend changes very little over time (the model cannot measure the effect)",
            "You have less than a year of history",
            "You need answers in seconds (use the fast frequentist version)",
        ],
        ["Weekly (or daily) sales, spend for each channel, and optional controls such as price"],
        [
            "Advertising has an effect that continues for some time, and diminishing returns",
            "The controls include the factors that change both spend and sales",
        ],
        [
            "Contribution and return for each channel, with ranges",
            "Response curves",
            "Budget plans and what-if answers",
        ],
        _I,
        _LONG,
        "TV returns 2.1 euros for each euro of spend (range 1.4 to 2.9) and search returns 3.4.",
        related=["mmm.frequentist", "mmm.nested", "mmm.multivariate", "core.resample"],
        expert="mmm-framework BayesianMMM: geometric/Weibull adstock, Hill or logistic "
        "saturation, trend options, Fourier seasonality, and hierarchical pooling over geos and "
        "products (MFF panels). Priors come from business beliefs. Lift tests are a "
        "calibration likelihood. NUTS through PyMC, nutpie or numpyro.",
        refs=[
            ("mmm-framework documentation", "https://redam94.github.io/mmm-framework/"),
            (
                "Jin et al. (2017), Bayesian methods for media mix modeling with carryover and "
                "shape effects",
                None,
            ),
        ],
        fits={
            "goal": ["explain_marketing", "plan_budget"],
            "data_shape": ["one_series", "panel"],
            "frequency": ["daily", "weekly"],
            "history": ["medium", "long"],
            "drivers": ["yes"],
            "uncertainty": ["essential", "nice"],
        },
    ),
    "mmm.nested": _a(
        "Marketing mix with steps in between",
        "Measures marketing effects that work through an intermediate result. For example, ads "
        "cause website visits, and the visits cause sales.",
        [
            "You have an intermediate measure, such as visits, searches or awareness",
            "You want to know how much of the effect of a channel goes through it",
        ],
        ["You only have sales and spend (use the standard marketing mix model)"],
        ["Sales, the intermediate measure and spend for each channel over time"],
        [
            "The intermediate measure carries all or part of the effect",
            "Advertising has carry-over and diminishing returns",
        ],
        [
            "Direct and indirect contribution for each channel",
            "Effects on the intermediate measure",
        ],
        _X,
        _LONG,
        "Search ads increase visits by 12%, and visits explain 40% of online sales.",
        related=["mmm.bayesian", "mmm.multivariate"],
        expert="mmm-framework mmm_extensions nested (mediated) model: a mediator equation and "
        "an outcome equation that share media transforms.",
        refs=[("mmm-framework documentation", "https://redam94.github.io/mmm-framework/")],
        fits={
            "goal": ["explain_marketing"],
            "data_shape": ["several_series", "panel"],
            "history": ["medium", "long"],
            "uncertainty": ["essential", "nice"],
        },
    ),
    "mmm.multivariate": _a(
        "Marketing mix for several products",
        "Measures marketing effects on two or more results at the same time, such as the sales "
        "of related products. It also shows how the advertising of one product changes the "
        "sales of another product.",
        [
            "You sell related products and you advertise them separately",
            "You think that the advertising of one product changes the sales of another product",
        ],
        ["Only one result is important to you"],
        ["Sales for each product, and spend for each channel and product over time"],
        ["Cross-product effects are steady over the period"],
        ["Contribution for each channel and product", "Cross-product effects"],
        _X,
        _LONG,
        "The advertising of the premium range also increases the sales of the basic range by 5%.",
        related=["mmm.bayesian", "mmm.nested"],
        expert="mmm-framework mmm_extensions multivariate-outcome model with cross-effect "
        "components and a correlated observation model.",
        refs=[("mmm-framework documentation", "https://redam94.github.io/mmm-framework/")],
        fits={
            "goal": ["explain_marketing"],
            "data_shape": ["several_series", "panel"],
            "history": ["medium", "long"],
            "uncertainty": ["essential", "nice"],
        },
    ),
    "mmm.frequentist": _a(
        "Fast marketing mix model",
        "Gives a quick first estimate of the contribution of each channel in seconds. Its ranges "
        "(from resampled data) are simpler than the uncertainty of a full Bayesian model.",
        [
            "You need a fast first look before a full model",
            "You want to compare many set-ups quickly",
        ],
        [
            "Decisions depend on the full uncertainty of the answer (use the Bayesian model)",
            "You want to add business beliefs or lift tests",
        ],
        ["Weekly sales and spend for each channel"],
        [
            "The model searches a grid for the carry-over and saturation settings",
            "Channel effects are not negative",
        ],
        ["Contribution and return for each channel, with bootstrap ranges"],
        _B,
        _SEC,
        "A first result in 5 seconds: TV 28% of sales, search 12%.",
        related=["mmm.bayesian"],
        expert="mmm-framework frequentist ridge / constrained least squares with a grid "
        "search over transform parameters. Block-bootstrap replicates (a minimum of 20) are the "
        "draws of each output, so its ranges are confidence intervals, not posteriors.",
        refs=[("mmm-framework documentation", "https://redam94.github.io/mmm-framework/")],
        fits={
            "goal": ["explain_marketing"],
            "data_shape": ["one_series", "panel"],
            "uncertainty": ["no", "nice"],
            "drivers": ["yes"],
        },
    ),
    # -------------------------------------------------------------------- statsmodels (v0.5)
    "ts.sarimax": _a(
        "Forecast with past values and drivers",
        "Forecasts a series from its recent past, its seasonal pattern and outside drivers that "
        "you know in advance, such as planned prices or holidays.",
        [
            "You forecast one regular series some periods ahead",
            "Known future drivers (prices, promotions, holidays) are important",
        ],
        [
            "You have less than about 50 periods",
            "Two or more series change each other (use VAR)",
        ],
        [
            "One number over time at a regular frequency, and optional driver columns that you "
            "know for the future"
        ],
        [
            "After differencing, the series varies around a stable level",
            "Relationships stay the same over time",
        ],
        ["A forecast with a range", "Effects of the drivers", "Residual checks"],
        _I,
        _SEC,
        "Forecast weekly orders 12 weeks ahead from the orders of recent weeks and planned "
        "discounts.",
        related=["ts.arima", "search.arma_order", "test.adf", "transform.difference"],
        expert="statsmodels SARIMAX (state space): ARIMA(p,d,q)(P,D,Q)s with exogenous "
        "regressors. Exact likelihood through the Kalman filter. Prediction intervals as "
        "quantiles. Forecasts need future exog.",
        refs=[
            _TS_REF,
            (
                "Hyndman & Athanasopoulos, Forecasting: Principles and Practice (ch. 9-10)",
                "https://otexts.com/fpp3/",
            ),
        ],
        fits={
            "goal": ["forecast", "explain_drivers"],
            "data_shape": ["one_series"],
            "frequency": ["daily", "weekly", "monthly"],
            "history": ["medium", "long"],
            "drivers": ["yes", "no"],
            "uncertainty": ["essential", "nice", "no"],
        },
    ),
    "ts.arima": _a(
        "Forecast from past values",
        "Forecasts a series from its recent past and trend, without outside drivers.",
        ["You have only the series", "You want a reliable, well-known forecast method"],
        ["Outside drivers explain most of the movement (use the version with drivers)"],
        ["One number over time at a regular frequency"],
        ["After differencing, the series varies around a stable level"],
        ["A forecast with a range", "Residual checks"],
        _I,
        _SEC,
        "Forecast monthly sales 6 months ahead from the sales of recent years.",
        related=["ts.sarimax", "search.arma_order", "test.adf"],
        expert="statsmodels ARIMA(p,d,q)(P,D,Q)s (state space) without exog.",
        refs=[_TS_REF],
        fits={
            "goal": ["forecast"],
            "data_shape": ["one_series"],
            "drivers": ["no"],
            "frequency": ["daily", "weekly", "monthly"],
            "history": ["medium", "long"],
        },
    ),
    "ts.var": _a(
        "Series that drive each other",
        "Models two or more series that change each other over time, such as the prices and "
        "sales of two brands. It forecasts them together.",
        [
            "Two or more series move together and change each other",
            "You want to see how a shock to one series goes to the other series",
        ],
        [
            "You have one series",
            "The series have the same trend permanently (use the error-correction model)",
        ],
        ["Two or more numbers over time at the same frequency"],
        [
            "Each series varies around a stable level, after differencing if necessary",
            "Effects work through past values",
        ],
        [
            "Joint forecasts with ranges",
            "How a shock goes through the series (impulse responses)",
            "Which series help to predict which series (Granger tests)",
        ],
        _X,
        _SEC,
        "A price decrease by brand A decreases the sales of brand B for three weeks.",
        related=["ts.varmax", "ts.vecm", "test.granger", "search.var_lag_order"],
        expert="statsmodels VAR(p): OLS for each equation. Lag order by AIC/BIC/HQIC. Impulse "
        "responses and forecast-error variance decomposition. Granger causality tests.",
        refs=[_TS_REF, ("Lutkepohl, New Introduction to Multiple Time Series Analysis", None)],
        fits={
            "goal": ["compare_series", "forecast"],
            "data_shape": ["several_series"],
            "history": ["medium", "long"],
        },
    ),
    "ts.varmax": _a(
        "Series that drive each other, with drivers",
        "Models two or more series that change each other and also respond to outside drivers "
        "that you know in advance. It forecasts them together.",
        ["Two or more series change each other, and outside drivers are also important"],
        ["You have one series", "You have many series and little history"],
        ["Two or more numbers over time, and driver columns that you know for the future"],
        ["Series vary around stable levels", "Driver effects are steady"],
        ["Joint forecasts with ranges", "Driver effects", "Impulse responses"],
        _X,
        _MIN,
        "Forecast the sales of two brands from the history of each brand and planned promotions.",
        related=["ts.var", "ts.sarimax"],
        expert="statsmodels VARMAX(p,q) with exogenous regressors (VARX when q = 0), "
        "estimated by maximum likelihood in state-space form.",
        refs=[_TS_REF],
        fits={
            "goal": ["compare_series", "forecast", "explain_drivers"],
            "data_shape": ["several_series"],
            "drivers": ["yes"],
            "history": ["medium", "long"],
        },
    ),
    "ts.vecm": _a(
        "Series tied together in the long run",
        "Models series that wander but stay tied together in the long run, such as the prices "
        "of close substitutes. It shows how fast they go back after a gap opens.",
        ["The series have a trend but keep a stable long-run relationship"],
        ["The series do not have a trend", "A long-run tie is not probable"],
        ["Two or more numbers with a trend over time"],
        ["A stable long-run relationship exists (the Johansen test does a check of this)"],
        ["Long-run relationships", "Speed of adjustment", "Joint forecasts with ranges"],
        _X,
        _SEC,
        "When the price of a competitor goes 5% above your price, half of the gap closes in 4 "
        "weeks.",
        related=["test.johansen", "search.coint_rank", "ts.var", "ts.ecm"],
        expert="statsmodels VECM with cointegration rank from the Johansen trace/max-eigenvalue "
        "tests. You can select the deterministic terms.",
        refs=[
            _TS_REF,
            ("Johansen (1991), Estimation and hypothesis testing of cointegration vectors", None),
        ],
        fits={
            "goal": ["compare_series", "forecast"],
            "data_shape": ["several_series"],
            "history": ["long", "medium"],
        },
    ),
    "ts.ecm": _a(
        "Long-run and short-run effects",
        "Separates the short-term effect of a driver from its long-term effect on one series. "
        "It also tests if a long-run relationship exists.",
        [
            "One result and some drivers have the same trend",
            "You want both the immediate effect and the lasting effect of a driver",
        ],
        ["The result and the drivers do not have a trend (a regression with lags is sufficient)"],
        ["One result and one or more drivers over time"],
        ["The drivers change the result, but the result does not change the drivers"],
        ["Short-run and long-run effects", "Speed of adjustment", "A bounds test result"],
        _X,
        _SEC,
        "A 1% price increase decreases sales by 0.4% immediately and by 1.1% in the long run.",
        related=["ts.ardl", "ts.vecm", "test.engle_granger"],
        expert="Single-equation error-correction model in UECM form (statsmodels UECM). "
        "Pesaran-Shin-Smith bounds test for a levels relationship.",
        refs=[_TS_REF, ("Pesaran, Shin & Smith (2001), Bounds testing approaches", None)],
        fits={
            "goal": ["explain_drivers", "forecast"],
            "data_shape": ["one_series"],
            "drivers": ["yes"],
            "history": ["medium", "long"],
        },
    ),
    "ts.ardl": _a(
        "Effects that build up over time",
        "Explains a series from its past and from the current and past values of drivers. You "
        "can see how an effect increases over several periods.",
        ["The effect of a driver spreads over several periods"],
        ["Effects are only immediate (a simple regression is sufficient)"],
        ["One result and one or more drivers over time"],
        [
            "The drivers change the result, but the result does not change the drivers",
            "The series vary around stable levels or share a long-run relationship",
        ],
        ["Effects for each lag", "Long-run multipliers", "Forecasts for given future drivers"],
        _I,
        _SEC,
        "A promotion increases sales by 8% in its week and by 3% in the next week.",
        related=["ts.ecm", "ts.sarimax"],
        expert="statsmodels ARDL with lag orders that an information criterion selects "
        "(ardl_select_order).",
        refs=[_TS_REF],
        fits={
            "goal": ["explain_drivers", "forecast"],
            "data_shape": ["one_series"],
            "drivers": ["yes"],
        },
    ),
    "ts.ets": _a(
        "Exponential smoothing forecast",
        "Forecasts a series and gives recent values more weight than older values. It has an "
        "optional trend and seasonality, and it is quick and robust.",
        [
            "You need a fast, reliable baseline forecast",
            "The level, trend and seasonality change slowly",
        ],
        ["Outside drivers explain most of the movement"],
        ["One number over time at a regular frequency"],
        ["Recent history is the best guide to the near future"],
        ["A forecast with a range", "Level, trend and seasonal parts"],
        _B,
        _SEC,
        "Forecast monthly demand 12 months ahead with a damped trend.",
        related=["ts.theta", "ts.arima", "evaluate.baseline"],
        expert="statsmodels ETSModel (error, trend, seasonal; additive or multiplicative; "
        "damped trend), with prediction intervals from simulation.",
        refs=[_TS_REF, ("Hyndman et al., Forecasting with Exponential Smoothing", None)],
        fits={
            "goal": ["forecast"],
            "data_shape": ["one_series"],
            "drivers": ["no"],
            "frequency": ["daily", "weekly", "monthly"],
        },
    ),
    "ts.unobserved_components": _a(
        "Trend, season and noise",
        "Divides a series into a level and a trend that change slowly, a seasonal pattern and "
        "noise. It forecasts the series from these parts.",
        [
            "You want to see the underlying trend without the noise",
            "The trend and the season change slowly over time",
        ],
        ["You only need a quick baseline (exponential smoothing is simpler)"],
        ["One number over time, and optional drivers"],
        ["Each part changes smoothly and independently"],
        ["Smoothed level, trend and season", "Forecasts with ranges"],
        _I,
        _SEC,
        "Weekly sales divide into an increasing trend, a Christmas peak and noise from week to "
        "week.",
        related=["ts.ets", "ts.hsgp_trend"],
        expert="statsmodels UnobservedComponents (local level/trend, stochastic seasonal, "
        "cycle, exog), estimated by maximum likelihood with the Kalman smoother.",
        refs=[
            _TS_REF,
            ("Harvey, Forecasting, Structural Time Series Models and the Kalman Filter", None),
        ],
        fits={"goal": ["forecast", "explain_drivers"], "data_shape": ["one_series"]},
    ),
    "ts.dynamic_factor": _a(
        "Common movements across many series",
        "Finds a few hidden forces that move many series together, such as the general economy "
        "behind the sales in many categories.",
        ["You have many related series and you want their shared drivers"],
        ["You have one or two series"],
        ["Many numbers over time at the same frequency"],
        ["Some common factors explain most of the joint movement"],
        ["The common factors over time", "How much each series follows them", "Forecasts"],
        _X,
        _MIN,
        "Twenty series of category sales follow one 'consumer mood' factor.",
        related=["ts.var"],
        expert="statsmodels DynamicFactor (state space; the factors follow a VAR).",
        refs=[_TS_REF],
        fits={"goal": ["compare_series", "forecast"], "data_shape": ["several_series", "panel"]},
    ),
    "ts.markov_switching": _a(
        "Changes between regimes",
        "Finds if a series changes between regimes, such as periods of high growth and low "
        "growth. It estimates the behaviour in each regime.",
        [
            "Behaviour seems to jump between distinct modes",
            "You want the probability of each mode now",
        ],
        ["Changes are gradual", "You have little history"],
        ["One number over time, and optional drivers"],
        [
            "There is a small number of regimes. A change of regime depends only on the current "
            "regime"
        ],
        ["The probability of each regime for each period", "Behaviour in each regime"],
        _X,
        _SEC,
        "Demand changes between a normal regime and a regime that promotions cause.",
        related=["ts.gaussian_hmm", "test.cusum"],
        expert="statsmodels MarkovRegression / MarkovAutoregression (Hamilton filter, EM then "
        "BFGS).",
        refs=[
            _TS_REF,
            (
                "Hamilton (1989), A new approach to the economic analysis of "
                "nonstationary time series",
                None,
            ),
        ],
        fits={"goal": ["find_regimes"], "data_shape": ["one_series"]},
    ),
    "ts.theta": _a(
        "Simple strong baseline",
        "Forecasts a series with a combination of a straight-line trend and smoothed recent "
        "values. This simple method is hard to beat.",
        ["You want a strong, simple baseline to compare with other models"],
        ["Outside drivers are very important", "You need detailed explanations"],
        ["One number over time (the model removes the seasonality and puts it back automatically)"],
        ["The recent trend continues"],
        ["A forecast with a range"],
        _B,
        _NOW,
        "A theta forecast of monthly sales that other models must beat before you use them.",
        related=["ts.ets", "evaluate.baseline"],
        expert="statsmodels ThetaModel (Assimakopoulos-Nikolopoulos; equivalent to SES with "
        "drift) with optional seasonal decomposition.",
        refs=[_TS_REF],
        fits={"goal": ["forecast"], "data_shape": ["one_series"], "drivers": ["no"]},
    ),
    # ------------------------------------------------------ scikit-learn tabular (mapi-sklearn)
    "sklearn.tabular_regressor": _a(
        "Predict a number from a table",
        "Predicts a number for each row of a table, such as a store and week, from the other "
        "columns. It uses a well-known machine-learning method that you select.",
        [
            "You have many rows with drivers (price, promotion, weather) and you want one best "
            "estimate for each row",
            "You want to see how much each driver adds to a prediction",
        ],
        [
            "You need a range of probable values (use the quantile, bootstrap or conformal "
            "versions)",
            "You have one short series over time",
        ],
        ["A table of the number to predict and the columns that explain it"],
        [
            "The rows that you predict are similar to the rows that the model learned from",
            "You know the future values of the drivers when you forecast",
        ],
        [
            "One prediction for each row",
            "The contribution of each driver (linear and tree models)",
        ],
        _B,
        _SEC,
        "Predict the units of next week for each store and product from planned prices, with "
        "gradient boosting.",
        related=["sklearn.tabular_quantiles", "sklearn.tabular_conformal", "core.one_hot"],
        expert="An allow-listed scikit-learn or xgboost regressor on rows stacked from the "
        "labelled dims (coordinates become features). Point representation. Exact linear or "
        "counterfactual-zeroing contributions. The server stores models as skops or xgboost "
        "JSON.",
        fits={
            "goal": ["predict_values", "explain_drivers"],
            "data_shape": ["table", "panel"],
            "drivers": ["yes"],
            "uncertainty": ["no"],
        },
    ),
    "sklearn.tabular_quantiles": _a(
        "Predict a number with a low and high estimate",
        "Predicts a low, middle and high value for each row of a table. These values show how "
        "certain the prediction is.",
        [
            "You need a best case and a worst case for each store, product or customer",
            "Stock or staff decisions depend on the high end or the low end",
        ],
        [
            "You need to add predictions into totals with a range (you cannot add the ranges "
            "of rows)"
        ],
        ["A table of the number to predict and the columns that explain it"],
        ["The rows that you predict are similar to the rows that the model learned from"],
        ["A low, middle and high prediction for each row"],
        _I,
        _SEC,
        "Plan stock at the 90% level: the high estimate of the demand of next week for each store.",
        related=["sklearn.tabular_regressor", "sklearn.tabular_bootstrap"],
        expert="xgboost multi-quantile regression or one scikit-learn quantile model for each "
        "level. Quantile representation (you cannot aggregate the quantiles of cells across "
        "cells).",
        fits={
            "goal": ["predict_values", "forecast"],
            "data_shape": ["table", "panel"],
            "drivers": ["yes"],
            "uncertainty": ["essential", "nice"],
        },
    ),
    "sklearn.tabular_bootstrap": _a(
        "Predict a number with ranges you can add up",
        "Fits the same method many times on resampled data. This gives ranges that stay correct "
        "when you add predictions into totals.",
        [
            "You need a range for totals (all stores, a full region)",
            "The data has groups (stores) that the resample must keep together",
        ],
        ["You need results in seconds on a very large table (it fits many models)"],
        ["A table of the number to predict and the columns that explain it"],
        [
            "The rows that you predict are similar to the rows that the model learned from",
            "A resample of full groups shows how uncertain the model is",
        ],
        ["Many joint simulated predictions for each row", "Ranges for all totals"],
        _I,
        _MIN,
        "A range for the total sales of next month across 300 stores.",
        related=["sklearn.tabular_quantiles", "sklearn.tabular_regressor"],
        expert="A bootstrap ensemble (with an optional resample of full groups with 'by'). Each "
        "replicate is a joint draw, so aggregation is valid (draws representation).",
        fits={
            "goal": ["predict_values", "forecast"],
            "data_shape": ["table", "panel"],
            "drivers": ["yes"],
            "uncertainty": ["essential"],
        },
    ),
    "sklearn.tabular_conformal": _a(
        "Predict a number with a guaranteed range",
        "Predicts a number for each row with a range. The range contains the true value about "
        "as frequently as you ask, such as nine times in ten.",
        [
            "You need ranges with a coverage that you can trust for each row",
            "You do not want to make assumptions about the shape of the errors",
        ],
        [
            "You need ranges for totals (use the bootstrap version)",
            "You have very little data (the model keeps a part of it to set the size of the "
            "ranges)",
        ],
        ["A table of the number to predict and the columns that explain it"],
        ["New rows behave like the rows that the model kept to set the size of the ranges"],
        ["One prediction and a range for each row"],
        _I,
        _SEC,
        "Ranges that contain the demand of next week for 90% of store-product pairs.",
        related=["sklearn.tabular_quantiles", "sklearn.tabular_regressor"],
        expert="Split-conformal prediction intervals at level 1 - alpha with a calibration "
        "fraction. Marginal coverage for each cell. Interval representation (you cannot "
        "aggregate it).",
        fits={
            "goal": ["predict_values"],
            "data_shape": ["table", "panel"],
            "drivers": ["yes"],
            "uncertainty": ["essential", "nice"],
        },
    ),
    "sklearn.tabular_classifier": _a(
        "Predict yes or no",
        "Estimates the probability that something occurs for each row of a table. For example, "
        "a customer buys again or an order arrives late.",
        [
            "The result is yes or no for each row",
            "You want a probability to put rows in order or to select rows",
        ],
        ["The result is a number or has more than two outcomes"],
        ["A table with a yes/no column and the columns that explain it"],
        ["The rows that you score are similar to the rows that the model learned from"],
        ["The probability of 'yes' for each row"],
        _B,
        _SEC,
        "The probability that each customer orders again next month, to decide who gets a voucher.",
        related=["sklearn.tabular_regressor", "choice.multinomial_logit"],
        expert="An allow-listed scikit-learn or xgboost binary classifier on stacked rows. "
        "Point representation of the positive-class probability.",
        fits={
            "goal": ["predict_values", "understand_choices"],
            "data_shape": ["table", "panel", "choices"],
            "drivers": ["yes"],
        },
    ),
    # ---------------------------------------------------------------- scientific models
    "sci.lgcp_grid": _a(
        "Event counts on a map over time",
        "Finds where and when events such as earthquakes occur more often. It counts the "
        "events in each cell of a map for each period, and it forecasts the next periods "
        "with a range.",
        [
            "You have counts of events in each cell of a regular grid for each period",
            "The area with many events moves or changes over time",
            "You want a map of the expected rate and a map of the uncertainty",
        ],
        [
            "You have exact locations and want a model for each point (bin them into cells first)",
            "The grid has thousands of cells (the fit becomes too slow)",
            "You must know what occurs if a cause changes, such as a new policy (the model has "
            "no columns for causes)",
            "You need a forecast further ahead than the model permits without a new fit",
        ],
        [
            "A count of events for each cell and each period (quarters, months or years)",
            "Cells of equal size, with the row and column number of each cell",
        ],
        [
            "The rate changes smoothly from one cell to the cells near it",
            "The map changes only from one era (a group of periods) to the next",
            "The total for the full grid changes gradually from one period to the next",
            "Events can occur in groups (more variation than a constant rate gives)",
        ],
        [
            "The expected events for each cell and period, with a range",
            "The expected total for each period, with a range",
            "Forecasts of the map and of the total for the next periods",
        ],
        _X,
        _MIN,
        "Count the earthquakes in each 25 km cell for each quarter from 2009 to 2017. Then "
        "forecast the map and the total for 2018 and 2019 with a 90% range.",
        related=["ts.hsgp_trend"],
        expert="Log-Gaussian Cox process on a space-time grid (pymc-challenges E07, rung 4). "
        "log rate = alpha + g_t + log softmax over cells of f_era(s): the total per period "
        "(exp(alpha + g_t), g a centred Gaussian random walk) is separate from the share of "
        "each cell, so the field cannot inflate the total. f is a 2-D Hilbert-space GP "
        "(Matern-5/2, product basis of m_row x m_col sine functions, boundary factor c) "
        "whose basis coefficients follow a random walk over eras, anchored at the busiest "
        "fit era. Negative binomial counts (prior on 1/phi). The random-walk steps and the "
        "era coefficients cover max_extension periods after the fit, so forecasts use "
        "posterior draws of their prior. Lengthscale prior: inverse gamma with 90% of its "
        "mass between ell_lower_km and ell_upper_km.",
        refs=[
            (
                "Moller, Syversveen and Waagepetersen (1998), Log Gaussian Cox processes",
                "https://doi.org/10.1111/1467-9469.00115",
            ),
            (
                "Riutort-Mayol et al. (2022), Practical Hilbert space approximate Bayesian "
                "Gaussian processes",
                "https://arxiv.org/abs/2004.11408",
            ),
        ],
        fits={
            "goal": ["map_fields", "forecast"],
            "data_shape": ["grid", "panel"],
            "frequency": ["monthly"],
            "history": ["medium", "long"],
            "uncertainty": ["essential", "nice"],
            "experience": ["expert"],
            "speed": ["minutes", "can_wait"],
        },
    ),
    "sci.fisher_kpp": _a(
        "Cells that close a wound",
        "Measures how fast cells move and divide when they close a scratch in a layer of "
        "cells. It also shows the speed of the cell front and forecasts the counts at later "
        "hours, with a range.",
        [
            "You have counts of cells in columns across a scratch at some times",
            "You want to know how fast the cells move and how fast they divide",
            "You want the speed at which the wound closes, with a range",
        ],
        [
            "The columns do not have equal width",
            "You have positions of single cells, not counts (count them in columns first)",
            "The cells move in a direction, for example to a chemical signal (the model has "
            "only random movement)",
            "You need a forecast further ahead than the model permits without a new fit",
        ],
        [
            "A count of cells for each column and each time, with the time in whole hours",
            "Columns of equal width, with the position of the center of each column in um",
            "Counts at the first time for all columns (the model starts from them)",
        ],
        [
            "Cells move at random and divide until the columns are full",
            "No cells go in or out at the two edges of the image",
            "All columns have the same maximum number of cells",
            "With the delay setting, the cells start slowly after the scratch",
        ],
        [
            "The expected cells for each column and time, with a range (a map over time and "
            "position)",
            "The speed of the cell front in um per hour, with a range",
            "The rates of movement and division, and the maximum cells in a column",
            "Forecasts of the counts for the next times",
        ],
        _X,
        _MIN,
        "Count PC-3 cells in 38 columns of 50 um at 0, 12, 24, 36 and 48 hours. Get the "
        "speed of the cell front (about 27 um per hour) and forecast the counts at 60 hours.",
        related=["sci.lgcp_grid"],
        expert="Fisher-KPP reaction-diffusion u_t = a(t) (D u_xx + lam u (1 - u)), "
        "a(t) = tanh(beta t) with the delay setting, else 1 (pymc-challenges E50). Method of "
        "lines: finite-volume Laplacian with zero-flux boundaries, explicit Euler steps of "
        "1/steps_per_hour hours in pytensor.scan. The PDE starts from the counts at the "
        "first fitted time (conditioned on, not in the likelihood, kept in the bundle). "
        "Counts are Poisson with mean K u. Priors: D ~ LogNormal(log 1000, 1) um^2/h, lam ~ "
        "LogNormal(log 0.05, 1) 1/h, K ~ Normal(122, 15), beta ~ LogNormal(log 0.1, 1) 1/h. "
        "The fit refuses settings where D dt / dx^2 > 1/2 at the 99% prior quantile of D "
        "(CFL bound). wave_speed = 2 sqrt(D lam). Without the delay, D is biased low by a "
        "factor of about four on the PC-3 data, and LOO prefers the delay model.",
        refs=[
            (
                "Jin et al. (2016), Reproducibility of scratch assays is affected by the "
                "initial degree of confluence, Journal of Theoretical Biology",
                None,  # lead: verify and add the DOI (not given in the E50 source)
            ),
            (
                "Simpson, Murphy and Maclaren (2024), likelihood analysis of the PC-3 scratch "
                "assay counts with a delayed Fisher-KPP model",
                None,  # lead: verify the exact title and add the DOI
            ),
        ],
        fits={
            "goal": ["fit_physics", "forecast"],
            "data_shape": ["grid"],
            "frequency": ["not_time"],
            "uncertainty": ["essential", "nice"],
            "experience": ["expert"],
            "speed": ["minutes", "can_wait"],
        },
    ),
    "sci.traction_force": _a(
        "Cell forces from gel movement",
        "Calculates the forces that cells apply to a soft gel from the measured movement of the "
        "gel surface. It gives a range of probable force maps.",
        [
            "You have a map of the gel movement under a cell or a colony (bead tracking or PIV)",
            "You need the strain energy or the contractility of the cells with a range",
            "You want to know which smoothness of the force map the data support",
        ],
        [
            "The displacement map has gaps (the model needs a value at each pixel)",
            "The gel is not much thicker than the cells are wide",
            "The forces occur only at a few small adhesions and you need sharp peaks",
        ],
        [
            "A displacement map with x and y components on a regular grid of pixels",
            "The grid spacing, the stiffness (Young's modulus) and the Poisson ratio of the gel",
            "Optional: a mask of the area under the cells",
        ],
        [
            "The gel is a linear elastic solid and its stiffness is known (or known to a given "
            "percentage)",
            "The force map is smooth and has the same statistical properties in all of the image",
            "The noise in the displacements is independent from pixel to pixel",
            "The image repeats at its edges (the Fourier transform uses this)",
        ],
        [
            "Probable force maps (traction, in Pa) with a mean, a spread and a 90% range for each "
            "pixel",
            "The displacement map that each force map causes",
            "The strain energy (fJ) and the contractility (uN) inside the mask, with a range",
        ],
        _X,
        _MIN,
        "The gel movement under a colony of seven cells on a 49 kPa gel gives a strain energy of "
        "271 fJ and a contractility of 2.17 uN.",
        related=["sci.strong_lens"],
        expert="Fourier-transform traction cytometry (FTTC) as a Gaussian inverse problem. The "
        "Boussinesq Green's function of an elastic half-space is diagonal in longitudinal and "
        "transverse Fourier modes. The prior spectrum is white (Tikhonov), squared exponential or "
        "Matern. NUTS samples the noise sd and the spectrum hyperparameters with the field "
        "integrated out (closed-form marginal likelihood per mode). At score time, exact joint "
        "draws of the field come from two FFTs per hyperparameter draw. An optional lognormal "
        "uncertainty of Young's modulus scales each draw. A diagnostic compares the data power in "
        "the finest modes with the model (correlated PIV noise makes the ranges too narrow).",
        refs=[
            (
                "Butler et al. (2002), Traction fields, moments, and strain energy that cells "
                "exert on their surroundings",
                "https://doi.org/10.1152/ajpcell.00270.2001",
            ),
            (
                "Huang et al. (2019), Traction force microscopy with optimized regularization and "
                "automated Bayesian parameter selection",
                "https://doi.org/10.1038/s41598-018-36896-x",
            ),
            (
                "Bauer et al. (2021), pyTFM: a tool for traction force and monolayer stress "
                "microscopy",
                "https://doi.org/10.1371/journal.pcbi.1008364",
            ),
        ],
        fits={
            "goal": ["map_fields", "fit_physics"],
            "data_shape": ["grid"],
            "frequency": ["not_time"],
            "uncertainty": ["essential", "nice"],
            "experience": ["expert"],
        },
    ),
    "sci.strong_lens": _a(
        "Gravitational lens from an image",
        "Measures the mass of a galaxy that bends the light of a more distant galaxy into a ring "
        "or arcs. It predicts each pixel of the telescope image and shows the distant galaxy "
        "without the lens.",
        [
            "You have a telescope image of one lens galaxy with a ring or arcs around it",
            "You need the Einstein radius and the mass inside it, with a range",
            "You want to see the source galaxy without the lens, and the critical curves of the "
            "lens",
        ],
        [
            "The lens is a group or a cluster of galaxies, or the image has many lenses",
            "You do not have a point-spread function (PSF) or a noise value for each pixel",
            "You need the slope of the mass profile (one image cannot give it)",
        ],
        [
            "A cutout of the image with the lens galaxy at the centre",
            "The inverse variance (weight) of each pixel",
            "The PSF as a small image with an odd number of rows and columns, for example from "
            "stars in the same image",
            "Optional: a mask of the pixels to use, and the redshifts of the lens and the source",
            "A plain dataset, or a tree dataset (for example HDF5 or NetCDF groups) with the image "
            "in one node and the PSF in a /psf node",
        ],
        [
            "The mass of the lens is an isothermal ellipsoid, with an optional external shear",
            "The light of the lens galaxy and of the source are smooth elliptical profiles",
            "The noise is Gaussian. Neighbouring pixels can have a correlated noise",
        ],
        [
            "The Einstein radius, and the mass inside it when you give the two redshifts",
            "The predicted image, and simulated images with noise",
            "An image of the source galaxy without the lens",
            "The critical curves and the caustics for posterior draws",
        ],
        _X,
        _MIN,
        "An HST image of the lens SDSS J1627-0053 gives an Einstein radius of 1.22 arcsec and a "
        "mass of 2.3e11 solar masses inside this radius.",
        related=["sci.traction_force"],
        expert="Pixel forward model in PyTensor: a singular isothermal ellipsoid (Kormann et al. "
        "1994) plus external shear, ray tracing on a sub-pixel grid, Sersic lens light and source, "
        "and PSF convolution as a separable SVD expansion (matrix products only). A separable "
        "lag-one correlation of the noise (drizzled images) is whitened exactly. The linear light "
        "amplitudes are integrated out in closed form and drawn after the fit from their "
        "conditional Gaussian. A staged optimisation finds the mode (lens light with sigma "
        "clipping, the Einstein radius from the ring profile, then starts around the lens axis). "
        "NUTS samples a parameterisation whitened by the Laplace approximation at that mode. "
        "Critical curves and caustics are analytic for each direction. The mass-sheet degeneracy "
        "is not broken: only the mass inside the Einstein radius is robust. No pointwise LOO.",
        refs=[
            (
                "Kormann, Schneider & Bartelmann (1994), Isothermal elliptical gravitational lens "
                "models",
                None,
            ),
            (
                "Bolton et al. (2008), The Sloan Lens ACS Survey V: the full ACS strong-lens "
                "sample",
                "https://arxiv.org/abs/0805.1931",
            ),
            (
                "Suyu et al. (2006), A Bayesian analysis of regularized source inversions in "
                "gravitational lensing",
                "https://arxiv.org/abs/astro-ph/0601493",
            ),
        ],
        fits={
            "goal": ["map_fields", "fit_physics"],
            "data_shape": ["grid"],
            "frequency": ["not_time"],
            "uncertainty": ["essential"],
            "experience": ["expert"],
        },
    ),
    "sci.areal_bym2": _a(
        "Disease risk on a map of areas",
        "Estimates the risk of a disease in each area of a map (a district or a county) from "
        "small counts, and uses the neighbouring areas to make the estimates of small areas "
        "more stable. It also gives the probability that the risk of each area is above the "
        "national level.",
        [
            "You have counts of cases and expected counts for each area of a map",
            "Some areas are small, so their raw rates are noisy",
            "You want a map of the risk and of the areas where it is surely high",
            "You want to know if a cause (a covariate) explains the map, and how much the "
            "answer changes when the areas share their risk with their neighbours",
        ],
        [
            "You have the location of each case, not counts for each area (use a point model)",
            "The values are not counts, for example a mean income for each area",
            "You must predict areas that are not on the map (the graph has only the fitted "
            "areas)",
            "The data change over time and you want a forecast",
        ],
        [
            "A count of cases and an expected count (age-standardised) for each area",
            "One covariate for each area, for example a percentage",
            "Which areas are neighbours (an adjacency matrix), for example from shared borders",
            "The outline of each area (polygon points), for the maps and to link islands",
        ],
        [
            "The count of an area is Poisson around its expected count times its relative risk",
            "Neighbouring areas have similar risks (kind icar, car or bym2), or the areas are "
            "independent (kind iid)",
            "An island is linked to the area with the closest border, unless you change the "
            "islands setting",
            "The covariate has the same effect in every area",
        ],
        [
            "The smoothed relative risk of each area, with a range",
            "The probability that the relative risk of each area is above 1",
            "The expected cases of each area, and new counts and maps (simulate)",
            "The effect of the covariate, the size of the area effects and the share of them "
            "that follows the map (rho, kind bym2)",
        ],
        _X,
        _SEC,
        "Map lip cancer in the 56 districts of Scotland from 1975 to 1980. Get the smoothed "
        "relative risk of each district, find the 19 districts where the risk is surely "
        "above the national level, and see the effect of outdoor work fall from 1.96 to 1.5 "
        "per 10 points when neighbours share their risk.",
        related=["sci.lgcp_grid", "tabular.hierarchical_glm"],
        expert="Poisson(E_i exp(b0 + b1 x_i + u_i)) with x = (covariate - mean) / "
        "covariate_scale (pymc-challenges E18). kind: poisson (u = 0), iid (u = sigma "
        "theta), icar (sigma pm.ICAR(W), soft sum-to-zero), car (pm.CAR with alpha ~ "
        "U(0, 1), tau = 1 / sigma^2; alpha piles up near 1 and b0 mixes slowly) or bym2 "
        "(Riebler et al. 2016: u = sigma (sqrt(rho / s) phi + sqrt(1 - rho) theta), s = "
        "geometric mean of diag(pinv(D - W)), rho ~ Beta(1, 1)). b0, b1 ~ N(0, 1), sigma "
        "~ HalfNormal(1). islands: link_nearest joins each area without neighbours (then "
        "each separate piece) to the area with the closest polygon vertex; isolate keeps "
        "them (iid and icar only: an isolated ICAR node has a flat prior); error refuses. "
        "The pointwise log-likelihood is always kept, so elpd_loo compares the kinds (Pareto "
        "k is often high: one parameter per area). A mask_block split over area holds "
        "districts out of the likelihood but keeps them in the graph (leave-areas-out). "
        "simulate draws new area effects per draw with a hand-written ICAR sampler "
        "(eigenvectors of the Laplacian), as pm.ICAR cannot be forward-sampled. On the "
        "Scottish data the AFF effect drops from 0.67 (iid) to about 0.4 (spatial kinds): "
        "spatial confounding.",
        refs=[
            (
                "Riebler et al. (2016), An intuitive Bayesian spatial model for disease "
                "mapping that accounts for scaling",
                "https://arxiv.org/abs/1601.01180",
            ),
            (
                "Besag, York and Mollié (1991), Bayesian image restoration, with two "
                "applications in spatial statistics",
                None,  # lead: add the DOI (not given in the E18 source)
            ),
            (
                "Clayton and Kaldor (1987), Empirical Bayes estimates of age-standardized "
                "relative risks for use in disease mapping",
                None,  # lead: add the DOI (not given in the E18 source)
            ),
        ],
        fits={
            "goal": ["map_fields", "explain_drivers"],
            "data_shape": ["grid", "table"],
            "frequency": ["not_time"],
            "uncertainty": ["essential", "nice"],
            "experience": ["intermediate", "expert"],
            "speed": ["seconds", "minutes"],
        },
    ),
}
"""Cards of every model the v0.5 catalog offers (built-in plugins, mmm-framework,
statsmodels)."""

SURVIVING_MODEL_IDS: Final[frozenset[str]] = frozenset(
    {"tabular.regressor", "tabular.hierarchical_glm", "choice.multinomial_logit",
     "ts.gaussian_hmm", "ts.hsgp_trend", "sklearn.tabular_regressor",
     "sklearn.tabular_quantiles", "sklearn.tabular_bootstrap", "sklearn.tabular_conformal",
     "sklearn.tabular_classifier"}
)  # fmt: skip
"""Built-in models shipped before v0.5 that remain (``mapi-plugin-tabular``, ``-ts`` and
the scikit-learn models of ``mapi-sklearn``)."""

SCIENCE_MODEL_IDS: Final[frozenset[str]] = frozenset(
    {"sci.lgcp_grid", "sci.fisher_kpp", "sci.traction_force", "sci.strong_lens", "sci.areal_bym2"}
)
"""The scientific models of ``mapi-plugin-science`` (fields, simulators, image inverse
problems ported from the pymc-challenges case studies)."""

PLANNED_MODEL_IDS: Final[frozenset[str]] = (
    frozenset(MODEL_ABOUTS) - SURVIVING_MODEL_IDS - SCIENCE_MODEL_IDS
)
"""Model ids the v0.5 mmm-framework and statsmodels plugins implement (their cards are
here so plugins, docs and the chooser use the same words)."""

# ================================================================================= templates

TEMPLATE_ABOUTS: Final[dict[str, About]] = {
    "geo_mmm": _a(
        "Geo marketing mix",
        "Find how much each marketing channel adds to your results in each market. See what "
        "occurs if you move budget between channels.",
        [
            "You have weekly sales and spend for each channel, by market",
            "You want the return of each channel and a budget plan",
        ],
        ["You have less than a year of data", "Spend never changes"],
        ["A table with date, market, the result (sales) and one spend column for each channel"],
        [
            "The effect of advertising continues for some time and has diminishing returns",
            "If other drivers (price, promotions) are important, the table includes them",
        ],
        [
            "Return and contribution for each channel",
            "What-if sliders",
            "A budget plan",
            "A scorecard",
        ],
        _B,
        _LONG,
        "Upload 2 years of weekly sales and TV, search and social spend for 10 markets.",
        related=["mmm.bayesian", "core.resample"],
        fits={
            "goal": ["explain_marketing", "plan_budget"],
            "data_shape": ["panel"],
            "frequency": ["daily", "weekly"],
            "history": ["medium", "long"],
            "drivers": ["yes"],
            "uncertainty": ["essential", "nice"],
        },
    ),
    "weekly_forecast": _a(
        "Weekly forecast",
        "Forecast one series, such as sales, visits or demand, some weeks or months ahead. Get "
        "a range of probable values.",
        ["You need a forecast of one number"],
        ["You need a forecast for each store (use Demand by store)"],
        ["A table with a date and the number to forecast"],
        ["The trend changes gradually and the season repeats"],
        ["A forecast chart with a range", "A scorecard"],
        _B,
        _MIN,
        "Forecast weekly visits 26 weeks ahead from 3 years of history.",
        related=["ts.hsgp_trend"],
        fits={
            "goal": ["forecast"],
            "data_shape": ["one_series"],
            "frequency": ["daily", "weekly"],
            "history": ["medium", "long"],
            "uncertainty": ["essential", "nice"],
        },
    ),
    "store_demand": _a(
        "Demand by store",
        "Predict how much each store will sell each week from prices, promotions and the "
        "calendar. Get a low and a high estimate.",
        ["You have sales for each store and week, and you know the planned prices or promotions"],
        ["You only have one total series"],
        ["A table with store, week, units sold and optional drivers"],
        ["You know the planned drivers for the weeks that you forecast"],
        ["A forecast for each store with low and high values", "A scorecard"],
        _B,
        _SEC,
        "Predict the units of next month for 300 stores.",
        related=["tabular.regressor"],
        fits={
            "goal": ["predict_values", "forecast"],
            "data_shape": ["panel"],
            "drivers": ["yes"],
            "frequency": ["daily", "weekly"],
        },
    ),
    "customer_choice": _a(
        "Customer choice",
        "Understand which product customers select on each shopping trip, and how price and "
        "promotions change the choice.",
        ["You have purchase records with the prices on offer at each trip"],
        ["You only have total sales"],
        ["A table with customer, trip number, the product chosen and the price of each product"],
        ["Customers think about price and promotions when they select a product"],
        ["Price sensitivity", "Choice shares", "What-if shares"],
        _I,
        _MIN,
        "See how a 10% price decrease on one brand changes its share.",
        related=["choice.multinomial_logit"],
        fits={"goal": ["understand_choices"], "data_shape": ["choices"]},
    ),
}
"""Cards of the built-in guided templates (``mapi_spec.guided.GUIDED_TEMPLATES``)."""


@functools.cache
def _registry() -> dict[str, tuple[AboutKind, About]]:
    # imported here: the node, check and workflow-template registries import this module
    from mapi_spec.governance import GOVERNANCE_CHECKS
    from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS, NODE_KIND_ABOUTS
    from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES

    out: dict[str, tuple[AboutKind, About]] = {}
    for kind, items in (
        (AboutKind.STEP, STEP_ABOUTS),
        (AboutKind.SPLIT, SPLIT_ABOUTS),
        (AboutKind.MODEL, MODEL_ABOUTS),
        (AboutKind.TEMPLATE, TEMPLATE_ABOUTS),
    ):
        for k, v in items.items():
            out[k] = (kind, v)
    for nk, about in NODE_KIND_ABOUTS.items():
        out[f"kind.{nk.value}"] = (AboutKind.NODE_KIND, about)
    for impl in NODE_IMPLEMENTATIONS.values():
        out[impl.id] = (AboutKind.NODE, impl.about)
    for check in GOVERNANCE_CHECKS.values():
        out[check.id] = (AboutKind.CHECK, check.about)
    for tpl in WORKFLOW_TEMPLATES.values():
        out[f"workflow.{tpl.id}"] = (AboutKind.WORKFLOW_TEMPLATE, tpl.about)
    return out


def builtin_abouts() -> dict[str, About]:
    """Every built-in About by id: steps and splits (``core.*``), models, guided templates
    (by template id), workflow node kinds (``kind.<kind>``), node implementations
    (``test.adf``), governance checks (``null.permutation_target``) and workflow templates
    (``workflow.<id>``)."""
    return {k: about for k, (_, about) in _registry().items()}


def builtin_about(item_id: str) -> About | None:
    """The built-in About of ``item_id`` (``None`` when the id has none)."""
    entry = _registry().get(item_id)
    return None if entry is None else entry[1]


def builtin_about_kind(item_id: str) -> AboutKind | None:
    """What kind of item ``item_id`` is (``None`` when it is not built in)."""
    entry = _registry().get(item_id)
    return None if entry is None else entry[0]
