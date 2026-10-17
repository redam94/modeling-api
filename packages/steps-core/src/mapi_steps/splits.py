"""The five built-in train/holdout splits (plan §4.1 "Split" group).

Splits are not pipeline steps: a spec chooses one in ``data.split`` and the harness applies
it at the split stage (:func:`mapi_sdk.split.split_dataset`), after the stateless prefix.
This module gives each split kind a catalog identity (``core.<kind>@1.0.0``) with its
settings schema and plain-language summaries for pickers and docs, plus a direct
:meth:`SplitStrategy.split` for notebooks and tests.

========================  ====================================================================
``core.time_holdout``     hold out the last ``last`` periods / a ``fraction`` / after a label
``core.as_of``            train up to and including ``as_of``; optional ``gap`` periods
``core.leave_group_out``  hold out whole groups (explicit ``groups`` or ``n`` random ones)
``core.last_per_group``   last ``last`` rows of each group with at least ``min_len`` rows
``core.mask_block``       random blocks over ``dims`` covering ``fraction`` of the cells
========================  ====================================================================
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

import xarray as xr
from mapi_sdk.split import SplitResult, split_dataset
from mapi_spec.about import About
from mapi_spec.abouts import SPLIT_ABOUTS
from mapi_spec.schema import DataSchema
from mapi_spec.spec import (
    AsOfSplit,
    LastPerGroupSplit,
    LeaveGroupOutSplit,
    MaskBlockSplit,
    TimeHoldoutSplit,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue

__all__ = [
    "SPLITS",
    "SplitCatalogEntry",
    "SplitStrategy",
    "split_catalog",
    "split_strategy",
]

SplitModel = (
    type[TimeHoldoutSplit]
    | type[AsOfSplit]
    | type[LeaveGroupOutSplit]
    | type[LastPerGroupSplit]
    | type[MaskBlockSplit]
)


class SplitCatalogEntry(BaseModel):
    """Static description of one split kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    version: str
    kind: str
    settings_schema: dict[str, JsonValue] = Field(default_factory=dict)
    ordered: bool = Field(..., description="Cuts an ordered dim (forecast-style evaluation)")
    masks_cells: bool = Field(
        ..., description="Masks target cells instead of cutting coordinates (model must accept NaN)"
    )
    summary: str
    summary_plain: str
    about: About | None = Field(None, description="M3.5: the plain-language About card")

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


@dataclass(frozen=True, slots=True)
class SplitStrategy:
    """One split kind: its spec model, catalog metadata and a direct ``split`` call."""

    kind: str
    model: SplitModel
    ordered: bool
    masks_cells: bool
    summary: str
    summary_plain: str
    version: ClassVar[str] = "1.0.0"

    @property
    def id(self) -> str:
        return f"core.{self.kind}"

    def parse(self, settings: Mapping[str, Any] | BaseModel) -> Any:
        """Validate settings into the spec's split model (``kind`` may be omitted)."""
        if isinstance(settings, self.model):
            return settings
        raw = settings.model_dump() if isinstance(settings, BaseModel) else dict(settings)
        return self.model.model_validate({**raw, "kind": self.kind})

    def split(
        self,
        ds: xr.Dataset,
        settings: Mapping[str, Any] | BaseModel,
        *,
        schema: DataSchema | None = None,
        seed: int = 0,
    ) -> SplitResult:
        """Split ``ds`` (data at the split stage); see :func:`mapi_sdk.split.split_dataset`."""
        return split_dataset(ds, self.parse(settings), schema=schema, seed=seed)

    def catalog_entry(self) -> SplitCatalogEntry:
        schema = self.model.model_json_schema(by_alias=True)
        props = schema.get("properties", {})
        if isinstance(props, dict):
            props.pop("kind", None)
        req = schema.get("required")
        if isinstance(req, list) and "kind" in req:
            schema["required"] = [r for r in req if r != "kind"]
        return SplitCatalogEntry(
            id=self.id,
            version=self.version,
            kind=self.kind,
            settings_schema=schema,
            ordered=self.ordered,
            masks_cells=self.masks_cells,
            about=SPLIT_ABOUTS.get(self.id),
            summary=self.summary,
            summary_plain=self.summary_plain,
        )


SPLITS: tuple[SplitStrategy, ...] = (
    SplitStrategy(
        "time_holdout",
        TimeHoldoutSplit,
        ordered=True,
        masks_cells=False,
        summary="Hold out the end of an ordered dim (last N, a fraction, or after a label)",
        summary_plain="Tests the model on the most recent periods it has not seen",
    ),
    SplitStrategy(
        "as_of",
        AsOfSplit,
        ordered=True,
        masks_cells=False,
        summary="Train up to an as-of label, hold out the rest (optional gap)",
        summary_plain="Pretends it is a past date and checks what the model would have said",
    ),
    SplitStrategy(
        "leave_group_out",
        LeaveGroupOutSplit,
        ordered=False,
        masks_cells=False,
        summary="Hold out whole groups (explicit or random)",
        summary_plain="Checks how well the model handles markets or stores it has never seen",
    ),
    SplitStrategy(
        "last_per_group",
        LastPerGroupSplit,
        ordered=True,
        masks_cells=True,
        summary="Hold out the last rows of each group with at least min_len rows",
        summary_plain="Tests each customer or store on its latest observations",
    ),
    SplitStrategy(
        "mask_block",
        MaskBlockSplit,
        ordered=False,
        masks_cells=True,
        summary="Hold out random blocks of cells over several dims",
        summary_plain="Hides random patches of the data and checks the model fills them in",
    ),
)


def split_strategy(kind_or_id: str) -> SplitStrategy:
    """The strategy for ``time_holdout`` or ``core.time_holdout`` (``KeyError`` if unknown)."""
    key = kind_or_id.removeprefix("core.").split("@", 1)[0]
    for s in SPLITS:
        if s.kind == key:
            return s
    raise KeyError(kind_or_id)


def split_catalog() -> list[SplitCatalogEntry]:
    """Catalog entries for every split kind."""
    return [s.catalog_entry() for s in SPLITS]
