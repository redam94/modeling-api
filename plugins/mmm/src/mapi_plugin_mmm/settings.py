"""Settings and fit schemas of the MMM plugins.

Every field carries ``x-ui-level`` (``basic`` or ``advanced``): Guided mode shows the basic
fields only, Expert mode shows everything (plan §11). Settings are frozen and reject
unknown keys; the JSON Schema exported in the manifest is what the control plane
validates specs against.
"""

from __future__ import annotations

from typing import Any, Literal

from mapi_sdk.models import ModelSettings
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mapi_plugin_mmm.priors import PRIOR_SLOTS, PriorSpec, resolve_priors
from mapi_plugin_mmm.transforms import AdstockKind, SaturationKind

__all__ = [
    "AdstockSettings",
    "GridSettings",
    "HierarchicalGeoSettings",
    "PyMCMarketingSettings",
    "SaturationSettings",
    "mcmc_fit_schema",
]

BASIC: dict[str, Any] = {"x-ui-level": "basic"}
ADVANCED: dict[str, Any] = {"x-ui-level": "advanced"}

_NAME = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"


class _Sub(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdstockSettings(_Sub):
    """Carry-over of spend into later periods."""

    kind: AdstockKind = Field(
        "geometric",
        description="geometric: decays from the spend period; delayed: peaks after theta periods",
        json_schema_extra=BASIC,
    )
    l_max: int | None = Field(
        None,
        ge=1,
        le=520,
        description="Number of lags (must equal the pipeline's lag_tensor l_max; default: "
        "taken from the data)",
        json_schema_extra=ADVANCED,
    )
    normalize: bool = Field(
        True,
        description="Normalise the lag weights to sum to one (steady-state adstock = spend)",
        json_schema_extra=ADVANCED,
    )


class SaturationSettings(_Sub):
    """Diminishing returns of (adstocked) spend."""

    kind: SaturationKind = Field(
        "logistic",
        description="logistic or hill curves; none for a linear response",
        json_schema_extra=BASIC,
    )


class GridSettings(_Sub):
    """Response-curve grids cached per thinned draw for Guided-mode sliders."""

    max_multiplier: float = Field(
        2.0,
        gt=0,
        le=20,
        description="Largest spend multiplier on the grid (1 = spend over the fit window)",
        json_schema_extra=ADVANCED,
    )
    points: int = Field(
        21,
        ge=3,
        le=201,
        description="Grid points from 0 to max_multiplier",
        json_schema_extra=ADVANCED,
    )
    marginal_step: float = Field(
        0.01,
        gt=0,
        le=0.25,
        description="Relative spend change used for marginal cost (central difference)",
        json_schema_extra=ADVANCED,
    )

    def multipliers(self) -> list[float]:
        step = self.max_multiplier / (self.points - 1)
        return [round(i * step, 12) for i in range(self.points)]


class HierarchicalGeoSettings(ModelSettings):
    """Settings of ``mmm.hierarchical_geo``.

    Canonical names (bind dataset names to these in ``data.bind``): dims ``geo``,
    ``channel`` and ``time``; target ``y``; spend ``spend`` as a lag tensor over
    ``lag_dim`` (``core.lag_tensor``). Every other numeric variable along ``time`` is a
    control (or list them in ``controls``).
    """

    pooling: Literal["partial", "complete", "none"] = Field(
        "partial",
        description="How channel effects are shared across geos: partial pooling (default), "
        "one effect for all geos, or independent geos (no new geos at score time)",
        json_schema_extra=BASIC,
    )
    centred: bool = Field(
        False,
        description="Centred parameterisation of the geo-level effects (non-centred samples "
        "better with few geos or little data per geo)",
        json_schema_extra=ADVANCED,
    )
    adstock: AdstockSettings = Field(AdstockSettings(), json_schema_extra=BASIC)
    saturation: SaturationSettings = Field(SaturationSettings(), json_schema_extra=BASIC)
    priors: dict[str, PriorSpec] = Field(
        {},
        description="Prior overrides by parameter: "
        + ", ".join(f"{k} ({v.description})" for k, v in PRIOR_SLOTS.items()),
        json_schema_extra={
            "x-ui-level": "advanced",
            "propertyNames": {"enum": list(sorted(PRIOR_SLOTS))},
        },
    )
    controls: list[str] | None = Field(
        None,
        max_length=64,
        description="Control variables (default: every other numeric variable along time)",
        json_schema_extra=ADVANCED,
    )
    lag_dim: str = Field(
        "lag", pattern=_NAME, description="Lag dim of the spend tensor", json_schema_extra=ADVANCED
    )
    grid: GridSettings = Field(GridSettings(), json_schema_extra=ADVANCED)

    @field_validator("priors")
    @classmethod
    def _priors(cls, v: dict[str, PriorSpec]) -> dict[str, PriorSpec]:
        resolve_priors(v)
        return v

    @field_validator("controls")
    @classmethod
    def _controls(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(set(v)) != len(v):
            raise ValueError("controls must be unique")
        return v

    @model_validator(mode="after")
    def _check(self) -> HierarchicalGeoSettings:
        if self.controls and {"y", "spend"} & set(self.controls):
            raise ValueError("the target and spend cannot be controls")
        return self

    def resolved_priors(self) -> dict[str, PriorSpec]:
        return resolve_priors(self.priors)


class PyMCMarketingSettings(ModelSettings):
    """Settings of ``mmm.pymc_marketing`` (an adapter around ``pymc_marketing.mmm.MMM``).

    The adapter owns scaling, carry-over and the inverse to original units, so the
    pipeline should only convert currencies and resample - no scaling, no lag tensors.
    Canonical names: ``time`` (dim), ``channel`` (dim), ``y`` (target), ``spend`` over
    ``(*dims, channel, time)``; ``dims`` lists extra panel dims (for example ``[geo]``).
    """

    dims: list[str] = Field(
        [],
        max_length=4,
        description="Extra panel dims besides time and channel (e.g. [geo])",
        json_schema_extra=BASIC,
    )
    adstock: AdstockSettings = Field(AdstockSettings(l_max=4), json_schema_extra=BASIC)
    saturation: SaturationSettings = Field(SaturationSettings(), json_schema_extra=BASIC)
    controls: list[str] = Field(
        [],
        max_length=64,
        description="Control variables over (*dims, time)",
        json_schema_extra=ADVANCED,
    )
    yearly_seasonality: int | None = Field(
        None,
        ge=1,
        le=20,
        description="Fourier order of yearly seasonality",
        json_schema_extra=BASIC,
    )
    adstock_first: bool = Field(True, json_schema_extra=ADVANCED)

    @field_validator("dims", "controls")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        import re

        bad = [n for n in v if not re.match(_NAME, n)]
        if bad:
            raise ValueError(f"invalid names {bad}")
        if len(set(v)) != len(v):
            raise ValueError("names must be unique")
        if {"time", "channel", "date"} & set(v):
            raise ValueError("'time', 'channel' and 'date' are reserved")
        return v

    @model_validator(mode="after")
    def _check(self) -> PyMCMarketingSettings:
        if self.adstock.kind != "geometric":
            raise ValueError("the pymc-marketing adapter supports geometric adstock only")
        if self.adstock.l_max is None:
            raise ValueError("adstock.l_max is required")
        return self


def mcmc_fit_schema(*, engines: list[str], default_engine: str) -> dict[str, Any]:
    """JSON Schema for ``model.fit`` of an MCMC plugin (limits + ``x-ui-level``)."""
    return {
        "type": "object",
        "properties": {
            "mode": {
                "enum": ["evaluate_then_refit", "train_only"],
                "default": "evaluate_then_refit",
                "x-ui-level": "advanced",
            },
            "engine": {"enum": engines, "default": default_engine, "x-ui-level": "advanced"},
            "tune": {"type": "integer", "minimum": 0, "maximum": 20000, "x-ui-level": "basic"},
            "draws": {"type": "integer", "minimum": 1, "maximum": 20000, "x-ui-level": "basic"},
            "chains": {"type": "integer", "minimum": 1, "maximum": 16, "x-ui-level": "basic"},
            "target_accept": {
                "type": "number",
                "exclusiveMinimum": 0.5,
                "exclusiveMaximum": 1,
                "x-ui-level": "advanced",
            },
            "seed": {"type": "integer", "minimum": 0, "x-ui-level": "advanced"},
            "prior_predictive": {"type": "object", "x-ui-level": "advanced"},
            "keep": {"type": "object", "x-ui-level": "advanced"},
            "checkpoint": {"type": "object", "x-ui-level": "advanced"},
        },
    }
