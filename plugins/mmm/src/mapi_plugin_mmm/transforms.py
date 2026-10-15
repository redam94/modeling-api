"""Carry-over (adstock) and saturation curves shared by the PyMC graph and NumPy.

The same functions build the model graph (``xp=pytensor.tensor``) and compute the derived
quantities from posterior draws (``xp=numpy``), so response curves, marginal costs and the
decision objective use exactly the curves the model was fitted with.

Parameters must already broadcast against the inputs: per-channel parameters are passed
with trailing singleton axes where needed (the callers in this package do that).

* adstock weights over lags ``0 .. l_max-1``:

  - ``geometric``: ``alpha ** l``
  - ``delayed``: ``alpha ** ((l - theta) ** 2)`` (peak effect after ``theta`` periods)

  optionally normalised to sum to one, so a constant spend ``s`` has steady-state
  adstock ``s``;
* saturation of adstocked spend ``x``:

  - ``logistic``: ``(1 - exp(-lam x)) / (1 + exp(-lam x))`` (pymc-marketing's logistic)
  - ``hill``: ``x**slope / (x**slope + half**slope)``
  - ``none``: ``x`` (linear response).
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np

__all__ = [
    "ADSTOCK_KINDS",
    "SATURATION_KINDS",
    "AdstockKind",
    "SaturationKind",
    "adstock_weights",
    "saturate",
    "saturation_params",
]

AdstockKind = Literal["geometric", "delayed"]
SaturationKind = Literal["logistic", "hill", "none"]
ADSTOCK_KINDS: tuple[str, ...] = ("geometric", "delayed")
SATURATION_KINDS: tuple[str, ...] = ("logistic", "hill", "none")

_HILL_EPS = 1e-12


def adstock_weights(
    kind: AdstockKind,
    lags: np.ndarray,
    *,
    alpha: Any,
    theta: Any = None,
    normalize: bool = True,
    xp: Any = np,
) -> Any:
    """Carry-over weights; ``alpha``/``theta`` broadcast against ``lags`` (last axis)."""
    lags_f = np.asarray(lags, dtype=np.float64)
    if kind == "geometric":
        w = alpha**lags_f
    elif kind == "delayed":
        if theta is None:
            raise ValueError("delayed adstock needs theta")
        w = alpha ** ((lags_f - theta) ** 2)
    else:  # pragma: no cover - guarded by the settings model
        raise ValueError(f"unknown adstock kind {kind!r}")
    if normalize:
        w = w / w.sum(axis=-1, keepdims=True)
    return w


def saturate(
    x: Any,
    kind: SaturationKind,
    *,
    lam: Any = None,
    slope: Any = None,
    half: Any = None,
    xp: Any = np,
) -> Any:
    """Apply the saturation curve ``kind`` to non-negative adstocked spend ``x``."""
    if kind == "logistic":
        e = xp.exp(-lam * x)
        return (1.0 - e) / (1.0 + e)
    if kind == "hill":
        # x**slope with x == 0 is fine for slope > 0; the epsilon keeps gradients finite
        xs = (x + _HILL_EPS) ** slope
        return xs / (xs + half**slope)
    if kind == "none":
        return x
    raise ValueError(f"unknown saturation kind {kind!r}")  # pragma: no cover


def saturation_params(kind: SaturationKind) -> tuple[str, ...]:
    """Names of the (per-channel) parameters a saturation curve uses."""
    return {"logistic": ("lam",), "hill": ("slope", "half"), "none": ()}[kind]
