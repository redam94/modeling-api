"""The shared field builder (:mod:`mapi_runner.worker.fieldview`) on score outputs: grids
declared under canonical names, read from a dataset-named ``/predictions`` group.

The bundles are fakes (a manifest with grid outputs and a binding alias), so the tests need
no plugin and no fit.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_runner.worker.explore_data import SectionSkipped
from mapi_runner.worker.fieldview import field_dim, fields_view
from mapi_spec.capabilities import OutputDecl, Representation
from mapi_spec.views import MAX_FIELD_FRAMES, FieldsView, decode_raster

_ALIAS = {"period": "quarter"}


def _bundle(outputs: dict[str, dict[str, Any]], rep: Representation = Representation.DRAWS) -> Any:
    manifest = SimpleNamespace(
        outputs={k: OutputDecl.model_validate(v) for k, v in outputs.items()},
        capabilities=SimpleNamespace(representation=rep),
    )
    return SimpleNamespace(
        model=SimpleNamespace(manifest=manifest, groups={}, derived={}),
        binding=SimpleNamespace(to_dataset=lambda n: _ALIAS.get(n, n)),
    )


_LGCP: dict[str, dict[str, Any]] = {
    "rate": {
        "dims": ["period", "row", "col"],
        "grid": {"y": "row", "x": "col", "frame": "period", "log": True},
    },
    "total": {"dims": ["period"]},
}


def _predictions(n_quarters: int = 6) -> xr.Dataset:
    rng = np.random.default_rng(0)
    quarters = [f"20{20 + i // 4}Q{i % 4 + 1}" for i in range(n_quarters)]
    rate = rng.gamma(3.0, 1.0, (2, 20, n_quarters, 5, 7))
    return xr.Dataset(
        {
            "rate": (("chain", "draw", "quarter", "row", "col"), rate),
            "total": (("chain", "draw", "quarter"), rate.sum(axis=(-2, -1))),
        },
        coords={"quarter": quarters},
    )


def test_field_dim_matches_canonical_and_dataset_names() -> None:
    b = _bundle(_LGCP)
    da = _predictions()["rate"]
    assert field_dim(b, da, "period") == "quarter"
    assert field_dim(b, da, "quarter") == "quarter"
    assert field_dim(b, da, "row") == "row"
    assert field_dim(b, da, "layer") is None and field_dim(b, da, None) is None


def test_score_predictions_give_one_frame_per_quarter() -> None:
    pred = _predictions()
    view = fields_view(_bundle(_LGCP), lambda n: pred.get(n), caption="Forecast map.")
    assert isinstance(view, FieldsView) and view.caption_plain == "Forecast map."
    FieldsView.model_validate(view.model_dump(mode="json", exclude_none=True))
    assert [f.output for f in view.fields] == ["rate"] and view.omitted == []
    f = view.fields[0]
    assert f.frame_dim == "quarter" and (f.y_dim, f.x_dim) == ("row", "col")
    assert f.n_draws == 40 and f.encoding == "log10"
    assert [fr.label for fr in f.frames] == list(pred["quarter"].values)
    mean = np.array(decode_raster(f.frames[2].mean, "log10"), dtype=float).reshape(5, 7)
    want = pred["rate"].isel(quarter=2).mean(["chain", "draw"]).values
    np.testing.assert_allclose(mean, want, rtol=1e-3)


def test_long_predictions_keep_a_sample_of_the_frames() -> None:
    pred = _predictions(MAX_FIELD_FRAMES + 6)
    f = fields_view(_bundle(_LGCP), lambda n: pred.get(n)).fields[0]
    assert len(f.frames) == MAX_FIELD_FRAMES and f.omitted_frames == 6


def test_quantile_score_output_gives_bands_and_no_draws() -> None:
    rng = np.random.default_rng(1)
    base = rng.gamma(3.0, 1.0, (3, 4, 5))
    q = xr.DataArray(
        np.stack([base * 0.5, base, base * 1.5]),
        dims=["quantile", "quarter", "row", "col"],
        coords={"quantile": [0.05, 0.5, 0.95]},
    )
    view = fields_view(_bundle(_LGCP, Representation.QUANTILES), {"rate": q}.get)
    f = view.fields[0]
    assert f.frame_dim == "quarter" and f.n_draws == 0 and len(f.frames) == 3
    fr = f.frames[0]
    assert not fr.draws and fr.q05 is not None and fr.q95 is not None and fr.sd is not None


def test_no_grid_outputs_or_no_grid_data_skip() -> None:
    with pytest.raises(SectionSkipped, match="2-D grid"):
        fields_view(_bundle({"total": {"dims": ["period"]}}), lambda n: None)
    with pytest.raises(SectionSkipped, match="predictions"):
        fields_view(_bundle(_LGCP), lambda n: None)
