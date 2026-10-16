"""The what-if grid view and the scorecard's read of the response curves, on hand-made
``response_curves`` (no fit): the grid's point at today's spend (multiplier 1), the per-geo
cap of the view, and how a view the contract refuses is reported."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_runner.worker import fit as fit_mod
from mapi_runner.worker.scorecard import _channel_totals, _plausibility
from mapi_spec.scorecard import rollup
from mapi_spec.views import RESPONSE_GRID_VIEW

pytestmark = pytest.mark.unit


def curves(
    *, multipliers: list[float], geos: int = 2, draws: int = 10, channels: int = 2
) -> xr.Dataset:
    rng = np.random.default_rng(0)
    m = np.asarray(multipliers)
    chans = [f"c{i}" for i in range(channels)]
    geo_labels = [f"g{i:03d}" for i in range(geos)]
    base = rng.gamma(2.0, 1.0, (draws, channels, 1, geos))
    contrib = base * np.sqrt(m)[None, None, :, None]
    spend_by_geo = np.ones((geos, channels, 1)) * m[None, None, :]
    return xr.Dataset(
        {
            "contribution": (("sample", "channel", "multiplier", "geo"), contrib),
            "spend": (("channel", "multiplier"), spend_by_geo.sum(0)),
            "spend_by_geo": (("geo", "channel", "multiplier"), spend_by_geo),
            "current_spend": (("channel",), np.full(channels, float(geos))),
            "baseline": (("sample",), np.full(draws, 100.0)),
        },
        coords={"channel": chans, "multiplier": m, "geo": geo_labels},
    )


def fake_bundle(rc: xr.Dataset) -> Any:
    return SimpleNamespace(
        model=SimpleNamespace(derived={"response_curves": rc}),
        resolved=SimpleNamespace(model=SimpleNamespace(input_schema=None)),
        binding=SimpleNamespace(to_dataset=lambda name: name),
    )


def test_the_scorecard_reads_the_curves_at_today_s_spend_exactly() -> None:
    """A grid without multiplier 1 used to be read at the nearest point (1.05x spend)."""
    on = _channel_totals(curves(multipliers=[0.0, 0.5, 1.0, 1.5]))
    assert on is not None
    off = curves(multipliers=[0.0, 0.35, 0.7, 1.05, 1.4])
    assert _channel_totals(off) is None
    out: dict[str, Any] = {}
    why: dict[str, str] = {}
    _plausibility(fake_bundle(off), out, why)  # type: ignore[arg-type]
    assert not out and "multiplier 1" in why["roi_max"]


def test_a_view_the_contract_refuses_is_reported_as_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[tuple[str, str]] = []
    written: dict[str, Any] = {}
    ctx = SimpleNamespace(
        write_json=lambda name, body: written.__setitem__(name, body),
        warning=lambda code, message, untrusted=None: warnings.append((code, message)),
    )
    monkeypatch.setattr(fit_mod, "fit_scorecard", lambda bundle, data: rollup([]))
    off = curves(multipliers=[0.0, 0.35, 0.7, 1.05, 1.4])
    fit_mod.write_guided_views(ctx, fake_bundle(off), xr.Dataset())  # type: ignore[arg-type]
    assert RESPONSE_GRID_VIEW not in written
    [(code, message)] = warnings
    assert code == "VIEW_INVALID" and "what-if grid" in message
