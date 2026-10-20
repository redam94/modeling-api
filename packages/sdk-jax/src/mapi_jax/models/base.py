"""What the ``dl.*`` models share: capabilities, settings, the image training path, the
uncertainty step, the bundle layout and scoring.

Bundle (contract with the ``ml.*`` training charts and the runner's default plots):

* groups ``training`` (per-epoch history, see :mod:`mapi_jax.trainer`) and, for image
  classifiers with validation images, ``misclassified``;
* native ``weights.safetensors`` (:func:`mapi_jax.nets.network_tensors`, metadata
  ``mapi_arch``) and, with ``uncertainty.method: laplace``, ``laplace.safetensors``
  (``mean`` and ``chol`` or ``var``);
* learned ``arch``, ``task``, ``classes``, ``image_size``, ``channels``, ``normalize``,
  ``features`` (``dl.mlp``), ``uncertainty`` (``method``, ``draws``, ``prior_precision``,
  ``hessian``) and ``train`` (``best_epoch``, ``epochs_run``, ``stopped_early``).

Scores are ``y_hat(chain=1, draw, <rows>, class)`` class probabilities (``meaning =
class_probability``), or ``y_hat(chain=1, draw, <rows>)`` for a regression. Only the requested
rows are scored (:func:`mapi_sdk.models.requested_positions`); padded rows are NaN.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal

import jax
import jax.numpy as jnp
import numpy as np
import xarray as xr
from flax import nnx
from mapi_sdk.collection import ArrayImageLoader, Augment, ImageLoader
from mapi_sdk.models import (
    PREDICTIONS,
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    ModelSettings,
    ScoreContext,
    requested_positions,
)
from mapi_sdk.safetensors import read_safetensors, write_safetensors
from mapi_spec.capabilities import Capabilities, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.resources import ResourceClass
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, model_validator

from mapi_jax.config import TrainConfig, UncertaintyConfig
from mapi_jax.nets import ArchSpec, Network, build_network, network_tensors, set_tensors
from mapi_jax.tasks import ClassificationTask
from mapi_jax.trainer import MISCLASSIFIED_GROUP, TRAINING_GROUP, Trainer, TrainResult
from mapi_jax.uncertainty import (
    LaplacePosterior,
    draw_positions,
    fit_laplace,
    laplace_logits,
    laplace_thetas,
    n_draws,
)
from mapi_jax.weights import ARCH_METADATA

__all__ = [
    "CLASS",
    "DL_VERSION",
    "IMAGE_PLOTS",
    "IMAGE_TEMPLATE",
    "LAPLACE_FILE",
    "MAX_CLASSES",
    "POSTERIOR_GROUP",
    "WEIGHTS_FILE",
    "DLImageModel",
    "ImageSettings",
    "Name",
    "Normalize",
    "class_codes",
    "dl_capabilities",
    "fit_seed",
    "network_logits",
    "softmax",
]

Name = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
DL_VERSION = "1.0.0"
CLASS = "class"
WEIGHTS_FILE = "weights.safetensors"
LAPLACE_FILE = "laplace.safetensors"
MAX_CLASSES = 1000
IMAGE_PLOTS = (
    "ml.training_curves",
    "ml.confusion_epochs",
    "ml.misclassified",
    "core.roc_pr",
    "core.confusion",
)
_Y, _X, _CHANNEL = "y", "x", "channel"
_RESERVED_DIMS = {_Y, _X, _CHANNEL, CLASS, "feature", "sample", "chain", "draw"}
_NORMALIZE_SAMPLE = 2000


def dl_capabilities(input: Literal["dataset", "collection"]) -> Capabilities:
    return Capabilities(
        inference="custom",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "evaluate"}),
        loo_valid=False,
        loo_invalid_reason="a network trained by gradient descent has no pointwise log likelihood",
        deterministic_given_seed=True,
        checkpointable=True,
        input=input,
    )


IMAGE_TEMPLATE: dict[str, Any] = {
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


class ImageSettings(ModelSettings):
    """Settings every image model takes."""

    pixels: Name = Field("pixels", description="The image variable (image, y, x, channel)")
    target: Name = Field("label", description="The class label of each image")
    image_dim: Name = Field("image", description="The dim that lists the images")
    image_size: tuple[int, int] | None = Field(
        None, description="Height and width the images are resized to (default: their size)"
    )
    mode: Literal["auto", "gray", "rgb"] = Field("auto", description="Channels of the input")
    normalize: Literal["unit", "standardize"] = Field(
        "unit", description="unit: pixels / 255; standardize: per-channel training mean and sd"
    )
    augment: Augment = Field(default_factory=Augment, description="Training augmentation")
    train: TrainConfig = Field(default_factory=TrainConfig)
    uncertainty: UncertaintyConfig = Field(default_factory=UncertaintyConfig)
    class_order: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(
        None, min_length=2, max_length=MAX_CLASSES, description="Classes in order (default: sorted)"
    )

    @model_validator(mode="after")
    def _check(self) -> ImageSettings:
        if self.image_dim in _RESERVED_DIMS:
            raise ValueError(f"image_dim {self.image_dim!r} is a reserved dim name")
        if self.pixels == self.target:
            raise ValueError("pixels and target must be different variables")
        if self.class_order is not None and len(set(self.class_order)) != len(self.class_order):
            raise ValueError("class_order must not repeat a class")
        if self.image_size is not None and min(self.image_size) < 1:
            raise ValueError("image_size must be (height, width) with both >= 1")
        return self


# ================================================================================= helpers


def fit_seed(cfg: FitConfig, ctx: FitContext) -> int:
    return int(cfg.seed if cfg.seed is not None else ctx.seed)


def checkpoint_key(
    ctx: FitContext, model_id: str, settings: ModelSettings, seed: int, phase: str = ""
) -> str:
    """``ctx.fit_key`` (or a hash of the settings and seed), varied per training phase."""
    base = (
        ctx.fit_key
        or hashlib.sha256(
            json.dumps(
                {"model": model_id, "settings": settings.model_dump(mode="json"), "seed": seed},
                sort_keys=True,
            ).encode()
        ).hexdigest()[:32]
    )
    if not phase:
        return base
    return hashlib.sha256(f"{base}:{phase}".encode()).hexdigest()[:32]


def checkpoint_every(cfg: FitConfig) -> int | None:
    ck = cfg.checkpoint
    return None if ck is None else getattr(ck, "every_epochs", None)


@dataclass(frozen=True, slots=True)
class Normalize:
    mean: tuple[float, ...]
    std: tuple[float, ...]
    kind: str = "unit"

    def __call__(self, raw: np.ndarray) -> np.ndarray:
        x = np.asarray(raw, dtype=np.float32) / 255.0
        if self.kind == "unit":
            return x
        return ((x - np.asarray(self.mean, np.float32)) / np.asarray(self.std, np.float32)).astype(
            np.float32
        )

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "mean": list(self.mean), "std": list(self.std)}

    @classmethod
    def from_json(cls, doc: Any) -> Normalize:
        if not isinstance(doc, dict):
            return cls((0.0,), (1.0,), "unit")
        return cls(
            tuple(float(v) for v in doc.get("mean", [0.0])),
            tuple(float(v) for v in doc.get("std", [1.0])),
            str(doc.get("kind", "unit")),
        )


def fit_normalize(kind: str, loader: ImageLoader, batch_size: int) -> Normalize:
    c = loader.channels
    if kind == "unit":
        return Normalize((0.0,) * c, (1.0,) * c, "unit")
    total = np.zeros(c)
    total2 = np.zeros(c)
    count = 0
    for batch in loader.batches(batch_size, shuffle=False):
        x = np.asarray(batch.pixels, dtype=np.float64) / 255.0
        total += x.sum(axis=(0, 1, 2))
        total2 += (x**2).sum(axis=(0, 1, 2))
        count += x.shape[0] * x.shape[1] * x.shape[2]
        if count >= _NORMALIZE_SAMPLE * x.shape[1] * x.shape[2]:
            break
    mean = total / max(count, 1)
    std = np.sqrt(np.maximum(total2 / max(count, 1) - mean**2, 1e-12))
    std = np.where(std < 1e-6, 1.0, std)
    return Normalize(tuple(float(v) for v in mean), tuple(float(v) for v in std), "standardize")


def _pad(a: np.ndarray, size: int) -> np.ndarray:
    if a.shape[0] >= size:
        return a
    pad = np.zeros((size - a.shape[0], *a.shape[1:]), dtype=a.dtype)
    return np.concatenate([a, pad], axis=0)


@nnx.jit
def _features(net: Network, x: jax.Array) -> jax.Array:
    return net.features(x)


@nnx.jit
def _logits(net: Network, x: jax.Array) -> jax.Array:
    return net(x)


@nnx.jit
def _dropout_logits(net: Network, x: jax.Array, key: jax.Array) -> jax.Array:
    return net(x, key)


def network_features(
    net: Network, raws: Iterable[np.ndarray], preprocess: Callable[[np.ndarray], Any], bs: int
) -> np.ndarray:
    """``(N, F)`` penultimate features (deterministic) of every row of ``raws`` batches."""
    out = []
    for raw in raws:
        m = raw.shape[0]
        out.append(np.asarray(_features(net, jnp.asarray(preprocess(_pad(raw, bs)))))[:m])
    return np.concatenate(out).astype(np.float64) if out else np.zeros((0, 0))


def network_logits(
    net: Network,
    raws: Iterable[np.ndarray],
    preprocess: Callable[[np.ndarray], Any],
    bs: int,
    *,
    method: str,
    positions: np.ndarray,
    seed: int,
    laplace: LaplacePosterior | None,
    n_out: int,
    check: Callable[[], None] | None = None,
) -> np.ndarray:
    """``(D, N, K)`` outputs of the draws ``positions`` for every row of ``raws``."""
    thetas = (
        laplace_thetas(laplace, positions, seed)
        if method == "laplace" and laplace is not None
        else None
    )
    base = jax.random.key(seed + 2)
    out: list[np.ndarray] = []
    for raw in raws:
        if check is not None:
            check()
        m = raw.shape[0]
        x = jnp.asarray(preprocess(_pad(raw, bs)))
        if thetas is not None:
            feats = np.asarray(_features(net, x))[:m]
            out.append(laplace_logits(feats, thetas, n_out))
        elif method == "mc_dropout":
            out.append(
                np.stack(
                    [
                        np.asarray(_dropout_logits(net, x, jax.random.fold_in(base, int(d))))[:m]
                        for d in positions
                    ]
                ).astype(np.float64)
            )
        else:
            one = np.asarray(_logits(net, x))[:m].astype(np.float64)
            out.append(np.repeat(one[None], positions.size, axis=0))
    if not out:
        return np.zeros((positions.size, 0, n_out))
    return np.concatenate(out, axis=1)


def softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return np.asarray(e / e.sum(axis=-1, keepdims=True))


def uncertainty_learned(
    u: UncertaintyConfig, cfg: FitConfig, laplace: LaplacePosterior | None
) -> dict[str, Any]:
    return {
        "method": u.method,
        "draws": n_draws(u.method, u.draws, cfg.draws),
        "prior_precision": None if laplace is None else laplace.prior_precision,
        "hessian": None if laplace is None else laplace.hessian,
    }


def native_files(
    net: Network, arch: ArchSpec, laplace: LaplacePosterior | None
) -> dict[str, bytes]:
    native = {
        WEIGHTS_FILE: write_safetensors(
            network_tensors(net), {ARCH_METADATA: arch.model_dump_json()}
        )
    }
    if laplace is not None:
        native[LAPLACE_FILE] = write_safetensors(laplace.tensors())
    return native


def restore_network(fitted: Fitted) -> tuple[Network, ArchSpec, LaplacePosterior | None]:
    """The trained network (and Laplace posterior) of a bundle."""
    learned: dict[str, Any] = dict(fitted.learned)
    raw = fitted.native.get(WEIGHTS_FILE)
    if raw is None or not isinstance(learned.get("arch"), dict):
        raise MapiError(
            "the bundle has no network weights; refit the model",
            code=ErrorCode.INTEGRITY_FAILURE,
        )
    arch = ArchSpec.model_validate(learned["arch"])
    task = learned.get("task")
    classes = learned.get("classes") or []
    n_out = len(classes) if task == "classification" else 1
    net = build_network(arch, n_out, seed=0)
    tensors, _ = read_safetensors(raw)
    set_tensors(net, tensors)
    laplace = None
    unc = learned.get("uncertainty")
    lap = fitted.native.get(LAPLACE_FILE)
    if lap is not None and isinstance(unc, dict) and unc.get("prior_precision") is not None:
        lt, _ = read_safetensors(lap)
        laplace = LaplacePosterior.from_tensors(
            lt,
            prior_precision=float(unc["prior_precision"]),
            n_out=n_out,
        )
    return net, arch, laplace


def laplace_for(
    net: Network,
    feats: np.ndarray,
    u: UncertaintyConfig,
    *,
    task: Literal["classification", "regression"],
    noise_var: float = 1.0,
) -> LaplacePosterior:
    tensors = network_tensors(net)
    return fit_laplace(
        feats,
        tensors["head.kernel"].astype(np.float64),
        tensors["head.bias"].astype(np.float64),
        task=task,
        hessian=u.hessian,
        prior_precision=u.prior_precision,
        noise_var=noise_var,
    )


POSTERIOR_GROUP = "posterior"
"""The bundle group that names the model's draws: the harness thins draws from it, so a
score request with ``output.draws`` gets DRAWS (as ``mapi_sklearn`` bootstrap bundles)."""


def posterior_group(uncertainty: Mapping[str, Any]) -> xr.Dataset:
    """``draw_id(chain=1, draw)``: one entry per draw of the uncertainty method (Laplace or
    MC-dropout draws, or one draw for ``none``)."""
    n = max(1, int(uncertainty.get("draws") or 1))
    return xr.Dataset(
        {"draw_id": (("chain", "draw"), np.arange(n, dtype=np.int64)[None, :])},
        coords={"chain": [0], "draw": np.arange(n)},
        attrs={"inference": str(uncertainty.get("method", "none"))},
    )


def train_result_groups(
    result: TrainResult, uncertainty: Mapping[str, Any]
) -> dict[str, xr.Dataset]:
    groups = {TRAINING_GROUP: result.history, POSTERIOR_GROUP: posterior_group(uncertainty)}
    if result.misclassified is not None:
        groups[MISCLASSIFIED_GROUP] = result.misclassified
    return groups


# ============================================================================== image model


class DLImageModel[S: ImageSettings](CustomModel[S]):
    """Base of the image classifiers: ``fit`` (a dataset) and ``fit_batches`` (a loader)
    share one training path; ``score``/``score_batches`` share one prediction path."""

    version = DL_VERSION
    capabilities = dl_capabilities("collection")
    schema_template = IMAGE_TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {
            "dims": [CLASS],
            "dims_of": "label",
            "units": "1",
            "description": "Probability of each class per image (sums to 1 over class)",
        }
    }
    plots = IMAGE_PLOTS
    pad_extend = False
    resources = ResourceHint(min_class=ResourceClass.S, default_class=ResourceClass.M, gpu=True)

    # ---------------------------------------------------------------------- overridable
    def build(
        self, s: S, channels: int, size: tuple[int, int], n_out: int, seed: int, ctx: FitContext
    ) -> tuple[Network, ArchSpec]:
        raise NotImplementedError

    def input_mode(self, s: S, loader: ImageLoader, ctx: FitContext) -> Literal["gray", "rgb"]:
        mode: Literal["gray", "rgb"] = getattr(
            loader, "mode", "rgb" if loader.channels == 3 else "gray"
        )
        return mode if s.mode == "auto" else s.mode

    def input_size(self, s: S, loader: ImageLoader, ctx: FitContext) -> tuple[int, int]:
        if s.image_size is not None:
            return (int(s.image_size[0]), int(s.image_size[1]))
        return loader.size

    def train(
        self,
        net: Network,
        arch: ArchSpec,
        task: ClassificationTask,
        s: S,
        cfg: FitConfig,
        ctx: FitContext,
        train: ImageLoader,
        val: ImageLoader | None,
        preprocess: Callable[[np.ndarray], Any],
        seed: int,
    ) -> TrainResult:
        trainer = Trainer(
            net,
            task,
            s.train,
            ctx,
            seed=seed,
            preprocess=preprocess,
            checkpoint_key=checkpoint_key(ctx, self.id, s, seed),
            checkpoint_every=checkpoint_every(cfg),
            augment=s.augment if s.augment.active else None,
            arch_params=arch.model_dump(mode="json"),
        )
        return trainer.fit(train, val)

    # ------------------------------------------------------------------------------ fit
    def fit(self, ds: xr.Dataset, s: S, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        if s.target not in ds:
            raise MapiError(
                f"the fit data has no target {s.target!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{s.target}",
            )
        loader = ArrayImageLoader(ds, pixels=s.pixels, target=s.target, dim=s.image_dim)
        return self.fit_batches(loader, s, cfg, ctx)

    def fit_batches(self, loader: ImageLoader, s: S, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        seed = fit_seed(cfg, ctx)
        labels = loader.labels
        if labels is None:
            raise MapiError(
                f"the images have no labels ({s.target!r})",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{s.target}",
            )
        keep = np.array([bool(str(v).strip()) for v in labels], dtype=bool)
        if keep.sum() < 2:
            raise MapiError("fewer than two images have a label", code=ErrorCode.SCHEMA_INVALID)
        loader = loader.subset(keep)
        classes = self._classes(loader, s)
        mode = self.input_mode(s, loader, ctx)
        size = self.input_size(s, loader, ctx)
        loader = loader.with_classes(classes).with_mode(mode).with_size(size)
        train, val = self._validation(loader, s, seed)
        bs = min(s.train.batch_size, train.n)
        norm = fit_normalize(s.normalize, train, bs)
        task = ClassificationTask(classes, s.train.label_smoothing)
        net, arch = self.build(s, loader.channels, size, len(classes), seed, ctx)
        ctx.check_cancelled()
        result = self.train(net, arch, task, s, cfg, ctx, train, val, norm, seed)
        ctx.check_cancelled()
        net = result.network
        laplace = None
        if s.uncertainty.method == "laplace":
            rows = _row_sample(train.n, s.uncertainty.max_rows, seed)
            sub = train.subset(rows)
            feats = network_features(
                net, (b.pixels for b in sub.batches(bs, shuffle=False)), norm, bs
            )
            laplace = laplace_for(net, feats, s.uncertainty, task="classification")
        ctx.progress(1.0, "network ready")
        learned: dict[str, Any] = {
            "arch": arch.model_dump(mode="json"),
            "task": "classification",
            "classes": list(classes),
            "image_size": [int(size[0]), int(size[1])],
            "channels": int(loader.channels),
            "mode": mode,
            "normalize": norm.to_json(),
            "uncertainty": uncertainty_learned(s.uncertainty, cfg, laplace),
            "train": {
                "best_epoch": result.best_epoch,
                "epochs_run": result.epochs_run,
                "stopped_early": result.stopped_early,
                "steps": result.steps,
                "n_train": int(train.n),
                "n_val": int(0 if val is None else val.n),
            },
            "seed": seed,
        }
        return FitOutput(
            groups=train_result_groups(result, learned["uncertainty"]),
            learned=learned,
            native=native_files(net, arch, laplace),
        )

    def _classes(self, loader: ImageLoader, s: S) -> list[str]:
        present = loader.classes
        if s.class_order is None:
            if len(present) < 2:
                raise MapiError(
                    "the labelled images have one class only; a classifier needs two or more",
                    code=ErrorCode.SCHEMA_INVALID,
                )
            if len(present) > MAX_CLASSES:
                raise MapiError(
                    f"the target has {len(present)} classes; the model takes at most {MAX_CLASSES}",
                    code=ErrorCode.SCHEMA_INVALID,
                )
            return present
        missing = [c for c in present if c not in s.class_order]
        if missing:
            raise MapiError(
                "class_order does not name every label",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/class_order",
                untrusted_text=", ".join(missing[:10]),
            )
        return list(s.class_order)

    def _validation(
        self, loader: ImageLoader, s: S, seed: int
    ) -> tuple[ImageLoader, ImageLoader | None]:
        t = s.train
        manifest = loader.manifest()
        if t.val_split_value is not None and "split" in manifest.variables:
            is_val = np.asarray(manifest["split"].values).astype(str) == t.val_split_value
            if is_val.any() and not is_val.all():
                return loader.subset(~is_val), loader.subset(is_val)
        if t.val_fraction > 0 and loader.n >= 4:
            train, val = loader.split(t.val_fraction, seed=seed, stratify=True)
            if val.n > 0 and train.n > 0:
                return train, val
        return loader, None

    # ---------------------------------------------------------------------------- score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        if s.pixels not in ds:
            raise MapiError(
                f"the score data has no image variable {s.pixels!r}",
                code=ErrorCode.MISSING_FUTURE_COVARIATE,
                pointer=f"/data/{s.pixels}",
            )
        n = int(ds.sizes[s.image_dim])
        positions = requested_positions(ds, s.image_dim)
        loader = ArrayImageLoader(ds, pixels=s.pixels, target=None, dim=s.image_dim)
        probs = self._predict(fitted, loader.subset(positions), ctx)
        full = np.full((1, probs.shape[0], n, probs.shape[2]), np.nan)
        full[0][:, positions, :] = probs
        return self._tree(fitted, full, ds[s.image_dim].values, s.image_dim)

    def score_batches(
        self, fitted: Fitted, loader: ImageLoader, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        probs = self._predict(fitted, loader, ctx)
        return self._tree(fitted, probs[None], np.asarray(loader.ids), s.image_dim)

    def _predict(self, fitted: Fitted, loader: ImageLoader, ctx: ScoreContext) -> np.ndarray:
        """``(D, n, K)`` probabilities of the loader's images (loader order)."""
        learned: dict[str, Any] = dict(fitted.learned)
        net, _, laplace = restore_network(fitted)
        size = learned.get("image_size") or list(loader.size)
        mode: Literal["gray", "rgb"] = (
            "rgb"
            if (learned.get("mode") or "") == "rgb" or learned.get("channels") == 3
            else "gray"
        )
        loader = loader.with_mode(mode).with_size((int(size[0]), int(size[1])))
        norm = Normalize.from_json(learned.get("normalize"))
        unc: dict[str, Any] = learned.get("uncertainty") or {}
        method = str(unc.get("method", "none"))
        total = int(unc.get("draws") or 1)
        positions = draw_positions(total, ctx.draw_index)
        classes = list(learned.get("classes") or [])
        seed = int(fitted.fit_config.seed if fitted.fit_config.seed is not None else ctx.seed)
        bs = min(
            int(fitted.settings.get("train", {}).get("batch_size", 64) or 64), max(loader.n, 1)
        )
        logits = network_logits(
            net,
            (b.pixels for b in loader.batches(bs, shuffle=False)),
            norm,
            bs,
            method=method,
            positions=positions,
            seed=seed,
            laplace=laplace,
            n_out=len(classes),
            check=ctx.check_cancelled,
        )
        if ctx.kind == "predictive":
            ctx.info("y_hat holds class probabilities for expected and predictive outputs")
        return softmax(logits)

    def _tree(self, fitted: Fitted, probs: np.ndarray, ids: np.ndarray, dim: str) -> xr.DataTree:
        learned: dict[str, Any] = dict(fitted.learned)
        classes = list(learned.get("classes") or [])
        y_hat = xr.DataArray(
            probs,
            dims=("chain", "draw", dim, CLASS),
            coords={
                "chain": [0],
                "draw": np.arange(probs.shape[1]),
                dim: ids,
                CLASS: classes,
            },
            attrs={"meaning": "class_probability", "units": "1"},
        )
        return xr.DataTree.from_dict({PREDICTIONS: xr.Dataset({"y_hat": y_hat})})


def _row_sample(n: int, max_rows: int, seed: int) -> np.ndarray:
    if n <= max_rows:
        return np.arange(n, dtype=np.int64)
    return np.sort(np.random.default_rng([seed, 17]).choice(n, max_rows, replace=False))


def class_codes(texts: Sequence[str], classes: Sequence[str]) -> np.ndarray:
    lookup = {c: i for i, c in enumerate(classes)}
    return np.array([lookup.get(t, -1) for t in texts], dtype=np.int32)
