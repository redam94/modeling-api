"""Contract checks specific to PyMC plugins (complementing ``mapi_sdk.testing.check_model``).

:func:`check_fast_score` is the plan's "fast ≈ exact within MCSE" check: the compiled
:class:`~mapi_pymc.frozen.FrozenPredictor` must agree with the exact rebuild-and-predict
score path, cell by cell, within the Monte Carlo standard error of both estimates.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import xarray as xr
from mapi_sdk.models import Fitted, ScoreContext
from mapi_spec.spec import ScoreRequest

from mapi_pymc.model import PyMCModel

__all__ = ["FastScoreReport", "check_fast_score"]


@dataclass(slots=True)
class FastScoreReport:
    """Per-output worst standardised difference ``|fast - exact| / MCSE``."""

    max_z: dict[str, float] = field(default_factory=dict)
    n_sigma: float = 4.0
    samples: int = 0

    @property
    def ok(self) -> bool:
        return all(z <= self.n_sigma for z in self.max_z.values())

    def raise_for_failure(self) -> None:
        bad = {k: round(v, 2) for k, v in self.max_z.items() if v > self.n_sigma}
        if bad:
            raise AssertionError(
                f"fast scoring disagrees with exact scoring beyond {self.n_sigma} MCSE: {bad}"
            )


def _request() -> ScoreRequest:
    return ScoreRequest.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": "endpoint://contract/fast",
            "mode": "predict",
            "output": {"kind": "expected"},
        }
    )


def _mcse_mean(draws: np.ndarray) -> np.ndarray:
    """MCSE of the mean along axis 0 for ``(chain, draw, ...)`` draws (ArviZ when possible)."""
    flat = draws.reshape(draws.shape[0] * draws.shape[1], -1)
    sd = flat.std(axis=0, ddof=1)
    try:
        import arviz as az

        da = xr.DataArray(
            draws.reshape(draws.shape[0], draws.shape[1], -1), dims=["chain", "draw", "cell"]
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ess = np.asarray(az.ess(xr.Dataset({"v": da}))["v"].values, dtype=float)
        ess = np.where(np.isfinite(ess) & (ess > 0), ess, flat.shape[0])
        return np.asarray(sd / np.sqrt(ess)).reshape(draws.shape[2:])
    except Exception:  # pragma: no cover - ArviZ is installed alongside PyMC
        return np.asarray(sd / np.sqrt(flat.shape[0])).reshape(draws.shape[2:])


def check_fast_score(
    plugin: PyMCModel[Any],
    fitted: Fitted,
    ds: xr.Dataset,
    *,
    inputs: Mapping[str, Any] | None = None,
    num_samples: int | None = None,
    seed: int = 0,
    n_sigma: float = 4.0,
    raise_on_failure: bool = True,
) -> FastScoreReport:
    """Compare the plugin's frozen predictor with its exact ``expected`` score on ``ds``.

    ``inputs`` default to ``ds[name]`` for each of the spec's ``input_vars``; the frozen
    predictor's reducer is ignored (raw samples are compared).
    """
    spec = plugin.fast_score_spec
    if spec is None:
        raise AssertionError(f"model {plugin.id!r} declares no fast_score_spec")
    exact_tree = plugin.score(
        fitted, ds, _request(), ScoreContext(kind="expected", seed=seed, schema=fitted.schema)
    )
    exact = exact_tree["predictions"].to_dataset()
    predictor = plugin.frozen_predictor(
        fitted,
        ds,
        spec.model_copy(update={"reducer": "none"}),
        num_samples=num_samples,
        seed=seed,
    )
    given = dict(inputs) if inputs is not None else {n: ds[n].values for n in spec.input_vars}
    fast = predictor(**{k: np.asarray(v) for k, v in given.items()})
    report = FastScoreReport(n_sigma=n_sigma, samples=predictor.num_samples)
    for name in spec.response_exprs:
        if name not in exact:
            continue
        e = np.asarray(exact[name].transpose("chain", "draw", ...).values, dtype=float)
        f = np.asarray(fast[name], dtype=float)
        se_e = _mcse_mean(e)
        se_f = f.std(axis=0, ddof=1) / np.sqrt(f.shape[0])
        diff = np.abs(f.mean(axis=0) - e.mean(axis=(0, 1)))
        z = diff / np.maximum(np.sqrt(se_e**2 + se_f**2), 1e-12)
        report.max_z[name] = float(np.max(z)) if z.size else 0.0
    if raise_on_failure:
        report.raise_for_failure()
    return report
