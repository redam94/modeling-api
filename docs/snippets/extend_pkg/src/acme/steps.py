"""acme.winsorize: a preprocessing step scaffolded by ``mapi plugin init --kind step``.

``WinsorizeStep`` clips extreme values of the named variables to quantiles it learns on the
training data only (a stateful step), so one outlier week does not drag a model. Replace
``fit``/``transform`` with your step, update the About card and keep ``mapi plugin test``
(the contract kit) green.
"""

from __future__ import annotations

from typing import ClassVar

import xarray as xr
from mapi_sdk import Step, StepContext, StepSettings, StepState
from mapi_spec import StepKind
from mapi_spec.about import About
from pydantic import Field, model_validator

__all__ = ["STEPS", "WinsorizeSettings", "WinsorizeStep"]


class WinsorizeSettings(StepSettings):
    """Validated settings; exported as JSON Schema for the pipeline editor and the API."""

    vars: list[str] = Field(..., min_length=1, description="The variables to clip")
    lower: float = Field(0.01, ge=0.0, lt=0.5, description="Quantile of the lower limit")
    upper: float = Field(0.99, gt=0.5, le=1.0, description="Quantile of the upper limit")

    @model_validator(mode="after")
    def _ordered(self) -> WinsorizeSettings:
        if self.lower >= self.upper:
            raise ValueError("lower must be smaller than upper")
        return self


class WinsorizeStep(Step[WinsorizeSettings, StepState]):
    """Clip each variable to its ``lower``/``upper`` quantiles (learned on train)."""

    id = "acme.winsorize"
    version = "0.1.0"
    kind = StepKind.VALUE
    Settings = WinsorizeSettings
    State = StepState  # stateful: the limits are fitted on the training data only
    var_params = ("vars",)  # settings that name variables, so the control plane checks them
    summary = "Clip each variable to quantiles learned on the training data"
    summary_plain = "Caps extreme values so one unusual week does not drag a model"
    # The card analysts read in the pipeline palette and the Catalog.
    about: ClassVar[About | None] = About(
        title="Cap extreme values",
        summary_plain="Caps values that are far above or below the usual range. One "
        "unusual week then does not pull the model away from the rest.",
        use_when=["A few weeks have values far outside the usual range"],
        avoid_when=["The extreme values are the events you want to model"],
        data_needs=["Numeric variables"],
        assumptions=["The extreme values are noise, not signal"],
        outputs=["The same variables with extreme values capped"],
        difficulty="beginner",
        typical_runtime="instant",
        example="With upper 0.99, a week of 10000 sales among weeks of about 200 becomes 260.",
    )

    def fit(self, ds: xr.Dataset, s: WinsorizeSettings, ctx: StepContext) -> StepState:
        arrays = {}
        for v in s.vars:
            arrays[f"lower__{v}"] = ds[v].quantile(s.lower).drop_vars("quantile")
            arrays[f"upper__{v}"] = ds[v].quantile(s.upper).drop_vars("quantile")
        return StepState(arrays=xr.Dataset(arrays))

    def transform(
        self, ds: xr.Dataset, s: WinsorizeSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        # never learns from ``ds``; skips absent variables (the target at score time)
        return ds.assign(
            {
                v: ds[v].clip(float(st.array(f"lower__{v}")), float(st.array(f"upper__{v}")))
                for v in s.vars
                if v in ds
            }
        )


STEPS = [WinsorizeStep]
"""The ``mapi.steps`` entry point: every step class listed here is registered."""
