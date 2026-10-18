"""Budgeted refits of a fitted model (governance checks and explorer backtests, M3.5).

A :class:`Refitter` rebuilds the bundle's spec with changes (no split, another split, a
cheaper fit for Bayesian models, extra evaluation metrics, other settings) and fits it with
the SDK harness on modified data. Every refit counts against the
:class:`~mapi_spec.governance.RefitBudget`; :class:`BudgetExhausted` stops a check early.

Refits are always ``train_only`` (no refit on the holdout) and skip plots, UI draws and
derived quantities unless asked for: they exist to measure, not to publish.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import FitHooks, fit_spec
from mapi_sdk.registry import Registry
from mapi_spec.governance import RefitBudget
from mapi_spec.schema import DataSchema
from mapi_spec.spec import ModelSpec

from mapi_runner.worker.modelview import bundle_kind

__all__ = ["KEEP", "BudgetExhausted", "Refitter"]

KEEP: Final = object()
"""Sentinel: keep the bundle's own value (e.g. its ``data.split``)."""


class BudgetExhausted(RuntimeError):
    """The run's refit budget is used up."""


@dataclass
class Refitter:
    """Refits of ``bundle`` under ``budget`` (see the module docstring)."""

    bundle: Bundle
    registry: Registry
    budget: RefitBudget = field(default_factory=RefitBudget)
    side_tables: Mapping[str, xr.Dataset] = field(default_factory=dict)
    cancel: Any = None
    on_refit: Callable[[int], None] | None = None
    used: int = 0
    tree: xr.DataTree | None = None
    """M4: the tree dataset the refit data was selected from (for tree-input models)."""

    @property
    def remaining(self) -> int:
        return max(0, self.budget.max_refits - self.used)

    def spec(
        self,
        *,
        split: Any = KEEP,
        metrics: Sequence[str] = (),
        settings: Mapping[str, Any] | None = None,
        keep_derived: bool = False,
        seed: int | None = None,
    ) -> ModelSpec:
        """The bundle's spec for a refit: ``split`` (a split document, ``None`` for none,
        :data:`KEEP`), extra ``metrics``, merged model ``settings``; Bayesian fits are capped
        at the budget's draws/tune/chains (and its engine)."""
        doc: dict[str, Any] = copy.deepcopy(
            self.bundle.spec.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
        if split is not KEEP:
            data = doc.setdefault("data", {})
            if split is None:
                data.pop("split", None)
            else:
                data["split"] = split
        fit = doc.setdefault("model", {}).setdefault("fit", {})
        fit["mode"] = "train_only"
        fit.pop("checkpoint", None)
        if seed is not None:
            fit["seed"] = seed
        if bundle_kind(self.bundle) == "bayesian":
            b = self.budget
            fit["draws"] = min(int(fit.get("draws") or b.draws), b.draws)
            fit["tune"] = min(int(fit.get("tune") or b.tune), b.tune)
            fit["chains"] = min(int(fit.get("chains") or b.chains), b.chains)
            if b.engine:
                fit["engine"] = b.engine
        if settings:
            model = doc["model"]
            model["settings"] = {**(model.get("settings") or {}), **dict(settings)}
        ev = doc.setdefault("evaluate", {})
        have = list(ev.get("metrics") or [])
        ev["metrics"] = list(dict.fromkeys([*have, *metrics])) or ["rmse"]
        outputs = doc.setdefault("outputs", {})
        outputs["plots"] = []
        outputs["ui_draws"] = 0
        if not keep_derived:
            outputs["derived"] = []
        return ModelSpec.model_validate(doc)

    def fit(
        self,
        data: xr.Dataset,
        spec: ModelSpec | None = None,
        *,
        raw_schema: DataSchema | None = None,
    ) -> Bundle:
        """One refit (counted against the budget)."""
        if self.used >= self.budget.max_refits:
            raise BudgetExhausted(
                f"the run's refit budget ({self.budget.max_refits} refits) is used up"
            )
        if self.cancel is not None:
            self.cancel.check()
        self.used += 1
        if self.on_refit is not None:
            self.on_refit(self.used)
        return fit_spec(
            spec or self.spec(),
            data,
            self.side_tables,
            self.registry,
            raw_schema=raw_schema or self.bundle.raw_schema,
            hooks=FitHooks(cancel=self.cancel),
            keep_evaluation_bundle=False,
            hash_data=False,
            tree=self.tree,
        )
