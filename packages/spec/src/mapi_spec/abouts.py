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
        "Adds up daily (or hourly) numbers into one number per week or month, so every row "
        "covers the same length of time.",
        [
            "Your data is daily but you plan and report by week or month",
            "A model expects weekly data",
        ],
        [
            "Your data already has one row per week or month",
            "The numbers cannot be added up and averaging would hide what matters",
        ],
        ["A date column with a regular rhythm"],
        [
            "Spend and sales can be summed; prices and rates are averaged (choose per column)",
            "Incomplete first and last periods are dropped when full periods are required",
        ],
        ["The same columns with one row per week or month"],
        _B,
        _NOW,
        "Daily TV spend of 100 every day becomes 700 for the week ending Sunday.",
        related=["core.count_events", "core.calendar"],
        expert="Group-by on the time coordinate at the target frequency (pandas offset alias); "
        "`how` per variable (sum, mean, last); `full_periods_only` drops partial bins.",
        fits=_PREP,
    ),
    "core.count_events": _a(
        "Count events per period",
        "Turns a list of individual events, such as orders or sign-ups, into counts per day, "
        "week or month.",
        ["Your file has one row per event and you need a time series"],
        ["Your data is already a series of totals per period"],
        ["A column with the time of each event"],
        ["Each row is one event; events outside the chosen dates are ignored"],
        ["A count per period (and per group if you choose one), plus optional sums"],
        _B,
        _NOW,
        "Three orders on Monday and one on Tuesday become 3 and 1.",
        related=["core.resample"],
        fits=_PREP,
    ),
    "core.filter_coords": _a(
        "Keep some markets, products or dates",
        "Keeps only the markets, products or date range you pick, and drops the rest before "
        "modelling.",
        [
            "Some markets or products should not be modelled",
            "Early history is unreliable and should be left out",
        ],
        [
            "You want the model to learn from everything",
            "You want to test on unseen markets (use a split instead)",
        ],
        ["A dimension such as market, product or date"],
        ["The dropped data is not needed by the model or later steps"],
        ["The same data with fewer markets, products or dates"],
        _B,
        _NOW,
        "Keep only the markets DE and FR, or only weeks from 2023 onwards.",
        related=["core.leave_group_out"],
        fits=_PREP,
    ),
    "core.to_long": _a(
        "One row per observation",
        "Rearranges a table where each group's rows sit side by side into one row per "
        "observation, dropping the empty padding.",
        [
            "A model works on individual observations, such as shopping trips",
            "Groups have very different numbers of rows",
        ],
        ["Every group has the same number of rows and the model expects a grid"],
        ["A group dimension (customer) and a position dimension (trip number)"],
        ["Padding cells are marked as missing or by a mask"],
        ["The same values with one row per observation and a group index"],
        _I,
        _NOW,
        "Household 1 with 3 trips and household 2 with 1 trip becomes 4 rows.",
        related=["core.to_padded"],
        fits=_PREP,
    ),
    "core.to_padded": _a(
        "Line groups up side by side",
        "Lines up each group's rows side by side in a grid, filling short groups with gaps "
        "that are marked as missing.",
        ["A model expects a grid of groups by position, such as households by trip"],
        ["Your data is already a grid"],
        ["A group column and an order within each group"],
        ["Gaps are marked so the model can skip them"],
        ["A grid of groups by position plus a mask of the real cells"],
        _I,
        _NOW,
        "Households with 3 and 1 trips become a 2 by 3 grid with two empty cells.",
        related=["core.to_long"],
        fits=_PREP,
    ),
    "core.stack": _a(
        "Combine dimensions",
        "Combines several dimensions, such as market and product, into one list of cells.",
        ["A model works on a flat list of cells instead of a grid"],
        ["The model can use the grid directly"],
        ["Two or more dimensions to combine"],
        ["Empty combinations can be dropped and are remembered so they can be restored"],
        ["One combined dimension"],
        _I,
        _NOW,
        "3 markets by 4 products become 12 market-product cells.",
        related=["core.unstack"],
        fits=_PREP,
    ),
    "core.unstack": _a(
        "Split a combined dimension",
        "Splits a combined dimension back into its parts, for example market-product cells "
        "back into markets and products.",
        ["You stacked dimensions earlier and need the grid back"],
        ["Nothing was stacked"],
        ["A dimension created by stacking"],
        ["The parts of every combined label are known"],
        ["The original dimensions"],
        _I,
        _NOW,
        "12 market-product cells become 3 markets by 4 products.",
        related=["core.stack"],
        fits=_PREP,
    ),
    "core.formula": _a(
        "New column from a formula",
        "Creates a new column from a simple formula over existing columns, such as price "
        "times units.",
        [
            "You need a derived number like revenue = price x units",
            "A ratio or difference of two columns explains the result better",
        ],
        ["A built-in step already does it (scaling, logs, lags)"],
        ["The columns the formula uses"],
        ["Only the allowed operations are used (arithmetic and a few functions); no code runs"],
        ["One new column"],
        _I,
        _NOW,
        "revenue = price * units adds a revenue column.",
        expert="Lark grammar compiled to an allow-listed AST of xarray operations, evaluated "
        "in the sandbox; a usability feature, not a security boundary.",
        fits=_PREP,
    ),
    "core.adstock": _a(
        "Carry-over of advertising",
        "Spreads each period's advertising effect over the following periods, because people "
        "remember an ad for a while after they see it.",
        [
            "You model media with a simple (non-Bayesian) model and need carry-over",
            "You want to see how a fixed carry-over rate changes spend series",
        ],
        ["The model already handles carry-over itself (the Bayesian MMM does)"],
        ["Media spend or exposure over time"],
        ["The carry-over rate is fixed by you, not learned from the data"],
        ["Media columns that include the lingering effect of past periods"],
        _I,
        _NOW,
        "With a 50% weekly carry-over, 100 spent this week counts as 100, then 50, then 25.",
        related=["core.saturation", "mmm.bayesian"],
        expert="Geometric (alpha), delayed (theta) or Weibull kernels up to l_max lags, "
        "optionally normalised to sum to 1; applied per group along time.",
        fits={"goal": ["explain_marketing", "check_data"]},
    ),
    "core.saturation": _a(
        "Diminishing returns",
        "Bends spend so that each extra unit of money adds a little less than the one before, "
        "as happens when a channel gets saturated.",
        ["You model media with a simple model and need diminishing returns"],
        ["The model learns saturation itself (the Bayesian MMM does)"],
        ["Media spend or exposure"],
        ["The shape and its settings are fixed by you, not learned from the data"],
        ["Media columns on a curved scale"],
        _I,
        _NOW,
        "With half-saturation at 1000, spending 1000 gives half the possible effect.",
        related=["core.adstock", "mmm.bayesian"],
        expert="Logistic (lam), Hill (slope, half) or Michaelis-Menten curves applied "
        "element-wise.",
        fits={"goal": ["explain_marketing", "check_data"]},
    ),
    "core.fx_convert": _a(
        "Convert currencies",
        "Converts money columns from local currencies into one currency, using exchange rates "
        "for each day or week.",
        ["Markets report in different currencies"],
        ["Everything is already in one currency"],
        ["Money columns and a table of exchange rates over time"],
        ["The exchange-rate table covers every date in the data"],
        ["Money columns in the chosen currency"],
        _B,
        _NOW,
        "1000 GBP on a day when 1 GBP = 1.17 EUR becomes 1170 EUR.",
        related=["core.resample"],
        fits=_PREP,
    ),
    "core.mean_scale": _a(
        "Put series on a common scale",
        "Divides each series by its own average, so that big and small markets can be "
        "compared on the same footing.",
        [
            "Markets or channels differ a lot in size",
            "A model works better when every series has an average of 1",
        ],
        ["The model scales the data itself"],
        ["Numeric columns"],
        ["The average is learned on the training data only and reused for new data"],
        ["The same columns with an average of 1 (results are converted back automatically)"],
        _B,
        _NOW,
        "A market selling 5000 a week on average becomes 1.0; a week with 6000 becomes 1.2.",
        related=["core.standardize", "core.max_abs_scale"],
        fits=_PREP,
    ),
    "core.max_abs_scale": _a(
        "Scale to between -1 and 1",
        "Divides each series by its largest absolute value, so every value falls between -1 and 1.",
        ["A model needs values in a fixed range"],
        ["Your data has extreme outliers (they set the scale)"],
        ["Numeric columns"],
        ["The largest value is learned on the training data only"],
        ["The same columns between -1 and 1"],
        _B,
        _NOW,
        "Values 50, 100 and -20 become 0.5, 1.0 and -0.2.",
        related=["core.mean_scale", "core.clip"],
        fits=_PREP,
    ),
    "core.standardize": _a(
        "Centre and scale",
        "Shifts each series so its average is zero and stretches it so its typical spread is one.",
        ["You compare the size of effects across inputs measured in different units"],
        ["Zero must keep its meaning (spend of zero should stay zero)"],
        ["Numeric columns"],
        ["Average and spread are learned on the training data only"],
        ["The same columns with average 0 and spread 1"],
        _B,
        _NOW,
        "Temperatures averaging 15 with spread 5 make 20 degrees become 1.0.",
        related=["core.mean_scale"],
        fits=_PREP,
    ),
    "core.log1p": _a(
        "Compress large values",
        "Takes the logarithm of one plus each value, which shrinks very large numbers and "
        "turns growth rates into steady steps.",
        ["Values span several orders of magnitude", "Effects work in percentages"],
        ["Values can be negative"],
        ["Numeric columns of zero or more"],
        ["Results are converted back; averages on the original scale are approximate"],
        ["The same columns on a log scale"],
        _B,
        _NOW,
        "Sales of 9, 99 and 999 become about 2.3, 4.6 and 6.9.",
        related=["core.logit"],
        fits=_PREP,
    ),
    "core.logit": _a(
        "Unbounded scale for shares",
        "Turns shares between 0 and 1, such as conversion rates, into numbers without limits "
        "so a model cannot predict impossible values.",
        ["You model a rate or a share"],
        ["Values are not between 0 and 1"],
        ["Columns with shares strictly between 0 and 1"],
        ["Exact 0 and 1 are nudged slightly inwards"],
        ["The same columns on an unbounded scale"],
        _I,
        _NOW,
        "A 50% share becomes 0; 90% becomes about 2.2.",
        related=["core.log1p"],
        fits=_PREP,
    ),
    "core.impute": _a(
        "Fill gaps",
        "Fills missing values with a sensible stand-in, such as the average, zero or the "
        "previous value.",
        ["A few values are missing and the model cannot handle gaps"],
        [
            "Many values are missing (fix the source instead)",
            "Missing means something (add a missing-value flag)",
        ],
        ["The columns with gaps"],
        ["Fill values are learned on the training data only"],
        ["The same columns without gaps"],
        _B,
        _NOW,
        "A missing week of spend in a market is filled with that market's average.",
        related=["core.missing_indicator", "core.replace_sentinel"],
        fits=_PREP,
    ),
    "core.replace_sentinel": _a(
        "Placeholder numbers to missing",
        "Marks placeholder numbers such as -99 or 9999, which some systems use for 'unknown', "
        "as missing.",
        ["Your source uses codes like -99 for missing values"],
        ["Every number is a real measurement"],
        ["Columns with placeholder codes"],
        ["The codes never occur as real values"],
        ["The same columns with the codes turned into gaps"],
        _B,
        _NOW,
        "A temperature of -99 becomes a missing value.",
        related=["core.impute"],
        fits=_PREP,
    ),
    "core.clip": _a(
        "Cap extreme values",
        "Caps values that are smaller or larger than limits you choose, which tames outliers.",
        ["A few extreme values would dominate the model", "Values outside a range are errors"],
        ["Extreme values are real and important"],
        ["Numeric columns and a lower and/or upper limit"],
        ["Values beyond the limits carry no extra information"],
        ["The same columns within the limits"],
        _B,
        _NOW,
        "With an upper limit of 100, a value of 250 becomes 100.",
        related=["core.max_abs_scale"],
        fits=_PREP,
    ),
    "core.fourier": _a(
        "Repeating seasonal patterns",
        "Adds smooth wave-shaped columns that let a model learn a repeating pattern, such as "
        "every year or every week.",
        [
            "Your result rises and falls with the season",
            "You want seasonality without one column per month",
        ],
        ["You have less than one full season of history"],
        ["A regular date column"],
        ["The pattern repeats with the same length each cycle"],
        ["Pairs of wave columns (more pairs give a more detailed shape)"],
        _I,
        _NOW,
        "Three yearly wave pairs on weekly data capture a summer peak and a winter dip.",
        related=["core.calendar", "core.holidays"],
        expert="sin/cos(2 pi k t / period), k = 1..order; the origin and period are stored "
        "state so forecasts continue the same phase.",
        fits=_PREP,
    ),
    "core.trend": _a(
        "Steady drift over time",
        "Adds a column that counts up over time, so a model can capture a steady rise or fall.",
        ["Your result grows or shrinks steadily"],
        ["The trend changes direction often (use a flexible trend model)"],
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
        "Adds calendar facts such as the month, the week of the year or whether a day falls "
        "on a weekend.",
        ["Behaviour depends on the day of week or month"],
        ["Your data is not over time"],
        ["A date column"],
        ["The same calendar effect applies every year"],
        ["One column per calendar feature"],
        _B,
        _NOW,
        "2024-06-01 gets month 6 and weekend = yes.",
        related=["core.holidays", "core.fourier"],
        fits=_PREP,
    ),
    "core.holidays": _a(
        "Holiday flags",
        "Marks the periods that contain public holidays, from a holiday table you provide.",
        ["Holidays move your sales"],
        ["Holidays make no difference to your result"],
        ["A date column and a holiday table (per country or market)"],
        ["The holiday table covers the whole period, including any forecast"],
        ["A holiday indicator or count per period"],
        _B,
        _NOW,
        "The week containing 25 December is flagged in every market that celebrates it.",
        related=["core.calendar"],
        fits=_PREP,
    ),
    "core.lag": _a(
        "Earlier values as columns",
        "Adds the values from one or more earlier periods as extra columns, such as last "
        "week's sales.",
        ["Last period's value helps predict this period"],
        ["You forecast many periods ahead and the lagged value would not be known"],
        ["A time dimension and the columns to lag"],
        ["Earlier rows exist; the first rows get a fill value"],
        ["One new column per lag"],
        _I,
        _NOW,
        "A one-week lag of sales puts last week's 120 next to this week's 130.",
        related=["core.lag_tensor"],
        fits=_PREP,
    ),
    "core.lag_tensor": _a(
        "Recent history per series",
        "Keeps the last few periods of each series side by side, which models use to compute "
        "carry-over effects themselves.",
        ["A model computes its own carry-over from a window of past spend"],
        ["The model takes plain series and handles history internally"],
        ["A time dimension and the columns to keep history for"],
        ["The window is long enough for the effect to fade"],
        ["A new dimension with the last few periods of every series"],
        _X,
        _NOW,
        "With a 6-week window, every week carries the spend of the 6 weeks before it.",
        related=["core.lag", "core.adstock"],
        fits=_PREP,
    ),
    "core.one_hot": _a(
        "Categories to yes/no columns",
        "Turns a category such as region into one yes/no column per value, so models that "
        "need numbers can use it.",
        ["A model needs numbers but you have categories"],
        ["A category has hundreds of values (number them instead)"],
        ["Category columns"],
        ["The categories seen in training are the ones that matter; new ones follow a rule"],
        ["One 0/1 column per category value"],
        _B,
        _NOW,
        "Region north/south becomes columns north = 1/0 and south = 1/0.",
        related=["core.encode_levels"],
        fits=_PREP,
    ),
    "core.encode_levels": _a(
        "Number each category",
        "Gives every category value a number, which many models use to look up a separate "
        "effect for each group.",
        ["A model needs group numbers (one effect per store or market)"],
        ["The model reads category labels itself"],
        ["Category columns or dimensions"],
        ["Numbers are assigned on the training data; new values follow a rule"],
        ["An integer code per category value"],
        _I,
        _NOW,
        "Stores A, B, C become 0, 1, 2.",
        related=["core.one_hot"],
        fits=_PREP,
    ),
    "core.missing_indicator": _a(
        "Flag missing values",
        "Adds a yes/no column that marks where a value was missing, so a model can tell real "
        "values from filled-in ones.",
        ["Whether a value is missing tells you something"],
        ["There are no gaps"],
        ["Columns that may have gaps"],
        ["Run it before filling gaps"],
        ["One 0/1 column per input column"],
        _B,
        _NOW,
        "A missing price gets a flag of 1 next to the filled-in price.",
        related=["core.impute"],
        fits=_PREP,
    ),
}
"""Cards of the built-in preprocessing steps (``mapi-steps``)."""

# ==================================================================================== splits

SPLIT_ABOUTS: Final[dict[str, About]] = {
    "core.time_holdout": _a(
        "Test on the latest periods",
        "Holds back the most recent periods, fits the model on the rest, and checks how well "
        "it predicts what it has not seen.",
        ["You forecast or want an honest error for recent periods"],
        ["Your data is not ordered in time"],
        ["An ordered dimension such as date"],
        ["The recent past looks like the near future"],
        ["A training part and a test part; holdout error metrics"],
        _B,
        _NOW,
        "With 104 weeks, the model learns on the first 96 and is tested on the last 8.",
        related=["core.as_of", "split.rolling_origin"],
        fits={"goal": ["forecast", "validate_model"]},
    ),
    "core.as_of": _a(
        "Pretend it is a past date",
        "Fits the model as if today were a past date and checks what it would have predicted "
        "for the periods after it.",
        ["You want to replay a past decision point", "Data arrives with a delay (use a gap)"],
        ["Your data is not ordered in time"],
        ["An ordered dimension and a cut-off date"],
        ["The cut-off leaves enough history to fit"],
        ["A training part up to the date and a test part after it"],
        _B,
        _NOW,
        "As of 2024-06-30 with a 2-week gap, test on July from week 3 onwards.",
        related=["core.time_holdout", "split.expanding_window"],
        fits={"goal": ["forecast", "validate_model"]},
    ),
    "core.leave_group_out": _a(
        "Test on unseen markets",
        "Leaves whole markets or stores out of fitting and checks how well the model does on them.",
        ["You will use the model for new markets or stores"],
        ["You only predict for markets the model has seen"],
        ["A group dimension with several levels"],
        ["Left-out groups resemble the others"],
        ["A training part and the held-out groups"],
        _I,
        _NOW,
        "Fit on 8 markets, test on the other 2.",
        related=["split.geo_holdout"],
        fits={"goal": ["predict_values", "validate_model"], "data_shape": ["panel"]},
    ),
    "core.last_per_group": _a(
        "Test each group on its latest rows",
        "Holds back the last few observations of every customer or store and tests the model "
        "on them.",
        ["Groups have different lengths of history, such as shopping trips per household"],
        ["Your data is one series"],
        ["A group dimension and an order within each group"],
        ["Each group has enough earlier rows to learn from"],
        ["Training rows and the held-out last rows per group"],
        _I,
        _NOW,
        "Every household's last trip is used for testing, if it has at least 3 trips.",
        related=["core.time_holdout"],
        fits={"goal": ["understand_choices", "validate_model"], "data_shape": ["choices"]},
    ),
    "core.mask_block": _a(
        "Hide random patches",
        "Hides random blocks of the data from fitting and checks how well the model fills them in.",
        ["You want to test filling in gaps rather than forecasting"],
        ["You need a forecast error (hold out the latest periods instead)"],
        ["One or more dimensions to cut blocks from"],
        ["The model accepts missing target values"],
        ["The data with hidden cells and their positions"],
        _X,
        _NOW,
        "Hide 10% of market-week cells in blocks of 4 weeks.",
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
        "calendar, with a low and a high estimate.",
        [
            "You have many stores or products and want a forecast for each",
            "Prices, promotions or stock explain demand",
        ],
        [
            "You need to add predictions up into totals with a range (ranges cannot be added)",
            "You have a single short series",
        ],
        ["A table of the number to predict and its drivers, per store and week"],
        [
            "Future values of the drivers are known when you forecast",
            "Stores the model has not seen behave like the average store",
        ],
        ["A median forecast with low and high values per cell", "Feature importance"],
        _B,
        _SEC,
        "Predict next week's units for 300 stores from planned prices and promotions.",
        related=["core.calendar", "core.one_hot"],
        expert="Gradient-boosted quantile regression (xgboost or sklearn) on stacked rows; "
        "coordinates become categorical features; direct multi-horizon forecasting; "
        "quantile representation (no cross-cell aggregation).",
        fits={
            "goal": ["predict_values", "forecast"],
            "data_shape": ["panel", "table"],
            "drivers": ["yes"],
            "frequency": ["daily", "weekly", "monthly", "not_time"],
        },
    ),
    "tabular.hierarchical_glm": _a(
        "Group effects with sharing",
        "Estimates a separate baseline for every group, such as a county or a store, while "
        "letting small groups borrow strength from the others.",
        [
            "Groups have very different amounts of data",
            "You want to predict for groups you have not seen yet",
        ],
        ["You have a single series over time"],
        ["One row per observation with a group label and predictors"],
        [
            "Groups are alike enough that sharing information is sensible",
            "Effects are additive on the model's scale",
        ],
        ["Effects per group with ranges", "Predictions for new groups"],
        _I,
        _MIN,
        "Radon levels per house, with each county's baseline pulled towards the state "
        "average when the county has few houses.",
        related=["core.encode_levels"],
        expert="Bayesian hierarchical GLM (PyMC, NUTS): partially pooled group intercepts and "
        "optional slopes; group-level covariates in the mean; new levels drawn from the "
        "fitted hyperprior.",
        fits={
            "goal": ["explain_drivers", "predict_values"],
            "data_shape": ["table", "panel"],
            "uncertainty": ["essential", "nice"],
            "frequency": ["not_time"],
        },
    ),
    "choice.multinomial_logit": _a(
        "Customer choice",
        "Explains which product a customer picks on each shopping trip and how price and "
        "promotions change that choice.",
        [
            "You have purchase records with the price of every product on offer",
            "You want to test a price change before making it",
        ],
        ["You only have total sales, not individual choices"],
        ["One row per trip: the product chosen and each product's price and promotion"],
        [
            "Customers pick the option they like best given price and promotion",
            "Taste differences between households are stable over time",
        ],
        [
            "How sensitive choices are to price and promotion",
            "Predicted choice shares",
            "What-if shares under new prices",
        ],
        _I,
        _MIN,
        "A 10% cut in Brand A's price moves its share from 30% to 36%.",
        related=["core.last_per_group"],
        expert="Conditional (McFadden) logit over alternative-specific covariates with "
        "household random effects; Bayesian-bootstrap draws over households.",
        refs=[("McFadden (1974), Conditional logit analysis of qualitative choice behavior", None)],
        fits={
            "goal": ["understand_choices"],
            "data_shape": ["choices"],
            "uncertainty": ["essential", "nice"],
        },
    ),
    "ts.gaussian_hmm": _a(
        "Hidden states over time",
        "Finds a small number of hidden states, such as calm and busy periods, that a series "
        "switches between, and says which state each period was probably in.",
        ["Your series seems to switch between distinct modes"],
        ["Behaviour changes smoothly rather than in jumps", "You have very short sequences"],
        ["One or more sequences of measurements over time"],
        [
            "Each state has its own typical level and spread",
            "The chance of switching depends only on the current state",
        ],
        ["The likely state of each period", "How often states switch", "Forecasts"],
        _X,
        _MIN,
        "An animal's track splits into resting and travelling phases.",
        related=["ts.markov_switching"],
        expert="K-state Gaussian HMM; states marginalised with the forward algorithm in PyMC; "
        "terminal state stored for forecast and filter; label switching fixed by ordering.",
        fits={
            "goal": ["find_regimes"],
            "data_shape": ["one_series", "panel"],
            "uncertainty": ["essential", "nice"],
        },
    ),
    "ts.hsgp_trend": _a(
        "Smooth trend forecast",
        "Forecasts one regular series with a smooth, flexible trend and a repeating seasonal "
        "pattern, and gives a range of likely values.",
        [
            "You forecast one weekly or daily series with a trend that bends over time",
            "You want an honest range around the forecast",
        ],
        [
            "You have many related series (forecast them together or per store)",
            "You need to forecast further ahead than the model allows without refitting",
        ],
        ["One number over time at a regular frequency"],
        ["The trend changes gradually", "The seasonal pattern repeats"],
        ["A forecast with a range", "Trend and seasonal parts"],
        _B,
        _MIN,
        "Forecast weekly website visits 26 weeks ahead with a 90% range.",
        related=["core.fourier", "ts.ets", "ts.sarimax"],
        expert="Hilbert-space Gaussian process (HSGP) trend with optional linear drift and "
        "Fourier seasonality (PyMC, NUTS); forecasts limited to max_extension periods.",
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
        "Measures how much each marketing channel adds to your sales, in total or per market, "
        "and shows what would happen if you moved budget between channels.",
        [
            "You have two or more years of weekly sales and spend per channel",
            "You want to know the return of each channel and plan budgets",
            "You have results per market or product and want them modelled together",
        ],
        [
            "Spend barely changes over time (the effect cannot be measured)",
            "You have less than a year of history",
            "You need answers in seconds (use the fast frequentist version)",
        ],
        ["Weekly (or daily) sales, spend per channel, and optional controls such as price"],
        [
            "Advertising has a lingering effect and diminishing returns",
            "Things that drive both spend and sales are included as controls",
        ],
        [
            "Contribution and return per channel with ranges",
            "Response curves",
            "Budget plans and what-if answers",
        ],
        _I,
        _LONG,
        "TV returns 2.1 euros per euro spent (range 1.4 to 2.9) and search 3.4.",
        related=["mmm.frequentist", "mmm.nested", "mmm.multivariate", "core.resample"],
        expert="mmm-framework BayesianMMM: geometric/Weibull adstock, Hill or logistic "
        "saturation, trend options, Fourier seasonality, hierarchical pooling over geos and "
        "products (MFF panels); priors from business beliefs; lift tests as calibration "
        "likelihood; NUTS via PyMC, nutpie or numpyro.",
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
        "Measures marketing effects that work through an intermediate result, such as ads "
        "driving website visits that then drive sales.",
        [
            "You have an in-between measure such as visits, searches or awareness",
            "You want to know how much of a channel's effect goes through it",
        ],
        ["You only have sales and spend (use the standard marketing mix model)"],
        ["Sales, the intermediate measure and spend per channel over time"],
        [
            "The in-between measure fully or partly carries the effect",
            "Advertising has carry-over and diminishing returns",
        ],
        ["Direct and indirect contribution per channel", "Effects on the intermediate measure"],
        _X,
        _LONG,
        "Search ads lift visits by 12%, and visits explain 40% of online sales.",
        related=["mmm.bayesian", "mmm.multivariate"],
        expert="mmm-framework mmm_extensions nested (mediated) model: a mediator equation and "
        "an outcome equation sharing media transforms.",
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
        "Measures marketing effects on several results at once, such as the sales of related "
        "products, including how one product's advertising helps or hurts another.",
        [
            "You sell related products and advertise them separately",
            "You suspect advertising one product moves another's sales",
        ],
        ["You care about a single result"],
        ["Sales per product and spend per channel and product over time"],
        ["Cross-product effects are steady over the period"],
        ["Contribution per channel and product", "Cross-product effects"],
        _X,
        _LONG,
        "Advertising the premium range also lifts the basic range by 5%.",
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
        "Gives a quick first estimate of each channel's contribution in seconds, without the "
        "ranges a full Bayesian model provides.",
        [
            "You need a fast first look before a full model",
            "You want to compare many set-ups quickly",
        ],
        [
            "Decisions depend on how certain the answer is",
            "You want to bring in business beliefs or lift tests",
        ],
        ["Weekly sales and spend per channel"],
        [
            "Carry-over and saturation settings are searched over a grid",
            "Channel effects are not negative",
        ],
        ["Contribution and return per channel (bootstrap ranges optional)"],
        _B,
        _SEC,
        "A first read in 5 seconds: TV 28% of sales, search 12%.",
        related=["mmm.bayesian"],
        expert="mmm-framework frequentist ridge / constrained least squares with a grid "
        "search over transform parameters; optional bootstrap.",
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
        "Forecasts a series from its own recent past, its seasonal pattern and outside drivers "
        "you know ahead, such as planned prices or holidays.",
        [
            "You forecast one regular series a few periods ahead",
            "Known future drivers (prices, promotions, holidays) matter",
        ],
        ["You have less than about 50 periods", "Several series drive each other (use VAR)"],
        [
            "One number over time at a regular frequency, plus optional driver columns known "
            "for the future"
        ],
        [
            "After differencing, the series varies around a stable level",
            "Relationships stay the same over time",
        ],
        ["A forecast with a range", "Effects of the drivers", "Residual checks"],
        _I,
        _SEC,
        "Forecast weekly orders 12 weeks ahead from orders in recent weeks and planned discounts.",
        related=["ts.arima", "search.arma_order", "test.adf", "transform.difference"],
        expert="statsmodels SARIMAX (state space): ARIMA(p,d,q)(P,D,Q)s with exogenous "
        "regressors; exact likelihood via the Kalman filter; prediction intervals as "
        "quantiles; future exog required for forecasts.",
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
        "Forecasts a series from its own recent past and trend, without outside drivers.",
        ["You have only the series itself", "You want a solid, well-known forecasting method"],
        ["Outside drivers explain most of the movement (use the version with drivers)"],
        ["One number over time at a regular frequency"],
        ["After differencing, the series varies around a stable level"],
        ["A forecast with a range", "Residual checks"],
        _I,
        _SEC,
        "Forecast monthly sales 6 months ahead from the last few years of sales.",
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
        "Models several series that influence each other over time, such as prices and sales "
        "of two brands, and forecasts them together.",
        [
            "Several series move together and affect each other",
            "You want to see how a shock to one series spreads to the others",
        ],
        ["You have one series", "Series trend together for good (use the error-correction model)"],
        ["Two or more numbers over time at the same frequency"],
        [
            "Each series varies around a stable level, after differencing if needed",
            "Effects work through past values",
        ],
        [
            "Joint forecasts with ranges",
            "How a shock spreads (impulse responses)",
            "Which series help predict which (Granger tests)",
        ],
        _X,
        _SEC,
        "A price cut by brand A lowers brand B's sales for three weeks.",
        related=["ts.varmax", "ts.vecm", "test.granger", "search.var_lag_order"],
        expert="statsmodels VAR(p): OLS per equation; lag order by AIC/BIC/HQIC; impulse "
        "responses and forecast-error variance decomposition; Granger causality tests.",
        refs=[_TS_REF, ("Lutkepohl, New Introduction to Multiple Time Series Analysis", None)],
        fits={
            "goal": ["compare_series", "forecast"],
            "data_shape": ["several_series"],
            "history": ["medium", "long"],
        },
    ),
    "ts.varmax": _a(
        "Series that drive each other, with drivers",
        "Models several series that influence each other and also respond to outside drivers "
        "you know ahead, and forecasts them together.",
        ["Several series affect each other and outside drivers matter too"],
        ["You have one series", "You have many series and little history"],
        ["Two or more numbers over time, plus driver columns known for the future"],
        ["Series vary around stable levels", "Driver effects are steady"],
        ["Joint forecasts with ranges", "Driver effects", "Impulse responses"],
        _X,
        _MIN,
        "Forecast sales of two brands from each other's history and planned promotions.",
        related=["ts.var", "ts.sarimax"],
        expert="statsmodels VARMAX(p,q) with exogenous regressors (VARX when q = 0); "
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
        "Series tied together for good",
        "Models series that wander but stay tied together in the long run, such as prices of "
        "close substitutes, and shows how fast they snap back after a gap opens.",
        ["Series trend but keep a stable long-run relationship"],
        ["Series do not trend", "The long-run tie is not plausible"],
        ["Two or more trending numbers over time"],
        ["A stable long-run relationship exists (checked by the Johansen test)"],
        ["Long-run relationships", "Speed of adjustment", "Joint forecasts with ranges"],
        _X,
        _SEC,
        "When a competitor's price drifts 5% above yours, half the gap closes within 4 weeks.",
        related=["test.johansen", "search.coint_rank", "ts.var", "ts.ecm"],
        expert="statsmodels VECM with cointegration rank from the Johansen trace/max-eigenvalue "
        "tests; deterministic terms selectable.",
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
        "Separates the short-term effect of a driver from its long-term effect on one series, "
        "and tests whether a long-run relationship exists at all.",
        [
            "One result and a few drivers trend together",
            "You want both the immediate and the lasting effect of a driver",
        ],
        ["Neither the result nor the drivers trend (a regression with lags is enough)"],
        ["One result and one or more drivers over time"],
        ["Drivers affect the result, not the other way round"],
        ["Short-run and long-run effects", "Speed of adjustment", "A bounds test verdict"],
        _X,
        _SEC,
        "A 1% price rise cuts sales 0.4% at once and 1.1% in the long run.",
        related=["ts.ardl", "ts.vecm", "test.engle_granger"],
        expert="Single-equation error-correction model in UECM form (statsmodels UECM); "
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
        "Explains a series from its own past and the current and past values of drivers, so "
        "you can see how an effect builds up over several periods.",
        ["A driver's effect is spread over several periods"],
        ["Effects are immediate only (a simple regression is enough)"],
        ["One result and one or more drivers over time"],
        [
            "Drivers affect the result, not the other way round",
            "The series vary around stable levels or share a long-run relationship",
        ],
        ["Effects per lag", "Long-run multipliers", "Forecasts given future drivers"],
        _I,
        _SEC,
        "A promotion lifts sales 8% in its week and 3% the week after.",
        related=["ts.ecm", "ts.sarimax"],
        expert="statsmodels ARDL with lag orders chosen by information criterion "
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
        "Forecasts a series by giving recent values more weight than older ones, with "
        "optional trend and seasonality, and is quick and robust.",
        [
            "You need a fast, reliable baseline forecast",
            "The level, trend and seasonality shift slowly",
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
        "damped trend) with simulation-based prediction intervals.",
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
        "Splits a series into a slowly changing level and trend, a seasonal pattern and "
        "noise, and forecasts it from those parts.",
        [
            "You want to see the underlying trend without the noise",
            "The trend and season change slowly over time",
        ],
        ["You only need a quick baseline (exponential smoothing is simpler)"],
        ["One number over time, plus optional drivers"],
        ["Each part changes smoothly and independently"],
        ["Smoothed level, trend and season", "Forecasts with ranges"],
        _I,
        _SEC,
        "Weekly sales split into a rising trend, a Christmas peak and week-to-week noise.",
        related=["ts.ets", "ts.hsgp_trend"],
        expert="statsmodels UnobservedComponents (local level/trend, stochastic seasonal, "
        "cycle, exog) estimated by maximum likelihood with the Kalman smoother.",
        refs=[
            _TS_REF,
            ("Harvey, Forecasting, Structural Time Series Models and the Kalman Filter", None),
        ],
        fits={"goal": ["forecast", "explain_drivers"], "data_shape": ["one_series"]},
    ),
    "ts.dynamic_factor": _a(
        "Common movements across many series",
        "Finds a few hidden forces that move many series together, such as the overall "
        "economy behind sales in many categories.",
        ["You have many related series and want their shared drivers"],
        ["You have one or two series"],
        ["Many numbers over time at the same frequency"],
        ["A few common factors explain most of the joint movement"],
        ["The common factors over time", "How strongly each series follows them", "Forecasts"],
        _X,
        _MIN,
        "Twenty category sales series follow one 'consumer mood' factor.",
        related=["ts.var"],
        expert="statsmodels DynamicFactor (state space; factors follow a VAR).",
        refs=[_TS_REF],
        fits={"goal": ["compare_series", "forecast"], "data_shape": ["several_series", "panel"]},
    ),
    "ts.markov_switching": _a(
        "Switching between regimes",
        "Detects whether a series switches between regimes, such as high-growth and "
        "low-growth periods, and estimates the behaviour in each.",
        [
            "Behaviour seems to jump between distinct modes",
            "You want the probability of being in each mode now",
        ],
        ["Changes are gradual", "You have little history"],
        ["One number over time, plus optional drivers"],
        ["A small number of regimes; switching depends only on the current regime"],
        ["Probability of each regime per period", "Behaviour per regime"],
        _X,
        _SEC,
        "Demand switches between a normal and a promotion-driven regime.",
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
        "Forecasts a series by combining a straight-line trend with smoothed recent values, "
        "a simple method that is hard to beat.",
        ["You want a strong, simple baseline to compare other models against"],
        ["Outside drivers matter a lot", "You need detailed explanations"],
        ["One number over time (seasonality is removed and put back automatically)"],
        ["The recent trend continues"],
        ["A forecast with a range"],
        _B,
        _NOW,
        "A theta forecast of monthly sales to beat before using anything more complex.",
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
        "columns, using a well-known machine-learning method you pick.",
        [
            "You have many rows with drivers (price, promotion, weather) and want one best "
            "guess per row",
            "You want to see how much each driver adds to a prediction",
        ],
        [
            "You need a range of likely values (use the quantile, bootstrap or conformal versions)",
            "You have a single short series over time",
        ],
        ["A table of the number to predict and the columns that explain it"],
        [
            "Rows you predict look like the rows the model learnt from",
            "Future values of the drivers are known when you forecast",
        ],
        ["One prediction per row", "Each driver's contribution (linear and tree models)"],
        _B,
        _SEC,
        "Predict next week's units per store and product from planned prices with gradient "
        "boosting.",
        related=["sklearn.tabular_quantiles", "sklearn.tabular_conformal", "core.one_hot"],
        expert="An allow-listed scikit-learn or xgboost regressor on rows stacked from the "
        "labelled dims (coordinates become features); point representation; exact linear or "
        "counterfactual-zeroing contributions; models stored as skops or xgboost JSON.",
        fits={
            "goal": ["predict_values", "explain_drivers"],
            "data_shape": ["table", "panel"],
            "drivers": ["yes"],
            "uncertainty": ["no"],
        },
    ),
    "sklearn.tabular_quantiles": _a(
        "Predict a number with a low and high estimate",
        "Predicts a low, middle and high value for each row of a table, so you see how sure "
        "the prediction is.",
        [
            "You need a best and worst case per store, product or customer",
            "Stock or staffing decisions depend on the high or low end",
        ],
        [
            "You need to add predictions up into totals with a range (ranges per row cannot be "
            "added)"
        ],
        ["A table of the number to predict and the columns that explain it"],
        ["Rows you predict look like the rows the model learnt from"],
        ["A low, middle and high prediction per row"],
        _I,
        _SEC,
        "Plan stock at the 90% level: the high estimate of next week's demand per store.",
        related=["sklearn.tabular_regressor", "sklearn.tabular_bootstrap"],
        expert="xgboost multi-quantile regression or one scikit-learn quantile model per level; "
        "quantile representation (per-cell quantiles cannot be aggregated across cells).",
        fits={
            "goal": ["predict_values", "forecast"],
            "data_shape": ["table", "panel"],
            "drivers": ["yes"],
            "uncertainty": ["essential", "nice"],
        },
    ),
    "sklearn.tabular_bootstrap": _a(
        "Predict a number with ranges you can add up",
        "Fits the same method many times on resampled data, which gives ranges that stay "
        "correct when you add predictions up into totals.",
        [
            "You need a range for totals (all stores, a whole region)",
            "The data has groups (stores) that should be resampled together",
        ],
        ["You need results in seconds on a very large table (it fits many models)"],
        ["A table of the number to predict and the columns that explain it"],
        [
            "Rows you predict look like the rows the model learnt from",
            "Resampling whole groups captures how uncertain the model is",
        ],
        ["Many joint simulated predictions per row", "Ranges for any total"],
        _I,
        _MIN,
        "A range for next month's total sales across 300 stores.",
        related=["sklearn.tabular_quantiles", "sklearn.tabular_regressor"],
        expert="A bootstrap ensemble (optionally resampling whole groups with 'by'); each "
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
        "Predicts a number for each row with a range that contains the true value about as "
        "often as you ask for, such as nine times out of ten.",
        [
            "You need ranges whose coverage you can rely on, row by row",
            "You do not want to make assumptions about the shape of the errors",
        ],
        [
            "You need ranges for totals (use the bootstrap version)",
            "You have very little data (part of it is kept aside to size the ranges)",
        ],
        ["A table of the number to predict and the columns that explain it"],
        ["New rows behave like the rows kept aside for sizing the ranges"],
        ["One prediction and a range per row"],
        _I,
        _SEC,
        "Ranges that hold next week's demand for 90% of store-product pairs.",
        related=["sklearn.tabular_quantiles", "sklearn.tabular_regressor"],
        expert="Split-conformal prediction intervals at level 1 - alpha with a calibration "
        "fraction; marginal per-cell coverage; interval representation (not aggregatable).",
        fits={
            "goal": ["predict_values"],
            "data_shape": ["table", "panel"],
            "drivers": ["yes"],
            "uncertainty": ["essential", "nice"],
        },
    ),
    "sklearn.tabular_classifier": _a(
        "Predict yes or no",
        "Estimates the chance that something happens for each row of a table, such as a "
        "customer buying again or an order arriving late.",
        ["The result is yes or no for each row", "You want a probability to rank or target rows"],
        ["The result is a number or has more than two outcomes"],
        ["A table with a yes/no column and the columns that explain it"],
        ["Rows you score look like the rows the model learnt from"],
        ["The probability of 'yes' for each row"],
        _B,
        _SEC,
        "The chance each customer orders again next month, to decide who gets a voucher.",
        related=["sklearn.tabular_regressor", "choice.multinomial_logit"],
        expert="An allow-listed scikit-learn or xgboost binary classifier on stacked rows; "
        "point representation of the positive-class probability.",
        fits={
            "goal": ["predict_values", "understand_choices"],
            "data_shape": ["table", "panel", "choices"],
            "drivers": ["yes"],
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

PLANNED_MODEL_IDS: Final[frozenset[str]] = frozenset(MODEL_ABOUTS) - SURVIVING_MODEL_IDS
"""Model ids the v0.5 mmm-framework and statsmodels plugins implement (their cards are
here so plugins, docs and the chooser use the same words)."""

# ================================================================================= templates

TEMPLATE_ABOUTS: Final[dict[str, About]] = {
    "geo_mmm": _a(
        "Geo marketing mix",
        "Find out how much each marketing channel adds to your results in each market, and "
        "what happens if you move budget between channels.",
        [
            "You have weekly sales and spend per channel, by market",
            "You want returns per channel and a budget plan",
        ],
        ["You have less than a year of data", "Spend never changes"],
        ["A table with date, market, the result (sales) and one spend column per channel"],
        [
            "Advertising lingers and has diminishing returns",
            "Other drivers (price, promotions) are included if they matter",
        ],
        ["Return and contribution per channel", "What-if sliders", "A budget plan", "A scorecard"],
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
        "Forecast one series, such as sales, visits or demand, a few weeks or months ahead, "
        "with a range of likely values.",
        ["You need a forecast of one number"],
        ["You need a forecast for every store (use Demand by store)"],
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
        "Predict how much each store will sell each week, with a low and a high estimate, "
        "from prices, promotions and the calendar.",
        ["You have sales per store and week and know planned prices or promotions"],
        ["You only have one total series"],
        ["A table with store, week, units sold and optional drivers"],
        ["Planned drivers are known for the weeks you forecast"],
        ["A forecast per store with low and high values", "A scorecard"],
        _B,
        _SEC,
        "Predict next month's units for 300 stores.",
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
        "Understand which product customers pick on each shopping trip, and how price and "
        "promotions shift the choice.",
        ["You have purchase records with the prices on offer at each trip"],
        ["You only have total sales"],
        ["A table with customer, trip number, the product chosen and each product's price"],
        ["Customers weigh price and promotions when they choose"],
        ["Price sensitivity", "Choice shares", "What-if shares"],
        _I,
        _MIN,
        "See how a 10% price cut on one brand changes its share.",
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
