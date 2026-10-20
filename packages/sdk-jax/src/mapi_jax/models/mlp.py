"""``dl.mlp``: a multilayer perceptron on a table (regression or classification).

Rows are the cells of the target's dims (``settings.rows``, all categorical with policy
``new_levels``). Features are the numeric variables next to the target (or
``settings.features``), broadcast over the rows; extra dims become columns
(:mod:`mapi_jax.tabular`). Columns are standardised with the training mean and sd; a
regression target is standardised too and predictions are mapped back to its units.

Scores: classification ``y_hat(chain, draw, *rows, class)`` probabilities (``meaning =
class_probability``); regression ``y_hat(chain, draw, *rows)`` (the predictive kind adds
``N(0, sigma^2)`` noise per draw, ``sigma`` the training residual sd).
"""

from __future__ import annotations

import math
from typing import Any, ClassVar, Literal

import numpy as np
import xarray as xr
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
from mapi_spec.capabilities import ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.resources import ResourceClass
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, model_validator

from mapi_jax.config import TrainConfig, UncertaintyConfig
from mapi_jax.models.base import (
    CLASS,
    DL_VERSION,
    MAX_CLASSES,
    Name,
    checkpoint_every,
    checkpoint_key,
    dl_capabilities,
    fit_seed,
    laplace_for,
    native_files,
    network_features,
    network_logits,
    restore_network,
    softmax,
    train_result_groups,
    uncertainty_learned,
)
from mapi_jax.nets import ArchSpec, build_network
from mapi_jax.tabular import Standardizer, TabularLoader, design_matrix, feature_vars
from mapi_jax.tasks import ClassificationTask, RegressionTask
from mapi_jax.trainer import Trainer
from mapi_jax.uncertainty import draw_positions

__all__ = ["MLP", "MLPSettings"]

_RESERVED = {CLASS, "chain", "draw", "sample"}


class MLPSettings(ModelSettings):
    """Settings of ``dl.mlp``."""

    target: Name = Field("y", description="The target variable")
    rows: list[Name] = Field(..., min_length=1, max_length=8, description="The dims of a row")
    target_dtype: Literal["float64", "str", "int64"] = Field(
        "float64", description="dtype of the target: numbers, text labels or integer labels"
    )
    features: list[Name] | None = Field(
        None, max_length=256, description="Feature variables (default: every numeric one)"
    )
    task: Literal["auto", "regression", "classification"] = Field(
        "auto", description="auto: classification for text or integer labels"
    )
    new_levels: Literal["pooled", "zero", "error"] = Field(
        "pooled", description="Unseen row levels at score"
    )
    hidden: list[int] = Field([64, 64], max_length=16, description="Hidden layer widths")
    dropout: float = Field(0.1, ge=0, lt=1, description="Dropout between and after the layers")
    train: TrainConfig = Field(default_factory=TrainConfig)
    uncertainty: UncertaintyConfig = Field(default_factory=UncertaintyConfig)
    class_order: list[str] | None = Field(None, min_length=2, max_length=MAX_CLASSES)

    @model_validator(mode="after")
    def _check(self) -> MLPSettings:
        if set(self.rows) & _RESERVED:
            raise ValueError(f"rows must not use the reserved dims {sorted(_RESERVED)}")
        if len(set(self.rows)) != len(self.rows):
            raise ValueError("rows must not repeat a dim")
        if any(w < 1 or w > 4096 for w in self.hidden):
            raise ValueError("hidden widths must be 1..4096")
        if self.features is not None and self.target in self.features:
            raise ValueError("the target cannot be a feature")
        return self

    @property
    def resolved_task(self) -> Literal["regression", "classification"]:
        if self.task != "auto":
            return self.task
        return "regression" if self.target_dtype == "float64" else "classification"


_TEMPLATE: dict[str, Any] = {
    "params": {
        "target": {"type": "string", "default": "y"},
        "rows": {"type": "string_list", "required": True},
        "target_dtype": {
            "type": "string",
            "enum": ["float64", "str", "int64"],
            "default": "float64",
        },
        "new_levels": {"type": "string", "enum": ["pooled", "zero", "error"], "default": "pooled"},
    },
    "template": {
        "dims": {
            "{*rows as d}": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "{new_levels}",
            }
        },
        "vars": {
            "{target}": {
                "dims": "{rows}",
                "dtype": "{target_dtype}",
                "role": "target",
                "missing": "allow",
            }
        },
    },
}


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


class MLP(CustomModel[MLPSettings]):
    """A multilayer perceptron for a regression or a classification on a table."""

    id = "dl.mlp"
    version = DL_VERSION
    Settings: ClassVar[type[MLPSettings]] = MLPSettings
    capabilities = dl_capabilities("dataset")
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {
            "dims": [],
            "dims_of": "y",
            "description": "Prediction per row; a classification adds a class dim (the "
            "probability of each class, summing to 1 over class)",
        }
    }
    plots = ("ml.training_curves",)
    pad_extend = False
    resources = ResourceHint(min_class=ResourceClass.S, default_class=ResourceClass.S, gpu=False)
    description = (
        "A multilayer perceptron for a table: numeric features predict a number or a class. "
        "It logs training and validation curves and gives draws by a last-layer Laplace "
        "approximation or MC dropout."
    )

    # ============================================================================== fit
    def _xy(self, ds: xr.Dataset, s: MLPSettings) -> tuple[np.ndarray, np.ndarray, list[str]]:
        if s.target not in ds:
            raise MapiError(
                f"the fit data has no target {s.target!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{s.target}",
            )
        da = ds[s.target]
        if set(da.dims) != set(s.rows):
            raise MapiError(
                f"the target has the dims {list(da.dims)}; settings.rows is {s.rows}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/model/settings/rows",
            )
        feats = feature_vars(ds, s.target, s.features)
        x, _ = design_matrix(ds, s.rows, feats)
        y = np.asarray(da.transpose(*s.rows).values).reshape(-1)
        return x, y, feats

    def _val_rows(self, ds: xr.Dataset, s: MLPSettings) -> np.ndarray | None:
        """Per row (the order of :meth:`_xy`): whether the ``split`` coord marks it with
        ``train.val_split_value``; ``None`` without such a coord on the row dims."""
        value = s.train.val_split_value
        if value is None or "split" not in ds.variables:
            return None
        split = ds["split"]
        if not split.dims or not set(split.dims) <= set(s.rows):
            return None
        full = xr.broadcast(split, ds[s.target])[0].transpose(*s.rows)
        return np.asarray(full.values).astype(str).reshape(-1) == value

    def fit(self, ds: xr.Dataset, s: MLPSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        seed = fit_seed(cfg, ctx)
        x, y_raw, feats = self._xy(ds, s)
        _, columns = design_matrix(ds.isel({d: slice(0, 1) for d in s.rows}), s.rows, feats)
        task_kind = s.resolved_task
        learned: dict[str, Any] = {}
        task: ClassificationTask | RegressionTask
        if task_kind == "classification":
            texts = [_label_text(v) for v in y_raw.tolist()]
            present = sorted({t for t in texts if t is not None})
            classes = list(s.class_order) if s.class_order is not None else present
            missing = [t for t in present if t not in classes]
            if missing:
                raise MapiError(
                    "class_order does not name every label",
                    code=ErrorCode.SETTINGS_INVALID,
                    pointer="/model/settings/class_order",
                    untrusted_text=", ".join(missing[:10]),
                )
            if len(present) < 2:
                raise MapiError(
                    "the training rows have one class only; a classifier needs two or more",
                    code=ErrorCode.SCHEMA_INVALID,
                )
            keep = np.array([t is not None for t in texts])
            lookup = {c: i for i, c in enumerate(classes)}
            targets = np.array([lookup[t] for t in texts if t is not None], dtype=np.int32)
            task = ClassificationTask(classes, s.train.label_smoothing)
            learned["classes"] = classes
        else:
            values = np.asarray(y_raw, dtype=float)
            keep = np.isfinite(values)
            if keep.sum() < 2:
                raise MapiError("fewer than two rows have a target", code=ErrorCode.SCHEMA_INVALID)
            mu, sd = float(values[keep].mean()), float(values[keep].std())
            sd = sd if sd > 1e-12 else 1.0
            targets = ((values[keep] - mu) / sd).astype(np.float32)
            task = RegressionTask(mu, sd)
            learned["target"] = {"mean": mu, "std": sd}
        x = x[keep]
        scaler = Standardizer.fit(x)
        loader = TabularLoader(
            scaler.apply(x),
            targets,
            strata=targets if task_kind == "classification" else None,
        )
        train, val = loader, None
        is_val = self._val_rows(ds, s)
        if is_val is not None:
            is_val = is_val[keep]
        if is_val is not None and is_val.any() and not is_val.all():
            # as the image models: the rows the split coord marks are the validation data
            train, val = loader.subset(~is_val), loader.subset(is_val)
        elif s.train.val_fraction > 0 and loader.n >= 4:
            train, val = loader.split(s.train.val_fraction, seed=seed, stratify=True)
        arch = ArchSpec(
            kind="mlp", in_features=loader.n_features, hidden=list(s.hidden), dropout=s.dropout
        )
        net = build_network(arch, task.n_out, seed=seed)
        result = Trainer(
            net,
            task,
            s.train,
            ctx,
            seed=seed,
            preprocess=lambda r: r,
            checkpoint_key=checkpoint_key(ctx, self.id, s, seed),
            checkpoint_every=checkpoint_every(cfg),
            arch_params=arch.model_dump(mode="json"),
        ).fit(train, val)
        net = result.network
        bs = min(s.train.batch_size, train.n)
        noise_var = 1.0
        if task_kind == "regression":
            pred = network_logits(
                net,
                (b.x for b in train.batches(bs)),
                lambda r: r,
                bs,
                method="none",
                positions=np.zeros(1, dtype=np.int64),
                seed=seed,
                laplace=None,
                n_out=1,
            )[0, :, 0]
            assert train.y is not None
            noise_var = float(max(np.mean((pred - train.y) ** 2), 1e-6))
            assert isinstance(task, RegressionTask)
            learned["target"]["sigma"] = math.sqrt(noise_var) * task.std
        laplace = None
        if s.uncertainty.method == "laplace":
            rows = np.arange(min(train.n, s.uncertainty.max_rows))
            feats_np = network_features(
                net, (b.x for b in train.subset(rows).batches(bs)), lambda r: r, bs
            )
            laplace = laplace_for(net, feats_np, s.uncertainty, task=task_kind, noise_var=noise_var)
        ctx.progress(1.0, "network ready")
        learned.update(
            {
                "arch": arch.model_dump(mode="json"),
                "task": task_kind,
                "features": {
                    "vars": feats,
                    "columns": columns,
                    "rows": list(s.rows),
                    **scaler.to_json(),
                },
                "image_size": None,
                "channels": None,
                "normalize": None,
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
        )
        if task_kind == "regression":
            learned["classes"] = None
        return FitOutput(
            groups=train_result_groups(result, learned["uncertainty"]),
            learned=learned,
            native=native_files(net, arch, laplace),
        )

    # ============================================================================ score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        learned: dict[str, Any] = dict(fitted.learned)
        doc: Any = learned.get("features")
        if not isinstance(doc, dict):
            raise MapiError(
                "the bundle has no feature layout; refit the model",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        rows = [str(r) for r in doc["rows"]]
        feats = [str(v) for v in doc["vars"]]
        missing = [f for f in feats if f not in ds]
        if missing:
            raise MapiError(
                f"the score data has no feature(s) {missing[:10]}",
                code=ErrorCode.MISSING_FUTURE_COVARIATE,
                pointer=f"/data/{missing[0]}",
            )
        x, columns = design_matrix(ds, rows, feats)
        if columns != list(doc["columns"]):
            raise MapiError(
                "the score data gives other feature columns than the fit data",
                code=ErrorCode.SCHEMA_INVALID,
            )
        scaler = Standardizer.from_json(doc)
        shape = [int(ds.sizes[d]) for d in rows]
        mask = np.ones(shape, dtype=bool)
        for axis, dim in enumerate(rows):
            keep = np.zeros(shape[axis], dtype=bool)
            keep[requested_positions(ds, dim)] = True
            mask &= keep.reshape([-1 if i == axis else 1 for i in range(len(rows))])
        flat = np.flatnonzero(mask.reshape(-1))
        loader = TabularLoader(scaler.apply(x[flat]))
        net, _, laplace = restore_network(fitted)
        unc: dict[str, Any] = learned.get("uncertainty") or {}
        method = str(unc.get("method", "none"))
        positions = draw_positions(int(unc.get("draws") or 1), ctx.draw_index)
        seed = int(fitted.fit_config.seed if fitted.fit_config.seed is not None else ctx.seed)
        bs = max(1, min(s.train.batch_size, loader.n))
        task = learned.get("task")
        classes = list(learned.get("classes") or [])
        n_out = len(classes) if task == "classification" else 1
        out = network_logits(
            net,
            (b.x for b in loader.batches(bs)),
            lambda r: r,
            bs,
            method=method,
            positions=positions,
            seed=seed,
            laplace=laplace,
            n_out=n_out,
            check=ctx.check_cancelled,
        )
        d = positions.size
        coords: dict[str, Any] = {"chain": [0], "draw": np.arange(d)}
        coords.update({r: ds[r].values for r in rows if r in ds.coords})
        if task == "classification":
            probs = softmax(out)
            full = np.full((d, int(np.prod(shape)), n_out), np.nan)
            full[:, flat, :] = probs
            values = full.reshape(1, d, *shape, n_out)
            coords[CLASS] = classes
            y_hat = xr.DataArray(
                values,
                dims=("chain", "draw", *rows, CLASS),
                coords=coords,
                attrs={"meaning": "class_probability", "units": "1"},
            )
            if ctx.kind == "predictive":
                ctx.info("y_hat holds class probabilities for expected and predictive outputs")
        else:
            tgt: dict[str, Any] = learned.get("target") or {}
            mu = float(tgt.get("mean", 0.0))
            sd = float(tgt.get("std", 1.0))
            pred = out[:, :, 0] * sd + mu
            if ctx.kind == "predictive":
                sigma = float(tgt.get("sigma", 0.0))
                for i, dd in enumerate(positions):
                    rng = np.random.default_rng([seed, 202, int(dd)])
                    pred[i] += rng.normal(0.0, sigma, size=pred.shape[1])
            full = np.full((d, int(np.prod(shape))), np.nan)
            full[:, flat] = pred
            y_hat = xr.DataArray(
                full.reshape(1, d, *shape), dims=("chain", "draw", *rows), coords=coords
            )
        return xr.DataTree.from_dict({PREDICTIONS: xr.Dataset({"y_hat": y_hat})})
