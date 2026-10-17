"""``ts.gaussian_hmm``: a K-state Gaussian hidden Markov model over padded sequences (E24).

Canonical inputs (padded ragged layout, plan §3.1)::

    dims  animal: categorical, new_levels (strategy pooled)    step: integer, extend
    vars  y(animal, step): target, float64, NaN where padded
          mask(animal, step): bool, True where y is observed

Every sequence shares the parameters ``pi`` (state distribution at the first fitted step),
``transition`` (``K x K``), and per-state emission ``mu``/``sigma``. The likelihood
marginalises the hidden states with the forward algorithm (a pytensor ``scan`` over steps,
vectorised over animals), so NUTS samples only continuous parameters.

**Terminal state.** After the fit the forward filter runs over every posterior draw; the
filtered state distribution at the last fitted step is stored as the plugin-owned
``terminal_state`` group ``alpha_T(chain, draw, animal, state)`` (attr ``step``: the step it
refers to). Per-step filtered probabilities are kept in the ``hidden_states`` group.

**Score modes** (outputs ``y_hat(chain, draw, animal, step)`` and
``state_prob(chain, draw, animal, step, state)``):

* ``predict``/``evaluate`` - the one-step-ahead state distribution ``P(z_t | y_{<t})`` from
  the fit for fitted steps, ``alpha_T @ A^h`` past the fit; never conditions on payload
  targets (so holdout scores use the training state, like every evaluation).
* ``forecast`` - the same, over a horizon (``alpha_T @ A^h``).
* ``filter`` - runs the forward filter over new observations that continue the fitted
  steps, starting from ``alpha_T``; ``y_hat`` are one-step-ahead predictions, ``state_prob``
  the filtered probabilities, and the returned tree carries an **updated** ``terminal_state``
  group as of the last filtered step.

New animals start from ``pi`` at the first fitted step. **Label switching**: the
``postprocess`` hook relabels every draw so that the state means increase
(``identification: relabel``, the default); ``identification: ordered`` instead constrains
the means with an ordered transform (the relabelling is then the identity).

Pointwise LOO is not offered: the likelihood is a potential over whole tracks and steps
within a track are dependent (``loo_valid = False``).
"""

from __future__ import annotations

import hashlib
import logging
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
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
    new_level_mask,
)
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field

from mapi_plugin_ts.hmm_math import (
    forward_filter,
    hmm_loglik,
    propagate,
    relabel_permutation,
)

__all__ = [
    "GROUP_DIM",
    "HIDDEN_STATES",
    "MASK",
    "STEP_DIM",
    "TARGET",
    "TERMINAL_STATE",
    "GaussianHMMModel",
    "GaussianHMMSettings",
]

GROUP_DIM = "animal"
STEP_DIM = "step"
TARGET = "y"
MASK = "mask"
STATE = "state"
STATE_NEXT = "state_next"
HIDDEN_STATES = "hidden_states"
TERMINAL_STATE = "terminal_state"
PARAMS = ("pi", "transition", "mu", "sigma")


class GaussianHMMSettings(ModelSettings):
    """Settings of ``ts.gaussian_hmm`` (priors on the model-stage scale of ``y``)."""

    n_states: int = Field(2, ge=2, le=8, description="Number of hidden states K")
    mu_center: float = Field(0.0, description="Prior mean of the state means")
    mu_sigma: float = Field(2.0, gt=0, description="Prior sd of the state means")
    sigma_scale: float = Field(1.0, gt=0, description="Half-normal scale of the emission sds")
    stickiness: float = Field(
        2.0, ge=0, le=1000, description="Extra Dirichlet weight on staying in the same state"
    )
    pi_concentration: float = Field(1.0, gt=0, description="Dirichlet weight of pi")
    identification: Literal["relabel", "ordered"] = Field(
        "relabel",
        description="relabel: sort states by mean after sampling; ordered: ordered prior",
    )


_TEMPLATE: dict[str, Any] = {
    "template": {
        "dims": {
            GROUP_DIM: {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "pooled",
            },
            STEP_DIM: {"kind": "integer", "policy": "extend"},
        },
        "vars": {
            TARGET: {
                "dims": [GROUP_DIM, STEP_DIM],
                "dtype": "float64",
                "role": "target",
                "missing": "allow",
            },
            MASK: {"dims": [GROUP_DIM, STEP_DIM], "dtype": "bool", "role": "mask"},
        },
        "ragged": [
            {"group_dim": GROUP_DIM, "within_dim": STEP_DIM, "form": "padded", "mask_var": MASK}
        ],
    }
}


class GaussianHMMModel(CustomModel[GaussianHMMSettings]):
    """Gaussian HMM with a terminal state, forecasting and filtering (plan §4.3, §5)."""

    id = "ts.gaussian_hmm"
    version = "1.0.0"
    Settings = GaussianHMMSettings
    description = (
        "A K-state Gaussian hidden Markov model over padded sequences (e.g. animal tracks). "
        "Hidden states are marginalised with the forward algorithm; forecasts and filters "
        "start from the stored terminal state."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "forecast", "filter", "evaluate"}),
        loo_valid=False,
        loo_invalid_reason="the HMM likelihood is a potential over whole sequences; steps "
        "within a sequence are dependent, so pointwise leave-one-out is not defined",
        identified_quantities=["mu", "sigma", "transition", "pi"],
        deterministic_given_seed=True,
        checkpointable=True,
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {
            "dims": [GROUP_DIM, STEP_DIM],
            "units_of": TARGET,
            "description": "One-step-ahead expected value or predictive draw of y",
        },
        "state_prob": {
            "dims": [GROUP_DIM, STEP_DIM, STATE],
            "units": "probability",
            "description": "Hidden-state probabilities (predicted, or filtered in filter mode)",
        },
    }
    plots = ("core.convergence", "core.posterior_forest")
    pad_extend = False
    default_engine: ClassVar[str] = "pymc"

    # ================================================================== fit
    def build_model(self, y: np.ndarray, mask: np.ndarray, s: GaussianHMMSettings) -> pm.Model:
        """The marginalised model for ``y(animal, step)`` (states summed out)."""
        k = s.n_states
        coords = {STATE: list(range(k)), STATE_NEXT: list(range(k))}
        alpha = np.ones((k, k)) + s.stickiness * np.eye(k)
        with pm.Model(coords=coords) as model:
            pi = pm.Dirichlet("pi", np.full(k, s.pi_concentration), dims=STATE)
            trans = pm.Dirichlet("transition", alpha, dims=(STATE, STATE_NEXT))
            spread = np.linspace(-1.0, 1.0, k) * s.mu_sigma / 2 + s.mu_center
            if s.identification == "ordered":
                mu = pm.Normal(
                    "mu",
                    s.mu_center,
                    s.mu_sigma,
                    dims=STATE,
                    transform=pm.distributions.transforms.ordered,
                    initval=spread,
                )
            else:
                mu = pm.Normal("mu", s.mu_center, s.mu_sigma, dims=STATE, initval=spread)
            sigma = pm.HalfNormal("sigma", s.sigma_scale, dims=STATE)
            pm.Potential(
                "sequence_loglik", hmm_loglik(pt.log(pi), pt.log(trans), mu, sigma, y, mask)
            )
        return model

    def fit(
        self, ds: xr.Dataset, s: GaussianHMMSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        y, mask = _arrays(ds, require_target=True)
        if not mask.any():
            raise MapiError("no observed values to fit", code=ErrorCode.SCHEMA_INVALID)
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        model = self.build_model(y, mask, s)
        plan = EnginePlan.from_config(cfg, default_engine=self.default_engine, seed=seed)
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
        posterior = _clean(posterior[list(PARAMS)], result.engine)
        groups: dict[str, xr.Dataset] = {"posterior": posterior}
        stats = result.group("sample_stats")
        if stats is not None:
            keep = [
                v
                for v in stats.data_vars
                if stats[v].dtype.kind in "biuf" and set(stats[v].dims) <= {"chain", "draw"}
            ]
            groups["sample_stats"] = _clean(stats[keep], result.engine)
        groups["observed_data"] = _clean(
            xr.Dataset({TARGET: ds[TARGET].transpose(GROUP_DIM, STEP_DIM)}), result.engine
        )
        groups["constant_data"] = _clean(
            xr.Dataset({MASK: xr.DataArray(mask, dims=[GROUP_DIM, STEP_DIM], coords=_coords(ds))}),
            result.engine,
        )
        ctx.progress(0.9, "filtering the hidden states")
        hidden, terminal = self._filter_fit(posterior, ds, y, mask)
        groups[HIDDEN_STATES] = hidden
        ctx.progress(1.0, "posterior ready")
        learned: dict[str, Any] = {
            "engine": result.engine,
            "tune": result.plan.tune,
            "draws": result.plan.draws,
            "chains": result.plan.chains,
            "resumed_from": result.resumed_from,
            "checkpoints": result.checkpoints,
            "n_states": s.n_states,
            "first_step": int(ds[STEP_DIM].values.min()),
            "last_step": int(ds[STEP_DIM].values.max()),
            "relabelled_draws": 0,
        }
        if cfg.keep.log_likelihood:
            learned["notes"] = ["log_likelihood not stored: pointwise LOO is not valid for HMMs"]
        return FitOutput(groups=groups, learned=learned, terminal_state=terminal)

    def _filter_fit(
        self, posterior: xr.Dataset, ds: xr.Dataset, y: np.ndarray, mask: np.ndarray
    ) -> tuple[xr.Dataset, xr.Dataset]:
        p = _params(posterior)
        res = forward_filter(p.pi, p.A, p.mu, p.sigma, y, mask)
        chain, draw = posterior["chain"].values, posterior["draw"].values
        shape = (chain.size, draw.size)
        coords = {"chain": chain, "draw": draw, **_coords(ds), STATE: posterior[STATE].values}
        filtered = res.filtered.reshape(*shape, *res.filtered.shape[1:])
        hidden = xr.Dataset(
            {"state_prob": (("chain", "draw", GROUP_DIM, STEP_DIM, STATE), filtered)},
            coords=coords,
            attrs={"kind": "filtered"},
        )
        loglik = res.loglik.reshape(*shape, -1)
        hidden["track_loglik"] = (("chain", "draw", GROUP_DIM), loglik)
        last = int(ds[STEP_DIM].values.max())
        observed_last = np.array(
            [
                int(ds[STEP_DIM].values[np.flatnonzero(row)].max()) if row.any() else -1
                for row in mask
            ],
            dtype=np.int64,
        )
        terminal = xr.Dataset(
            {
                "alpha_T": (("chain", "draw", GROUP_DIM, STATE), filtered[:, :, :, -1, :]),
                "last_observed": ((GROUP_DIM,), observed_last),
            },
            coords={
                "chain": chain,
                "draw": draw,
                GROUP_DIM: ds[GROUP_DIM].values,
                STATE: posterior[STATE].values,
            },
            attrs={"step": last},
        )
        return hidden, terminal

    # ================================================================== postprocess
    def postprocess(self, out: FitOutput, ds: xr.Dataset, s: GaussianHMMSettings) -> FitOutput:
        """Relabel every draw so that the state means increase (label switching)."""
        post = out.groups.get("posterior")
        if post is None or "mu" not in post:
            return out
        mu = np.asarray(post["mu"].transpose("chain", "draw", STATE).values)
        perm = relabel_permutation(mu.reshape(-1, mu.shape[-1])).reshape(mu.shape)
        moved = int((perm != np.arange(mu.shape[-1])).any(axis=-1).sum())
        if moved == 0:
            return out
        k = mu.shape[-1]
        groups = dict(out.groups)
        groups["posterior"] = _permute(post, perm, k)
        if HIDDEN_STATES in groups:
            groups[HIDDEN_STATES] = _permute(groups[HIDDEN_STATES], perm, k)
        terminal = _permute(out.terminal_state, perm, k) if out.terminal_state is not None else None
        learned = {**out.learned, "relabelled_draws": moved}
        return FitOutput(
            groups=groups,
            learned=learned,
            native=out.native,
            terminal_state=terminal,
            derived=out.derived,
            diagnostics=out.diagnostics,
        )

    def diagnostics(self, fitted: Fitted, ds: xr.Dataset) -> list[Diagnostic]:
        post = fitted.groups.get("posterior")
        if post is None:
            return []
        out = convergence_diagnostics(
            post, fitted.groups.get("sample_stats"), var_names=list(PARAMS)
        )
        moved = fitted.learned.get("relabelled_draws")
        if isinstance(moved, int) and moved > 0:
            total = int(post.sizes["chain"] * post.sizes["draw"])
            out.append(
                Diagnostic(
                    code="LABEL_SWITCHING",
                    severity="info",
                    metric="relabelled_fraction",
                    value=moved / max(total, 1),
                    message_expert=f"{moved} of {total} draws were relabelled so that the state "
                    "means increase (label switching between or within chains)",
                    message_plain="The model sorted the hidden states in one order "
                    "(from the lowest to the highest average value).",
                )
            )
        return out

    # ================================================================== score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        if fitted.terminal_state is None or HIDDEN_STATES not in fitted.groups:
            raise MapiError(
                "the fitted HMM has no terminal state; refit the model",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        post = fitted.posterior
        p = _params(post)
        animals = np.asarray(ds[GROUP_DIM].values)
        steps = np.asarray(ds[STEP_DIM].values).astype(np.int64)
        is_new = np.asarray(new_level_mask(ds, GROUP_DIM).values, dtype=bool)
        first = int(_learned_int(fitted, "first_step"))
        terminal = fitted.terminal_state
        last = int(terminal.attrs.get("step", _learned_int(fitted, "last_step")))
        ctx.check_cancelled()
        extra: dict[str, xr.Dataset] = {}
        if ctx.mode == "filter":
            probs, reported, new_terminal = self._filter_new(fitted, ds, p, animals, steps, is_new)
            extra[TERMINAL_STATE] = new_terminal
        else:
            probs = self._state_probs(fitted, p, animals, steps, is_new, first, last)
            reported = probs
        y_hat, noise = self._emit(probs, p, ctx)
        chain, draw = post["chain"].values, post["draw"].values
        shape = (chain.size, draw.size)
        coords = {
            "chain": chain,
            "draw": draw,
            GROUP_DIM: animals,
            STEP_DIM: steps,
            STATE: post[STATE].values,
        }
        dims4 = ("chain", "draw", GROUP_DIM, STEP_DIM)
        predictions = xr.Dataset(
            {
                "y_hat": xr.DataArray(
                    y_hat.reshape(*shape, *y_hat.shape[1:]),
                    dims=dims4,
                    attrs={"kind": ctx.kind, "noise": noise},
                ),
                "state_prob": xr.DataArray(
                    reported.reshape(*shape, *reported.shape[1:]),
                    dims=(*dims4, STATE),
                    attrs={
                        "kind": "filtered" if ctx.mode == "filter" else "predicted",
                        "units": "probability",
                    },
                ),
            },
            coords=coords,
        )
        nodes: dict[str, xr.Dataset] = {PREDICTIONS: predictions, **extra}
        return xr.DataTree.from_dict(nodes)

    def _state_probs(
        self,
        fitted: Fitted,
        p: _Params,
        animals: np.ndarray,
        steps: np.ndarray,
        is_new: np.ndarray,
        first: int,
        last: int,
    ) -> np.ndarray:
        """``P(z_t | y_{<t})`` per (sample, animal, step), never using payload targets."""
        hidden = fitted.groups[HIDDEN_STATES]["state_prob"].transpose(
            "chain", "draw", GROUP_DIM, STEP_DIM, STATE
        )
        terminal = fitted.terminal_state
        assert terminal is not None
        alpha = terminal["alpha_T"].transpose("chain", "draw", GROUP_DIM, STATE)
        s_len, k = p.mu.shape
        out = np.empty((s_len, animals.size, steps.size, k))
        fit_steps = np.asarray(hidden[STEP_DIM].values).astype(np.int64)
        fit_animals = list(np.asarray(hidden[GROUP_DIM].values).tolist())
        inside = steps <= last
        if (steps < first).any():
            raise MapiError(
                f"score steps start before the first fitted step {first}",
                code=ErrorCode.COORD_NOT_ALLOWED,
            )
        after = steps[~inside] - last
        # new animals: pi propagated from the first fitted step
        if is_new.any():
            from_pi = propagate(p.pi, p.A, steps - first)  # (S, H, K)
            out[:, is_new] = from_pi[:, None]
        known = np.flatnonzero(~is_new)
        if known.size:
            rows = [fit_animals.index(a) for a in animals[known].tolist()]
            filt = hidden.values.reshape(s_len, len(fit_animals), fit_steps.size, k)[:, rows]
            if inside.any():
                pos = np.searchsorted(fit_steps, steps[inside])
                prev = filt[:, :, np.clip(pos - 1, 0, None)]
                one_step = np.einsum("snhk,skj->snhj", prev, p.A)
                at_first = pos == 0
                one_step[:, :, at_first] = p.pi[:, None, None, :]
                out[:, known[:, None], np.flatnonzero(inside)[None, :]] = one_step
            if after.size:
                a_t = alpha.values.reshape(s_len, len(fit_animals), k)[:, rows]
                fwd = propagate(a_t, p.A, after)  # (S, n_known, H, K)
                out[:, known[:, None], np.flatnonzero(~inside)[None, :]] = fwd
        return out

    def _filter_new(
        self,
        fitted: Fitted,
        ds: xr.Dataset,
        p: _Params,
        animals: np.ndarray,
        steps: np.ndarray,
        is_new: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, xr.Dataset]:
        terminal = fitted.terminal_state
        assert terminal is not None
        last = int(terminal.attrs["step"])
        first = int(_learned_int(fitted, "first_step"))
        if steps.size == 0 or steps.min() <= last:
            raise MapiError(
                f"filter mode continues the fitted sequences: every step must come after the "
                f"last fitted step {last}",
                code=ErrorCode.COORD_NOT_ALLOWED,
                pointer="/data",
            )
        if TARGET not in ds:
            raise MapiError(
                f"filter mode needs observations of {TARGET!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data",
            )
        y, mask = _arrays(ds, require_target=True)
        s_len, k = p.mu.shape
        prior = np.empty((s_len, animals.size, k))
        gap = int(steps[0]) - last
        alpha = terminal["alpha_T"].transpose("chain", "draw", GROUP_DIM, STATE)
        fit_animals = list(np.asarray(alpha[GROUP_DIM].values).tolist())
        known = np.flatnonzero(~is_new)
        if known.size:
            rows = [fit_animals.index(a) for a in animals[known].tolist()]
            a_t = alpha.values.reshape(s_len, len(fit_animals), k)[:, rows]
            prior[:, known] = propagate(a_t, p.A, np.array([gap]))[:, :, 0]
        if is_new.any():
            prior[:, is_new] = propagate(p.pi, p.A, np.array([int(steps[0]) - first]))[:, None, 0]
        res = forward_filter(prior, p.A, p.mu, p.sigma, y, mask)
        post = fitted.posterior
        chain, draw = post["chain"].values, post["draw"].values
        new_alpha = res.filtered[:, :, -1, :].reshape(chain.size, draw.size, animals.size, k)
        prev_last = terminal["last_observed"].reindex({GROUP_DIM: animals}, fill_value=-1).values
        seen = np.array(
            [int(steps[np.flatnonzero(row)].max()) if row.any() else -1 for row in mask],
            dtype=np.int64,
        )
        updated = xr.Dataset(
            {
                "alpha_T": (("chain", "draw", GROUP_DIM, STATE), new_alpha),
                "last_observed": ((GROUP_DIM,), np.maximum(seen, prev_last.astype(np.int64))),
                "loglik": (
                    ("chain", "draw", GROUP_DIM),
                    res.loglik.reshape(chain.size, draw.size, animals.size),
                ),
            },
            coords={
                "chain": chain,
                "draw": draw,
                GROUP_DIM: animals,
                STATE: post[STATE].values,
            },
            attrs={"step": int(steps.max()), "previous_step": last},
        )
        return res.predicted, res.filtered, updated

    def _emit(self, probs: np.ndarray, p: _Params, ctx: ScoreContext) -> tuple[np.ndarray, str]:
        """Expected values ``Σ p_k mu_k`` or seeded predictive draws (common random numbers)."""
        if ctx.kind != "predictive":
            return np.einsum("snhk,sk->snh", probs, p.mu), "none"
        rng = _rng(ctx.seed, "y_hat")
        u = rng.uniform(size=probs.shape[:-1])
        eps = rng.standard_normal(size=probs.shape[:-1])
        cdf = np.cumsum(probs, axis=-1)
        state = np.minimum((cdf < u[..., None]).sum(axis=-1), probs.shape[-1] - 1)
        s_idx = np.arange(probs.shape[0])[:, None, None]
        y = p.mu[s_idx, state] + p.sigma[s_idx, state] * eps
        return y, "crn_seeded"


# ============================================================================== helpers


class _Params:
    """Posterior parameters flattened over (chain, draw)."""

    __slots__ = ("A", "mu", "pi", "sigma")

    def __init__(self, pi: np.ndarray, A: np.ndarray, mu: np.ndarray, sigma: np.ndarray) -> None:
        self.pi, self.A, self.mu, self.sigma = pi, A, mu, sigma


def _params(post: xr.Dataset) -> _Params:
    def flat(name: str, *dims: str) -> np.ndarray:
        da = post[name].transpose("chain", "draw", *dims)
        arr = np.asarray(da.values, dtype=float)
        return arr.reshape(-1, *arr.shape[2:])

    return _Params(
        flat("pi", STATE),
        flat("transition", STATE, STATE_NEXT),
        flat("mu", STATE),
        flat("sigma", STATE),
    )


def _arrays(ds: xr.Dataset, *, require_target: bool) -> tuple[np.ndarray, np.ndarray]:
    for d in (GROUP_DIM, STEP_DIM):
        if d not in ds.dims:
            raise MapiError(f"the data has no dim {d!r}", code=ErrorCode.SCHEMA_INVALID)
    if TARGET not in ds:
        if require_target:
            raise MapiError(f"the data has no variable {TARGET!r}", code=ErrorCode.SCHEMA_INVALID)
        shape = (ds.sizes[GROUP_DIM], ds.sizes[STEP_DIM])
        return np.full(shape, np.nan), np.zeros(shape, dtype=bool)
    y = np.asarray(ds[TARGET].transpose(GROUP_DIM, STEP_DIM).values, dtype=float)
    mask = np.isfinite(y)
    if MASK in ds:
        mask &= np.asarray(ds[MASK].transpose(GROUP_DIM, STEP_DIM).values, dtype=bool)
    return y, mask


def _coords(ds: xr.Dataset) -> dict[str, np.ndarray]:
    return {GROUP_DIM: np.asarray(ds[GROUP_DIM].values), STEP_DIM: np.asarray(ds[STEP_DIM].values)}


def _learned_int(fitted: Fitted, key: str) -> int:
    value = fitted.learned.get(key)
    if not isinstance(value, int):
        raise MapiError(f"the fitted HMM is missing {key!r}", code=ErrorCode.INTEGRITY_FAILURE)
    return value


def _permute(ds: xr.Dataset, perm: np.ndarray, k: int) -> xr.Dataset:
    """Reorder the state dims of every variable by ``perm(chain, draw, K)``."""
    out = {}
    pa = xr.DataArray(perm, dims=("chain", "draw", "__k"))
    for name, da in ds.data_vars.items():
        new = da
        for dim in (STATE, STATE_NEXT):
            if dim in new.dims and "chain" in new.dims:
                new = _take(new, pa.rename({"__k": dim}), dim)
        out[name] = new.transpose(*da.dims)
    return ds.assign(out)


def _take(da: xr.DataArray, idx: xr.DataArray, dim: str) -> xr.DataArray:
    """``da`` with ``dim`` gathered per (chain, draw) by ``idx(chain, draw, dim)``."""
    moved = da.transpose("chain", "draw", dim, ...)
    arr = np.asarray(moved.values)
    ind = np.asarray(idx.transpose("chain", "draw", dim).values)
    ind = ind.reshape(ind.shape + (1,) * (arr.ndim - 3))
    taken = np.take_along_axis(arr, np.broadcast_to(ind, arr.shape), axis=2)
    return moved.copy(data=taken)


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
