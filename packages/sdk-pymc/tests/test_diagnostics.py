"""Convergence diagnostics become scorecard Diagnostics with plain-language messages."""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_pymc.diagnostics import convergence_diagnostics


def _posterior(shift: float = 0.0, chains: int = 4, draws: int = 400, seed: int = 0) -> xr.Dataset:
    rng = np.random.default_rng(seed)
    a = rng.standard_normal((chains, draws))
    a[0] += shift  # one chain stuck elsewhere
    b = rng.standard_normal((chains, draws, 3))
    return xr.Dataset({"a": (("chain", "draw"), a), "b": (("chain", "draw", "k"), b)})


def _by_code(diags: list) -> dict:
    return {d.code: d for d in diags}


def test_healthy_chains_pass_every_check() -> None:
    stats = xr.Dataset(
        {"diverging": (("chain", "draw"), np.zeros((4, 400), bool))},
    )
    d = _by_code(convergence_diagnostics(_posterior(), stats))
    assert {d[c].severity for c in ("RHAT", "ESS_BULK", "ESS_TAIL", "DIVERGENCES")} == {"info"}
    assert d["RHAT"].value < 1.01 and d["RHAT"].metric == "rhat"
    assert "agree" in d["RHAT"].message_plain and d["RHAT"].suggested_action is None
    assert "TREE_DEPTH" not in d
    assert all(x.passed for x in d.values())


def test_disagreeing_chains_and_divergences_are_flagged() -> None:
    div = np.zeros((4, 400), bool)
    div[1, :30] = True
    depth = np.zeros((4, 400), bool)
    depth[:, :100] = True
    stats = xr.Dataset(
        {
            "diverging": (("chain", "draw"), div),
            "maxdepth_reached": (("chain", "draw"), depth),
        }
    )
    d = _by_code(convergence_diagnostics(_posterior(shift=3.0), stats, var_names=["a", "zzz"]))
    assert d["RHAT"].severity == "error" and d["RHAT"].var == "a"
    assert "disagree about 'a'" in d["RHAT"].message_plain
    assert d["DIVERGENCES"].severity == "warning" and d["DIVERGENCES"].value == 30
    assert "target_accept" in (d["DIVERGENCES"].suggested_action or "")
    assert d["TREE_DEPTH"].severity == "warning"
    many = div.copy()
    many[:, :40] = True
    severe = _by_code(
        convergence_diagnostics(_posterior(), xr.Dataset({"diverging": (("chain", "draw"), many)}))
    )
    assert severe["DIVERGENCES"].severity == "error"


def test_low_ess_and_degenerate_inputs() -> None:
    rng = np.random.default_rng(1)
    walk = np.cumsum(rng.standard_normal((2, 200)), axis=1)  # strongly autocorrelated
    d = _by_code(convergence_diagnostics(xr.Dataset({"w": (("chain", "draw"), walk)})))
    assert d["ESS_BULK"].severity == "warning" and d["ESS_BULK"].threshold == 200
    assert "too little independent information" in d["ESS_BULK"].message_plain
    one_chain = xr.Dataset({"a": (("chain", "draw"), np.zeros((1, 50)))})
    assert convergence_diagnostics(one_chain) == []
    assert convergence_diagnostics(one_chain, xr.Dataset({"other": ("x", [1])})) == []
    nan = xr.Dataset({"a": (("chain", "draw"), np.full((2, 10), np.nan))})
    assert convergence_diagnostics(nan) == []
