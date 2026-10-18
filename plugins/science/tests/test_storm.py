"""``sci.storm_localisation`` (E58): camera calibration, detection, the PSF graph, recovery on a
simulated movie, every score mode, typed refusals, plots, the contract kit and the real frames."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_plots.recipes import render_plots
from mapi_plugin_science import storm as storm_mod
from mapi_plugin_science.challenges import challenges_file
from mapi_plugin_science.storm import (
    StormLocalisationModel,
    StormLocalisationSettings,
    expected_photons,
    find_spots,
    frame_statistics,
    photon_transfer,
    pixel_gauss,
    to_counts,
)
from mapi_plugin_science.storm_data import (
    CHALLENGES_FILE,
    STORM_SCHEMA,
    STORM_SPEC_YAML,
    STORM_TRUTH,
    challenges_reference,
    challenges_settings,
    challenges_spec,
    from_challenges,
    simulate_storm,
)
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import validate_dataset
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import ErrorCode, MapiError, load_model_spec, load_score_request, validate_spec
from scipy.special import gammaln

CHALLENGES = challenges_file(CHALLENGES_FILE)
HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://science/storm\n"
TINY = StormLocalisationSettings(pixel_nm=127, offset_adu=457, scale_adu=13.8, max_spots=16)


# ------------------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(StormLocalisationModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def synthetic() -> tuple[xr.Dataset, xr.Dataset]:
    return simulate_storm(seed=0)


@pytest.fixture(scope="module")
def bundle(registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]) -> Bundle:
    data, _ = synthetic
    return fit_spec(STORM_SPEC_YAML, data, registry=registry, raw_schema=STORM_SCHEMA)


def _score(bundle: Bundle, registry: Registry, body: str, data: xr.Dataset) -> xr.DataTree:
    return score_bundle(
        bundle, load_score_request(HEADER + body), data, registry=registry, return_full=True
    )


def _predictions(tree: xr.DataTree) -> xr.Dataset:
    node = tree["predictions"]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()


def _matches(bundle: Bundle, truth: xr.Dataset) -> list[tuple[int, int]]:
    """``(spot, emitter)`` pairs: each spot and the nearest true blink of its frame."""
    post = bundle.model.posterior
    px = float(bundle.model.learned["pixel_nm"])  # type: ignore[arg-type]
    frames = bundle.model.groups["constant_data"]["spot_frame"].values
    mx = post["x"].mean(("chain", "draw")).values / px
    my = post["y"].mean(("chain", "draw")).values / px
    out = []
    for k, f in enumerate(frames.tolist()):
        cand = np.flatnonzero(truth["frame"].values == f)
        d = np.hypot(truth["x"].values[cand] - mx[k], truth["y"].values[cand] - my[k])
        if cand.size and d.min() < 1.0:
            out.append((k, int(cand[np.argmin(d)])))
    return out


# ------------------------------------------------------------------------------ manifest


def test_manifest_declares_spot_outputs_grids_and_modes() -> None:
    m = StormLocalisationModel.manifest()
    caps = m.capabilities
    assert caps.score_modes == frozenset({"predict", "evaluate"})
    assert caps.deterministic_given_seed and caps.loo_valid
    assert {"x", "y", "photons", "background", "psf_sd"} <= set(m.outputs)
    assert m.outputs["x"].dims == ["spot"] and m.outputs["x"].units == "nm"
    render = m.outputs["render"].grid
    assert render is not None and (render.y, render.x) == ("render_y", "render_x")
    patch = m.outputs["patch_fit"].grid
    assert patch is not None and (patch.y, patch.x, patch.frame) == ("py", "px", "spot")
    frames = m.outputs["frames_hat"]
    assert frames.units_of == "frames" and frames.grid is not None
    assert frames.grid.frame == "frame"
    assert set(m.plots) == {
        "core.convergence",
        "sci.point_pattern",
        "sci.image_residuals",
        "sci.field_map",
    }
    schema = m.schema_template.resolve({})
    assert schema.vars["frames"].dims == ("frame", "row", "col")
    assert all(d.policy.value == "fixed" for d in schema.dims.values())


def test_spec_validates_statically(registry: Registry) -> None:
    res = validate_spec(load_model_spec(STORM_SPEC_YAML), registry.catalog(), STORM_SCHEMA)
    assert res.ok, [i.message for i in res.errors]


def test_synthetic_movie_matches_the_schema(synthetic: tuple[xr.Dataset, xr.Dataset]) -> None:
    data, truth = synthetic
    assert validate_dataset(STORM_SCHEMA, data) == []
    assert data["frames"].dtype == np.int32
    assert truth.sizes["emitter"] > 25
    assert truth.attrs["scale"] == pytest.approx(2 * STORM_TRUTH["gain"])


# ------------------------------------------------------------------------------ camera + PSF


def test_effective_counts_are_poisson_with_half_the_photons() -> None:
    from mapi_plugin_science.storm_data import simulate_emccd

    rng = np.random.default_rng(1)
    lam = np.repeat(np.array([50.0, 500.0])[:, None], 100_000, 1)
    z = to_counts(simulate_emccd(lam, rng), STORM_TRUTH["offset"], 2 * STORM_TRUTH["gain"])
    np.testing.assert_allclose(z.mean(1), [25.0, 250.0], rtol=0.01)
    np.testing.assert_allclose(z.var(1), [25.0, 250.0], rtol=0.05)


def test_photon_transfer_curve_recovers_offset_and_gain() -> None:
    data, truth = simulate_storm(frames=400, size=32, rate=0.3, background_slope=0.8, seed=3)
    offset, scale = photon_transfer(*frame_statistics(data["frames"].values))
    assert offset == pytest.approx(truth.attrs["offset"], abs=40)
    assert scale == pytest.approx(truth.attrs["scale"], rel=0.06)


def test_pixel_integrated_psf_and_the_pytensor_graph_match_numpy(
    synthetic: tuple[xr.Dataset, xr.Dataset],
) -> None:
    e = pixel_gauss(np.array([5.0]), np.array([1.0]), 11)
    assert e.sum() == pytest.approx(1.0, abs=1e-6) and int(e.argmax()) == 5
    data, _ = synthetic
    for kind in ("flat", "plane"):
        s = TINY.model_copy(update={"kind": kind})
        spots = find_spots(np.asarray(data["frames"].values, float), s)
        model = StormLocalisationModel().build_model(spots, s)
        free = {rv.name for rv in model.free_RVs}
        assert free == set(StormLocalisationModel.free_names(s))
        rng = np.random.default_rng(2)
        B = spots.patches.shape[0]
        point = {
            "x_px": spots.centre[:, 0] + rng.normal(0, 0.2, B),
            "y_px": spots.centre[:, 1] + rng.normal(0, 0.2, B),
            "logN": np.log(rng.uniform(800, 2500, B)),
            "logb": np.log(rng.uniform(90, 130, B)),
            "logs": np.log(rng.uniform(0.9, 1.2, B)),
        }
        if kind == "plane":
            point |= {"grad_x": rng.normal(0, 0.03, B), "grad_y": rng.normal(0, 0.03, B)}
        got = float(model.compile_fn(model.observedlogp, point_fn=True)(point))
        m = (
            expected_photons(
                point["x_px"],
                point["y_px"],
                point["logN"],
                point["logb"],
                point["logs"],
                s.patch_px,
                point.get("grad_x"),
                point.get("grad_y"),
            )
            / 2
        )
        z = spots.patches
        want = float(np.sum(z * np.log(m) - m - gammaln(z + 1)))
        assert got == pytest.approx(want, rel=1e-9)


def test_detection_finds_isolated_spots_near_true_blinks(
    synthetic: tuple[xr.Dataset, xr.Dataset],
) -> None:
    data, truth = synthetic
    spots = find_spots(np.asarray(data["frames"].values, float), TINY)
    assert spots.patches.shape == (16, 11, 11)
    assert spots.n_isolated >= 16 and spots.n_blobs >= spots.n_isolated
    assert spots.calibration == "settings"
    # the selected blob centres lie within 1.5 px of a true blink of their frame (two
    # blinks close together can merge into one blob)
    near = []
    for (t, r0, c0), (cx, cy) in zip(spots.origin, spots.centre, strict=True):
        cand = truth["frame"].values == t
        d = np.hypot(truth["x"].values[cand] - (c0 + cx), truth["y"].values[cand] - (r0 + cy))
        near.append(bool(d.size) and d.min() < 1.5)
    assert sum(near) >= 15
    # the amplitude band keeps the middle of the isolated blobs only
    mid = find_spots(
        np.asarray(data["frames"].values, float),
        TINY.model_copy(update={"amplitude_band": (0.25, 0.75), "max_spots": 100}),
    )
    assert 0 < mid.patches.shape[0] < spots.n_isolated


# ------------------------------------------------------------------------------ typed refusals


def test_settings_refusals() -> None:
    check = StormLocalisationModel.check_settings
    cases: list[tuple[dict[str, Any], str]] = [
        ({"offset_adu": 400.0}, "scale_adu"),
        ({"scale_adu": 13.0}, "offset_adu"),
        ({"patch_px": 10}, "patch_px"),
        ({"min_blob_px": 20, "max_blob_px": 10}, "min_blob_px"),
        ({"amplitude_band": (0.6, 0.4)}, "amplitude_band"),
    ]
    for update, field in cases:
        with pytest.raises(MapiError) as exc:
            check(StormLocalisationSettings(**update))
        assert exc.value.code is ErrorCode.SETTINGS_INVALID
        assert exc.value.pointer == f"/model/settings/{field}"
    with pytest.raises(MapiError) as exc:
        check(StormLocalisationSettings(), (8, 8))
    assert exc.value.code is ErrorCode.SCHEMA_INVALID


def test_data_refusals(synthetic: tuple[xr.Dataset, xr.Dataset]) -> None:
    data, _ = synthetic
    holed = data.copy(deep=True)
    holed["frames"] = holed["frames"].astype(float)
    holed["frames"][2, 3, 4] = np.nan
    gapped = data.isel(col=[0, 1, 2, 5, 6, 7, 8, 9, 10, 11, 12, 13])
    short = data.isel(frame=[0, 1])
    for bad in (holed, gapped, short, data.drop_vars("frames")):
        with pytest.raises(MapiError) as exc:
            StormLocalisationModel.movie(bad)
        assert exc.value.code is ErrorCode.SCHEMA_INVALID
    # an empty movie has no spot; a flat one cannot be calibrated
    rng = np.random.default_rng(0)
    blank = rng.poisson(800, (20, 16, 16)).astype(float)
    with pytest.raises(MapiError, match=r"no spot|isolated") as exc:
        find_spots(blank, TINY)
    assert exc.value.code is ErrorCode.SCHEMA_INVALID
    with pytest.raises(MapiError, match="calibrated") as exc:
        find_spots(np.full((20, 16, 16), 800.0), StormLocalisationSettings())
    assert exc.value.code is ErrorCode.SCHEMA_INVALID


def test_fit_refuses_half_a_calibration(
    registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, _ = synthetic
    spec = STORM_SPEC_YAML.replace("    scale_adu: 13.8\n", "")
    with pytest.raises(MapiError) as exc:
        fit_spec(spec, data, registry=registry, raw_schema=STORM_SCHEMA)
    assert exc.value.code is ErrorCode.SETTINGS_INVALID


# ------------------------------------------------------------------------------ fit + recovery


def test_bundle_keeps_the_groups_the_plots_and_loo_need(bundle: Bundle) -> None:
    g = bundle.model.groups
    post = g["posterior"]
    assert {"x_px", "y_px", "logN", "logb", "logs", "x", "y", "photons", "psf_sd"} <= set(
        post.data_vars
    )
    assert dict(post["x"].sizes) == {"chain": 2, "draw": 100, "spot": 16}
    assert g["observed_data"]["patch_fit"].shape == (16, 11, 11)
    assert dict(g["posterior_predictive"]["patch_fit"].sizes) == {
        "chain": 2,
        "draw": 5,
        "spot": 16,
        "py": 11,
        "px": 11,
    }
    assert dict(g["log_likelihood"]["patch_fit"].sizes) == {
        "chain": 2,
        "draw": 100,
        "spot": 16,
        "py": 11,
        "px": 11,
    }
    assert g["constant_data"]["background_counts"].dims == ("row", "col")
    assert "prior" in g
    elpd = bundle.metrics.values["elpd_loo"]
    assert elpd is not None and math.isfinite(elpd) and elpd < 0
    learned = bundle.model.learned
    assert learned["n_spots"] == 16 and learned["calibration"] == "settings"
    assert learned["scale_adu"] == pytest.approx(13.8)
    assert "PATCH_MISFIT" not in {d.code for d in bundle.diagnostics}


def test_recovery_of_positions_photons_and_psf(
    bundle: Bundle, synthetic: tuple[xr.Dataset, xr.Dataset]
) -> None:
    _, truth = synthetic
    post = bundle.model.posterior
    px = STORM_TRUTH["pixel_nm"]
    pairs = _matches(bundle, truth)
    assert len(pairs) >= 15
    inside = []
    for k, e in pairs:
        for var, true in (("x", truth["x"].values[e] * px), ("y", truth["y"].values[e] * px)):
            lo, hi = np.quantile(post[var].sel(spot=k).values, [0.01, 0.99])
            inside.append(lo <= true <= hi)
    assert np.mean(inside) >= 0.9, f"98% intervals cover {np.mean(inside):.2f}"
    err = [
        np.hypot(
            post["x"].sel(spot=k).mean() / px - truth["x"].values[e],
            post["y"].sel(spot=k).mean() / px - truth["y"].values[e],
        )
        for k, e in pairs
    ]
    assert np.median(err) * px < 25.0  # nm
    psf = post["psf_sd"].mean(("chain", "draw")).values / px
    assert abs(np.median(psf) / STORM_TRUTH["psf_sd_px"] - 1) < 0.1
    ratio = [
        float(post["photons"].sel(spot=k).mean()) / truth["photons"].values[e] for k, e in pairs
    ]
    assert abs(np.median(ratio) - 1) < 0.15


# ------------------------------------------------------------------------------ score modes


def test_predict_returns_spots_render_patches_and_frames(
    bundle: Bundle, registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, _ = synthetic
    tree = _score(bundle, registry, "mode: predict\nseed: 5\noutput: {draws: 4}\n", data)
    pred = _predictions(tree)
    assert dict(pred["x"].sizes) == {"chain": 2, "draw": 10, "spot": 16}
    assert pred["render"].dims == ("chain", "draw", "render_y", "render_x")
    # each draw places every spot once (a little mass falls outside the grid edge)
    mass = pred["render"].sum(("render_y", "render_x")).values
    assert np.all((mass > 15.0) & (mass <= 16.0 + 1e-3))
    assert dict(pred["frames_hat"].sizes) == {
        "chain": 2,
        "draw": 10,
        "frame": 60,
        "row": 24,
        "col": 24,
    }
    assert tree["draws"]["x"].sizes["sample"] == 4
    summary = tree["summary"].to_dataset()
    assert summary["x"].attrs["units"] == "nm"
    # the expected patch inside frames_hat is offset + scale x patch_fit
    const = bundle.model.groups["constant_data"]
    k = 0
    f, r0, c0 = (int(const[n].values[k]) for n in ("spot_frame", "spot_row0", "spot_col0"))
    window = pred["frames_hat"].sel(frame=f).isel(row=slice(r0, r0 + 11), col=slice(c0, c0 + 11))
    want = 457 + 13.8 * pred["patch_fit"].sel(spot=k)
    np.testing.assert_allclose(window.values, want.values, rtol=1e-5)


def test_scores_are_deterministic_and_subsets_agree(
    bundle: Bundle, registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, _ = synthetic
    body = "mode: predict\nseed: 4\noutput: {kind: predictive}\n"
    a = _predictions(_score(bundle, registry, body, data))
    b = _predictions(_score(bundle, registry, body, data))
    xr.testing.assert_identical(a, b)
    c = _predictions(_score(bundle, registry, body.replace("seed: 4", "seed: 5"), data))
    assert not np.array_equal(a["patch_fit"].values, c["patch_fit"].values)
    sub = data.isel(frame=[7, 3, 30], row=slice(2, 9))
    s = _predictions(_score(bundle, registry, body, sub))
    want = a["frames_hat"].sel(frame=sub["frame"].values, row=sub["row"].values)
    np.testing.assert_array_equal(s["frames_hat"].values, want.values)
    np.testing.assert_array_equal(s["x"].values, a["x"].values)
    # predictive patches are Poisson counts around the expected ones
    e = _predictions(_score(bundle, registry, "mode: predict\nseed: 4\n", data))
    assert np.allclose(a["patch_fit"].values, np.round(a["patch_fit"].values))
    z = (a["patch_fit"] - e["patch_fit"]) / np.sqrt(e["patch_fit"])
    assert abs(float(z.std()) - 1) < 0.1


def test_evaluate_compares_frames_in_adu(
    bundle: Bundle, registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, _ = synthetic
    tree = score_bundle(
        bundle,
        load_score_request(
            HEADER + "mode: evaluate\ndata: dataset://science/storm@v1\nseed: 3\n"
            "output: {kind: predictive}\n"
        ),
        data,
        registry=registry,
        metrics=["rmse", "mae", "coverage_90"],
    )
    values = json.loads(tree.attrs["metrics"])["values"]
    # background pixels: sd of the counts in ADU is about scale x sqrt(55) = 100
    assert 80 < values["mae"] < 200
    assert 0.7 < values["coverage_90"] < 0.97


def test_typed_score_refusals(
    bundle: Bundle,
    registry: Registry,
    synthetic: tuple[xr.Dataset, xr.Dataset],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, _ = synthetic
    outside = data.isel(row=slice(0, 3)).assign_coords(row=[40, 41, 42])
    with pytest.raises(MapiError) as exc:
        _score(bundle, registry, "mode: predict\n", outside)
    assert exc.value.code is ErrorCode.UNSEEN_LEVELS
    with pytest.raises(MapiError) as exc:
        _score(
            bundle,
            registry,
            "mode: counterfactual\ninterventions: [{var: frames, multiply: 1.1}]\n",
            data,
        )
    assert exc.value.code is ErrorCode.MODE_NOT_SUPPORTED
    monkeypatch.setattr(storm_mod, "MAX_FRAME_VALUES", 1000)
    with pytest.raises(MapiError) as exc:
        _score(bundle, registry, "mode: predict\n", data)
    assert exc.value.code is ErrorCode.PAYLOAD_TOO_LARGE


def test_the_fit_alone_draws_the_render_when_the_movie_is_too_long_to_score(
    bundle: Bundle,
    registry: Registry,
    synthetic: tuple[xr.Dataset, xr.Dataset],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the runner draws plots from the fit bundle, then from an in-sample
    evaluate of the whole fit data. On the real 2,000-frame movie that evaluate is over
    MAX_FRAME_VALUES and refused, and the bundle had no ``render``: sci.point_pattern was
    skipped and sci.field_map showed ``patch_fit``. The bundle now keeps ``render``."""
    data, _ = synthetic
    full = _predictions(_score(bundle, registry, "mode: predict\n", data))
    group = bundle.model.groups[storm_mod.RENDER_GROUP]
    assert group["render"].dims == ("chain", "draw", "render_y", "render_x")
    assert group["render"].shape == full["render"].shape  # score_draws draws, as a score
    np.testing.assert_allclose(group["render"].values, full["render"].values, rtol=1e-6)
    # the real movie's case, scaled down: the runner's evaluate is refused
    monkeypatch.setattr(storm_mod, "MAX_FRAME_VALUES", full["frames_hat"].size // 2)
    with pytest.raises(MapiError) as exc:
        score_bundle(  # what packages/runner worker/fit.py _evaluation_tree sends
            bundle,
            load_score_request(
                HEADER + "mode: evaluate\ndata: dataset://runner/holdout@latest\nseed: 0\n"
                "output: {names: canonical, kind: predictive}\n"
            ),
            data,
            registry=registry,
            metrics=["rmse"],
            return_full=True,
        )
    assert exc.value.code is ErrorCode.PAYLOAD_TOO_LARGE
    refs = ["core.convergence", "sci.point_pattern", "sci.image_residuals", "sci.field_map"]
    out = render_plots(bundle.to_tree(), refs)  # the runner's first tree
    assert not out.issues, [i.untrusted_text or i.message for i in out.issues]
    drawn = {str(p.id): json.dumps(p.model_dump(mode="json")) for p in out.plots}
    assert set(drawn) == set(refs)
    assert "render" in drawn["sci.point_pattern"]
    assert "render" in drawn["sci.field_map"] and "patch_fit" not in drawn["sci.field_map"]


# ------------------------------------------------------------------------------ plots


def test_declared_plots_render(
    bundle: Bundle, registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, _ = synthetic
    fit_tree = bundle.to_tree()
    out = render_plots(fit_tree, ["sci.image_residuals", "core.convergence"])
    assert not out.issues, [i.untrusted_text or i.message for i in out.issues]
    score_tree = _score(bundle, registry, "mode: predict\nseed: 1\n", data)
    out = render_plots(score_tree, ["sci.point_pattern", "sci.field_map"])
    assert not out.issues, [i.untrusted_text or i.message for i in out.issues]
    pp = out.plots[0].model_dump(mode="json")
    assert "render" in json.dumps(pp)


# ------------------------------------------------------------------------------ kind + kit


def test_plane_background_kind_fits_and_has_an_elpd(
    registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, _ = synthetic
    spec = STORM_SPEC_YAML.replace("max_spots: 16", "max_spots: 16\n    kind: plane")
    b = fit_spec(spec, data, registry=registry, raw_schema=STORM_SCHEMA)
    post = b.model.posterior
    assert {"grad_x", "grad_y"} <= set(post.data_vars)
    # the simulated background rises by 30 % across 24 columns: about 0.011 per px
    gx = float(post["grad_x"].mean())
    assert 0.0 < gx < 0.03
    assert b.metrics.values["elpd_loo"] is not None


@pytest.mark.mapi_contract
def test_contract_kit(registry: Registry, synthetic: tuple[xr.Dataset, xr.Dataset]) -> None:
    data, _ = synthetic
    report = check_model(
        StormLocalisationModel,
        ModelFixture(spec=STORM_SPEC_YAML, data=data, raw_schema=STORM_SCHEMA, registry=registry),
    )
    passed = {r.name for r in report.results if r.passed and not r.skipped}
    assert {
        "bundle_roundtrip_no_pickle",
        "about",
        "unsupported_mode",
        "output_space_and_units",
        "outputs_match_manifest",
        "seed_determinism",
        "plots_sanitize",
    } <= passed


# ------------------------------------------------------------------------------ real data


@pytest.mark.skipif(CHALLENGES is None, reason="pymc-challenges data are not present")
def test_from_challenges_reads_the_unc13_frames(tmp_path: Path) -> None:
    assert CHALLENGES is not None
    ds = from_challenges(CHALLENGES)
    assert validate_dataset(STORM_SCHEMA, ds) == []
    assert dict(ds["frames"].sizes) == {"frame": 2000, "row": 40, "col": 40}
    assert ds.attrs["pixel_nm"] == 127.0
    ref = challenges_reference()
    assert ds.attrs["offset_adu"] == pytest.approx(ref["offset_adu"], abs=1)
    assert ds.attrs["scale_adu"] == pytest.approx(ref["scale_adu"], abs=0.05)
    s = StormLocalisationSettings(**challenges_settings(ds))
    spots = find_spots(np.asarray(ds["frames"].values, float), s)
    assert spots.n_blobs == ref["n_blobs"]
    assert spots.patches.shape == (200, 11, 11)
    assert 2 * np.median(spots.background) == pytest.approx(ref["background_photons"], abs=2)
    assert load_model_spec(challenges_spec(ds)).model.settings["max_spots"] == 200
    with pytest.raises(ValueError, match="missing"):
        bad = tmp_path / "missing.npz"  # never write next to the shared real data
        np.savez(bad, frames=np.zeros((3, 4, 4)))
        from_challenges(bad)


@pytest.mark.slow
@pytest.mark.skipif(CHALLENGES is None, reason="pymc-challenges data are not present")
def test_real_spot_matches_the_notebook(registry: Registry) -> None:
    """The notebook's single-spot NUTS fit (blob 12, frame 5): same detection, same spot."""
    assert CHALLENGES is not None
    ds = from_challenges(CHALLENGES)
    settings = challenges_settings(ds, max_spots=1, amplitude_band=[0.45, 0.55])
    body = ", ".join(f"{k}: {v!r}" for k, v in settings.items())
    spec = f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: unc13-one-spot, project: microscopy}}
data:
  dataset: dataset://microscopy/unc13@latest
model:
  use: sci.storm_localisation@^1
  settings: {{{body}}}
  fit: {{seed: 58, tune: 1000, draws: 1000, chains: 4, engine: pymc}}
"""
    b = fit_spec(spec, ds, registry=registry, raw_schema=STORM_SCHEMA)
    ref = challenges_reference()["spot"]
    assert int(b.model.groups["constant_data"]["spot_frame"].values[0]) == ref["frame"]
    post = b.model.posterior
    for name in ("x_px", "y_px", "logN", "logb", "logs"):
        mean, sd = ref[name]
        v = post[name].values.ravel()
        assert v.mean() == pytest.approx(mean, abs=0.5 * sd + 0.005), name
        assert v.std() == pytest.approx(sd, rel=0.2), name
    assert float(post["photons"].mean()) == pytest.approx(ref["photons"], rel=0.02)
    assert float(post["psf_sd"].mean()) == pytest.approx(ref["psf_sd_nm"], abs=2)
    assert float(post["x"].std()) == pytest.approx(ref["x_nm_sd"], rel=0.15)
    assert "DIVERGENCES" not in {d.code for d in b.diagnostics if d.severity == "error"}
