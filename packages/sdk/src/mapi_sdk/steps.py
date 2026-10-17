"""The preprocessing step contract: ``Step``, ``StepState`` and ``StepContext``.

A step is a class with static metadata (``id``, ``version``, ``kind``, a Pydantic
``Settings`` model, an optional ``State`` model) and four methods::

    fit(ds, settings, ctx) -> State           # learn on train data only
    transform(ds, settings, state, ctx) -> ds # never learns; must tolerate an absent target
    inverse(ds, settings, state, ctx) -> ds   # invertible VALUE steps only

Rules the contract kit (:mod:`mapi_sdk.testing`) enforces:

* ``transform(train, fit(train))`` equals the fit-time output exactly, and transform is
  deterministic and does not mutate its input;
* reductions name the dims they reduce *over*; the state stays labelled by the rest;
* group-local operations take ``along``/``within`` and declare a :class:`ContextNeed`;
* unseen levels go through :meth:`StepContext.align_state`;
* the static :class:`~mapi_spec.effects.SchemaEffect` (``effect_template``) matches the real
  output schema.

The static metadata is exported as a :class:`~mapi_spec.catalog.StepCatalogEntry`, so the
control plane validates specs without importing step code.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

import numpy as np
import xarray as xr
from mapi_spec.about import About
from mapi_spec.abouts import SPLIT_ABOUTS, STEP_ABOUTS
from mapi_spec.catalog import StepCatalogEntry
from mapi_spec.effects import (
    ContextNeed,
    SchemaEffect,
    StepKind,
    resolve_context,
    resolve_effect,
)
from mapi_spec.errors import CoordPlanError, ErrorCode, MapiError
from mapi_spec.schema import DataSchema, DimPolicy
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from mapi_sdk._util import as_list, json_hash, py_scalar
from mapi_sdk.errors import RunCancelled, StepError

__all__ = [
    "CancelToken",
    "Step",
    "StepContext",
    "StepSettings",
    "StepState",
    "UnseenPolicy",
    "states_equal",
]

UnseenPolicy = Literal["error", "pooled", "fill"]


class StepSettings(BaseModel):
    """Base for step settings: unknown keys are rejected and instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class StepState(BaseModel):
    """Learned state of one step: JSON (``learned`` and subclass fields) plus arrays.

    ``arrays`` holds labelled arrays (e.g. ``scale(market, channel)``) and ``context`` the
    per-group tail of the step's *input stage* recorded at fit time. Subclass fields must
    be JSON-serialisable; arrays must use persistable dtypes. Nothing is ever pickled.
    """

    model_config = ConfigDict(
        extra="forbid", arbitrary_types_allowed=True, validate_assignment=False
    )

    step_id: str = ""
    step_type: str = ""
    step_version: str = ""
    settings_hash: str = ""
    learned: dict[str, JsonValue] = Field(default_factory=dict)
    arrays: xr.Dataset | None = None
    context: xr.Dataset | None = None
    context_need: ContextNeed | None = None

    ARRAY_FIELDS: ClassVar[tuple[str, ...]] = ("arrays", "context")

    def json_payload(self) -> dict[str, Any]:
        """Every JSON field (including subclass fields), without the arrays."""
        return self.model_dump(mode="json", exclude=set(self.ARRAY_FIELDS))

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        arrays: xr.Dataset | None = None,
        context: xr.Dataset | None = None,
    ) -> StepState:
        try:
            return cls.model_validate({**payload, "arrays": arrays, "context": context})
        except ValidationError as exc:
            raise StepError(f"stored state does not match {cls.__name__}: {exc}") from None

    def array(self, name: str) -> xr.DataArray:
        """A named array from ``arrays`` (``StepError`` if absent)."""
        if self.arrays is None or name not in self.arrays:
            raise StepError(f"step state has no array {name!r}")
        return self.arrays[name]


def states_equal(a: StepState, b: StepState) -> bool:
    """Exact equality of two states (JSON fields and labelled arrays)."""
    if type(a) is not type(b) or a.json_payload() != b.json_payload():
        return False
    for f in a.ARRAY_FIELDS:
        x, y = getattr(a, f), getattr(b, f)
        if (x is None) != (y is None):
            return False
        if x is not None and not x.identical(y):
            return False
    return True


class CancelToken:
    """Cooperative cancellation flag shared between an executor and a running job."""

    __slots__ = ("_cancelled", "reason")

    def __init__(self) -> None:
        self._cancelled = False
        self.reason = ""

    def cancel(self, reason: str = "cancelled") -> None:
        self._cancelled = True
        self.reason = reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def check(self) -> None:
        if self._cancelled:
            raise RunCancelled(f"run was cancelled: {self.reason}")


@dataclass(slots=True)
class StepContext:
    """What a step may use besides its data, settings and state."""

    step_id: str
    phase: Literal["fit", "transform", "score", "inverse"] = "fit"
    schema: DataSchema | None = None
    side_tables: Mapping[str, xr.Dataset] = field(default_factory=dict)
    seed: int = 0
    log: Callable[[str], None] | None = None
    cancel: CancelToken | None = None
    rng: np.random.Generator = field(init=False)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)

    def side_table(self, name: str) -> xr.Dataset:
        """A side table by its name in ``data.side_tables`` (``SIDE_TABLE_COVERAGE`` if absent)."""
        try:
            return self.side_tables[name]
        except KeyError:
            raise MapiError(
                f"step {self.step_id!r} needs side table {name!r}, but the run did not get it",
                code=ErrorCode.SIDE_TABLE_COVERAGE,
            ) from None

    def policy_for(self, dim: str) -> DimPolicy | None:
        if self.schema is None or dim not in self.schema.dims:
            return None
        return self.schema.dims[dim].policy

    def info(self, message: str) -> None:
        if self.log is not None:
            self.log(message)

    def check_cancelled(self) -> None:
        if self.cancel is not None:
            self.cancel.check()

    def align_state(
        self,
        state: xr.DataArray,
        ds: xr.Dataset | xr.DataArray,
        policy: UnseenPolicy = "error",
        *,
        pooled: xr.DataArray | float | None = None,
        fill_value: float = np.nan,
        dims: Sequence[str] | None = None,
    ) -> xr.DataArray:
        """Reindex a labelled state array to the coords of ``ds``, handling unseen levels.

        ``error`` raises ``UNSEEN_LEVELS``; ``pooled`` uses ``pooled`` (default: the state's
        mean over the dim) for new levels; ``fill`` uses ``fill_value``. Levels the state
        knows but ``ds`` lacks are dropped, so the result aligns with ``ds`` by label.
        """
        target_dims = [str(d) for d in (dims if dims is not None else state.dims)]
        out = state
        for d in target_dims:
            if d not in ds.dims or d not in ds.coords or d not in out.coords:
                continue
            want = np.asarray(ds[d].values)
            known = np.asarray(out[d].values)
            unseen = want[~np.isin(want, known)]
            if unseen.size:
                if policy == "error":
                    preview = ", ".join(str(py_scalar(v)) for v in unseen[:10])
                    raise CoordPlanError(
                        f"step {self.step_id!r}: the fit did not see {unseen.size} level(s) of "
                        f"{d!r}, "
                        "and its unseen-level policy is 'error'",
                        code=ErrorCode.UNSEEN_LEVELS,
                        untrusted_text=preview,
                    )
                if policy == "pooled":
                    fallback: xr.DataArray | float = (
                        out.mean(d, skipna=True) if pooled is None else pooled
                    )
                    reindexed = out.reindex({d: want})
                    is_new = xr.DataArray(~np.isin(want, known), dims=[d], coords={d: want})
                    out = reindexed.where(~is_new, fallback).transpose(*reindexed.dims)
                else:
                    out = out.reindex({d: want}, fill_value=fill_value)
            else:
                out = out.sel({d: want})
        return out


class Step[SettingsT: StepSettings, StateT: StepState]:
    """Base class for preprocessing steps. See the module docstring for the contract."""

    id: ClassVar[str]
    version: ClassVar[str]
    kind: ClassVar[StepKind]
    Settings: ClassVar[type[StepSettings]] = StepSettings
    State: ClassVar[type[StepState] | None] = None
    invertible: ClassVar[bool] = False
    linear: ClassVar[bool] = True
    """Whether the inverse is affine (point retransformation is exact for means)."""
    additive: ClassVar[bool] = False
    """Whether the inverse is linear *without an offset* (``inv(a + b) = inv(a) + inv(b)``,
    e.g. multiplying by a scale or a rate). Only then do additive decompositions
    (``contributions(component, ...)``) invert component by component."""
    depends_on_target: ClassVar[bool] = False
    target_vars_param: ClassVar[str | None] = None
    var_params: ClassVar[tuple[str, ...]] = ()
    dim_params: ClassVar[tuple[str, ...]] = ()
    side_table_params: ClassVar[tuple[str, ...]] = ()
    provides: ClassVar[tuple[Literal["scaling", "carryover"], ...]] = ()
    """Preprocessing a model can own (``Capabilities.owns``): scaling or carry-over."""
    unseen_levels_param: ClassVar[str | None] = None
    effect_template: ClassVar[Mapping[str, JsonValue]] = {}
    context_template: ClassVar[Mapping[str, JsonValue] | None] = None
    summary: ClassVar[str | None] = None
    summary_plain: ClassVar[str | None] = None
    about: ClassVar[About | None] = None
    """M3.5: the plain-language About card (``mapi_spec.about.About``). Built-in ``core.*``
    steps take theirs from :mod:`mapi_spec.abouts` when this is unset."""

    # ------------------------------------------------------------------ metadata
    @classmethod
    def stateful(cls) -> bool:
        return cls.State is not None

    @classmethod
    def ref(cls) -> str:
        return f"{cls.id}@{cls.version}"

    @classmethod
    def settings_schema(cls) -> dict[str, Any]:
        return cls.Settings.model_json_schema(by_alias=True)

    @classmethod
    def about_card(cls) -> About | None:
        """The step's About card: ``about``, else the built-in card of its id."""
        if cls.about is not None:
            return cls.about
        return STEP_ABOUTS.get(cls.id) or SPLIT_ABOUTS.get(cls.id)

    @classmethod
    def catalog_entry(cls) -> StepCatalogEntry:
        """Static JSON describing this step (no data, no code) for the control plane."""
        return StepCatalogEntry(
            id=cls.id,
            version=cls.version,
            kind=cls.kind,
            stateful=cls.stateful(),
            invertible=cls.invertible,
            depends_on_target=cls.depends_on_target,
            target_vars_param=cls.target_vars_param,
            settings_schema=cls.settings_schema(),
            effect=dict(cls.effect_template),
            context=None if cls.context_template is None else dict(cls.context_template),
            var_params=list(cls.var_params),
            dim_params=list(cls.dim_params),
            side_table_params=list(cls.side_table_params),
            provides=list(cls.provides),
            summary=cls.summary,
            summary_plain=cls.summary_plain,
            about=cls.about_card(),
        )

    # ------------------------------------------------------------------ settings
    def parse_settings(self, raw: Mapping[str, Any] | StepSettings | None) -> SettingsT:
        if isinstance(raw, self.Settings):
            return raw  # type: ignore[return-value]
        try:
            return self.Settings.model_validate(dict(raw or {}))  # type: ignore[return-value]
        except ValidationError as exc:
            raise MapiError(
                f"invalid settings for step {self.id!r}",
                code=ErrorCode.SETTINGS_INVALID,
                untrusted_text=str(exc)[:4000],
            ) from None

    def settings_json(self, s: StepSettings) -> dict[str, Any]:
        out: dict[str, Any] = s.model_dump(mode="json", by_alias=True)
        return out

    def settings_hash(self, s: StepSettings) -> str:
        return json_hash({"step": self.ref(), "settings": self.settings_json(s)})

    def effect(self, s: SettingsT) -> SchemaEffect:
        """The schema effect for concrete settings (default: render ``effect_template``)."""
        return resolve_effect(dict(self.effect_template), self.settings_json(s))

    def context(self, s: SettingsT) -> ContextNeed | None:
        """Per-group tail needed at score time (default: render ``context_template``)."""
        if self.context_template is None:
            return None
        return resolve_context(dict(self.context_template), self.settings_json(s))

    def touched_vars(self, s: SettingsT) -> set[str]:
        """Variables whose values this step changes (for the inverse chain)."""
        settings = self.settings_json(s)
        names: set[str] = set()
        for p in self.var_params:
            names.update(str(v) for v in as_list(settings.get(p)))
        try:
            eff = self.effect(s)
        except (ValueError, TypeError):
            return names
        names.update(eff.updates)
        names.update(eff.renames)
        return names

    # ------------------------------------------------------------------ behaviour
    def new_state(self, **kw: Any) -> StateT:
        cls = self.State or StepState
        return cls(**kw)  # type: ignore[return-value]

    def fit(self, ds: xr.Dataset, s: SettingsT, ctx: StepContext) -> StateT:
        """Learn the state on training data. Stateless steps keep the default."""
        return self.new_state()

    def transform(self, ds: xr.Dataset, s: SettingsT, st: StateT, ctx: StepContext) -> xr.Dataset:
        raise NotImplementedError(f"step {self.id!r} does not implement transform")

    def inverse(self, ds: xr.Dataset, s: SettingsT, st: StateT, ctx: StepContext) -> xr.Dataset:
        raise StepError(f"step {self.id!r} is not invertible")
