"""The Model Explorer ``fields`` section: outputs that declare a ``grid`` as quantised rasters.

The contexts are fakes (a manifest with grid outputs and a pre-scored prediction dataset), so
the tests need no plugin and no fit.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_runner.worker.explore_data import SectionSkipped
from mapi_runner.worker.explore_model import (
    FIT_SECTIONS,
    MODEL_SECTION_FUNCS,
    ModelContext,
    block_mean,
    encode_raster,
)
from mapi_spec.capabilities import OutputDecl, Representation
from mapi_spec.views import (
    MAX_FIELD_CELLS,
    MAX_FIELD_DRAWS,
    MAX_FIELD_FRAMES,
    MAX_FIELD_PIXELS,
    FieldsView,
    decode_raster,
)


def _ctx(
    outputs: dict[str, dict[str, Any]],
    pred: xr.Dataset,
    rep: Representation = Representation.DRAWS,
) -> ModelContext:
    manifest = SimpleNamespace(
        outputs={k: OutputDecl.model_validate(v) for k, v in outputs.items()},
        capabilities=SimpleNamespace(representation=rep),
    )
    bundle = SimpleNamespace(
        model=SimpleNamespace(manifest=manifest, groups={}, derived={}),
        binding=SimpleNamespace(to_dataset=lambda n: n),
    )
    c = ModelContext(bundle, xr.Dataset(), None)  # type: ignore[arg-type]
    c._scored = pred
    return c


def _draws(rng: np.random.Generator, shape: tuple[int, ...], dims: list[str]) -> xr.DataArray:
    return xr.DataArray(rng.gamma(4.0, 1.0, (2, 25, *shape)), dims=["chain", "draw", *dims])


def test_encode_and_decode_round_trip() -> None:
    a = np.array([[0.0, 1.0, np.nan], [2.0, 4.0, -1.0]])
    lin = decode_raster(encode_raster(a))
    assert lin[2] is None and lin[5] == pytest.approx(-1.0)
    assert lin[3] == pytest.approx(2.0, abs=5 / 65534)
    log = decode_raster(encode_raster(a, "log10"), "log10")
    assert log[0] is None and log[5] is None  # values <= 0 have no log
    assert log[4] == pytest.approx(4.0, rel=1e-4)
    flat = encode_raster(np.full((2, 2), 3.0))
    assert flat.lo == flat.hi == 3.0 and decode_raster(flat) == [3.0] * 4
    empty = encode_raster(np.full((1, 3), np.nan))
    assert empty.lo is None and decode_raster(empty) == [None] * 3


def test_block_mean_ignores_nan_and_keeps_partial_edges() -> None:
    a = np.arange(25.0).reshape(5, 5)
    a[0, 0] = np.nan
    b = block_mean(a, 2, 2)
    assert b.shape == (3, 3)
    assert b[0, 0] == pytest.approx((1 + 5 + 6) / 3) and b[2, 2] == 24.0


def test_fields_section_is_a_fit_section_and_skips_without_grids() -> None:
    assert "fields" in FIT_SECTIONS and "fields" in MODEL_SECTION_FUNCS
    c = _ctx({"y": {"dims": ["t"]}}, xr.Dataset())
    with pytest.raises(SectionSkipped, match="2-D grid"):
        MODEL_SECTION_FUNCS["fields"](c)


def test_framed_scalar_and_vector_fields() -> None:
    rng = np.random.default_rng(0)
    pred = xr.Dataset(
        {
            "intensity": _draws(rng, (5, 6, 7), ["period", "row", "col"]),
            "traction": _draws(rng, (2, 6, 7), ["component", "row", "col"]),
            "density": _draws(rng, (9, 4), ["t", "x"]),
        },
        coords={"period": [2001, 2002, 2003, 2004, 2005], "component": ["x", "y"]},
    )
    c = _ctx(
        {
            "intensity": {
                "dims": ["period", "row", "col"],
                "units": "events/km2",
                "grid": {"y": "row", "x": "col", "frame": "period", "log": True},
            },
            "traction": {
                "dims": ["component", "row", "col"],
                "grid": {"y": "row", "x": "col", "component": "component", "origin": "lower"},
            },
            "density": {"dims": ["t", "x"], "grid": {"y": "t", "x": "x", "colormap": "magma"}},
            "missing": {"dims": ["row", "col"], "grid": {"y": "row", "x": "col"}},
        },
        pred,
    )
    view = MODEL_SECTION_FUNCS["fields"](c)
    assert isinstance(view, FieldsView)
    doc = view.model_dump(mode="json", exclude_none=True)
    json.dumps(doc, allow_nan=False)
    FieldsView.model_validate(doc)
    by = {f.output: f for f in view.fields}
    assert view.omitted == ["missing"]

    inten = by["intensity"]
    assert inten.encoding == "log10" and inten.log and inten.units == "events/km2"
    assert [f.label for f in inten.frames] == [2001, 2002, 2003, 2004, 2005]
    assert (inten.height, inten.width, inten.n_draws) == (6, 7, 50)
    first = inten.frames[0]
    assert len(first.draws) == MAX_FIELD_DRAWS
    mean = np.array(decode_raster(first.mean, "log10"), dtype=float).reshape(6, 7)
    want = pred["intensity"].isel(period=0).mean(["chain", "draw"]).values
    np.testing.assert_allclose(mean, want, rtol=1e-3)
    q05 = np.array(decode_raster(first.q05, "log10"), dtype=float)  # type: ignore[arg-type]
    q95 = np.array(decode_raster(first.q95, "log10"), dtype=float)  # type: ignore[arg-type]
    assert (q05 <= mean.reshape(-1) * 1.001).all() and (q95 >= mean.reshape(-1) * 0.999).all()

    trac = by["traction"]
    assert trac.kind == "vector" and trac.origin == "lower" and trac.frames[0].arrows is not None
    mag = np.sqrt((pred["traction"] ** 2).sum("component")).mean(["chain", "draw"]).values
    got = np.array(decode_raster(trac.frames[0].mean), dtype=float).reshape(6, 7)
    np.testing.assert_allclose(got, mag, rtol=1e-3)
    u = pred["traction"].sel(component="x").mean(["chain", "draw"]).values
    assert trac.frames[0].arrows.u[0][0] == pytest.approx(u[0, 0])

    dens = by["density"]
    assert dens.colormap == "magma" and dens.frame_dim is None and len(dens.frames) == 1


def test_large_grids_are_downsampled_and_kept_under_the_cap() -> None:
    rng = np.random.default_rng(1)
    da = xr.DataArray(
        rng.normal(size=(1, 20, 30, 330, 170)).astype("float32"),
        dims=["chain", "draw", "time", "row", "col"],
    )
    c = _ctx(
        {
            "field": {
                "dims": ["time", "row", "col"],
                "grid": {"y": "row", "x": "col", "frame": "time"},
            }
        },
        xr.Dataset({"field": da}),
    )
    view = MODEL_SECTION_FUNCS["fields"](c)
    assert isinstance(view, FieldsView)
    f = view.fields[0]
    assert f.downsampled and f.height <= MAX_FIELD_PIXELS and f.width <= MAX_FIELD_PIXELS
    assert len(f.frames) == MAX_FIELD_FRAMES and f.omitted_frames == 30 - MAX_FIELD_FRAMES
    assert len(f.y_coords) == f.height and f.source_height == 330
    assert sum(e.cells() for e in view.fields) <= MAX_FIELD_CELLS
    assert view.warnings_plain
    assert len(view.model_dump_json()) < 3_000_000


def test_quantile_representation_gives_bands_but_no_draws() -> None:
    rng = np.random.default_rng(2)
    base = rng.normal(size=(4, 5))
    q = xr.DataArray(
        np.stack([base - 1, base, base + 1]),
        dims=["quantile", "row", "col"],
        coords={"quantile": [0.05, 0.5, 0.95]},
    )
    c = _ctx(
        {"img": {"dims": ["row", "col"], "grid": {"y": "row", "x": "col"}}},
        xr.Dataset({"img": q}),
        Representation.QUANTILES,
    )
    view = MODEL_SECTION_FUNCS["fields"](c)
    assert isinstance(view, FieldsView)
    fr = view.fields[0].frames[0]
    assert not fr.draws and fr.q05 is not None and fr.sd is not None
    got = np.array(decode_raster(fr.mean), dtype=float).reshape(4, 5)
    np.testing.assert_allclose(got, base, atol=1e-3)
