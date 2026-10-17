"""Forecasts conditioned on actuals: the request rules and the conditioning record.

A forecast (``mode: forecast``) may carry **actuals** - observed target values for periods
the model was not fitted on - next to the **future periods** to forecast. A model that
declares :attr:`~mapi_spec.capabilities.Capabilities.conditions_on_actuals` updates its
state with the actuals using its *fitted* parameters (a Kalman filter run over the new
observations, or the new lags of an autoregression; nothing is re-estimated) and forecasts
from the last actual. Everything else refuses such a request; actuals are never ignored.

The rules, per series (a cell of the target's other dims) along the horizon dim:

* **Actual or future.** A row whose target holds a value is an actual. A row whose target
  is empty (``null``/NaN) after the series' last actual is a period to forecast. Only
  ``forecast`` requests read targets this way; other modes never use them as actuals.
* **Overlap with the fit.** Actuals may start inside the fitted range: there they *replace*
  the fitted values for those periods (revised data), with the parameters still fixed. An
  empty target inside the fitted range keeps the fitted value.
* **Contiguity.** Actuals continue the fitted history: an empty target between the end of
  the fit and the last actual is a gap, refused with ``ACTUALS_INVALID`` unless the model's
  input schema allows missing target values (state-space models), which filter it as a
  missing observation. Coordinates that skip periods stay ``NON_CONTIGUOUS_EXTENSION``.
* **Horizon.** When the payload has rows after the last actual, exactly those are forecast
  and ``horizon.periods`` is not used (a warning says so when it differs); when the payload
  ends with an actual, ``horizon.periods`` periods after its last period are appended and
  forecast. ``horizon.dim`` always names the dim (:data:`HorizonSource`).
* **Drivers.** Actual rows after the fitted history need the drivers observed with them
  (``ACTUALS_INVALID`` otherwise). Empty driver values on future rows follow
  ``future_covariates``: ``supplied`` refuses them (``MISSING_FUTURE_COVARIATE``),
  ``carry_last``, ``seasonal_naive`` and ``zero`` fill them from the fitted history *and*
  the drivers of the actual rows.
* **Refusal.** A model without the capability refuses a forecast whose payload carries an
  actual with ``ACTUALS_NOT_SUPPORTED`` (HTTP 422) and a suggested action (refit with the
  new data, or leave the target empty to forecast from the end of the fit).

The score result says what the forecast was conditioned on: a :class:`ForecastConditioning`
in the result tree's root attribute :data:`CONDITIONING_ATTR` (JSON), in ``result.json`` and
inline in ``ScoreSummary.conditioning``. Every forecast carries one; ``conditioned`` is
false when no actuals were sent.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

from pydantic import Field

from mapi_spec._dto import ResponseModel, dto_examples
from mapi_spec._types import Name, Scalar
from mapi_spec.errors import ActualsNotSupported, ErrorCode, Issue

__all__ = [
    "CONDITIONING_ATTR",
    "MAX_CONDITIONING_SERIES",
    "ForecastConditioning",
    "HorizonSource",
    "SeriesConditioning",
    "actuals_not_supported",
    "actuals_not_supported_issue",
    "build_conditioning",
    "format_label",
]

CONDITIONING_ATTR: Final = "conditioning"
"""Root attribute of a forecast's result ``DataTree``: the :class:`ForecastConditioning` as
JSON (the runner copies it into ``result.json`` and ``summary.json``)."""

MAX_CONDITIONING_SERIES: Final = 1000
"""Series listed in :attr:`ForecastConditioning.series` at most (``truncated`` beyond)."""

HorizonSource = Literal["rows", "horizon"]
"""Where the forecast periods came from: ``rows`` - the payload's rows after the last actual
(``horizon.periods`` unused); ``horizon`` - ``horizon.periods`` periods appended after the
payload (or after the fitted history when there is no payload)."""


def format_label(value: Scalar | None) -> Scalar | None:
    """A coordinate label for people: ISO dates without a midnight time."""
    if isinstance(value, str) and value.endswith("T00:00:00"):
        return value[: -len("T00:00:00")]
    return value


class SeriesConditioning(ResponseModel):
    """What one series' forecast was conditioned on.

    ``series`` holds the series' labels on the target's other dims (empty for a single
    series). ``origin`` is the last period the forecast starts from: the last actual, or the
    end of the fitted history when the series has none.
    """

    series: dict[Name, Scalar] = Field(default_factory=dict)
    n_actuals: int = Field(..., ge=0, description="Periods with a supplied actual")
    first_actual: Scalar | None = None
    actuals_through: Scalar | None = Field(None, description="The last supplied actual")
    revised: int = Field(
        0, ge=0, description="Actuals inside the fitted range: they replaced the fitted values"
    )
    missing: int = Field(
        0,
        ge=0,
        description="Empty periods between the fitted history and the last actual, filtered "
        "as missing observations (only models that accept missing targets)",
    )
    origin: Scalar
    forecast_from: Scalar | None = None
    forecast_to: Scalar | None = None
    n_forecast: int = Field(0, ge=0)

    model_config = dto_examples(
        {
            "series": {},
            "n_actuals": 3,
            "first_actual": "1961-01-01",
            "actuals_through": "1961-03-01",
            "revised": 0,
            "missing": 0,
            "origin": "1961-03-01",
            "forecast_from": "1961-04-01",
            "forecast_to": "1961-11-01",
            "n_forecast": 8,
        }
    )


class ForecastConditioning(ResponseModel):
    """What a forecast was conditioned on: the fitted history, the supplied actuals and where
    the forecast periods came from. ``parameters`` is always ``fitted``: conditioning never
    re-estimates the model. ``message`` says the same in plain language."""

    dim: Name
    fit_end: Scalar = Field(..., description="The last period of the fitted history")
    conditioned: bool = Field(..., description="Whether any supplied actual was used")
    n_actuals: int = Field(..., ge=0, description="Actuals used, over every series")
    actuals_through: Scalar | None = Field(None, description="The latest actual over every series")
    revised: int = Field(0, ge=0)
    series_conditioned: int = Field(0, ge=0, description="Series with at least one actual")
    n_series: int = Field(1, ge=0)
    horizon_source: HorizonSource
    periods: int = Field(..., ge=0, description="Forecast periods (the most over series)")
    parameters: Literal["fitted"] = "fitted"
    series: list[SeriesConditioning] = Field(
        default_factory=list, max_length=MAX_CONDITIONING_SERIES
    )
    truncated: bool = Field(False, description="More series than MAX_CONDITIONING_SERIES")
    message: str = Field(..., max_length=2000)

    model_config = dto_examples(
        {
            "dim": "t",
            "fit_end": "1960-12-01",
            "conditioned": True,
            "n_actuals": 3,
            "actuals_through": "1961-03-01",
            "revised": 0,
            "series_conditioned": 1,
            "n_series": 1,
            "horizon_source": "horizon",
            "periods": 8,
            "parameters": "fitted",
            "series": [SeriesConditioning.model_json_schema()["examples"][0]],
            "truncated": False,
            "message": "Forecast updated with 3 actuals (1961-01-01 to 1961-03-01) after "
            "the fitted history, which ends 1960-12-01; the model's parameters were not "
            "re-estimated. 8 periods forecast from 1961-04-01 to 1961-11-01.",
        },
        {
            "dim": "t",
            "fit_end": "1960-12-01",
            "conditioned": False,
            "n_actuals": 0,
            "horizon_source": "horizon",
            "periods": 8,
            "series": [
                {
                    "n_actuals": 0,
                    "origin": "1960-12-01",
                    "forecast_from": "1961-01-01",
                    "forecast_to": "1961-08-01",
                    "n_forecast": 8,
                }
            ],
            "message": "Forecast from the end of the fitted history (1960-12-01); no actuals "
            "were supplied. 8 periods forecast from 1961-01-01 to 1961-08-01.",
        },
    )


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _span(first: Scalar | None, last: Scalar | None) -> str:
    a, b = format_label(first), format_label(last)
    return f"{a}" if a == b else f"{a} to {b}"


def build_conditioning(
    *,
    dim: str,
    fit_end: Scalar,
    series: Sequence[SeriesConditioning],
    horizon_source: HorizonSource,
) -> ForecastConditioning:
    """The :class:`ForecastConditioning` of ``series`` (totals and the plain message)."""
    fit_label = format_label(fit_end)
    assert fit_label is not None
    with_actuals = [s for s in series if s.n_actuals > 0]
    n_actuals = sum(s.n_actuals for s in series)
    revised = sum(s.revised for s in series)
    missing = sum(s.missing for s in series)
    throughs = [s.actuals_through for s in with_actuals if s.actuals_through is not None]
    firsts = [s.first_actual for s in with_actuals if s.first_actual is not None]
    through = max(throughs, key=str) if throughs else None
    first = min(firsts, key=str) if firsts else None
    periods = max((s.n_forecast for s in series), default=0)
    starts = [s.forecast_from for s in series if s.forecast_from is not None]
    ends = [s.forecast_to for s in series if s.forecast_to is not None]
    if with_actuals:
        where = ""
        if len(series) > 1:
            where = f" across {len(with_actuals)} of {len(series)} series"
        # where the actuals fall against the fit: after it, revising it, or both
        if not revised:
            against = "after"
        elif revised == n_actuals:
            against = "inside"
        else:
            against = "overlapping the end of"
        text = (
            f"Forecast updated with {_plural(n_actuals, 'actual')} ({_span(first, through)})"
            f"{where} {against} the fitted history, which ends {fit_label}"
        )
        if revised:
            text += f"; {revised} of them replaced fitted values"
        if missing:
            text += f"; {_plural(missing, 'empty period')} filtered as missing"
        text += "; the model's parameters were not re-estimated."
    else:
        text = (
            f"Forecast from the end of the fitted history ({fit_label}); no actuals were supplied."
        )
    if periods and starts and ends:
        text += (
            f" {_plural(periods, 'period')} forecast from {format_label(min(starts, key=str))} "
            f"to {format_label(max(ends, key=str))}."
        )
    listed = list(series[:MAX_CONDITIONING_SERIES])
    return ForecastConditioning(
        dim=dim,
        fit_end=fit_label,
        conditioned=bool(with_actuals),
        n_actuals=n_actuals,
        actuals_through=format_label(through),
        revised=revised,
        series_conditioned=len(with_actuals),
        n_series=len(series),
        horizon_source=horizon_source,
        periods=periods,
        series=[
            s.model_copy(
                update={
                    k: format_label(getattr(s, k))
                    for k in (
                        "first_actual",
                        "actuals_through",
                        "origin",
                        "forecast_from",
                        "forecast_to",
                    )
                }
            )
            for s in listed
        ],
        truncated=len(series) > MAX_CONDITIONING_SERIES,
        message=text[:2000],
    )


def actuals_not_supported_issue(
    model: str,
    *,
    var: str,
    n: int | None = None,
    first: Scalar | None = None,
    last: Scalar | None = None,
    fit_end: Scalar | None = None,
    pointer: str = "/data",
) -> Issue:
    """The ``ACTUALS_NOT_SUPPORTED`` issue (the static check and the sandbox share it)."""
    where = []
    if n is not None:
        where.append(_plural(n, "period"))
    if first is not None:
        where.append(f"({_span(first, last)})")
    end = f" ({format_label(fit_end)})" if fit_end is not None else ""
    message = (
        f"model {model!r} cannot update a forecast with actuals: the payload has values of "
        f"the target {var!r}"
        + (f" for {' '.join(where)}" if where else "")
        + f". It would forecast from the end of its fitted history{end} without them."
    )
    return Issue(
        code=ErrorCode.ACTUALS_NOT_SUPPORTED,
        pointer=pointer,
        message=message[:2000],
        suggested_action=(
            "Refit the model with the new data to forecast from it, or leave the target "
            f"empty (null) to forecast from the end of the fitted history{end}."
        ),
        context={"target": var, "actuals": n, "fit_end": format_label(fit_end)},
    )


def actuals_not_supported(
    model: str,
    *,
    var: str,
    n: int | None = None,
    first: Scalar | None = None,
    last: Scalar | None = None,
    fit_end: Scalar | None = None,
    pointer: str = "/data",
) -> ActualsNotSupported:
    """:func:`actuals_not_supported_issue` as an exception (HTTP 422)."""
    issue = actuals_not_supported_issue(
        model, var=var, n=n, first=first, last=last, fit_end=fit_end, pointer=pointer
    )
    return ActualsNotSupported(issue.message, pointer=issue.pointer, issues=[issue])
