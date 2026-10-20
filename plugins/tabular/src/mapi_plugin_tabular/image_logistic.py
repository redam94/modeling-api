"""``tabular.image_logistic``: Bayesian multinomial logistic regression on small images (v0.14).

Canonical inputs (the layout of an ``image_folder`` ingest; ``{p}``, ``{t}`` and ``{i}`` are
``settings.pixels``, ``settings.target`` and ``settings.image_dim``)::

    dims  {i}: categorical, new_levels (pooled)    y, x: integer, fixed    channel: fixed
    vars  {p}({i}, y, x, channel): uint8 covariate (any numbers in 0..255 are accepted)
          {t}({i}): str target (missing allowed; those images do not train)

Features: the pixels are scaled to [0, 1], averaged over ``downsample x downsample`` blocks
and flattened. ``features: pca`` (default) projects them on the first ``n_components``
principal components of the training images (``np.linalg.svd`` of the centred rows; at most
``_PCA_ROWS`` seeded training rows fit the basis) and standardises the scores;
``features: pixels`` uses the standardised block means directly (at most ``max_features``).

The model, with ``K`` classes and ``F`` features ``Z``::

    alpha ~ ZeroSumNormal(2, dims=class)
    beta  ~ ZeroSumNormal(prior_sigma, dims=(feature, class))   (zero sum over class)
    label ~ Categorical(softmax(alpha + Z @ beta))

sampled with :func:`mapi_pymc.engines.run_engine` (nutpie by default). The ``features``
group keeps the preprocessing (``mean``, ``components``, ``scale``) so scoring projects new
images on the same basis. Scores are ``y_hat(chain, draw, image, class)`` softmax
probabilities (``meaning = class_probability``), thinned to at most 500 draws by a ``draw``
stride; ``chain`` and ``draw`` are kept (the image dim is never called ``sample``, the
platform's name for draws).

When a tracker is attached the fit logs ``image.*`` params and the PCA
``explained_variance``; when it takes media it also logs ``pca_components`` (the first 8
components side by side) and ``class_means`` (the mean training image of each class).
"""

from __future__ import annotations

import json
import logging
import math
import warnings
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Annotated, Any, ClassVar, Literal

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import xarray as xr
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
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.media import MAX_IMAGE_SIDE, MAX_MEDIA_CAPTION_CHARS
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, model_validator

__all__ = ["FEATURES", "MAX_SCORE_DRAWS", "ImageLogistic", "ImageLogisticSettings"]

Name = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
FEATURES = "features"
"""The bundle group holding the image preprocessing (mean, components, scale)."""
MAX_SCORE_DRAWS = 500
"""Scores keep at most this many posterior draws (a ``draw`` stride per chain)."""
CLASS = "class"
FEATURE = "feature"
FEATURE_PX = "feature_px"
_Y, _X, _CHANNEL = "y", "x", "channel"
_PCA_ROWS = 5000
_MAX_PCA_INPUTS = 16_384
_MAX_LOGGED_COMPONENTS = 8
_MIN_SCALE = 1e-6


class ImageLogisticSettings(ModelSettings):
    """Settings of ``tabular.image_logistic``."""

    pixels: Name = Field("pixels", description="The image variable (image, y, x, channel)")
    target: Name = Field("label", description="The class label of each image")
    image_dim: Name = Field("image", description="The dim that lists the images")
    features: Literal["pca", "pixels"] = Field(
        "pca", description="pca: principal component scores; pixels: the block means"
    )
    n_components: int = Field(16, ge=2, le=64, description="PCA components (features: pca)")
    downsample: int = Field(1, ge=1, le=16, description="Block size of the mean over y and x")
    max_features: int = Field(
        256, ge=4, le=1024, description="features: pixels: the most block means allowed"
    )
    prior_sigma: float = Field(1.0, gt=0, le=10, description="Prior sd of the class weights")
    class_order: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(
        None, min_length=2, max_length=50, description="The classes in order (default: sorted)"
    )

    @model_validator(mode="after")
    def _check(self) -> ImageLogisticSettings:
        if self.image_dim in {_Y, _X, _CHANNEL, CLASS, FEATURE, "sample", "chain", "draw"}:
            raise ValueError(f"image_dim {self.image_dim!r} is a reserved dim name")
        if self.pixels == self.target:
            raise ValueError("pixels and target must be different variables")
        if self.class_order is not None and len(set(self.class_order)) != len(self.class_order):
            raise ValueError("class_order must not repeat a class")
        return self


_TEMPLATE: dict[str, Any] = {
    "params": {
        "pixels": {"type": "string", "default": "pixels"},
        "target": {"type": "string", "default": "label"},
        "image_dim": {"type": "string", "default": "image"},
    },
    "template": {
        "dims": {
            "{image_dim}": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "pooled",
            },
            _Y: {"kind": "integer", "policy": "fixed"},
            _X: {"kind": "integer", "policy": "fixed"},
            _CHANNEL: {"kind": "categorical", "policy": "fixed"},
        },
        "vars": {
            "{pixels}": {
                "dims": ["{image_dim}", _Y, _X, _CHANNEL],
                "dtype": "uint8",
                "role": "covariate",
            },
            "{target}": {
                "dims": ["{image_dim}"],
                "dtype": "str",
                "role": "target",
                "missing": "allow",
            },
        },
    },
}


class ImageLogistic(CustomModel[ImageLogisticSettings]):
    """Multinomial logistic regression on PCA scores (or block means) of small images."""

    id = "tabular.image_logistic"
    version = "1.0.0"
    Settings = ImageLogisticSettings
    description = (
        "A Bayesian multinomial logistic regression for small images of the same size: "
        "principal component scores (or block means) of the pixels predict the class, with "
        "zero-sum normal priors on the class weights."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "evaluate"}),
        loo_valid=False,
        loo_invalid_reason="the model stores no pointwise log-likelihood",
        deterministic_given_seed=True,
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {
            "dims": [CLASS],
            "dims_of": "label",
            "units": "1",
            "description": "Probability of each class per image (sums to 1 over class)",
        }
    }
    plots = ("core.roc_pr", "core.confusion", "core.convergence")
    pad_extend = False
    default_engine: ClassVar[str] = "nutpie"

    # ================================================================== fit
    def build_model(
        self, z: np.ndarray, codes: np.ndarray, classes: Sequence[str], s: ImageLogisticSettings
    ) -> pm.Model:
        """The PyMC model of ``codes`` (``0..K-1``) given standardised features ``z``."""
        coords = {CLASS: list(classes), FEATURE: _feature_names(z.shape[1], s.features)}
        with pm.Model(coords=coords) as model:
            alpha = pm.ZeroSumNormal("alpha", sigma=2.0, dims=CLASS)
            beta = pm.ZeroSumNormal("beta", sigma=s.prior_sigma, dims=(FEATURE, CLASS))
            logit = alpha[None, :] + pt.dot(pt.as_tensor(z), beta)
            pm.Categorical("label", logit_p=logit, observed=codes)
        return model

    def fit(
        self, ds: xr.Dataset, s: ImageLogisticSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        seed = int(cfg.seed if cfg.seed is not None else ctx.seed)
        px = _pixels(ds, s, phase="fit")
        if s.target not in ds:
            raise MapiError(
                f"the fit data has no target {s.target!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{s.target}",
            )
        texts = _labels(ds[s.target], s.image_dim)
        classes = _classes(texts, s)
        keep = np.array([t is not None for t in texts])
        if keep.sum() < 2:
            raise MapiError("fewer than two images have a label", code=ErrorCode.SCHEMA_INVALID)
        index = {c: j for j, c in enumerate(classes)}
        codes = np.array([index[t] for t in texts if t is not None], dtype=np.int64)
        if np.unique(codes).size < 2:
            raise MapiError(
                "the labelled images have one class only; a classifier needs two or more",
                code=ErrorCode.SCHEMA_INVALID,
            )
        blocks = _block_mean(px[keep], s.downsample)
        _, hh, ww, cc = blocks.shape
        flat = blocks.reshape(blocks.shape[0], -1)
        basis = _fit_basis(flat, s, seed)
        z = _project(flat, basis)
        ctx.check_cancelled()
        if ctx.tracking:
            ctx.log_params(
                {
                    "image.features": s.features,
                    "image.n_components": int(z.shape[1]),
                    "image.height": int(px.shape[1]),
                    "image.width": int(px.shape[2]),
                }
            )
            if basis.explained is not None:
                ctx.log_metric("explained_variance", basis.explained)
            if ctx.tracking_media:
                self._log_images(ctx, blocks, codes, classes, basis, (hh, ww, cc))
        model = self.build_model(z, codes, classes, s)
        plan = EnginePlan.from_config(cfg, default_engine=self.default_engine, seed=seed)
        report = None
        if ctx.on_progress is not None:

            def report(fraction: float, message: str) -> None:
                ctx.progress(0.9 * fraction, message)

        with _quiet():
            result = run_engine(model, plan, EngineHooks.for_context(ctx, progress=report))
        ctx.check_cancelled()
        posterior = result.group("posterior")
        if posterior is None:
            raise MapiError(
                f"engine {result.engine!r} returned no posterior", code=ErrorCode.INTERNAL_ERROR
            )
        groups: dict[str, xr.Dataset] = {
            "posterior": _clean(posterior[["alpha", "beta"]], result.engine)
        }
        stats = result.group("sample_stats")
        if stats is not None:
            numeric = [
                v
                for v in stats.data_vars
                if stats[v].dtype.kind in "biuf" and set(stats[v].dims) <= {"chain", "draw"}
            ]
            groups["sample_stats"] = _clean(stats[numeric], result.engine)
        groups[FEATURES] = basis.dataset(s, (int(px.shape[1]), int(px.shape[2]), cc), classes)
        ctx.progress(1.0, "posterior ready")
        learned: dict[str, Any] = {
            "engine": result.engine,
            "tune": result.plan.tune,
            "draws": result.plan.draws,
            "chains": result.plan.chains,
            "classes": list(classes),
            "height": int(px.shape[1]),
            "width": int(px.shape[2]),
            "channels": int(cc),
            "channel_labels": [str(c) for c in np.asarray(ds[_CHANNEL].values).tolist()]
            if _CHANNEL in ds.coords
            else [],
            "downsample": s.downsample,
            "features": s.features,
            "n_features": int(z.shape[1]),
            "n_train": int(codes.size),
            "explained_variance": basis.explained,
        }
        return FitOutput(groups=groups, learned=learned)

    def _log_images(
        self,
        ctx: FitContext,
        blocks: np.ndarray,
        codes: np.ndarray,
        classes: Sequence[str],
        basis: _Basis,
        shape: tuple[int, int, int],
    ) -> None:
        """``pca_components`` and ``class_means`` grids (one tile per component / class).

        The grids wrap into rows and keep only the tiles that fit ``MAX_IMAGE_SIDE``; an image
        the media limits still refuse is skipped with a warning line, so media never fail a fit.
        """
        if basis.components is not None:
            comps = basis.components[:_MAX_LOGGED_COMPONENTS].reshape(-1, *shape)
            grid, n = _grid([_unit(c) for c in comps])
            self._log_grid(
                ctx,
                "pca_components",
                grid,
                f"The first {n} principal components of the training images",
            )
        present = [j for j in range(len(classes)) if (codes == j).any()]
        if present:
            means = [np.clip(blocks[codes == j].mean(axis=0), 0.0, 1.0) for j in present]
            grid, n = _grid(means)
            shown = ", ".join(classes[j] for j in present[:n])
            more = f" (the first {n} of {len(present)} classes)" if n < len(present) else ""
            self._log_grid(
                ctx, "class_means", grid, f"The mean training image of each class{more}: {shown}"
            )

    @staticmethod
    def _log_grid(ctx: FitContext, key: str, grid: np.ndarray | None, caption: str) -> None:
        if grid is None:
            ctx.log_line(f"{key}: one tile is larger than the image limit; not logged", "warning")
            return
        try:
            ctx.log_image(key, grid, caption=caption[:MAX_MEDIA_CAPTION_CHARS])
        except ValueError as exc:  # over a media limit (for example the encoded size)
            ctx.log_line(f"{key}: not logged ({exc})", "warning")

    def diagnostics(self, fitted: Fitted, ds: xr.Dataset) -> list[Diagnostic]:
        post = fitted.groups.get("posterior")
        if post is None:
            return []
        return convergence_diagnostics(
            post, fitted.groups.get("sample_stats"), var_names=["alpha", "beta"]
        )

    # ================================================================== score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        feats = fitted.groups.get(FEATURES)
        if feats is None:
            raise MapiError(
                "the bundle has no features group; refit the model",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        basis = _Basis.from_dataset(feats)
        px = _pixels(ds, s, phase="score")
        height, width = int(feats.attrs["height"]), int(feats.attrs["width"])
        channels = int(feats.attrs["channels"])
        if px.shape[1:] != (height, width, channels):
            raise MapiError(
                f"the images are {px.shape[1]} x {px.shape[2]} with {px.shape[3]} channel(s); "
                f"the model was fitted on {height} x {width} with {channels}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{s.pixels}",
            )
        flat = _block_mean(px, basis.downsample).reshape(px.shape[0], -1)
        z = _project(flat, basis)
        post = _thin(fitted.posterior, MAX_SCORE_DRAWS)
        alpha = np.asarray(post["alpha"].transpose("chain", "draw", CLASS).values, dtype=float)
        beta = np.asarray(
            post["beta"].transpose("chain", "draw", FEATURE, CLASS).values, dtype=float
        )
        ctx.check_cancelled()
        logit = alpha[:, :, None, :] + np.einsum("nf,cdfk->cdnk", z, beta)
        logit -= logit.max(axis=-1, keepdims=True)
        prob = np.exp(logit)
        prob /= prob.sum(axis=-1, keepdims=True)
        classes = json.loads(str(feats.attrs["classes"]))
        if ctx.kind == "predictive":
            ctx.info("y_hat holds class probabilities for expected and predictive outputs")
        y_hat = xr.DataArray(
            prob,
            dims=("chain", "draw", s.image_dim, CLASS),
            coords={
                "chain": post["chain"].values,
                "draw": post["draw"].values,
                s.image_dim: ds[s.image_dim].values,
                CLASS: classes,
            },
            attrs={"meaning": "class_probability", "units": "1"},
        )
        return xr.DataTree.from_dict({PREDICTIONS: xr.Dataset({"y_hat": y_hat})})


# ================================================================================ features


class _Basis:
    """Fitted preprocessing: ``z = ((x - mean) @ components.T) / scale`` (pca) or
    ``z = (x - mean) / scale`` (pixels)."""

    __slots__ = ("components", "downsample", "explained", "mean", "scale")

    def __init__(
        self,
        mean: np.ndarray,
        scale: np.ndarray,
        components: np.ndarray | None,
        downsample: int,
        explained: float | None,
    ) -> None:
        self.mean = mean
        self.scale = scale
        self.components = components
        self.downsample = downsample
        self.explained = explained

    def dataset(
        self, s: ImageLogisticSettings, shape: tuple[int, int, int], classes: Sequence[str]
    ) -> xr.Dataset:
        n = self.scale.size
        data: dict[str, Any] = {
            "mean": ((FEATURE_PX,), self.mean),
            "scale": ((FEATURE,), self.scale),
        }
        if self.components is not None:
            data["components"] = ((FEATURE, FEATURE_PX), self.components)
        return xr.Dataset(
            data,
            coords={FEATURE: _feature_names(n, s.features)},
            attrs={
                "downsample": self.downsample,
                "height": shape[0],
                "width": shape[1],
                "channels": shape[2],
                "classes": json.dumps(list(classes)),
                "features": s.features,
                **({"explained_variance": self.explained} if self.explained is not None else {}),
            },
        )

    @classmethod
    def from_dataset(cls, ds: xr.Dataset) -> _Basis:
        comps = ds["components"].values if "components" in ds else None
        return cls(
            np.asarray(ds["mean"].values, dtype=float),
            np.asarray(ds["scale"].values, dtype=float),
            None if comps is None else np.asarray(comps, dtype=float),
            int(ds.attrs.get("downsample", 1)),
            None,
        )


def _fit_basis(flat: np.ndarray, s: ImageLogisticSettings, seed: int) -> _Basis:
    n, f = flat.shape
    mean = flat.mean(axis=0)
    centred = flat - mean
    if s.features == "pixels":
        if f > s.max_features:
            raise MapiError(
                f"the images give {f} block means; max_features is {s.max_features}. Set a "
                "larger downsample or use features: pca",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/max_features",
            )
        scale = np.maximum(centred.std(axis=0), _MIN_SCALE)
        return _Basis(mean, scale, None, s.downsample, None)
    if f > _MAX_PCA_INPUTS:
        raise MapiError(
            f"the images give {f} values per image after the block mean; PCA takes at most "
            f"{_MAX_PCA_INPUTS}. Set a larger downsample",
            code=ErrorCode.SETTINGS_INVALID,
            pointer="/model/settings/downsample",
        )
    rows = centred
    if n > _PCA_ROWS:
        pick = np.sort(np.random.default_rng(seed).choice(n, _PCA_ROWS, replace=False))
        rows = centred[pick]
    k = min(s.n_components, rows.shape[0] - 1, f)
    if k < 1:
        raise MapiError("too few training images for PCA", code=ErrorCode.SCHEMA_INVALID)
    _, sing, vt = np.linalg.svd(rows, full_matrices=False)
    comps = vt[:k]
    # a deterministic sign: the largest loading of each component is positive
    flip = np.sign(comps[np.arange(k), np.argmax(np.abs(comps), axis=1)])
    comps = comps * np.where(flip == 0, 1.0, flip)[:, None]
    total = float((sing**2).sum())
    explained = float((sing[:k] ** 2).sum() / total) if total > 0 else 0.0
    scores = centred @ comps.T
    scale = np.maximum(scores.std(axis=0), _MIN_SCALE)
    return _Basis(mean, scale, comps, s.downsample, explained)


def _project(flat: np.ndarray, basis: _Basis) -> np.ndarray:
    centred = flat - basis.mean
    scores = centred if basis.components is None else centred @ basis.components.T
    return np.asarray(scores / basis.scale, dtype=float)


def _block_mean(px: np.ndarray, k: int) -> np.ndarray:
    """``(n, H//k, W//k, C)`` means of ``k x k`` blocks (edge rows/columns beyond a whole
    block are dropped)."""
    if k == 1:
        return px
    n, h, w, c = px.shape
    hh, ww = h // k, w // k
    if hh < 1 or ww < 1:
        raise MapiError(
            f"downsample {k} is larger than the images ({h} x {w})",
            code=ErrorCode.SETTINGS_INVALID,
            pointer="/model/settings/downsample",
        )
    cut = px[:, : hh * k, : ww * k, :]
    return cut.reshape(n, hh, k, ww, k, c).mean(axis=(2, 4))


def _pixels(ds: xr.Dataset, s: ImageLogisticSettings, *, phase: str) -> np.ndarray:
    """``(n, H, W, C)`` pixels scaled to [0, 1]; values must be finite numbers in 0..255."""
    code = ErrorCode.MISSING_FUTURE_COVARIATE if phase == "score" else ErrorCode.SCHEMA_INVALID
    if s.pixels not in ds:
        raise MapiError(
            f"the {phase} data has no image variable {s.pixels!r}",
            code=code,
            pointer=f"/data/{s.pixels}",
        )
    da = ds[s.pixels]
    want = (s.image_dim, _Y, _X, _CHANNEL)
    if set(da.dims) != set(want):
        raise MapiError(
            f"{s.pixels!r} must have the dims {list(want)}, not {list(da.dims)}",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{s.pixels}",
        )
    if da.dtype.kind not in "uifb":
        raise MapiError(
            f"{s.pixels!r} must hold numbers, not {da.dtype}",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{s.pixels}",
        )
    values = np.asarray(da.transpose(*want).values, dtype=float)
    if not np.isfinite(values).all() or values.min(initial=0) < 0 or values.max(initial=0) > 255:
        raise MapiError(
            f"{s.pixels!r} must hold pixel values from 0 to 255 without missing values",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{s.pixels}",
        )
    return values / 255.0


def _label_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return str(int(value)) if value.is_integer() else repr(value)
    text = str(value)
    return text if text.strip() else None


def _labels(da: xr.DataArray, image_dim: str) -> list[str | None]:
    if da.dims != (image_dim,):
        raise MapiError(
            f"the target must be one-dimensional along {image_dim!r}, not {list(da.dims)}",
            code=ErrorCode.SCHEMA_INVALID,
        )
    return [_label_text(v) for v in np.asarray(da.values).tolist()]


def _classes(texts: Sequence[str | None], s: ImageLogisticSettings) -> list[str]:
    present = sorted({t for t in texts if t is not None})
    if s.class_order is None:
        if len(present) > 50:
            raise MapiError(
                f"the target has {len(present)} classes; the model takes at most 50",
                code=ErrorCode.SCHEMA_INVALID,
            )
        return present
    missing = [t for t in present if t not in s.class_order]
    if missing:
        raise MapiError(
            "class_order does not name every label",
            code=ErrorCode.SETTINGS_INVALID,
            pointer="/model/settings/class_order",
            untrusted_text=", ".join(missing[:10]),
        )
    return list(s.class_order)


def _feature_names(n: int, features: str) -> list[str]:
    prefix = "pc" if features == "pca" else "px"
    return [f"{prefix}{j}" for j in range(n)]


def _thin(post: xr.Dataset, limit: int) -> xr.Dataset:
    """At most ``limit`` draws in total: every ``stride``-th draw of each chain."""
    chains, draws = int(post.sizes["chain"]), int(post.sizes["draw"])
    per_chain = max(1, limit // max(chains, 1))
    stride = max(1, math.ceil(draws / per_chain))
    return post.isel(draw=slice(None, None, stride)) if stride > 1 else post


# ================================================================================== images


def _unit(a: np.ndarray) -> np.ndarray:
    """``a`` stretched to [0, 1] (constant arrays become 0.5)."""
    lo, hi = float(a.min()), float(a.max())
    return np.full_like(a, 0.5) if hi - lo < 1e-12 else (a - lo) / (hi - lo)


def _grid(
    tiles: Sequence[np.ndarray], gap: int = 1, limit: int = MAX_IMAGE_SIDE
) -> tuple[np.ndarray | None, int]:
    """Tiles ``(h, w, c)`` in rows with a ``gap`` of white, and how many tiles it holds.

    The rows wrap so that each side stays within ``limit`` pixels; tiles that do not fit are
    left out. ``(None, 0)`` when one tile is already larger than ``limit``. Gray tiles give
    ``(H, W)``.
    """
    h, w, c = tiles[0].shape
    if h > limit or w > limit:
        return None, 0
    cols = min(len(tiles), (limit + gap) // (w + gap))
    rows = min(math.ceil(len(tiles) / cols), (limit + gap) // (h + gap))
    n = min(len(tiles), rows * cols)
    rows = math.ceil(n / cols)
    out = np.ones((rows * (h + gap) - gap, cols * (w + gap) - gap, c))
    for i, t in enumerate(tiles[:n]):
        r, k = divmod(i, cols)
        out[r * (h + gap) : r * (h + gap) + h, k * (w + gap) : k * (w + gap) + w, :] = t
    if c == 1:
        return out[..., 0], n
    return (out[..., :3] if c >= 3 else out[..., 0]), n


# ================================================================================= helpers


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
