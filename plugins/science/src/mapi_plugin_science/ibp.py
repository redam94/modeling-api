"""``sci.ibp_features``: latent binary features of small images, a truncated Indian buffet
process with the binary matrix summed out (E31, handwritten digits).

Canonical inputs (bind dataset names onto them in the spec)::

    dims  digit: categorical, fixed          (one image per level)
          y, x:  integer, fixed              (pixel rows and columns)
    vars  ink(digit, y, x): target, float64 - ink of each pixel (0 = blank, 1 = full); a
            missing value is a pixel the model does not see
          shown(digit, y, x): mask, bool (optional, setting ``shown_mask``) - False hides a
            pixel from the fit although its value is in the data (held-out halves)

**Model** (Griffiths & Ghahramani's linear-Gaussian latent feature model). Every image is
the average image plus the sum of the features it owns, plus white noise::

    x_n = mu + z_n A + e_n,   z_nk in {0, 1},   e_nd ~ Normal(0, sigma_X^2)
    A[k, :] ~ Normal(0, sigma_A^2),   sigma_A ~ HalfNormal(feature_scale_sd)

The feature probabilities come from the IBP's stick-breaking construction (Teh, Gorur &
Ghahramani 2007) truncated at ``K = n_features``: ``alpha ~ Gamma(a, b)``,
``nu_k ~ Beta(alpha, 1)``, ``pi_k = prod_{j <= k} nu_j``, ``z_nk ~ Bernoulli(pi_k)``.

**The binary matrix is summed out exactly.** With ``K`` features an image has ``2^K``
possible patterns ``c``, so ``p(x_n | A, pi) = sum_c prod_k pi_k^c_k (1 - pi_k)^(1 - c_k)
Normal(x_n | mu + c A, sigma_X^2 I)``: a ``logsumexp`` over ``2^K`` terms per image, added to
the model as a ``pm.Potential``. Only the pixels the model sees enter the sum, so an image
with a missing half is still used, and its pattern weights complete the missing half. The
cost doubles with each extra feature: ``K <= 10``.

**Start.** NUTS from the default start can fall into a trap: early in warm-up the random
features explain nothing, every ``pi_k`` falls to zero, and then no feature gets a gradient
again ("no features at all"). ``start: digits`` (the default) starts every chain at the
notebook's point, without jitter: features at half of ``K`` random (centred) images,
``nu_k = 0.8``, ``sigma_A = 0.3``, ``alpha = 2``. ``start: default`` uses the platform's
jittered start (it shows the trap).

**Variants** (``kind``): ``fixed_noise`` keeps ``sigma_X = noise_sd`` (0.25 in the notebook:
a resolution knob, one bitmap pixel of the 32 x 32 original moves up to 4 of the 16 counts);
``free_noise`` learns it with a HalfNormal(1) prior. Both store the log-likelihood of each
image (``fit.keep.log_likelihood``), so ``elpd_loo`` can compare them, and different ``K``.

**Outputs** (representation ``draws``; ``score_draws`` evenly spaced posterior draws):

* ``ink_hat(digit, y, x)`` - the expected image given the pixels the fit saw (``expected``:
  the denoised image where it was seen, the completion where it was not), or one draw of
  the pattern and the noise (``predictive``); ink units;
* ``features(feature, y, x)`` - the feature images ``A`` (change of ink from the average);
* ``feature_prob(feature)`` - ``pi_k``;
* ``owns(digit, feature)`` - the probability that an image owns a feature.

Scores use the pixels stored in the bundle (``observed_data/ink`` and ``constant_data/shown``),
never target values in the score payload, so a held-out pixel cannot leak into its own
prediction. Only the fitted digits can be scored (``digit`` is fixed): to complete a new
image, add it to the data with its missing pixels empty (or hidden by ``shown``) and fit.
Use a ``mask_block`` split over ``(digit, y)`` with ``block: {y: 4}`` to hold out halves.

**Diagnostics.** The posterior over ``A`` is multimodal like a mixture model's: chains often
disagree on ``pi`` (r-hat 1.15-1.9 in the notebook) while ``alpha`` and the predictions
agree. ``TRUNCATION_BINDS`` warns when the last ``pi_K`` is not small (more features would be
used) and ``FEATURES_COLLAPSED`` when a chain switched every feature off.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Literal

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import xarray as xr
from mapi_pymc.checkpoint import CheckpointSink, fit_key
from mapi_pymc.diagnostics import convergence_diagnostics
from mapi_pymc.engines import EngineHooks, EnginePlan, run_engine
from mapi_sdk.models import (
    PREDICTIONS,
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    ModelSettings,
    ScoreContext,
)
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, PosteriorPredictiveKeep, ScoreRequest
from pydantic import Field
from scipy.special import logsumexp

__all__ = [
    "DIGIT_DIM",
    "FEATURES",
    "FEATURE_DIM",
    "PIXEL_MEAN",
    "SHOWN",
    "TARGET",
    "X_DIM",
    "Y_DIM",
    "IBPFeaturesModel",
    "IBPFeaturesSettings",
    "heldout_scores",
    "pattern_logpost",
    "patterns",
    "row_loglik",
]

DIGIT_DIM = "digit"
Y_DIM = "y"
X_DIM = "x"
FEATURE_DIM = "feature"
TARGET = "ink"
SHOWN = "shown"
PIXEL_MEAN = "pixel_mean"
FEATURES = "features"
"""Posterior variable (and output) of the feature images ``A(feature, y, x)``."""
IMAGE_DIMS = (DIGIT_DIM, Y_DIM, X_DIM)
SCALARS = ("alpha", "sigma_A", "sigma_X")
"""Scalar parameters (``sigma_X`` only with ``kind: free_noise``)."""
MAX_FEATURES = 10
Kind = Literal["fixed_noise", "free_noise"]


class IBPFeaturesSettings(ModelSettings):
    """Settings of ``sci.ibp_features`` (ink on a 0-1 scale)."""

    n_features: int = Field(
        8,
        ge=1,
        le=MAX_FEATURES,
        description="Truncation K: the largest number of features. Each image sums over 2^K "
        "patterns, so each extra feature doubles the cost (x-ui-level: basic)",
    )
    kind: Kind = Field(
        "fixed_noise",
        description="fixed_noise keeps the pixel noise at noise_sd (the notebook); free_noise "
        "learns it (x-ui-level: basic)",
    )
    noise_sd: float = Field(
        0.25,
        gt=0,
        description="Pixel noise sigma_X (ink units). It sets the resolution: a smaller value "
        "makes more features worth keeping (x-ui-level: basic)",
    )
    alpha_shape: float = Field(1.0, gt=0, description="Gamma shape of the IBP mass alpha")
    alpha_rate: float = Field(1.0, gt=0, description="Gamma rate of the IBP mass alpha")
    feature_scale_sd: float = Field(1.0, gt=0, description="HalfNormal scale of sigma_A")
    start: Literal["digits", "default"] = Field(
        "digits",
        description="digits: every chain starts at half of K random images with nu = 0.8 and "
        "no jitter (avoids the 'no features' trap); default: the jittered platform start",
    )
    score_draws: int = Field(
        200, ge=1, le=4000, description="Posterior draws (spread over the chains) per score"
    )
    shown_mask: bool = Field(
        False,
        description="The data has a shown(digit, y, x) mask: pixels where it is False are "
        "hidden from the fit (held-out halves)",
    )
    truncation_warn: float = Field(
        0.05,
        gt=0,
        lt=1,
        description="Warn when the posterior mean of the last feature probability is above this",
    )


_TEMPLATE: dict[str, Any] = {
    "params": {"shown_mask": {"type": "boolean", "default": False}},
    "template": {
        "dims": {
            DIGIT_DIM: {
                "kind": "categorical",
                "policy": "fixed",
                "description": "One small image (a digit) per level",
            },
            Y_DIM: {"kind": "integer", "policy": "fixed", "description": "Pixel row (top = 0)"},
            X_DIM: {"kind": "integer", "policy": "fixed", "description": "Pixel column"},
        },
        "vars": {
            TARGET: {
                "dims": list(IMAGE_DIMS),
                "dtype": "float64",
                "role": "target",
                "units": "ink",
                "missing": "allow",
                "description": "Ink of each pixel (0 blank, 1 full); empty = not seen",
            }
        },
    },
    "cases": [
        {
            "when": {"shown_mask": True},
            "merge": {
                "vars": {
                    SHOWN: {
                        "dims": list(IMAGE_DIMS),
                        "dtype": "bool",
                        "role": "mask",
                        "description": "False hides the pixel from the fit (held out)",
                    }
                }
            },
        }
    ],
}

_IMAGE_GRID = {"y": Y_DIM, "x": X_DIM, "origin": "upper"}


# ================================================================================ the maths


def patterns(k: int) -> np.ndarray:
    """All ``2^k`` binary feature patterns, ``(2^k, k)`` floats, in ``itertools.product``
    order (the notebook's)."""
    return np.array(list(itertools.product([0, 1], repeat=k)), dtype=float)


def _log1mexp(x: np.ndarray) -> np.ndarray:
    """``log(1 - exp(x))`` for ``x < 0``."""
    x = np.minimum(x, -1e-300)
    return np.where(x > -0.6931, np.log(-np.expm1(x)), np.log1p(-np.exp(x)))


def pattern_logpost(
    xc: np.ndarray,
    seen: np.ndarray,
    a: np.ndarray,
    logpi: np.ndarray,
    sx: float,
    c: np.ndarray,
) -> np.ndarray:
    """``log p(pattern) + log Normal(seen pixels | pattern image, sx)`` per image and pattern,
    ``(N, 2^K)``. ``xc`` are centred images ``(N, D)`` (any value where not ``seen``), ``a``
    the features ``(K, D)``, ``logpi`` ``(K,)``. ``logsumexp`` over axis 1 is the
    log-likelihood of each image's seen pixels."""
    m = seen.astype(float)
    xo = np.where(seen, xc, 0.0)
    means = c @ a
    sq = (xo**2).sum(1)[:, None] - 2.0 * xo @ means.T + m @ (means**2).T
    prior = c @ logpi + (1.0 - c) @ _log1mexp(logpi)
    norm = -0.5 * m.sum(1) * np.log(2.0 * np.pi * sx**2)
    return prior[None, :] - sq / (2.0 * sx**2) + norm[:, None]


def row_loglik(
    xc: np.ndarray, seen: np.ndarray, a: np.ndarray, logpi: np.ndarray, sx: float, c: np.ndarray
) -> np.ndarray:
    """Log-likelihood of each image's seen pixels with ``z`` summed out, ``(N,)`` - the NumPy
    twin of the potential in :meth:`IBPFeaturesModel.build_model`."""
    return np.asarray(logsumexp(pattern_logpost(xc, seen, a, logpi, sx, c), axis=1))


def heldout_scores(
    ink: np.ndarray,
    seen: np.ndarray,
    hidden: np.ndarray,
    pixel_mean: np.ndarray,
    a_draws: np.ndarray,
    logpi_draws: np.ndarray,
    sx_draws: np.ndarray,
) -> dict[str, Any]:
    """Complete the ``hidden`` pixels of each image from its ``seen`` pixels, over posterior
    draws (the notebook's ``heldout_pymc``).

    ``ink``, ``seen``, ``hidden`` are ``(N, D)`` (``hidden`` pixels must hold their true
    values); ``a_draws`` ``(S, K, D)``, ``logpi_draws`` ``(S, K)``, ``sx_draws`` ``(S,)``.
    Returns ``lpd`` - ``log p(hidden | seen)`` per image (averaged over draws in probability),
    ``rmse`` of the posterior-mean completion on the hidden pixels, ``mean`` - the completed
    images ``(N, D)`` in ink units - and ``n_images`` with at least one hidden pixel.
    """
    xc = ink - pixel_mean[None, :]
    rows = hidden.any(1)
    c = patterns(a_draws.shape[1])
    lls, means = [], []
    h = hidden.astype(float)
    for a, logpi, sx in zip(a_draws, logpi_draws, sx_draws, strict=True):
        lp = pattern_logpost(xc, seen, a, logpi, float(sx), c)
        logw = lp - logsumexp(lp, axis=1, keepdims=True)
        pm_ = c @ a
        xh = np.where(hidden, xc, 0.0)
        sq = (xh**2).sum(1)[:, None] - 2.0 * xh @ pm_.T + h @ (pm_**2).T
        llh = -0.5 * sq / sx**2 - 0.5 * h.sum(1)[:, None] * np.log(2 * np.pi * sx**2)
        lls.append(logsumexp(logw + llh, axis=1))
        means.append(np.exp(logw) @ pm_)
    ll = np.asarray(lls)
    lpd = logsumexp(ll, axis=0) - np.log(len(ll))
    mean = np.mean(means, axis=0) + pixel_mean[None, :]
    err = (mean - ink)[hidden]
    return {
        "lpd": lpd[rows],
        "rmse": float(np.sqrt(np.mean(err**2))) if err.size else float("nan"),
        "mean": mean,
        "n_images": int(rows.sum()),
    }


# ============================================================================== the model


@dataclass(frozen=True, slots=True)
class _Images:
    """The fitted images flattened to ``(digit, pixel)``."""

    ink: np.ndarray
    """``(N, D)`` ink (NaN where missing)."""
    seen: np.ndarray
    """``(N, D)`` bool: finite and shown - the pixels the model uses."""
    shown: np.ndarray
    """``(N, D)`` bool: the ``shown`` mask (all True without one)."""
    mean: np.ndarray
    """``(D,)`` the average image the features are added to."""
    digits: np.ndarray
    ys: np.ndarray
    xs: np.ndarray

    @property
    def centred(self) -> np.ndarray:
        return np.where(self.seen, self.ink - self.mean[None, :], 0.0)

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.ys), len(self.xs)

    def image(self, flat: np.ndarray) -> np.ndarray:
        """``(..., D)`` -> ``(..., ny, nx)``."""
        return flat.reshape(*flat.shape[:-1], *self.shape)

    @classmethod
    def from_dataset(cls, ds: xr.Dataset, s: IBPFeaturesSettings) -> _Images:
        if TARGET not in ds:
            raise MapiError(
                f"the data has no {TARGET!r} variable",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TARGET}",
            )
        ink_da = ds[TARGET].transpose(*IMAGE_DIMS)
        n, ny, nx = ink_da.shape
        ink = np.asarray(ink_da.values, dtype=float).reshape(n, ny * nx)
        if s.shown_mask:
            if SHOWN not in ds:
                raise MapiError(
                    f"shown_mask is set but the data has no {SHOWN!r} variable",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{SHOWN}",
                )
            shown = np.asarray(ds[SHOWN].transpose(*IMAGE_DIMS).values, dtype=bool)
            shown = shown.reshape(n, ny * nx)
        else:
            shown = np.ones_like(ink, dtype=bool)
        return cls.build(
            ink,
            shown,
            np.asarray(ds[DIGIT_DIM].values),
            np.asarray(ds[Y_DIM].values),
            np.asarray(ds[X_DIM].values),
        )

    @classmethod
    def build(
        cls, ink: np.ndarray, shown: np.ndarray, digits: Any, ys: Any, xs: Any, mean: Any = None
    ) -> _Images:
        seen = np.isfinite(ink) & shown
        if mean is None:
            if not seen.any():
                raise MapiError(
                    f"{TARGET!r} has no pixel the model can see",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{TARGET}",
                )
            mean = _average_image(ink, seen)
        return cls(ink, seen, shown, np.asarray(mean, dtype=float), digits, ys, xs)

    @classmethod
    def from_fitted(cls, fitted: Fitted) -> _Images:
        obs = fitted.groups.get("observed_data")
        const = fitted.groups.get("constant_data")
        if obs is None or const is None or TARGET not in obs or PIXEL_MEAN not in const:
            raise MapiError(
                "the bundle of sci.ibp_features has no stored images",
                code=ErrorCode.INTERNAL_ERROR,
            )
        ink_da = obs[TARGET].transpose(*IMAGE_DIMS)
        n, ny, nx = ink_da.shape
        ink = np.asarray(ink_da.values, dtype=float).reshape(n, ny * nx)
        shown = (
            np.asarray(const[SHOWN].transpose(*IMAGE_DIMS).values, dtype=bool).reshape(n, -1)
            if SHOWN in const
            else np.ones_like(ink, dtype=bool)
        )
        mean = np.asarray(const[PIXEL_MEAN].transpose(Y_DIM, X_DIM).values).reshape(-1)
        return cls.build(
            ink,
            shown,
            np.asarray(ink_da[DIGIT_DIM].values),
            np.asarray(ink_da[Y_DIM].values),
            np.asarray(ink_da[X_DIM].values),
            mean,
        )


def _average_image(ink: np.ndarray, seen: np.ndarray) -> np.ndarray:
    """The average of the images with every pixel seen (the notebook's training mean); per
    pixel over the seen values when no image is complete; 0 where nothing is seen."""
    complete = np.asarray(seen.all(axis=1), dtype=bool)
    use = seen & complete[:, None] if complete.any() else seen
    count = use.sum(0)
    total = np.where(use, ink, 0.0).sum(0)
    return np.where(count > 0, total / np.maximum(count, 1), 0.0)


class IBPFeaturesModel(CustomModel[IBPFeaturesSettings]):
    """Indian buffet process latent features of small images, z summed out (E31)."""

    id = "sci.ibp_features"
    version = "1.0.0"
    Settings = IBPFeaturesSettings
    description = (
        "Latent binary features of small images (handwritten digits): every image is the "
        "average image plus the features it owns. A stick-breaking Indian buffet process "
        "truncated at K features, with every image's binary pattern summed out over 2^K "
        "patterns, so NUTS samples the feature images and their probabilities. Outputs are "
        "the feature images, which images own them, and completions of unseen pixels."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "evaluate"}),
        identified_quantities=["alpha", "sigma_A", "ink_hat"],
        deterministic_given_seed=True,
        checkpointable=True,
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "ink_hat": {
            "dims": list(IMAGE_DIMS),
            "units_of": TARGET,
            "description": "Expected image given the seen pixels (completion where unseen), "
            "or a draw of pattern and noise",
            "grid": {**_IMAGE_GRID, "frame": DIGIT_DIM, "colormap": "gray"},
        },
        "features": {
            "dims": [FEATURE_DIM, Y_DIM, X_DIM],
            "units": "ink",
            "description": "Feature images: the ink a feature adds to (or removes from) the "
            "average image",
            "grid": {**_IMAGE_GRID, "frame": FEATURE_DIM, "colormap": "diverging"},
        },
        "feature_prob": {
            "dims": [FEATURE_DIM],
            "units": "1",
            "description": "Probability pi_k that an image owns feature k",
        },
        "owns": {
            "dims": [DIGIT_DIM, FEATURE_DIM],
            "units": "1",
            "description": "Probability that each image owns each feature, given its seen pixels",
        },
    }
    plots = (
        "core.convergence",
        "core.posterior_forest",
        "sci.field_map",
        "sci.ppc_image_grid",
        "sci.image_residuals",
    )
    # 300 images, K = 8: about 45 s with nutpie on 4 cores, 2-3 min with the pymc engine
    resources = ResourceHint(est_fit_seconds=180)
    default_engine: ClassVar[str] = "pymc"

    # ================================================================== build
    def build_model(self, images: _Images, s: IBPFeaturesSettings) -> pm.Model:
        """The truncated stick-breaking IBP with every image's pattern summed out."""
        k = s.n_features
        c = patterns(k)
        xo = images.centred
        m = images.seen.astype(float)
        n_seen = m.sum(1)
        coords = {FEATURE_DIM: np.arange(k), "pixel": np.arange(xo.shape[1])}
        with pm.Model(coords=coords) as model:
            alpha = pm.Gamma("alpha", s.alpha_shape, s.alpha_rate)
            nu = pm.Beta("nu", alpha, 1.0, dims=FEATURE_DIM)
            logpi = pm.Deterministic("logpi", pt.cumsum(pt.log(nu)), dims=FEATURE_DIM)
            sa = pm.HalfNormal("sigma_A", s.feature_scale_sd)
            a = pm.Normal("A", 0.0, sa, dims=(FEATURE_DIM, "pixel"))
            sx: Any = pm.HalfNormal("sigma_X", 1.0) if s.kind == "free_noise" else s.noise_sd
            means = pt.dot(pt.as_tensor(c), a)
            xt = pt.as_tensor(xo)
            sq = (
                pt.as_tensor((xo**2).sum(1))[:, None]
                - 2.0 * pt.dot(xt, means.T)
                + pt.dot(pt.as_tensor(m), (means**2).T)
            )
            prior_z = pt.dot(pt.as_tensor(c), logpi) + pt.dot(
                pt.as_tensor(1.0 - c), pt.log1mexp(logpi)
            )
            ll_rows = pt.logsumexp(
                prior_z[None, :] - sq / (2.0 * sx**2), axis=1
            ) - 0.5 * pt.as_tensor(n_seen) * pt.log(2.0 * np.pi * sx**2)
            pm.Potential("loglik", ll_rows.sum())
        return model

    def start_point(self, images: _Images, s: IBPFeaturesSettings, seed: int) -> dict[str, Any]:
        """The notebook's start: half of ``K`` random centred images, ``nu = 0.8``."""
        rng = _rng(seed, "start")
        complete = np.flatnonzero(images.seen.all(1))
        pool = complete if complete.size >= s.n_features else np.arange(images.ink.shape[0])
        pick = rng.choice(pool, s.n_features, replace=pool.size < s.n_features)
        start: dict[str, Any] = {
            "A": 0.5 * images.centred[pick],
            "nu": np.full(s.n_features, 0.8),
            "sigma_A": 0.3,
            "alpha": 2.0,
        }
        if s.kind == "free_noise":
            start["sigma_X"] = s.noise_sd
        return start

    # ================================================================== fit
    def fit(
        self, ds: xr.Dataset, s: IBPFeaturesSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        images = _Images.from_dataset(ds, s)
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        model = self.build_model(images, s)
        plan = EnginePlan.from_config(cfg, default_engine=self.default_engine, seed=seed)
        if s.start == "digits":
            plan = replace(plan, initvals=self.start_point(images, s, seed), jitter=False)
        sink = ctx.checkpoint_sink if isinstance(ctx.checkpoint_sink, CheckpointSink) else None
        key = None
        if sink is not None:
            key = fit_key(
                model_id=self.id,
                version=self.version,
                settings=s.model_dump(mode="json"),
                fit_config=cfg.model_dump(mode="json"),
                data=ds,
            )
        report = None
        if ctx.on_progress is not None:

            def report(fraction: float, message: str) -> None:
                ctx.progress(0.85 * fraction, message)

        with _quiet():
            result = run_engine(
                model,
                plan,
                EngineHooks(progress=report, cancel=ctx.cancel, log=ctx.log, sink=sink, key=key),
            )
        ctx.check_cancelled()
        posterior = result.group("posterior")
        if posterior is None:
            raise MapiError(
                f"engine {result.engine!r} returned no posterior", code=ErrorCode.INTERNAL_ERROR
            )
        keep = ["alpha", "nu", "logpi", "sigma_A"] + (["sigma_X"] if "sigma_X" in posterior else [])
        a = posterior["A"].transpose("chain", "draw", FEATURE_DIM, "pixel").values
        posterior = posterior[keep].assign(
            {
                FEATURES: (
                    ("chain", "draw", FEATURE_DIM, Y_DIM, X_DIM),
                    a.reshape(*a.shape[:3], *images.shape),
                )
            }
        )
        posterior = posterior.assign_coords(
            {FEATURE_DIM: np.arange(s.n_features), Y_DIM: images.ys, X_DIM: images.xs}
        )
        groups: dict[str, xr.Dataset] = {"posterior": _clean(posterior, result.engine)}
        stats = result.group("sample_stats")
        if stats is not None:
            names = [
                v
                for v in stats.data_vars
                if stats[v].dtype.kind in "biuf" and set(stats[v].dims) <= {"chain", "draw"}
            ]
            groups["sample_stats"] = _clean(stats[names], result.engine)
        image_coords = {DIGIT_DIM: images.digits, Y_DIM: images.ys, X_DIM: images.xs}
        n = images.ink.shape[0]
        groups["observed_data"] = _clean(
            xr.Dataset({TARGET: (IMAGE_DIMS, images.image(images.ink))}, coords=image_coords),
            result.engine,
        )
        const: dict[str, Any] = {PIXEL_MEAN: ((Y_DIM, X_DIM), images.image(images.mean))}
        if s.shown_mask:
            const[SHOWN] = (IMAGE_DIMS, images.image(images.shown))
        groups["constant_data"] = _clean(xr.Dataset(const, coords=image_coords), result.engine)
        ctx.progress(0.9, "pointwise log-likelihood")
        draws = _flat_draws(groups["posterior"], s)
        if cfg.keep.log_likelihood:
            ll = self._pointwise(images, draws, s)
            shape = (posterior.sizes["chain"], posterior.sizes["draw"], n)
            groups["log_likelihood"] = _clean(
                xr.Dataset(
                    {TARGET: (("chain", "draw", DIGIT_DIM), ll.reshape(shape))},
                    coords={
                        "chain": posterior["chain"].values,
                        "draw": posterior["draw"].values,
                        DIGIT_DIM: images.digits,
                    },
                ),
                result.engine,
            )
        if cfg.keep.posterior_predictive:
            thin = (
                cfg.keep.posterior_predictive.thin
                if isinstance(cfg.keep.posterior_predictive, PosteriorPredictiveKeep)
                else 1
            )
            pp = _clean(self._replicates(images, groups["posterior"], s, thin, seed), result.engine)
            pp[TARGET].attrs["grid"] = _grid("ink_hat")
            groups["posterior_predictive"] = pp
        with _quiet():
            prior = pm.sample_prior_predictive(
                draws=200, model=model, var_names=["alpha", "sigma_A"], random_seed=seed
            )
        groups["prior"] = _clean(prior["prior"].to_dataset(), result.engine)
        learned: dict[str, Any] = {
            "engine": result.engine,
            "tune": result.plan.tune,
            "draws": result.plan.draws,
            "chains": result.plan.chains,
            "resumed_from": result.resumed_from,
            "checkpoints": result.checkpoints,
            "kind": s.kind,
            "n_features": s.n_features,
            "patterns": 2**s.n_features,
            "start": s.start,
            "images": int(n),
            "pixels_seen": int(images.seen.sum()),
            "pi_mean_by_chain": xr.apply_ufunc(np.exp, posterior["logpi"])
            .mean("draw")
            .round(4)
            .values.tolist(),
        }
        hidden = ~images.shown & np.isfinite(images.ink)
        if hidden.any():
            # the notebook's held-out test: complete the hidden pixels from the shown ones
            sub = _thin(draws, s.score_draws)
            res = heldout_scores(
                images.ink, images.seen, hidden, images.mean, sub.a, sub.logpi, sub.sx
            )
            learned["heldout"] = {
                "images": res["n_images"],
                "lpd_per_image": round(float(np.mean(res["lpd"])), 4),
                "rmse": round(res["rmse"], 5),
            }
        ctx.progress(1.0, "posterior ready")
        return FitOutput(groups=groups, learned=learned)

    def _pointwise(self, images: _Images, draws: _Draws, s: IBPFeaturesSettings) -> np.ndarray:
        """``(S, N)`` log-likelihood of each image's seen pixels, every posterior draw."""
        c = patterns(s.n_features)
        xc = images.centred
        return np.stack(
            [
                row_loglik(xc, images.seen, a, lp, float(sx), c)
                for a, lp, sx in zip(draws.a, draws.logpi, draws.sx, strict=True)
            ]
        )

    def _replicates(
        self, images: _Images, post: xr.Dataset, s: IBPFeaturesSettings, thin: int, seed: int
    ) -> xr.Dataset:
        """Replicate images ``ink(chain, draw, digit, y, x)`` of every ``thin``-th draw."""
        sub = post.isel(draw=slice(None, None, thin))
        draws = _flat_draws(sub, s)
        rng = _rng(seed, "posterior_predictive")
        reps = np.stack(
            [
                _draw_images(images, a, lp, float(sx), s, rng, predictive=True)[0]
                for a, lp, sx in zip(draws.a, draws.logpi, draws.sx, strict=True)
            ]
        )
        shape = (sub.sizes["chain"], sub.sizes["draw"], *images.ink.shape[:1], *images.shape)
        return xr.Dataset(
            {TARGET: (("chain", "draw", *IMAGE_DIMS), reps.reshape(shape))},
            coords={
                "chain": sub["chain"].values,
                "draw": sub["draw"].values,
                DIGIT_DIM: images.digits,
                Y_DIM: images.ys,
                X_DIM: images.xs,
            },
        )

    # ================================================================== diagnostics
    def diagnostics(self, fitted: Fitted, ds: xr.Dataset) -> list[Diagnostic]:
        post = fitted.groups.get("posterior")
        if post is None:
            return []
        names = [n for n in ("alpha", "sigma_A", "sigma_X", "logpi") if n in post]
        out = convergence_diagnostics(post, fitted.groups.get("sample_stats"), var_names=names)
        s = self.parse_settings(fitted.settings)
        pi = xr.apply_ufunc(np.exp, post["logpi"])
        last = float(pi.isel({FEATURE_DIM: -1}).mean())
        if last > s.truncation_warn:
            out.append(
                Diagnostic(
                    code="TRUNCATION_BINDS",
                    severity="warning",
                    metric="pi_last",
                    value=last,
                    threshold=s.truncation_warn,
                    message_expert=f"the last feature probability pi_{s.n_features} has "
                    f"posterior mean {last:.2f}: the truncation at K = {s.n_features} binds, "
                    "and more features would be used",
                    message_plain="The model uses all the features it has. More features "
                    "would possibly describe the images better.",
                    suggested_action="Increase n_features (each extra feature doubles the "
                    "cost), or increase noise_sd to look at the images at a coarser level.",
                )
            )
        first = pi.isel({FEATURE_DIM: 0}).mean("draw").values
        dead = [int(i) for i in np.flatnonzero(first < 0.01)]
        if dead:
            out.append(
                Diagnostic(
                    code="FEATURES_COLLAPSED",
                    severity="warning",
                    metric="pi_first",
                    value=float(first.min()),
                    threshold=0.01,
                    message_expert=f"chain(s) {dead} switched every feature off (mean pi_1 < "
                    "0.01): a trap of the start, not a mode",
                    message_plain="Some chains stopped at a point where the images use no "
                    "features. The results of these chains are not correct.",
                    suggested_action="Use start: digits (the default), which starts every "
                    "chain from real images.",
                )
            )
        return out

    # ================================================================== score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        images = _Images.from_fitted(fitted)
        post = fitted.posterior
        chains, idx = _draw_index(post, s.score_draws)
        sub = post.isel(chain=chains, draw=idx)
        draws = _flat_draws(sub, s)
        rng = _rng(ctx.seed, f"score/{ctx.kind}")
        predictive = ctx.kind == "predictive"
        inks, owns = [], []
        for i, (a, lp, sx) in enumerate(zip(draws.a, draws.logpi, draws.sx, strict=True)):
            if i % 16 == 0:
                ctx.check_cancelled()
            ink, own = _draw_images(images, a, lp, float(sx), s, rng, predictive=predictive)
            inks.append(ink)
            owns.append(own)
        nc, nd = len(chains), len(idx)
        n = images.ink.shape[0]
        k = s.n_features
        coords = {
            "chain": np.asarray(post["chain"].values)[chains],
            "draw": np.asarray(post["draw"].values)[idx],
            DIGIT_DIM: images.digits,
            Y_DIM: images.ys,
            X_DIM: images.xs,
            FEATURE_DIM: np.arange(k),
        }
        noise = "seeded_white" if predictive else "none"
        pred = xr.Dataset(
            {
                "ink_hat": xr.DataArray(
                    np.asarray(inks).reshape(nc, nd, n, *images.shape),
                    dims=("chain", "draw", *IMAGE_DIMS),
                    attrs={"kind": ctx.kind, "noise": noise, "grid": _grid("ink_hat")},
                ),
                FEATURES: xr.DataArray(
                    images.image(draws.a).reshape(nc, nd, k, *images.shape),
                    dims=("chain", "draw", FEATURE_DIM, Y_DIM, X_DIM),
                    attrs={"kind": "posterior", "units": "ink", "grid": _grid(FEATURES)},
                ),
                "feature_prob": xr.DataArray(
                    np.exp(draws.logpi).reshape(nc, nd, k),
                    dims=("chain", "draw", FEATURE_DIM),
                    attrs={"kind": "posterior", "units": "1"},
                ),
                "owns": xr.DataArray(
                    np.asarray(owns).reshape(nc, nd, n, k),
                    dims=("chain", "draw", DIGIT_DIM, FEATURE_DIM),
                    attrs={"kind": "posterior", "units": "1"},
                ),
            },
            coords=coords,
        )
        sel = {d: np.asarray(ds[d].values) for d in IMAGE_DIMS if d in ds.coords}
        if DIGIT_DIM in sel:
            unknown = np.setdiff1d(sel[DIGIT_DIM], images.digits)
            if unknown.size:
                raise MapiError(
                    f"{unknown.size} digit(s) were not in the fit (for example "
                    f"{unknown[0]!r}): add them to the data with their unseen pixels empty "
                    "and fit again",
                    code=ErrorCode.COORD_NOT_ALLOWED,
                    pointer=f"/data/{DIGIT_DIM}",
                )
        pred = pred.sel(sel)
        return xr.DataTree.from_dict({PREDICTIONS: pred})


# ============================================================================== helpers


@dataclass(frozen=True, slots=True)
class _Draws:
    a: np.ndarray
    """``(S, K, D)``"""
    logpi: np.ndarray
    """``(S, K)``"""
    sx: np.ndarray
    """``(S,)``"""


def _grid(output: str) -> str:
    """The output's grid declaration as JSON (a ``grid`` attribute the sci recipes read)."""
    return json.dumps(IBPFeaturesModel.outputs[output]["grid"])


def _flat_draws(post: xr.Dataset, s: IBPFeaturesSettings) -> _Draws:
    """Posterior draws stacked over ``(chain, draw)`` in C order."""
    a = post[FEATURES].transpose("chain", "draw", FEATURE_DIM, Y_DIM, X_DIM).values
    lp = post["logpi"].transpose("chain", "draw", FEATURE_DIM).values
    nc, nd = a.shape[:2]
    a = a.reshape(nc, nd, a.shape[2], -1)
    if "sigma_X" in post:
        sx = post["sigma_X"].transpose("chain", "draw").values.reshape(-1)
    else:
        sx = np.full(nc * nd, s.noise_sd)
    return _Draws(a.reshape(nc * nd, *a.shape[2:]), lp.reshape(nc * nd, -1), sx)


def _thin(d: _Draws, n: int) -> _Draws:
    idx = np.unique(np.linspace(0, len(d.sx) - 1, min(n, len(d.sx))).round().astype(np.int64))
    return _Draws(d.a[idx], d.logpi[idx], d.sx[idx])


def _draw_images(
    images: _Images,
    a: np.ndarray,
    logpi: np.ndarray,
    sx: float,
    s: IBPFeaturesSettings,
    rng: np.random.Generator,
    *,
    predictive: bool,
) -> tuple[np.ndarray, np.ndarray]:
    """One posterior draw: the images ``(N, D)`` (expected, or a pattern and noise draw) and
    the ownership probabilities ``(N, K)`` given each image's seen pixels."""
    c = patterns(s.n_features)
    lp = pattern_logpost(images.centred, images.seen, a, logpi, sx, c)
    w = np.exp(lp - logsumexp(lp, axis=1, keepdims=True))
    means = c @ a
    own = np.clip(w @ c, 0.0, 1.0)
    if not predictive:
        return w @ means + images.mean[None, :], own
    u = rng.random((w.shape[0], 1))
    pick = np.minimum((w.cumsum(1) < u).sum(1), w.shape[1] - 1)
    noise = sx * rng.standard_normal(images.ink.shape)
    return means[pick] + noise + images.mean[None, :], own


def _draw_index(post: xr.Dataset, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Every chain and ``n // chains`` evenly spaced draw positions (at least one)."""
    chains, draws = int(post.sizes["chain"]), int(post.sizes["draw"])
    per = min(draws, max(1, n // chains))
    idx = np.unique(np.linspace(0, draws - 1, per).round().astype(np.int64))
    return np.arange(chains), idx


def _rng(seed: int, name: str) -> np.random.Generator:
    digest = hashlib.sha256(name.encode()).digest()
    return np.random.default_rng([int(seed), int.from_bytes(digest[:8], "little")])


def _clean(ds: xr.Dataset, engine: str) -> xr.Dataset:
    out = ds.copy()
    for v in out.variables:
        out[v].attrs = {}
    out.attrs = {
        "inference_library": "pymc",
        "inference_library_version": pm.__version__,
        "engine": engine,
    }
    return out


@contextmanager
def _quiet() -> Iterator[None]:
    logger = logging.getLogger("pymc")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        logger.setLevel(level)
