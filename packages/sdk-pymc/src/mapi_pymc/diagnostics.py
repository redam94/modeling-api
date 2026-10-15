"""Convergence diagnostics as scorecard-ready :class:`~mapi_spec.capabilities.Diagnostic` objects.

Every check reports something - ``info`` when it passes - so the sanity scorecard can list
it. Thresholds follow current practice (Vehtari et al. 2021): rank-normalised split r-hat
below 1.01 and at least 100 effective draws per chain for bulk and tail ESS.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from typing import Literal

import numpy as np
import xarray as xr
from mapi_spec.capabilities import Diagnostic

__all__ = ["DIVERGENCE_STATS", "TREEDEPTH_STATS", "convergence_diagnostics"]

DIVERGENCE_STATS = ("diverging",)
TREEDEPTH_STATS = ("reached_max_treedepth", "maxdepth_reached")
RHAT_ERROR = 1.1

Severity = Literal["info", "warning", "error"]


def convergence_diagnostics(
    posterior: xr.Dataset,
    sample_stats: xr.Dataset | None = None,
    *,
    var_names: Sequence[str] | None = None,
    rhat_max: float = 1.01,
    ess_per_chain: int = 100,
) -> list[Diagnostic]:
    """r-hat, bulk/tail ESS, divergences and tree-depth saturation for an MCMC posterior.

    ``var_names`` limits the checks (pass the free random variables; deterministics over
    data dims are large and add nothing). Needs at least 2 chains and 4 draws for r-hat/ESS.
    """
    out: list[Diagnostic] = []
    names = [v for v in (var_names or list(map(str, posterior.data_vars))) if v in posterior]
    chains = int(posterior.sizes.get("chain", 1))
    draws = int(posterior.sizes.get("draw", 1))
    if names and chains >= 2 and draws >= 4:
        out += _rhat(posterior[names], rhat_max)
        out += _ess(posterior[names], ess_per_chain * chains)
    if sample_stats is not None:
        out += _divergences(sample_stats)
        out += _treedepth(sample_stats)
    return out


def _worst(values: xr.Dataset, *, largest: bool) -> tuple[str, float] | None:
    best: tuple[str, float] | None = None
    for name, da in values.data_vars.items():
        arr = np.asarray(da.values, dtype=float)
        arr = arr[np.isfinite(arr)]
        if not arr.size:
            continue
        v = float(arr.max() if largest else arr.min())
        if best is None or (v > best[1] if largest else v < best[1]):
            best = (str(name), v)
    return best


def _rhat(post: xr.Dataset, rhat_max: float) -> list[Diagnostic]:
    import arviz as az

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        values = az.rhat(post)
    worst = _worst(values, largest=True)
    if worst is None:
        return []
    var, value = worst
    severity: Severity = (
        "error" if value > RHAT_ERROR else "warning" if value > rhat_max else "info"
    )
    if severity == "info":
        plain = "The independent sampling runs agree with each other."
        action = None
    else:
        plain = (
            f"The independent sampling runs disagree about {var!r}, so its estimates are not "
            "reliable yet."
        )
        action = "Run longer (more tuning and draws) or reparameterise the model."
    return [
        Diagnostic(
            code="RHAT",
            severity=severity,
            metric="rhat",
            value=value,
            threshold=rhat_max,
            var=var,
            message_expert=f"max rank-normalised split r-hat {value:.3f} for {var!r} "
            f"(threshold {rhat_max})",
            message_plain=plain,
            suggested_action=action,
        )
    ]


def _ess(post: xr.Dataset, minimum: int) -> list[Diagnostic]:
    import arviz as az

    out: list[Diagnostic] = []
    for method, code, what in (
        ("bulk", "ESS_BULK", "averages"),
        ("tail", "ESS_TAIL", "intervals"),
    ):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            values = az.ess(post, method=method)
        worst = _worst(values, largest=False)
        if worst is None:
            continue
        var, value = worst
        low = value < minimum
        out.append(
            Diagnostic(
                code=code,
                severity="warning" if low else "info",
                metric=f"ess_{method}",
                value=value,
                threshold=float(minimum),
                var=var,
                message_expert=f"min {method} ESS {value:.0f} for {var!r} (threshold {minimum})",
                message_plain=(
                    f"There is too little independent information about {var!r} (about "
                    f"{value:.0f} effective draws) for stable {what}."
                    if low
                    else f"There are enough effective draws for stable {what}."
                ),
                suggested_action="Increase the number of draws." if low else None,
            )
        )
    return out


def _stat(stats: xr.Dataset, names: Sequence[str]) -> np.ndarray | None:
    for n in names:
        if n in stats:
            return np.asarray(stats[n].values).astype(bool)
    return None


def _divergences(stats: xr.Dataset) -> list[Diagnostic]:
    div = _stat(stats, DIVERGENCE_STATS)
    if div is None:
        return []
    n, total = int(div.sum()), int(div.size)
    share = n / total if total else 0.0
    if n == 0:
        return [
            Diagnostic(
                code="DIVERGENCES",
                severity="info",
                metric="divergences",
                value=0.0,
                threshold=0.0,
                message_expert=f"0 divergent transitions in {total} draws",
                message_plain="The sampler explored the model without running into trouble.",
            )
        ]
    return [
        Diagnostic(
            code="DIVERGENCES",
            severity="error" if share > 0.05 else "warning",
            metric="divergences",
            value=float(n),
            threshold=0.0,
            message_expert=f"{n} divergent transitions in {total} draws ({share:.1%})",
            message_plain=(
                f"The sampler ran into trouble in {n} of {total} steps, which can bias the results."
            ),
            suggested_action=(
                "Raise fit.target_accept (for example to 0.95) or use a non-centred "
                "parameterisation."
            ),
        )
    ]


def _treedepth(stats: xr.Dataset) -> list[Diagnostic]:
    hit = _stat(stats, TREEDEPTH_STATS)
    if hit is None or not hit.any():
        return []
    n, total = int(hit.sum()), int(hit.size)
    return [
        Diagnostic(
            code="TREE_DEPTH",
            severity="warning" if n / total > 0.1 else "info",
            metric="max_treedepth_hits",
            value=float(n),
            threshold=0.0,
            message_expert=f"{n} of {total} draws hit the maximum tree depth",
            message_plain="Sampling was slow in places; results are fine but fits take longer.",
            suggested_action="Consider rescaling or reparameterising the model.",
        )
    ]
