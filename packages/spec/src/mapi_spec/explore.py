"""Requests for the Data Explorer and the Model Explorer (M3.5).

``POST /v1/datasets/{dataset_id}/versions/{version}/explore`` (a :class:`DataExploreRequest`)
and ``POST /v1/models/{model_id}/versions/{version}/explore`` (a
:class:`ModelExploreRequest`) start an ``explore`` run: the sandbox decodes the data (or
the bundle), computes the requested sections and writes the views of
:mod:`mapi_spec.views` under ``ui/explore/``. Results are cached per (target, request
hash), so asking again is instant. :class:`ExploreJob` is the run's ``RunSpec.spec``.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import Field, model_validator

from mapi_spec._dto import RequestModel, dto_examples
from mapi_spec._types import Name, StrictModel
from mapi_spec.splitplan import SplitPlan
from mapi_spec.views import (
    MAX_EXPLORE_LAGS,
    DataSection,
    ModelSection,
)


def _all_data_sections() -> list[DataSection]:
    return list(get_args(DataSection))


def _all_model_sections() -> list[ModelSection]:
    return list(get_args(ModelSection))


__all__ = [
    "DataExploreRequest",
    "ExploreJob",
    "ModelExploreRequest",
]


class DataExploreRequest(RequestModel):
    """Which Data Explorer sections to compute, and how.

    ``kpi`` is the result the cross-correlations are computed against (default: the
    dataset's target variable); ``facet_by`` splits series by up to two dims; ``period``
    is the season length for STL and seasonal strength (default: from the frequency).
    """

    sections: list[DataSection] = Field(
        default_factory=_all_data_sections, min_length=1, max_length=13
    )
    vars: list[Name] = Field(default_factory=list, max_length=256, description="Default: all")
    kpi: Name | None = None
    time_dim: Name | None = Field(None, description="Default: the dataset's time dim")
    facet_by: list[Name] = Field(default_factory=list, max_length=2)
    group_by: Name | None = Field(None, description="Dim for group comparisons")
    max_lag: int = Field(26, ge=1, le=MAX_EXPLORE_LAGS)
    period: int | None = Field(None, ge=2, le=400)
    alpha: float = Field(0.05, gt=0, lt=0.5)
    correlation: Literal["pearson", "spearman"] = "pearson"
    outliers: Literal["iqr", "mad", "zscore"] = "mad"
    outlier_threshold: float = Field(3.5, gt=0, le=20)
    prewhiten: bool = Field(False, description="Prewhiten before cross-correlations")
    max_series: int = Field(50, ge=1, le=200)

    model_config = dto_examples(
        {
            "sections": ["overview", "timeseries", "stationarity", "ccf", "stl", "quality"],
            "kpi": "revenue",
            "facet_by": ["geo"],
            "max_lag": 12,
        }
    )

    @model_validator(mode="after")
    def _unique(self) -> DataExploreRequest:
        if len(set(self.sections)) != len(self.sections):
            raise ValueError("sections must be unique")
        return self


class ModelExploreRequest(RequestModel):
    """Which Model Explorer sections to compute. ``compare_with`` adds other model versions
    (``model://…``) to the comparison section; ``backtest`` runs a split plan for the
    backtest section (fits run as child runs)."""

    sections: list[ModelSection] = Field(
        default_factory=_all_model_sections, min_length=1, max_length=11
    )
    compare_with: list[str] = Field(default_factory=list, max_length=10)
    backtest: SplitPlan | None = None
    horizon: int | None = Field(None, ge=1, le=520, description="Forecast/IRF horizon")
    interval: Literal["hdi_90", "hdi_95", "ci_90", "ci_95"] = "hdi_90"
    max_params: int = Field(500, ge=1, le=5000)

    model_config = dto_examples(
        {
            "sections": ["params", "fit", "residuals", "irf", "fevd", "comparison"],
            "compare_with": ["model://growth/sales-arimax@v2"],
            "horizon": 12,
        }
    )

    @model_validator(mode="after")
    def _refs(self) -> ModelExploreRequest:
        from mapi_spec.refs import ModelVersionRef

        for ref in self.compare_with:
            ModelVersionRef.parse(ref)
        if len(set(self.sections)) != len(self.sections):
            raise ValueError("sections must be unique")
        return self


class ExploreJob(StrictModel):
    """``RunSpec.spec`` of an ``explore`` run. Inputs: ``dataset`` (and ``schema``) for a
    dataset; ``bundle`` and ``bundle.manifest`` (plus ``compare.<k>`` bundles) for a model."""

    target: Literal["dataset", "model"]
    ref: str = Field(..., max_length=512, description="dataset://… or model://…")
    data: DataExploreRequest | None = None
    model: ModelExploreRequest | None = None

    @model_validator(mode="after")
    def _one(self) -> ExploreJob:
        if (self.target == "dataset") != (self.data is not None) or (self.target == "model") != (
            self.model is not None
        ):
            raise ValueError("a dataset job carries data options, a model job model options")
        return self
