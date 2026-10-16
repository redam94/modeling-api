"""Business priors, lift tests and prior translation (plan §11 Guided task 3; M3).

Guided users state beliefs in business units instead of model parameters:

* :class:`BusinessPriors` - per channel an ROI range ``[low, high]`` *or* a contribution-share
  range, each held with a stated ``credibility`` (the probability that the truth lies inside
  the range), plus an optional baseline share;
* :class:`LiftTest` - the result of an experiment (geo holdout, conversion lift): extra spend
  on one channel over a window and the incremental KPI it caused, with a standard error or a
  confidence interval. Lift tests are *calibration data*: the model treats each one as an
  extra observation of the channel's incremental effect.

Both attach to a ``ModelSpec`` through its optional ``calibration`` block (:class:`Calibration`).
A model plugin uses them only if its manifest declares the matching capability
(``Capabilities.calibration``); otherwise static validation reports
``CALIBRATION_UNSUPPORTED``.

Turning business priors into model-space priors needs the data (spend and KPI scale) and the
model's transforms, so it runs in the sandbox as a ``translate_priors`` run
(:class:`PriorTranslationJob` → ``priors.json`` = :class:`PriorTranslationResult`), which
also samples the prior predictive and reports it in business units (ROI and share
distributions, a KPI fan). :func:`apply_prior_translation` writes the result into a spec.

Pure Pydantic and stdlib (``statistics.NormalDist`` for quantiles): the control plane and
the web app (through OpenAPI) share it.
"""

from __future__ import annotations

import copy
import math
from datetime import date
from statistics import NormalDist
from typing import Annotated, Any, Final, Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from mapi_spec._dto import ResponseModel, dto_examples
from mapi_spec._types import Name, StrictModel
from mapi_spec.errors import ErrorCode, Issue, Severity, json_pointer

__all__ = [
    "MAX_CHANNELS",
    "MAX_LIFT_TESTS",
    "PRIORS_POINTER",
    "PRIOR_TRANSLATION_FORMAT",
    "PRIOR_TRANSLATION_OUTPUT",
    "BusinessPriors",
    "Calibration",
    "ChannelBelief",
    "DistributionSummary",
    "FanChart",
    "LiftTest",
    "PriorPreview",
    "PriorTranslationJob",
    "PriorTranslationResult",
    "ShareBelief",
    "apply_prior_translation",
    "check_business_priors",
    "logitnormal_from_range",
    "lognormal_from_range",
]

MAX_CHANNELS: Final = 64
MAX_LIFT_TESTS: Final = 500
PRIOR_TRANSLATION_FORMAT: Final = "mapi.prior-translation/v1"
PRIOR_TRANSLATION_OUTPUT: Final = "priors.json"
"""The output object of a ``translate_priors`` run."""
PRIORS_POINTER: Final = "/model/settings/priors"
"""Where translated priors go in a ``ModelSpec`` (pymc-marketing ``Prior`` dict form)."""

Credibility = Annotated[
    float,
    Field(
        ge=0.5,
        le=0.99,
        description="Probability that the true value lies inside the range (0.8 = 'I am 80% sure')",
    ),
]


def _check_range(v: tuple[float, float], *, unit: bool) -> tuple[float, float]:
    lo, hi = v
    if not (math.isfinite(lo) and math.isfinite(hi)):
        raise ValueError("range bounds must be finite numbers")
    if lo >= hi:
        raise ValueError("a range is [low, high] with low < high")
    if lo < 0:
        raise ValueError("range bounds cannot be negative")
    if unit and hi > 1:
        raise ValueError("a share range lies within [0, 1]")
    return v


class ChannelBelief(StrictModel):
    """What the user believes about one channel: exactly one of ``roi`` or
    ``contribution_share``, held with ``credibility``.

    * ``roi``: KPI units gained per spend unit over the fit window (``[1.5, 4]`` = "every
      euro returns between 1.5 and 4 euros of revenue");
    * ``contribution_share``: the channel's share of the KPI over the fit window.
    """

    roi: tuple[float, float] | None = Field(None, examples=[[1.5, 4.0]])
    contribution_share: tuple[float, float] | None = Field(None, examples=[[0.05, 0.15]])
    credibility: Credibility = 0.8

    @field_validator("roi")
    @classmethod
    def _roi(cls, v: tuple[float, float] | None) -> tuple[float, float] | None:
        return None if v is None else _check_range(v, unit=False)

    @field_validator("contribution_share")
    @classmethod
    def _share(cls, v: tuple[float, float] | None) -> tuple[float, float] | None:
        return None if v is None else _check_range(v, unit=True)

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.roi is None) == (self.contribution_share is None):
            raise ValueError("give exactly one of 'roi' or 'contribution_share'")
        return self


class ShareBelief(StrictModel):
    """A share range (for example of the baseline: KPI that happens without any media)."""

    share: tuple[float, float]
    credibility: Credibility = 0.8

    @field_validator("share")
    @classmethod
    def _share(cls, v: tuple[float, float]) -> tuple[float, float]:
        return _check_range(v, unit=True)


class BusinessPriors(StrictModel):
    """Beliefs in business units, per channel (keys are channel *labels* as in the data),
    and/or about the baseline. At least one belief is stated: a baseline belief alone is
    fine (channels without a belief keep the model's default priors)."""

    channels: dict[str, ChannelBelief] = Field(default_factory=dict, max_length=MAX_CHANNELS)
    baseline_share: ShareBelief | None = None
    kpi_units: str | None = Field(None, max_length=64, examples=["EUR"])
    spend_units: str | None = Field(None, max_length=64, examples=["EUR"])

    model_config = dto_examples(
        {
            "channels": {
                "tv": {"roi": [1.5, 4.0], "credibility": 0.8},
                "search": {"roi": [2.0, 6.0], "credibility": 0.9},
                "social": {"contribution_share": [0.02, 0.1]},
            },
            "baseline_share": {"share": [0.5, 0.8], "credibility": 0.8},
            "kpi_units": "EUR",
            "spend_units": "EUR",
        }
    )

    @field_validator("channels")
    @classmethod
    def _labels(cls, v: dict[str, ChannelBelief]) -> dict[str, ChannelBelief]:
        for label in v:
            if not label or len(label) > 128 or any(ord(c) < 32 for c in label):
                raise ValueError("channel labels are 1-128 printable characters")
        return v

    @model_validator(mode="after")
    def _some_belief(self) -> Self:
        if not self.channels and self.baseline_share is None:
            raise ValueError("state at least one belief: a channel's or the baseline's")
        return self


def check_business_priors(priors: BusinessPriors, pointer: str = "") -> list[Issue]:
    """Cross-field consistency of business priors (``BUSINESS_PRIORS_INVALID`` issues).

    Shares cannot add up to more than the whole KPI: the lower bounds of every channel's
    share plus the baseline's must stay at or below 1. Upper bounds summing below 1 with a
    baseline belief leave KPI unexplained, which is a warning.
    """
    issues: list[Issue] = []
    shares = {k: b.contribution_share for k, b in priors.channels.items() if b.contribution_share}
    low = sum(s[0] for s in shares.values())
    high = sum(s[1] for s in shares.values())
    if priors.baseline_share is not None:
        low += priors.baseline_share.share[0]
        high += priors.baseline_share.share[1]
    if low > 1:
        issues.append(
            Issue(
                code=ErrorCode.BUSINESS_PRIORS_INVALID,
                pointer=pointer + json_pointer("channels"),
                message=f"the smallest shares you gave already add up to {low:.0%} of the KPI",
                suggested_action="Lower some share ranges or the baseline share.",
            )
        )
    elif (
        priors.baseline_share is not None
        and priors.channels
        and len(shares) == len(priors.channels)
        and high < 1
    ):
        issues.append(
            Issue(
                code=ErrorCode.BUSINESS_PRIORS_INVALID,
                severity=Severity.WARNING,
                pointer=pointer + json_pointer("baseline_share"),
                message=f"even the largest shares you gave explain only {high:.0%} of the KPI",
                suggested_action="Widen the share ranges or the baseline share.",
            )
        )
    return issues


def _z(credibility: float) -> float:
    return NormalDist().inv_cdf(0.5 + credibility / 2)


def lognormal_from_range(low: float, high: float, credibility: float) -> tuple[float, float]:
    """``(mu, sigma)`` of the LogNormal whose central ``credibility`` interval is
    ``[low, high]`` (the translation of an ROI range). ``low`` must be positive."""
    if not 0 < low < high:
        raise ValueError("a log-normal range needs 0 < low < high")
    z = _z(credibility)
    return (math.log(low) + math.log(high)) / 2, (math.log(high) - math.log(low)) / (2 * z)


def logitnormal_from_range(low: float, high: float, credibility: float) -> tuple[float, float]:
    """``(mu, sigma)`` of the logit-normal whose central ``credibility`` interval is
    ``[low, high]`` (the translation of a share range). Needs ``0 < low < high < 1``."""
    if not 0 < low < high < 1:
        raise ValueError("a logit-normal range needs 0 < low < high < 1")

    def logit(p: float) -> float:
        return math.log(p / (1 - p))

    z = _z(credibility)
    return (logit(low) + logit(high)) / 2, (logit(high) - logit(low)) / (2 * z)


# ===================================================================================== lift tests


class LiftTest(StrictModel):
    """One experiment: ``spend_delta`` extra spend on ``channel`` (optionally in one ``geo``)
    from ``start`` to ``end`` (inclusive) caused ``kpi_delta`` incremental KPI.

    Give the measurement's uncertainty as a standard error ``se`` *or* a confidence interval
    ``ci`` at ``ci_level``. A holdout ("turn the channel off") is a negative ``spend_delta``
    with (usually) a negative ``kpi_delta``.
    """

    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")] | None = None
    channel: str = Field(..., min_length=1, max_length=128)
    geo: str | None = Field(None, min_length=1, max_length=128)
    start: date
    end: date
    spend_delta: float = Field(..., description="Extra spend during the test (spend units)")
    kpi_delta: float = Field(..., description="Measured incremental KPI (KPI units)")
    se: float | None = Field(None, gt=0, description="Standard error of kpi_delta")
    ci: tuple[float, float] | None = Field(None, description="Confidence interval of kpi_delta")
    ci_level: float = Field(0.9, ge=0.5, le=0.999)
    description: str | None = Field(None, max_length=500)

    model_config = dto_examples(
        {
            "id": "tv-holdout-2026q2",
            "channel": "tv",
            "geo": "de",
            "start": "2026-04-06",
            "end": "2026-05-03",
            "spend_delta": -120000.0,
            "kpi_delta": -260000.0,
            "ci": [-380000.0, -140000.0],
            "ci_level": 0.9,
            "description": "TV off in Germany for four weeks",
        }
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.end < self.start:
            raise ValueError("end must not be before start")
        if not math.isfinite(self.spend_delta) or self.spend_delta == 0:
            raise ValueError("spend_delta must be a non-zero number")
        if not math.isfinite(self.kpi_delta):
            raise ValueError("kpi_delta must be a finite number")
        if (self.se is None) == (self.ci is None):
            raise ValueError("give exactly one of 'se' or 'ci'")
        if self.ci is not None:
            lo, hi = self.ci
            if not lo < hi:
                raise ValueError("ci is [low, high] with low < high")
            if not lo <= self.kpi_delta <= hi:
                raise ValueError("kpi_delta must lie inside its confidence interval")
        return self

    @property
    def sigma(self) -> float:
        """The standard error (from ``ci`` assuming a symmetric normal interval)."""
        if self.se is not None:
            return self.se
        assert self.ci is not None
        return (self.ci[1] - self.ci[0]) / (2 * _z(self.ci_level))

    @property
    def implied_roi(self) -> float:
        """KPI gained per extra spend unit during the test."""
        return self.kpi_delta / self.spend_delta


class Calibration(StrictModel):
    """The ``calibration`` block of a ``ModelSpec``: beliefs and experiment results the model
    should respect. ``prior_translation_run`` records which ``translate_priors`` run produced
    the priors in ``model.settings.priors`` (lineage only)."""

    business_priors: BusinessPriors | None = None
    lift_tests: list[LiftTest] = Field(default_factory=list, max_length=MAX_LIFT_TESTS)
    prior_translation_run: str | None = Field(None, pattern=r"^run_[0-9a-z]{26}$")

    model_config = dto_examples(
        {
            "business_priors": BusinessPriors.model_json_schema()["examples"][0],
            "lift_tests": [LiftTest.model_json_schema()["examples"][0]],
        }
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        ids = [t.id for t in self.lift_tests if t.id is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("lift test ids must be unique")
        return self

    @property
    def empty(self) -> bool:
        return self.business_priors is None and not self.lift_tests


# =========================================================================== prior translation


class PriorTranslationJob(StrictModel):
    """``RunSpec.spec`` of a ``translate_priors`` run.

    ``spec`` is the resolved ``ModelSpec`` document (its ``data.dataset`` is the
    ``dataset`` input). The runner fits nothing: it builds the model, maps the beliefs to
    model-space priors and samples ``draws`` prior-predictive draws.
    """

    spec: dict[str, JsonValue]
    business_priors: BusinessPriors
    lift_tests: list[LiftTest] = Field(default_factory=list, max_length=MAX_LIFT_TESTS)
    draws: int = Field(500, ge=50, le=2000)
    seed: int | None = Field(None, ge=0, lt=2**32)
    quantiles: tuple[float, ...] = (0.05, 0.25, 0.5, 0.75, 0.95)

    @field_validator("quantiles")
    @classmethod
    def _quantiles(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if not v or list(v) != sorted(set(v)) or not all(0 < q < 1 for q in v):
            raise ValueError("quantiles must be unique, ascending and inside (0, 1)")
        return v


def _check_quantile_keys(v: dict[str, float]) -> dict[str, float]:
    for key in v:
        try:
            q = float(key)
        except ValueError:
            raise ValueError(f"quantile key {key!r} is not a number") from None
        if not 0 < q < 1:
            raise ValueError(f"quantile key {key!r} is outside (0, 1)")
    return v


class DistributionSummary(ResponseModel):
    """A distribution in business units: mean, sd and named quantiles (``"0.05"`` …)."""

    mean: float
    sd: float = Field(..., ge=0)
    quantiles: dict[str, float] = Field(default_factory=dict, max_length=32)
    units: str | None = Field(None, max_length=64)

    model_config = dto_examples(
        {"mean": 2.6, "sd": 0.8, "quantiles": {"0.05": 1.5, "0.5": 2.5, "0.95": 4.1}}
    )

    @field_validator("quantiles")
    @classmethod
    def _keys(cls, v: dict[str, float]) -> dict[str, float]:
        return _check_quantile_keys(v)


class FanChart(ResponseModel):
    """Quantile bands of a series along one dim (a KPI fan): ``values[i][j]`` is quantile
    ``quantiles[i]`` at ``coords[j]``. ``observed`` (if any) is the data over ``coords``."""

    dim: Name
    coords: list[str | int | float] = Field(..., max_length=10_000)
    quantiles: list[float] = Field(..., min_length=1, max_length=16)
    values: list[list[float | None]] = Field(..., max_length=16)
    observed: list[float | None] | None = None
    units: str | None = Field(None, max_length=64)

    model_config = dto_examples(
        {
            "dim": "time",
            "coords": ["2026-01-04", "2026-01-11"],
            "quantiles": [0.05, 0.5, 0.95],
            "values": [[80.0, 82.0], [100.0, 103.0], [125.0, 129.0]],
            "observed": [101.0, 99.5],
            "units": "kEUR",
        }
    )

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.quantiles != sorted(set(self.quantiles)) or not all(
            0 < q < 1 for q in self.quantiles
        ):
            raise ValueError("quantiles must be unique, ascending and inside (0, 1)")
        if len(self.values) != len(self.quantiles):
            raise ValueError("values needs one row per quantile")
        if any(len(row) != len(self.coords) for row in self.values):
            raise ValueError("every values row needs one value per coord")
        if self.observed is not None and len(self.observed) != len(self.coords):
            raise ValueError("observed needs one value per coord")
        return self


class PriorPreview(ResponseModel):
    """The prior predictive in business units: what the model believes *before* seeing the
    KPI, so users can check that their beliefs came out as intended."""

    roi: dict[str, DistributionSummary] = Field(default_factory=dict, max_length=MAX_CHANNELS)
    contribution_share: dict[str, DistributionSummary] = Field(
        default_factory=dict, max_length=MAX_CHANNELS
    )
    baseline_share: DistributionSummary | None = None
    kpi_fan: FanChart | None = None

    model_config = dto_examples(
        {
            "roi": {"tv": DistributionSummary.model_json_schema()["examples"][0]},
            "contribution_share": {
                "tv": {"mean": 0.12, "sd": 0.04, "quantiles": {"0.05": 0.06, "0.95": 0.19}}
            },
            "kpi_fan": FanChart.model_json_schema()["examples"][0],
        }
    )


class PriorTranslationResult(ResponseModel):
    """``priors.json`` of a ``translate_priors`` run.

    ``priors`` are model-space priors in the plugin's ``settings.priors`` form (pymc-marketing
    ``Prior`` dicts; per-channel parameters are lists in ``channels`` order), to be written
    at :data:`PRIORS_POINTER`. ``coverage`` is the prior mass inside each stated range
    (close to the stated credibility when the translation is faithful).
    """

    format: Literal["mapi.prior-translation/v1"] = PRIOR_TRANSLATION_FORMAT
    channels: list[str] = Field(..., max_length=MAX_CHANNELS)
    priors: dict[str, dict[str, JsonValue]] = Field(default_factory=dict, max_length=64)
    preview: PriorPreview = Field(default_factory=PriorPreview)
    coverage: dict[str, float] = Field(default_factory=dict, max_length=MAX_CHANNELS)
    draws: int = Field(..., ge=1)
    seed: int | None = None
    issues: list[Issue] = Field(default_factory=list, max_length=256)

    model_config = dto_examples(
        {
            "format": PRIOR_TRANSLATION_FORMAT,
            "channels": ["tv", "search", "social"],
            "priors": {
                "beta_channel": {
                    "distribution": "LogNormal",
                    "mu": [-1.2, -0.9, -2.1],
                    "sigma": [0.38, 0.33, 0.5],
                    "dims": ["channel"],
                }
            },
            "preview": PriorPreview.model_json_schema()["examples"][0],
            "coverage": {"tv": 0.79, "search": 0.88, "social": 0.81},
            "draws": 500,
            "seed": 7,
        }
    )

    @model_validator(mode="after")
    def _coverage(self) -> Self:
        if any(not 0 <= v <= 1 for v in self.coverage.values()):
            raise ValueError("coverage values are probabilities")
        return self


def apply_prior_translation(
    document: dict[str, Any],
    result: PriorTranslationResult,
    *,
    business_priors: BusinessPriors | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """A copy of the ``ModelSpec`` ``document`` with ``result.priors`` merged into
    ``model.settings.priors`` (translated keys win) and the ``calibration`` block updated
    with ``business_priors`` and ``run_id`` (lineage)."""
    doc = copy.deepcopy(document)
    model = doc.setdefault("model", {})
    if not isinstance(model, dict):
        raise ValueError("the document's 'model' block is not an object")
    settings = model.setdefault("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("model.settings is not an object")
    priors = settings.get("priors") or {}
    if not isinstance(priors, dict):
        raise ValueError("model.settings.priors is not an object")
    settings["priors"] = {**priors, **copy.deepcopy(result.priors)}
    if business_priors is not None or run_id is not None:
        cal = doc.get("calibration") or {}
        if not isinstance(cal, dict):
            raise ValueError("the document's 'calibration' block is not an object")
        if business_priors is not None:
            cal["business_priors"] = business_priors.model_dump(mode="json", exclude_none=True)
        if run_id is not None:
            cal["prior_translation_run"] = run_id
        doc["calibration"] = cal
    return doc
