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
from mapi_spec.tree import NodePath
from mapi_spec.views import (
    DATA_SECTIONS,
    MAX_EXPLORE_LAGS,
    DataSection,
    ModelSection,
)


def _all_data_sections() -> list[DataSection]:
    return list(get_args(DataSection))


def _all_model_sections() -> list[ModelSection]:
    return list(get_args(ModelSection))


def _comparison_only() -> list[ModelSection]:
    return ["comparison"]


__all__ = [
    "DataExploreRequest",
    "ExploreJob",
    "ModelCompareRequest",
    "ModelExploreRequest",
    "StructureHints",
]


AngleUnits = Literal["deg", "rad", "h", "d", "month"]


class StructureHints(StrictModel):
    """Corrections to the structure the Data Explorer detects (each one overrides the rule
    for that dim or variable only): the dims of an image (``y_dim``, ``x_dim``,
    ``frame_dim``), the node dim of a network or tree, the time dim, and variables that are
    angles or times of day with their units (``{"wind_from": "deg"}``)."""

    y_dim: Name | None = None
    x_dim: Name | None = None
    frame_dim: Name | None = None
    node_dim: Name | None = None
    time_dim: Name | None = None
    angle_vars: dict[Name, AngleUnits] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def _pair(self) -> StructureHints:
        if (self.y_dim is None) != (self.x_dim is None):
            raise ValueError("give both y_dim and x_dim, or neither")
        if self.y_dim is not None and self.y_dim == self.x_dim:
            raise ValueError("y_dim and x_dim must differ")
        return self


class DataExploreRequest(RequestModel):
    """Which Data Explorer sections to compute, and how.

    ``kpi`` is the result the cross-correlations are computed against (default: the
    dataset's target variable); ``facet_by`` splits series by up to two dims; ``period``
    is the season length for STL and seasonal strength (default: from the frequency).
    """

    sections: list[DataSection] = Field(
        default_factory=_all_data_sections, min_length=1, max_length=len(DATA_SECTIONS)
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
    prewhiten: bool = Field(
        True,
        description="Prewhiten before cross-correlations (filter both series with the "
        "driver's AR model, so a peak is not just two smooth series moving together)",
    )
    max_series: int = Field(50, ge=1, le=200)
    node: NodePath | None = Field(
        None,
        description="M4: the node of a tree dataset to explore (default: the root, or the "
        "first node with variables)",
        exclude_if=lambda v: v is None,
    )
    hints: StructureHints | None = Field(
        None,
        description="Corrections to the detected structure (image dims, node dim, angle variables)",
        exclude_if=lambda v: v is None,
    )

    model_config = dto_examples(
        {
            "sections": ["overview", "timeseries", "stationarity", "ccf", "stl", "quality"],
            "kpi": "revenue",
            "facet_by": ["geo"],
            "max_lag": 12,
        },
        {
            "sections": ["structure", "images", "circular"],
            "hints": {"y_dim": "row", "x_dim": "col", "angle_vars": {"wind_from": "deg"}},
        },
    )

    @model_validator(mode="after")
    def _unique(self) -> DataExploreRequest:
        if len(set(self.sections)) != len(self.sections):
            raise ValueError("sections must be unique")
        hinted = self.hints.time_dim if self.hints is not None else None
        if hinted is not None and self.time_dim is not None and self.time_dim != hinted:
            raise ValueError("time_dim and hints.time_dim disagree")
        return self


class ModelExploreRequest(RequestModel):
    """Which Model Explorer sections to compute. ``compare_with`` adds other model versions
    (``model://…``) to the comparison section; ``backtest`` runs a split plan for the
    backtest section (fits run as child runs)."""

    sections: list[ModelSection] = Field(
        default_factory=_all_model_sections, min_length=1, max_length=12
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


class ModelCompareRequest(RequestModel):
    """``POST /v1/models:compare``: several model versions of the project side by side.

    Starts an ``explore`` run on the first version with the others as ``compare_with`` and
    the ``comparison`` section (plus ``backtest`` when a split plan is given): a metrics
    table, a ranking per metric and pairwise Diebold-Mariano tests on the shared holdout.
    """

    models: list[str] = Field(..., min_length=2, max_length=11, description="model:// refs")
    sections: list[ModelSection] = Field(
        default_factory=_comparison_only, min_length=1, max_length=12
    )
    backtest: SplitPlan | None = None
    horizon: int | None = Field(None, ge=1, le=520)

    model_config = dto_examples(
        {
            "models": ["model://growth/sales-arimax@v3", "model://growth/sales-ets@v1"],
            "sections": ["comparison", "params"],
        }
    )

    @model_validator(mode="after")
    def _refs(self) -> ModelCompareRequest:
        from mapi_spec.refs import ModelVersionRef

        for ref in self.models:
            ModelVersionRef.parse(ref)
        if len(set(self.models)) != len(self.models):
            raise ValueError("compare each model version once")
        if "comparison" not in self.sections:
            raise ValueError("a comparison needs the comparison section")
        if len(set(self.sections)) != len(self.sections):
            raise ValueError("sections must be unique")
        return self

    def explore_request(self) -> ModelExploreRequest:
        """The :class:`ModelExploreRequest` of the first version."""
        return ModelExploreRequest(
            sections=list(self.sections),
            compare_with=list(self.models[1:]),
            backtest=self.backtest,
            horizon=self.horizon,
        )


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
