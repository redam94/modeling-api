"""Declarative priors in the pymc-marketing ``Prior`` dict form (plan §6.1).

A spec writes::

    priors:
      beta_channel: {distribution: HalfNormal, sigma: 1.0, dims: [channel]}
      lam:          {distribution: Gamma, alpha: 3, beta: 1, dims: [channel]}

Only allow-listed distributions and their numeric parameters are accepted (no code, no
strings evaluated). ``dims`` use canonical dim names and must be a subset of the dims the
parameter slot supports; a parameter value may be a number or a list with one value per
label of the (single) dim it varies over. Every slot has a *support* (``real``,
``positive`` or ``unit``) and a prior whose support does not fit is rejected, so a
negative media effect or an adstock decay above one cannot be specified by accident.

The per-slot models (:class:`RealScalarPrior`, :class:`PositiveScalarPrior`,
:class:`PositiveChannelPrior`, :class:`UnitChannelPrior`) narrow the distribution and the
dims in their JSON Schema, so the control plane catches those mistakes statically; the
bounds of ``Uniform``/``TruncatedNormal`` priors are checked by :func:`resolve_priors`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "DISTRIBUTIONS",
    "PRIOR_SLOTS",
    "PositiveChannelPrior",
    "PositiveScalarPrior",
    "PriorSlot",
    "PriorSpec",
    "RealScalarPrior",
    "Support",
    "UnitChannelPrior",
    "build_prior",
    "resolve_priors",
]

Support = Literal["real", "positive", "unit"]

DistributionName = Literal[
    "Normal",
    "StudentT",
    "Laplace",
    "HalfNormal",
    "HalfStudentT",
    "HalfCauchy",
    "Exponential",
    "Gamma",
    "InverseGamma",
    "LogNormal",
    "Weibull",
    "Beta",
    "Uniform",
    "TruncatedNormal",
]

DISTRIBUTIONS: dict[str, tuple[frozenset[str], str]] = {
    # name: (allowed parameters, support)
    "Normal": (frozenset({"mu", "sigma"}), "real"),
    "StudentT": (frozenset({"nu", "mu", "sigma"}), "real"),
    "Laplace": (frozenset({"mu", "b"}), "real"),
    "HalfNormal": (frozenset({"sigma"}), "positive"),
    "HalfStudentT": (frozenset({"nu", "sigma"}), "positive"),
    "HalfCauchy": (frozenset({"beta"}), "positive"),
    "Exponential": (frozenset({"lam", "scale"}), "positive"),
    "Gamma": (frozenset({"alpha", "beta", "mu", "sigma"}), "positive"),
    "InverseGamma": (frozenset({"alpha", "beta", "mu", "sigma"}), "positive"),
    "LogNormal": (frozenset({"mu", "sigma"}), "positive"),
    "Weibull": (frozenset({"alpha", "beta"}), "positive"),
    "Beta": (frozenset({"alpha", "beta", "mu", "sigma"}), "unit"),
    "Uniform": (frozenset({"lower", "upper"}), "bounded"),
    "TruncatedNormal": (frozenset({"mu", "sigma", "lower", "upper"}), "bounded"),
}

_POSITIVE_PARAMS = frozenset({"sigma", "nu", "b", "beta", "lam", "scale", "alpha"})

ParamValue = float | list[float]


class PriorSpec(BaseModel):
    """One prior: ``{distribution, dims, **numeric parameters}``."""

    model_config = ConfigDict(extra="allow", frozen=True)

    distribution: DistributionName = Field(..., description="PyMC distribution name")
    dims: list[str] = Field(
        default_factory=list, max_length=2, description="Canonical dims the parameter varies over"
    )
    __pydantic_extra__: dict[str, ParamValue] = Field(init=False)

    @model_validator(mode="after")
    def _check(self) -> PriorSpec:
        allowed, support = DISTRIBUTIONS[self.distribution]
        params = self.params
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise ValueError(
                f"{self.distribution} does not take {unknown}; allowed: {sorted(allowed)}"
            )
        if not params and self.distribution not in ("HalfNormal", "HalfCauchy", "Normal"):
            raise ValueError(f"{self.distribution} needs parameters ({sorted(allowed)})")
        for name, value in params.items():
            values = value if isinstance(value, list) else [value]
            if not values:
                raise ValueError(f"parameter {name!r} is an empty list")
            if isinstance(value, list) and not self.dims:
                raise ValueError(f"parameter {name!r} is a list but the prior declares no dims")
            for v in values:
                if not math.isfinite(v):
                    raise ValueError(f"parameter {name!r} must be finite")
                if name in _POSITIVE_PARAMS and v <= 0:
                    raise ValueError(f"parameter {name!r} must be > 0")
        if support == "bounded":
            lo, hi = self.bounds
            if lo is not None and hi is not None and not lo < hi:
                raise ValueError("lower must be below upper")
            if self.distribution == "Uniform" and (lo is None or hi is None):
                raise ValueError("Uniform needs lower and upper")
        if len(set(self.dims)) != len(self.dims):
            raise ValueError("dims must be unique")
        return self

    @property
    def params(self) -> dict[str, ParamValue]:
        return dict(self.__pydantic_extra__ or {})

    @property
    def bounds(self) -> tuple[float | None, float | None]:
        p = self.params
        lo, hi = p.get("lower"), p.get("upper")
        if isinstance(lo, list) or isinstance(hi, list):
            lo_v = min(lo) if isinstance(lo, list) else lo
            hi_v = max(hi) if isinstance(hi, list) else hi
            return lo_v, hi_v
        return lo, hi

    def support(self) -> str:
        """``real``, ``positive``, ``unit`` - bounded priors map onto the tightest fitting."""
        _, support = DISTRIBUTIONS[self.distribution]
        if support != "bounded":
            return support
        lo, hi = self.bounds
        if lo is not None and hi is not None and lo >= 0 and hi <= 1:
            return "unit"
        if lo is not None and lo >= 0:
            return "positive"
        return "real"

    def fits(self, wanted: Support) -> bool:
        """Does this prior's support lie inside ``wanted``?"""
        got = self.support()
        order = {"unit": 0, "positive": 1, "real": 2}
        if wanted == "unit":
            return got == "unit"
        if wanted == "positive":
            # a unit-interval prior is positive too, but open at 0 only for Beta
            return got in ("unit", "positive")
        return order[got] <= order["real"]


PositiveDistribution = Literal[
    "HalfNormal",
    "HalfStudentT",
    "HalfCauchy",
    "Exponential",
    "Gamma",
    "InverseGamma",
    "LogNormal",
    "Weibull",
    "Beta",
    "Uniform",
    "TruncatedNormal",
]
UnitDistribution = Literal["Beta", "Uniform", "TruncatedNormal"]


# Narrower prior models per (support, dims): their JSON Schemas let the control plane
# reject a wrong distribution or dim statically. Bounded distributions (Uniform,
# TruncatedNormal) additionally have their bounds checked by resolve_priors().
class RealScalarPrior(PriorSpec):
    """A real-valued prior shared by every label (no dims)."""

    dims: list[str] = Field(default_factory=list, max_length=0)


class PositiveScalarPrior(PriorSpec):
    """A positive prior shared by every label (no dims)."""

    distribution: PositiveDistribution = Field(..., description="PyMC distribution name")
    dims: list[str] = Field(default_factory=list, max_length=0)


class PositiveChannelPrior(PriorSpec):
    """A positive prior, optionally one per channel (``dims: [channel]``)."""

    distribution: PositiveDistribution = Field(..., description="PyMC distribution name")
    dims: list[Literal["channel"]] = Field(default_factory=list, max_length=1)  # type: ignore[assignment]


class UnitChannelPrior(PriorSpec):
    """A prior on (0, 1), optionally one per channel (``dims: [channel]``)."""

    distribution: UnitDistribution = Field(..., description="PyMC distribution name")
    dims: list[Literal["channel"]] = Field(default_factory=list, max_length=1)  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class PriorSlot:
    """A model parameter whose prior a spec may override."""

    name: str
    support: Support
    dims: tuple[str, ...]
    default: PriorSpec
    description: str

    @property
    def model(self) -> type[PriorSpec]:
        """The prior model whose JSON Schema describes what this slot accepts."""
        if self.support == "real":
            return RealScalarPrior
        if self.support == "unit":
            return UnitChannelPrior
        return PositiveChannelPrior if self.dims else PositiveScalarPrior


def _p(distribution: str, dims: Sequence[str] = (), **params: float) -> PriorSpec:
    return PriorSpec.model_validate({"distribution": distribution, "dims": list(dims), **params})


PRIOR_SLOTS: dict[str, PriorSlot] = {
    s.name: s
    for s in (
        PriorSlot(
            "intercept",
            "real",
            (),
            _p("Normal", mu=0.5, sigma=0.5),
            "Population mean of the geo intercepts (model units: the target is mean-scaled)",
        ),
        PriorSlot(
            "intercept_sigma",
            "positive",
            (),
            _p("HalfNormal", sigma=0.25),
            "Spread of the geo intercepts around their population mean",
        ),
        PriorSlot(
            "beta_channel",
            "positive",
            ("channel",),
            _p("HalfNormal", ["channel"], sigma=1.0),
            "Typical (median across geos) effect of each channel at full saturation",
        ),
        PriorSlot(
            "beta_sigma",
            "positive",
            ("channel",),
            _p("HalfNormal", ["channel"], sigma=0.3),
            "Log-scale spread of a channel's effect across geos (partial pooling)",
        ),
        PriorSlot(
            "alpha",
            "unit",
            ("channel",),
            _p("Beta", ["channel"], alpha=1.0, beta=3.0),
            "Adstock decay per period",
        ),
        PriorSlot(
            "theta",
            "positive",
            ("channel",),
            _p("HalfNormal", ["channel"], sigma=2.0),
            "Delayed adstock: periods until the peak effect",
        ),
        PriorSlot(
            "lam",
            "positive",
            ("channel",),
            _p("Gamma", ["channel"], alpha=3.0, beta=1.0),
            "Logistic saturation rate (per unit of scaled spend)",
        ),
        PriorSlot(
            "slope",
            "positive",
            ("channel",),
            _p("Gamma", ["channel"], alpha=3.0, beta=2.0),
            "Hill saturation slope",
        ),
        PriorSlot(
            "half",
            "positive",
            ("channel",),
            _p("Gamma", ["channel"], alpha=2.0, beta=2.0),
            "Hill half-saturation point (scaled spend)",
        ),
        PriorSlot(
            "gamma_control",
            "real",
            (),
            _p("Normal", mu=0.0, sigma=0.5),
            "Coefficient of each control feature (seasonality, holidays, ...)",
        ),
        PriorSlot(
            "sigma",
            "positive",
            (),
            _p("HalfNormal", sigma=0.5),
            "Observation noise (model units)",
        ),
    )
}


def resolve_priors(overrides: Mapping[str, PriorSpec]) -> dict[str, PriorSpec]:
    """Defaults overlaid with the spec's priors; checks names, dims and support."""
    unknown = sorted(set(overrides) - set(PRIOR_SLOTS))
    if unknown:
        raise ValueError(f"unknown prior(s) {unknown}; known: {sorted(PRIOR_SLOTS)}")
    out: dict[str, PriorSpec] = {}
    for name, slot in PRIOR_SLOTS.items():
        spec = overrides.get(name, slot.default)
        bad_dims = sorted(set(spec.dims) - set(slot.dims))
        if bad_dims:
            raise ValueError(
                f"prior {name!r} may vary over {list(slot.dims) or 'no dims'}, not {bad_dims}"
            )
        if not spec.fits(slot.support):
            raise ValueError(
                f"prior {name!r} needs {slot.support} support; {spec.distribution} "
                f"has {spec.support()} support"
            )
        out[name] = spec
    return out


def build_prior(
    name: str,
    spec: PriorSpec,
    *,
    dims: Sequence[str] = (),
    iid_dims: Sequence[str] = (),
    coords: Mapping[str, Sequence[Any]] | None = None,
) -> Any:
    """Create the PyMC random variable ``name`` for ``spec``.

    ``dims`` are the dims the parameter slot supports: the variable gets those listed in
    ``spec.dims`` (a prior without them is one value shared across them - the caller
    broadcasts). ``iid_dims`` are always added in front (independent draws per label, e.g.
    one coefficient per control feature). A list-valued parameter varies along the last
    used dim and must have one value per label.
    """
    import pymc as pm

    used = [*iid_dims, *(d for d in dims if d in spec.dims)]
    along = [d for d in dims if d in spec.dims]
    kwargs: dict[str, Any] = {}
    for key, value in spec.params.items():
        if isinstance(value, list):
            if len(along) != 1:
                raise ValueError(
                    f"prior {name!r}: list-valued {key!r} needs exactly one dim, got {along}"
                )
            dim = along[0]
            if coords is not None and dim in coords and len(value) != len(coords[dim]):
                raise MapiError(
                    f"prior {name!r}: {key!r} has {len(value)} values but {dim!r} has "
                    f"{len(coords[dim])} labels",
                    code=ErrorCode.SETTINGS_INVALID,
                    pointer=f"/model/settings/priors/{name}/{key}",
                )
            kwargs[key] = np.asarray(value, dtype=np.float64)
        else:
            kwargs[key] = float(value)
    dist = getattr(pm, spec.distribution)
    return dist(name, **kwargs, dims=used or None)
