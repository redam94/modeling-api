"""Raw schema, example specs, synthetic data and the real E31 digits for ``sci.ibp_features``.

* :func:`ibp_dataset` - small images made from known stroke features by the model's own
  forward process (truncated stick-breaking pattern probabilities, ``x = mu + z A + noise``),
  with known truth :data:`IBP_TRUTH`;
* :func:`from_challenges` - the UCI optdigits of pymc-challenges E31 as the notebook uses
  them: 30 training digits per class, 20 per class by the 13 other writers of the test file,
  cropped to 8 x 6 pixels, ink scaled to 0-1. The bottom half of every test digit is hidden
  with the ``visible`` mask, so the fit completes it (the notebook's held-out test).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

__all__ = [
    "CHALLENGES_FILE",
    "CHALLENGES_TEST_FILE",
    "DIGITS_SPEC_YAML",
    "IBP_SCHEMA",
    "IBP_SPEC_YAML",
    "IBP_TRUTH",
    "NOTEBOOK_REFERENCE",
    "from_challenges",
    "ibp_dataset",
    "stroke_features",
]

CHALLENGES_FILE = "optdigits_train.csv"
"""The training digits (30 writers) in pymc-challenges ``data/``."""
CHALLENGES_TEST_FILE = "optdigits_test.csv"
"""The test digits (13 other writers), read from the same directory when present."""

IBP_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "digit": {"kind": "categorical", "policy": "fixed"},
            "row": {"kind": "integer", "policy": "fixed"},
            "col": {"kind": "integer", "policy": "fixed"},
        },
        "vars": {
            "pixels": {
                "dims": ["digit", "row", "col"],
                "dtype": "float64",
                "role": "target",
                "units": "ink",
                "missing": "allow",
            },
            "visible": {"dims": ["digit", "row", "col"], "dtype": "bool", "role": "mask"},
        },
    }
)
"""Raw schema of :func:`ibp_dataset` and :func:`from_challenges`
(``pixels(digit, row, col)`` and the ``visible`` mask)."""

IBP_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: stroke-features-ibp, project: vision}
data:
  dataset: dataset://vision/stroke_images@latest
  bind:
    dims: {y: row, x: col}
    vars: {ink: pixels}
  split: {kind: mask_block, dims: [digit, y], fraction: 0.15, block: {y: 4}, seed: 3}
model:
  use: sci.ibp_features@^1
  settings: {n_features: 4, noise_sd: 0.2, score_draws: 100}
  fit:
    mode: evaluate_then_refit
    seed: 31
    tune: 150
    draws: 100
    chains: 2
    engine: pymc
    keep: {log_likelihood: true}
evaluate: {metrics: [rmse, mae, coverage_90, elpd_loo]}
outputs: {plots: [core.convergence, sci.field_map, sci.ppc_image_grid]}
"""
"""A tiny fit on :func:`ibp_dataset`: random top or bottom halves are held out and
completed (``mask_block`` over ``(digit, y)`` in blocks of 4 rows)."""

DIGITS_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: optdigits-ibp, project: vision}
data:
  dataset: dataset://vision/optdigits_e31@latest
  bind:
    dims: {y: row, x: col}
    vars: {ink: pixels, shown: visible}
model:
  use: sci.ibp_features@^1
  settings: {n_features: 8, noise_sd: 0.25, shown_mask: true}
  fit:
    mode: train_only
    seed: 2005
    tune: 1000
    draws: 1000
    chains: 4
    keep: {log_likelihood: true}
evaluate: {metrics: [rmse, mae]}
outputs:
  plots: [core.convergence, core.posterior_forest, sci.field_map, sci.ppc_image_grid]
"""
"""E31 at the notebook's settings: K = 8, sigma_X = 0.25, 4 chains of 1000 after 1000 warm-up
steps. The bottom halves of the 200 test digits are hidden; ``learned.heldout`` scores
their completion."""

IBP_TRUTH: dict[str, Any] = {"pi": (0.6, 0.45, 0.3), "noise_sd": 0.15, "level": 0.2}
"""Generating values of :func:`ibp_dataset`: feature probabilities, pixel noise and the ink
of the average image. The features are :func:`stroke_features`."""

NOTEBOOK_REFERENCE: dict[str, Any] = {
    "alpha_mean": 4.19,
    "sigma_A_mean": 0.16,
    "logpi_mean": (-0.69, -0.78, -0.86, -0.92, -0.96, -0.98, -1.07, -1.15),
    "pi_last_by_chain": (0.32, 0.30, 0.32, 0.33),
    "heldout_lpd_per_digit": -0.3,
    "heldout_rmse": 0.283,
}
"""Numbers the E31 notebook printed for the PyMC fit at K = 8 (4 chains, digits start)."""


def stroke_features(shape: tuple[int, int] = (8, 6), amplitude: float = 0.8) -> np.ndarray:
    """Three stroke images ``(3, ny, nx)``: a bar across the top, a stroke down the left side
    and a bar across the bottom-right quarter."""
    ny, nx = shape
    f = np.zeros((3, ny, nx))
    f[0, 1, :] = amplitude
    f[1, :, 1] = amplitude
    f[2, ny - 2, nx // 2 :] = amplitude
    return f


def ibp_dataset(
    *,
    n: int = 60,
    shape: tuple[int, int] = (8, 6),
    truth: dict[str, Any] | None = None,
    seed: int = 0,
) -> xr.Dataset:
    """Simulated images ``pixels(digit, row, col)`` with every pixel ``visible``.

    Each image owns stroke ``k`` with probability ``pi[k]`` and is
    ``level + sum of its strokes + Normal(0, noise_sd)`` noise (the model's forward process).
    """
    p = {**IBP_TRUTH, **(truth or {})}
    rng = np.random.default_rng(seed)
    feats = stroke_features(shape)
    pi = np.asarray(p["pi"], dtype=float)
    z = (rng.random((n, len(pi))) < pi[None, :]).astype(float)
    images = p["level"] + np.einsum("nk,kyx->nyx", z, feats)
    images = images + p["noise_sd"] * rng.standard_normal(images.shape)
    digits = np.array([f"img-{i:03d}" for i in range(n)])
    return xr.Dataset(
        {
            "pixels": (("digit", "row", "col"), images, {"units": "ink"}),
            "visible": (("digit", "row", "col"), np.ones(images.shape, dtype=bool)),
        },
        coords={
            "digit": digits,
            "row": np.arange(shape[0], dtype=np.int64),
            "col": np.arange(shape[1], dtype=np.int64),
        },
        attrs={"strokes_per_image": float(z.sum(1).mean())},
    )


def _take(
    pix: np.ndarray, labels: np.ndarray, per_class: int, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """The notebook's ``take``: ``per_class`` digits of each class, border columns dropped."""
    idx = np.concatenate(
        [rng.choice(np.flatnonzero(labels == d), per_class, replace=False) for d in range(10)]
    )
    return pix[idx][:, :, 1:7], labels[idx]


def _read(path: Path) -> tuple[np.ndarray, np.ndarray]:
    df = pd.read_csv(path, header=None)
    if df.shape[1] != 65:
        raise ValueError(f"{path}: expected 64 pixel columns and a label, got {df.shape[1]}")
    values = df.to_numpy()
    return values[:, :64].reshape(-1, 8, 8) / 16.0, values[:, 64].astype(np.int64)


def from_challenges(
    path: str | Path,
    test_path: str | Path | None = None,
    *,
    per_class: int = 30,
    per_class_test: int = 20,
    seed: int = 2005,
) -> xr.Dataset:
    """Read pymc-challenges' ``data/optdigits_train.csv`` (and ``optdigits_test.csv`` next to
    it, or ``test_path``) as the E31 notebook does, in :data:`IBP_SCHEMA`.

    The notebook draws ``per_class`` training digits of each class, then ``per_class_test``
    test digits, from ``numpy.random.default_rng(2005)`` before any other draw, so the same
    500 digits come back. Pixels are ink counts / 16 with the two blank border columns
    dropped (8 x 6). Test digits are labelled ``test-NNN`` and their bottom 4 rows are not
    ``visible``. Coordinates ``label(digit)`` and ``writers(digit)`` (``train``/``test``)
    keep the class and the file.
    """
    path = Path(path)
    pix, labels = _read(path)
    rng = np.random.default_rng(seed)
    train, train_lab = _take(pix, labels, per_class, rng)
    tp = Path(test_path) if test_path is not None else path.parent / "optdigits_test.csv"
    if tp.is_file() and per_class_test > 0:
        tpix, tlab = _read(tp)
        test, test_lab = _take(tpix, tlab, per_class_test, rng)
    else:
        test, test_lab = np.zeros((0, 8, 6)), np.zeros(0, dtype=np.int64)
    ink = np.concatenate([train, test])
    visible = np.ones(ink.shape, dtype=bool)
    visible[len(train) :, 4:, :] = False
    names = [f"train-{i:03d}" for i in range(len(train))] + [
        f"test-{i:03d}" for i in range(len(test))
    ]
    return xr.Dataset(
        {
            "pixels": (("digit", "row", "col"), ink.astype(float), {"units": "ink"}),
            "visible": (("digit", "row", "col"), visible),
        },
        coords={
            "digit": np.array(names),
            "row": np.arange(8, dtype=np.int64),
            "col": np.arange(6, dtype=np.int64),
            "label": ("digit", np.concatenate([train_lab, test_lab]).astype(np.int64)),
            "writers": ("digit", np.array(["train"] * len(train) + ["test"] * len(test))),
        },
    )
