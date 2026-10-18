"""``sci.colour_naming``: how the speakers of one language name colour chips (E77, WCS).

Canonical inputs (one language; bind the dataset names onto them in the spec)::

    dims  speaker: categorical, new_levels (hyperprior) - the people asked
          chip:    integer, fixed - the colour chips shown (the WCS palette has 330)
    vars  naming(speaker, chip): target, str - the term the speaker gave ('' = no answer)
          lab_l, lab_a, lab_b(chip): the CIELAB colour of each chip
          grid_row, grid_col(chip): where the chip sits on the printed palette (WCS: rows
            A-J as 0-9, hue columns 1-40 and 0 for the greys)

**Model** (the E77 naming model). A speaker names chip ``c`` with term ``t`` with
probability ``softmax_t(eta_t(x_c))``, and the logits are a quadratic surface in colour
space written as a multinomial logistic regression on standardised features::

    eta[s, c, t] = sum_k F[c, k] W_s[k, t]
    F = 1, L, a, b, L^2, a^2, b^2, ab, La, Lb        (features: cielab, standardised)
    W ~ ZeroSumNormal(w_sd, dims=(feat, term))       (zero-sum over terms)

``kind: pooled`` gives every speaker the language's weights ``W`` (the likelihood is the
chip x term count table). ``kind: hierarchical`` gives each speaker their own intercept and
linear weights (the first ``n_random`` features), non-centred around the language's::

    W_s[k, t] = W[k, t] + sd[k, t] z[s, k, t]    (k < n_random)
    sd ~ HalfNormal(sd_scale),  z ~ ZeroSumNormal(1, dims=(speaker, rfeat, term))

``features: grid_line`` and ``grid_circle`` are the notebook's printed-grid alternatives
(row and hue column as numbers; hue as a line or as a circle), for an ``elpd_loo`` ladder.

**Outputs.** ``p_name(speaker, chip, term)`` is the probability that each speaker gives
each term (a held-out or new speaker gets fresh effects per draw, so the mean over draws is
the new-speaker predictive). The language-level outputs are computed at fit for every draw
and kept in the posterior, so the fit-time plots find them: ``p_term(term, lightness,
hue)``, the share of the language's speakers (``new_speakers`` fresh speakers per draw) who
give each term, on the palette grid; ``mode_term_rgb(chip, rgb)``, each chip painted in the
display colour of its most probable term (the posterised map; coords ``row``/``col`` on
``chip``); ``p_top(chip)``, the share for the term that is most probable on average; and the
effective numbers of categories ``k_eff_speaker`` and ``k_eff_language`` (``exp`` of the
mutual information between chip and term).

**Pointwise log-likelihood**: one value per chip, summed over the speakers who named it
(leave-one-chip-out), so ``elpd_loo`` compares the pooled, hierarchical and grid variants on
the same points. Non-responses are missing at random and leave the likelihood.
"""

from __future__ import annotations

import hashlib
import logging
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field

__all__ = [
    "CHIP_DIM",
    "FEATURE_NAMES",
    "HUE_DIM",
    "LAB_VARS",
    "LIGHTNESS_DIM",
    "SPEAKER_DIM",
    "TARGET",
    "TERM_DIM",
    "ColourNamingModel",
    "ColourNamingSettings",
    "chip_features",
    "effective_categories",
    "exemplar_colours",
    "lab_to_srgb",
    "naming_probabilities",
    "softmax",
]

SPEAKER_DIM = "speaker"
CHIP_DIM = "chip"
TERM_DIM = "term"
FEAT_DIM = "feat"
RFEAT_DIM = "rfeat"
LIGHTNESS_DIM = "lightness"
HUE_DIM = "hue"
RGB_DIM = "rgb"
TARGET = "naming"
LAB_VARS = ("lab_l", "lab_a", "lab_b")
GRID_VARS = ("grid_row", "grid_col")
FEATURES = "features"
CHIP_RGB = "chip_rgb"
FEATURE_NAMES: dict[str, tuple[str, ...]] = {
    "cielab": ("1", "L", "a", "b", "L2", "a2", "b2", "ab", "La", "Lb"),
    "grid_line": ("1", "row", "col", "row2", "col2", "row_col", "grey"),
    "grid_circle": (
        "1", "row", "cos1", "sin1", "cos2", "sin2", "row_cos1", "row_sin1", "row2", "grey",
    ),
}  # fmt: skip
"""Feature names per feature set (the first is the intercept)."""
FeatureSet = Literal["cielab", "grid_line", "grid_circle"]
D65_WHITE_2DEG = np.array([0.95047, 1.0, 1.08883])
M_SRGB = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
MAX_TERMS = 40
MAX_GRID = 200


# ============================================================================== numpy maths


def lab_to_srgb(lab: np.ndarray) -> np.ndarray:
    """CIELAB (``..., 3``) -> sRGB in [0, 1] (D65 white, clipped to the gamut), as in E77."""
    lab = np.asarray(lab, dtype=float)
    d = 6 / 29
    fy = (lab[..., 0] + 16) / 116
    f = np.stack([fy + lab[..., 1] / 500, fy, fy - lab[..., 2] / 200], -1)
    xyz = np.where(f > d, f**3, 3 * d**2 * (f - 4 / 29)) * D65_WHITE_2DEG
    lin = np.clip(xyz @ np.linalg.inv(M_SRGB).T, 0, 1)
    out: np.ndarray = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)
    return out


def _standardise(raw: np.ndarray) -> np.ndarray:
    """An intercept column, then every column centred and scaled over the chips (E77)."""
    sd = raw.std(0)
    z = (raw - raw.mean(0)) / np.where(sd > 0, sd, 1.0)
    return np.c_[np.ones(len(raw)), z]


def chip_features(
    lab: np.ndarray,
    row: np.ndarray,
    col: np.ndarray,
    features: FeatureSet = "cielab",
    *,
    hue_columns: int | None = None,
) -> np.ndarray:
    """The ``(chip, feat)`` design of the notebook's feature sets (standardised over chips).

    ``cielab`` is the quadratic in ``L, a, b`` (scaled by 50 first); ``grid_line`` uses the
    row and hue column as numbers; ``grid_circle`` puts the hue column on a circle with
    ``hue_columns`` columns (default: the largest column). Column 0 holds the greys.
    """
    lab = np.asarray(lab, dtype=float)
    row = np.asarray(row, dtype=float)
    col = np.asarray(col, dtype=float)
    if features == "cielab":
        lc, ac, bc = (lab[:, 0] - 50) / 50, lab[:, 1] / 50, lab[:, 2] / 50
        raw = np.c_[lc, ac, bc, lc**2, ac**2, bc**2, ac * bc, lc * ac, lc * bc]
    elif features == "grid_line":
        chrom = (col > 0).astype(float)
        raw = np.c_[row, col, row**2, col**2, row * col, 1 - chrom]
    else:
        chrom = (col > 0).astype(float)
        period = float(hue_columns or max(col.max(), 1.0))
        theta = 2 * np.pi * col / period
        raw = np.c_[
            row,
            chrom * np.cos(theta),
            chrom * np.sin(theta),
            chrom * np.cos(2 * theta),
            chrom * np.sin(2 * theta),
            row * chrom * np.cos(theta),
            row * chrom * np.sin(theta),
            row**2,
            1 - chrom,
        ]
    return _standardise(raw)


def softmax(eta: np.ndarray) -> np.ndarray:
    """Softmax over the last axis."""
    e = np.exp(eta - eta.max(-1, keepdims=True))
    out: np.ndarray = e / e.sum(-1, keepdims=True)
    return out


def naming_probabilities(
    feats: np.ndarray,
    w: np.ndarray,
    sd: np.ndarray | None = None,
    z: np.ndarray | None = None,
) -> np.ndarray:
    """``P(term | chip)`` from weights: ``(chip, term)``, or ``(speaker, chip, term)`` when
    speaker effects ``z (speaker, rfeat, term)`` and their scales ``sd (rfeat, term)`` are
    given (the NumPy twin of the PyMC graph)."""
    if sd is None or z is None:
        return softmax(feats @ w)
    ws = np.repeat(w[None], z.shape[0], 0)
    r = z.shape[1]
    ws[:, :r] += sd[None] * z
    return softmax(np.einsum("ck,skt->sct", feats, ws))


def _entropy(p: np.ndarray, axis: int = -1) -> np.ndarray:
    out: np.ndarray = -(p * np.log(np.clip(p, 1e-300, 1))).sum(axis)
    return out


def effective_categories(p: np.ndarray) -> np.ndarray:
    """``exp`` of the mutual information between chip and term of ``p (..., chip, term)``
    (chips equally likely): ``k`` equally used, perfectly crisp categories score ``k``."""
    out: np.ndarray = np.exp(_entropy(p.mean(-2)) - _entropy(p).mean(-1))
    return out


def exemplar_colours(p_mean: np.ndarray, lab: np.ndarray) -> np.ndarray:
    """A display colour per term ``(term, 3)``: the CIELAB mean of the chips weighted by
    ``P(term | chip)^8`` (the term's core), as sRGB."""
    w = p_mean**8 + 1e-300
    return lab_to_srgb((w.T @ lab) / w.sum(0)[:, None])


def _fresh_z(rng: np.random.Generator, shape: tuple[int, ...]) -> np.ndarray:
    """Standard-normal speaker effects, zero-sum over the last (term) axis."""
    z = rng.standard_normal(shape)
    out: np.ndarray = z - z.mean(-1, keepdims=True)
    return out


# ============================================================================== settings


class ColourNamingSettings(ModelSettings):
    """Settings of ``sci.colour_naming``."""

    kind: Literal["hierarchical", "pooled"] = Field(
        "hierarchical",
        description="hierarchical: every speaker has their own intercept and linear "
        "weights around the language's; pooled: all speakers share one naming "
        "(x-ui-level: basic)",
    )
    features: FeatureSet = Field(
        "cielab",
        description="cielab: a quadratic surface in CIELAB; grid_line / grid_circle: on the "
        "printed palette, with the hue column as a line or as a circle (x-ui-level: basic)",
    )
    w_sd: float = Field(3.0, gt=0, le=20, description="Prior sd of the zero-sum weights")
    sd_scale: float = Field(
        1.0, gt=0, le=10, description="Half-normal scale of the between-speaker sds"
    )
    n_random: int = Field(
        4,
        ge=1,
        le=10,
        description="Leading features with speaker effects (4: the intercept and L, a, b)",
    )
    hue_columns: int | None = Field(
        None, ge=2, le=MAX_GRID, description="Hue columns around the circle (default: the "
        "largest grid_col; grid_circle only)",
    )  # fmt: skip
    new_speakers: int = Field(
        20,
        ge=1,
        le=200,
        description="Simulated new speakers per draw for the language-level shares",
    )
    score_draws: int = Field(
        200, ge=10, le=4000, description="Posterior draws used by one score request"
    )


_TEMPLATE: dict[str, Any] = {
    "params": {},
    "template": {
        "dims": {
            SPEAKER_DIM: {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "hyperprior",
            },
            CHIP_DIM: {"kind": "integer", "policy": "fixed"},
        },
        "vars": {
            TARGET: {
                "dims": [SPEAKER_DIM, CHIP_DIM],
                "dtype": "str",
                "role": "target",
                "missing": "allow",
                "description": "The term each speaker gave each chip ('' = no answer)",
            },
            **{
                v: {
                    "dims": [CHIP_DIM],
                    "dtype": "float64",
                    "role": "covariate",
                    "description": f"CIELAB {axis} of the chip",
                }
                for v, axis in zip(LAB_VARS, ("L*", "a*", "b*"), strict=True)
            },
            "grid_row": {
                "dims": [CHIP_DIM],
                "dtype": "int64",
                "role": "covariate",
                "description": "Row of the chip on the palette (WCS: A-J as 0-9)",
            },
            "grid_col": {
                "dims": [CHIP_DIM],
                "dtype": "int64",
                "role": "covariate",
                "description": "Hue column of the chip on the palette (0 = greys)",
            },
        },
    },
}

_GRID = {
    "y": LIGHTNESS_DIM,
    "x": HUE_DIM,
    "frame": TERM_DIM,
    "origin": "upper",
    "colormap": "viridis",
}


# ============================================================================== the model


class ColourNamingModel(CustomModel[ColourNamingSettings]):
    """Softmax naming over quadratic CIELAB logits, pooled or with speaker effects (E77)."""

    id = "sci.colour_naming"
    version = "1.0.0"
    Settings = ColourNamingSettings
    description = (
        "Colour naming in one language (World Color Survey): the probability that a speaker "
        "gives each term to each colour chip, as a softmax over quadratic CIELAB logits with "
        "zero-sum weights, pooled or with speaker-level intercepts and linear weights. "
        "Predicts new speakers and maps each term's share of speakers on the chip palette."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "simulate", "evaluate"}),
        deterministic_given_seed=True,
        checkpointable=True,
        identified_quantities=["W", "sd"],
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "p_name": {
            "dims": [SPEAKER_DIM, CHIP_DIM, TERM_DIM],
            "units": "probability",
            "description": "Probability that the speaker gives the term to the chip (new "
            "speakers: fresh effects per draw); predictive: one sampled term per draw",
        },
        "p_term": {
            "dims": [TERM_DIM, LIGHTNESS_DIM, HUE_DIM],
            "units": "probability",
            "description": "Share of the language's speakers who give the term to the chip, "
            "on the palette grid (cells without a chip: empty in the posterior, 0 in scores)",
            "grid": _GRID,
        },
        "mode_term_rgb": {
            "dims": [CHIP_DIM, RGB_DIM],
            "units": "sRGB",
            "description": "Each chip in the display colour of its most probable term in "
            "the draw (the posterised map)",
        },
        "p_top": {
            "dims": [CHIP_DIM],
            "units": "probability",
            "description": "Share of speakers who give the chip the term that is most "
            "probable on average",
        },
        "k_eff_speaker": {
            "dims": [],
            "units": "categories",
            "description": "Effective number of categories of one speaker",
        },
        "k_eff_language": {
            "dims": [],
            "units": "categories",
            "description": "Effective number of categories of the language (all speakers)",
        },
    }
    plots = ("core.convergence", "core.posterior_forest", "sci.swatches", "sci.field_map")
    # Nafaanra (29 speakers, 4 terms): pooled ~3 s, hierarchical ~20 s on nutpie
    resources = ResourceHint(est_fit_seconds=60)
    default_engine: ClassVar[str] = "pymc"

    # ================================================================== build
    def build_model(self, data: _Data, s: ColourNamingSettings) -> pm.Model:
        """The E77 naming model (``naming_model`` in the notebook)."""
        k, t = data.features.shape[1], len(data.terms)
        coords = {
            TERM_DIM: list(data.terms),
            FEAT_DIM: list(data.feature_names),
            RFEAT_DIM: list(data.feature_names[: s.n_random]),
            SPEAKER_DIM: list(data.speakers),
        }
        sp, ch = np.nonzero(data.codes >= 0)
        y = data.codes[sp, ch]
        feats = pt.as_tensor(data.features)
        with pm.Model(coords=coords) as model:
            w = pm.ZeroSumNormal("W", s.w_sd, dims=(FEAT_DIM, TERM_DIM))
            if s.kind == "hierarchical":
                n_s = len(data.speakers)
                sd = pm.HalfNormal("sd", s.sd_scale, dims=(RFEAT_DIM, TERM_DIM))
                z = pm.ZeroSumNormal("z", 1.0, dims=(SPEAKER_DIM, RFEAT_DIM, TERM_DIM))
                ws = w[None] + pt.concatenate(
                    [sd[None] * z, pt.zeros((n_s, k - s.n_random, t))], axis=1
                )
                eta = pt.einsum("ck,skt->sct", feats, ws)
                lse = pt.logsumexp(eta, axis=-1)
                flat = eta.reshape((n_s * data.n_chips, t))
                cell = sp * data.n_chips + ch
                pm.Potential("loglik", pt.sum(flat[cell, y]) - pt.sum(lse.reshape((-1,))[cell]))
            else:
                eta = pt.dot(feats, w)
                logp = eta - pt.logsumexp(eta, axis=-1, keepdims=True)
                pm.Potential("loglik", pt.sum(logp * data.counts))
        return model

    # ================================================================== fit
    def fit(
        self, ds: xr.Dataset, s: ColourNamingSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        data = _Data.from_dataset(ds, s)
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        model = self.build_model(data, s)
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
                ctx.progress(0.8 * fraction, message)

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
        names = [n for n in ("W", "sd", "z") if n in posterior]
        posterior = posterior[names].assign_coords(
            {SPEAKER_DIM: list(data.speakers)} if "z" in names else {}
        )
        ctx.progress(0.85, "language-level shares and colours")
        lang = _language_level(data, s, posterior, seed)
        posterior = xr.merge([posterior, lang.posterior])
        engine = result.engine
        groups: dict[str, xr.Dataset] = {"posterior": _clean(posterior, engine)}
        stats = result.group("sample_stats")
        if stats is not None:
            keep = [
                v
                for v in stats.data_vars
                if stats[v].dtype.kind in "biuf" and set(stats[v].dims) <= {"chain", "draw"}
            ]
            groups["sample_stats"] = _clean(stats[keep], engine)
        groups["observed_data"] = _clean(
            xr.Dataset(
                {
                    TARGET: xr.DataArray(
                        data.codes.astype(np.int16), dims=(SPEAKER_DIM, CHIP_DIM)
                    ),
                    "mode_term_rgb": lang.observed_rgb,
                },
                coords={SPEAKER_DIM: list(data.speakers), CHIP_DIM: data.chips},
            ),
            engine,
        )
        groups["constant_data"] = _clean(data.constant(), engine)
        groups["log_likelihood"] = _clean(_log_likelihood(data, s, posterior), engine)
        with _quiet():
            prior = pm.sample_prior_predictive(
                draws=200, model=model, var_names=[n for n in ("W", "sd") if n in names],
                random_seed=seed,
            )  # fmt: skip
        groups["prior"] = _clean(prior["prior"].to_dataset(), engine)
        ctx.progress(1.0, "posterior ready")
        learned: dict[str, Any] = {
            "engine": engine,
            "tune": result.plan.tune,
            "draws": result.plan.draws,
            "chains": result.plan.chains,
            "resumed_from": result.resumed_from,
            "checkpoints": result.checkpoints,
            "kind": s.kind,
            "features": s.features,
            "terms": list(data.terms),
            "n_speakers": len(data.speakers),
            "n_responses": int((data.codes >= 0).sum()),
            "missing_share": float((data.codes < 0).mean()),
            "term_rgb": [[round(float(v), 4) for v in rgb] for rgb in lang.term_rgb],
        }
        return FitOutput(groups=groups, learned=learned)

    def diagnostics(self, fitted: Fitted, ds: xr.Dataset) -> list[Diagnostic]:
        post = fitted.groups.get("posterior")
        if post is None:
            return []
        names = [n for n in ("W", "sd") if n in post]
        return convergence_diagnostics(post, fitted.groups.get("sample_stats"), var_names=names)

    # ================================================================== score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        post = fitted.posterior
        const = fitted.groups.get("constant_data")
        if const is None or FEATURES not in const:
            raise MapiError(
                "the fitted model has no chip features; refit the model",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        for dim in (SPEAKER_DIM, CHIP_DIM):
            if dim not in ds.dims:
                raise MapiError(
                    f"the score data has no {dim!r} dim",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer="/data",
                )
        terms = [str(t) for t in post[TERM_DIM].values]
        fit_chips = np.asarray(const[CHIP_DIM].values)
        pos = {int(c): i for i, c in enumerate(fit_chips.tolist())}
        try:
            chip_idx = np.array([pos[int(c)] for c in np.asarray(ds[CHIP_DIM].values)])
        except KeyError:
            raise MapiError(
                "the score data has chips the model was not fitted on",
                code=ErrorCode.UNSEEN_LEVELS,
                pointer=f"/data/{CHIP_DIM}",
            ) from None
        feats = np.asarray(const[FEATURES].transpose(CHIP_DIM, FEAT_DIM).values)[chip_idx]
        chains, draws = _score_draws(post, ctx.draw_index, s.score_draws)
        n = chains.size
        w = _at(post["W"], chains, draws)  # (n, feat, term)
        speakers = np.asarray(ds[SPEAKER_DIM].values)
        new = np.asarray(new_level_mask(ds, SPEAKER_DIM).values, dtype=bool)
        fresh = np.ones_like(new) if ctx.mode == "simulate" else new
        strategy = ctx.strategy(SPEAKER_DIM)
        if new.any() and strategy == "error" and ctx.mode != "simulate":
            raise MapiError(
                f"{int(new.sum())} speaker(s) were not seen at fit and the new-level "
                "strategy of 'speaker' is 'error'",
                code=ErrorCode.UNSEEN_LEVELS,
                pointer=f"/data/{SPEAKER_DIM}",
            )
        n_t = len(terms)
        ws = np.repeat(w[:, None], speakers.size, 1)  # (n, speaker, feat, term)
        if "z" in post and "sd" in post:
            sd = _at(post["sd"], chains, draws)  # (n, rfeat, term)
            z_fit = _at(post["z"], chains, draws)  # (n, speaker_fit, rfeat, term)
            r = sd.shape[1]
            fit_pos = {str(v): i for i, v in enumerate(post[SPEAKER_DIM].values.tolist())}
            for i, label in enumerate(speakers.tolist()):
                if fresh[i]:
                    if strategy in ("pooled", "zero") and ctx.mode != "simulate":
                        continue  # the language's own weights
                    z = _fresh_z(_rng(ctx.seed, f"speaker:{label}"), (n, r, n_t))
                elif str(label) in fit_pos:
                    z = z_fit[:, fit_pos[str(label)]]
                else:  # pragma: no cover - canonicalisation flags unseen labels as new
                    continue
                ws[:, i, :r] += sd * z
        prob = softmax(np.einsum("ck,nskt->nsct", feats, ws))  # (n, speaker, chip, term)
        kind = "expected"
        if ctx.kind == "predictive" and ctx.mode != "evaluate":
            kind = "predictive"
            pick = np.empty(prob.shape[:-1], dtype=np.int64)
            for i, label in enumerate(speakers.tolist()):
                u = _rng(ctx.seed, f"names:{label}").random((n, fit_chips.size))[:, chip_idx]
                cum = prob[:, i].cumsum(-1)
                pick[:, i] = np.minimum((cum < u[..., None]).sum(-1), n_t - 1)
            prob = np.eye(n_t)[pick]
        chain_draw = {"chain": [0], "draw": np.arange(n)}
        p_name = xr.DataArray(
            prob[None],
            dims=("chain", "draw", SPEAKER_DIM, CHIP_DIM, TERM_DIM),
            coords={
                **chain_draw,
                SPEAKER_DIM: speakers,
                CHIP_DIM: np.asarray(ds[CHIP_DIM].values),
                TERM_DIM: terms,
            },
            attrs={
                "kind": kind,
                "units": "probability",
                "noise": "seeded_stream" if kind == "predictive" else "none",
            },
        )
        out: dict[str, xr.DataArray] = {"p_name": p_name}
        for name in ("p_term", "mode_term_rgb", "p_top", "k_eff_speaker", "k_eff_language"):
            da = post[name]
            arr = _at(da, chains, draws)[None]
            other = [str(d) for d in da.dims if d not in ("chain", "draw")]
            coords: dict[str, Any] = {**chain_draw}
            coords.update({d: da[d].values for d in other if d in da.coords})
            if CHIP_DIM in other:
                arr = np.take(arr, chip_idx, axis=2 + other.index(CHIP_DIM))
                coords[CHIP_DIM] = np.asarray(ds[CHIP_DIM].values)
                for c in ("row", "col"):  # the palette place, for map layouts
                    if c in da.coords:
                        coords[c] = (CHIP_DIM, np.asarray(da[c].values)[chip_idx])
            if name == "p_term":  # grid cells without a chip: 0 (scores stay finite)
                arr = np.nan_to_num(arr, nan=0.0)
            units = self.outputs[name]["units"]
            out[name] = xr.DataArray(
                arr.astype(np.float64),
                dims=("chain", "draw", *other),
                coords=coords,
                attrs={"kind": "expected", "units": units, "noise": "none"},
            )
        pred = xr.Dataset(out)
        pred.attrs = {"representation": "draws"}
        obs = fitted.groups.get("observed_data")
        nodes: dict[str, xr.Dataset] = {PREDICTIONS: pred}
        if obs is not None and "mode_term_rgb" in obs:
            nodes["observed_data"] = obs[["mode_term_rgb"]]
        return xr.DataTree.from_dict(nodes)


# ============================================================================== helpers


@dataclass(slots=True)
class _Data:
    """One language's naming as the model needs it."""

    speakers: list[str]
    chips: np.ndarray  # (chip,) labels in fit order
    terms: list[str]
    codes: np.ndarray  # (speaker, chip) term index, -1 = no answer
    lab: np.ndarray  # (chip, 3)
    row: np.ndarray  # (chip,) int
    col: np.ndarray  # (chip,) int
    features: np.ndarray  # (chip, feat)
    feature_names: tuple[str, ...]

    @property
    def n_chips(self) -> int:
        return int(self.chips.size)

    @property
    def counts(self) -> np.ndarray:
        """``(chip, term)`` responses over speakers."""
        t = len(self.terms)
        return np.stack([(self.codes == k).sum(0) for k in range(t)], 1).astype(float)

    @property
    def rgb(self) -> np.ndarray:
        return lab_to_srgb(self.lab)

    def grid_index(self) -> tuple[np.ndarray, np.ndarray]:
        """Labels of the ``lightness`` and ``hue`` dims (every row / column in range)."""
        return (
            np.arange(int(self.row.min()), int(self.row.max()) + 1),
            np.arange(int(self.col.min()), int(self.col.max()) + 1),
        )

    def constant(self) -> xr.Dataset:
        coords = {CHIP_DIM: self.chips, FEAT_DIM: list(self.feature_names), RGB_DIM: list("rgb")}
        return xr.Dataset(
            {
                FEATURES: ((CHIP_DIM, FEAT_DIM), self.features),
                CHIP_RGB: ((CHIP_DIM, RGB_DIM), self.rgb),
                **{v: ((CHIP_DIM,), self.lab[:, i]) for i, v in enumerate(LAB_VARS)},
                "grid_row": ((CHIP_DIM,), self.row.astype(np.int64)),
                "grid_col": ((CHIP_DIM,), self.col.astype(np.int64)),
            },
            coords={**coords, "row": (CHIP_DIM, self.row), "col": (CHIP_DIM, self.col)},
        )

    @classmethod
    def from_dataset(cls, ds: xr.Dataset, s: ColourNamingSettings) -> _Data:
        for d in (SPEAKER_DIM, CHIP_DIM):
            if d not in ds.dims:
                raise MapiError(f"the data has no dim {d!r}", code=ErrorCode.SCHEMA_INVALID)
        for v in (TARGET, *LAB_VARS, *GRID_VARS):
            if v not in ds:
                raise MapiError(
                    f"the data has no variable {v!r}",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{v}",
                )
        chips = np.asarray(ds[CHIP_DIM].values)
        lab = np.stack([np.asarray(ds[v].values, dtype=float) for v in LAB_VARS], 1)
        if not np.isfinite(lab).all():
            raise MapiError(
                "every chip needs a CIELAB colour (lab_l, lab_a, lab_b have missing values)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data/lab_l",
            )
        grid = np.stack([np.asarray(ds[v].values, dtype=float) for v in GRID_VARS], 1)
        if not np.isfinite(grid).all() or (grid < 0).any() or (grid > MAX_GRID).any():
            raise MapiError(
                f"grid_row and grid_col must be whole numbers from 0 to {MAX_GRID}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data/grid_row",
            )
        row, col = grid[:, 0].astype(int), grid[:, 1].astype(int)
        if len(set(zip(row.tolist(), col.tolist(), strict=True))) != chips.size:
            raise MapiError(
                "two chips have the same grid_row and grid_col",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data/grid_row",
            )
        raw = ds[TARGET].transpose(SPEAKER_DIM, CHIP_DIM).values
        labels = np.array([_label(v) for v in raw.reshape(-1)], dtype=object).reshape(raw.shape)
        given = labels != ""
        values, counts = np.unique(labels[given].astype(str), return_counts=True)
        order = sorted(range(values.size), key=lambda i: (-int(counts[i]), str(values[i])))
        terms = [str(values[i]) for i in order]
        if len(terms) < 2:
            raise MapiError(
                f"the naming has {len(terms)} distinct term(s); the model needs at least 2",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TARGET}",
            )
        if len(terms) > MAX_TERMS:
            raise MapiError(
                f"the naming has {len(terms)} distinct terms (at most {MAX_TERMS}); merge the "
                "rare terms into one 'other' term first",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TARGET}",
            )
        index = {t: i for i, t in enumerate(terms)}
        codes = np.full(labels.shape, -1, dtype=np.int64)
        codes[given] = [index[str(v)] for v in labels[given]]
        speakers = [str(v) for v in np.asarray(ds[SPEAKER_DIM].values).tolist()]
        if s.kind == "hierarchical" and len(speakers) < 2:
            raise MapiError(
                "kind 'hierarchical' needs at least 2 speakers (use kind 'pooled')",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/kind",
            )
        names = FEATURE_NAMES[s.features]
        if s.n_random > len(names):
            raise MapiError(
                f"n_random is {s.n_random} but the {s.features!r} features have only "
                f"{len(names)} columns",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/n_random",
            )
        feats = chip_features(lab, row, col, s.features, hue_columns=s.hue_columns)
        return cls(speakers, chips, terms, codes, lab, row, col, feats, names)


def _label(value: Any) -> str:
    """A naming value as a term label (``''`` for no answer)."""
    if value is None:
        return ""
    if isinstance(value, float) and not np.isfinite(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none") else text


@dataclass(slots=True)
class _Language:
    posterior: xr.Dataset
    observed_rgb: xr.DataArray
    term_rgb: np.ndarray


def _language_level(
    data: _Data, s: ColourNamingSettings, posterior: xr.Dataset, seed: int
) -> _Language:
    """Language-level quantities per draw (kept in the posterior; see the module doc)."""
    w = np.asarray(posterior["W"].transpose("chain", "draw", FEAT_DIM, TERM_DIM).values)
    n_c, n_d = w.shape[:2]
    t = len(data.terms)
    flat_w = w.reshape(n_c * n_d, *w.shape[2:])
    hier = "sd" in posterior
    if hier:
        sd = np.asarray(posterior["sd"].transpose("chain", "draw", RFEAT_DIM, TERM_DIM).values)
        flat_sd = sd.reshape(n_c * n_d, *sd.shape[2:])
    rng = _rng(seed, "new-speakers")
    shares = np.empty((flat_w.shape[0], data.n_chips, t))
    k_spk = np.empty(flat_w.shape[0])
    for d in range(flat_w.shape[0]):
        if hier:
            z = _fresh_z(rng, (s.new_speakers, flat_sd.shape[1], t))
            p = naming_probabilities(data.features, flat_w[d], flat_sd[d], z)
            shares[d] = p.mean(0)
            k_spk[d] = float(effective_categories(p).mean())
        else:
            shares[d] = naming_probabilities(data.features, flat_w[d])
            k_spk[d] = float(effective_categories(shares[d]))
    k_lang = effective_categories(shares)
    mean = shares.mean(0)
    term_rgb = exemplar_colours(mean, data.lab)
    top = mean.argmax(1)
    p_top = shares[:, np.arange(data.n_chips), top]
    modal = shares.argmax(2)  # (draw, chip)
    rgb = term_rgb[modal]  # (draw, chip, 3)
    light, hue = data.grid_index()
    grid = np.full((flat_w.shape[0], t, light.size, hue.size), np.nan, dtype=np.float32)
    grid[:, :, data.row - light[0], data.col - hue[0]] = shares.transpose(0, 2, 1)
    cd = ("chain", "draw")
    chip_coords = {
        CHIP_DIM: data.chips,
        "row": (CHIP_DIM, data.row.astype(np.int64)),
        "col": (CHIP_DIM, data.col.astype(np.int64)),
    }
    lang = xr.Dataset(
        {
            "p_term": (
                (*cd, TERM_DIM, LIGHTNESS_DIM, HUE_DIM),
                grid.reshape(n_c, n_d, t, light.size, hue.size),
            ),
            "mode_term_rgb": (
                (*cd, CHIP_DIM, RGB_DIM),
                rgb.reshape(n_c, n_d, data.n_chips, 3).astype(np.float32),
            ),
            "p_top": ((*cd, CHIP_DIM), p_top.reshape(n_c, n_d, -1).astype(np.float32)),
            "k_eff_speaker": (cd, k_spk.reshape(n_c, n_d)),
            "k_eff_language": (cd, k_lang.reshape(n_c, n_d)),
        },
        coords={
            "chain": posterior["chain"].values,
            "draw": posterior["draw"].values,
            TERM_DIM: list(data.terms),
            LIGHTNESS_DIM: light,
            HUE_DIM: hue,
            RGB_DIM: list("rgb"),
            **chip_coords,
        },
    )
    counts = data.counts
    answered = counts.sum(1) > 0
    observed = np.full((data.n_chips, 3), np.nan)
    observed[answered] = term_rgb[counts[answered].argmax(1)]
    obs_rgb = xr.DataArray(
        observed, dims=(CHIP_DIM, RGB_DIM), coords={RGB_DIM: list("rgb"), **chip_coords}
    )
    return _Language(lang, obs_rgb, term_rgb)


def _log_likelihood(data: _Data, s: ColourNamingSettings, posterior: xr.Dataset) -> xr.Dataset:
    """Pointwise log-likelihood: one value per chip, summed over its speakers."""
    w = np.asarray(posterior["W"].transpose("chain", "draw", FEAT_DIM, TERM_DIM).values)
    n_c, n_d = w.shape[:2]
    flat_w = w.reshape(n_c * n_d, *w.shape[2:])
    codes = data.codes
    ok = codes >= 0
    safe = np.where(ok, codes, 0)
    out = np.empty((flat_w.shape[0], data.n_chips))
    hier = "sd" in posterior and "z" in posterior
    if hier:
        sd = np.asarray(posterior["sd"].transpose("chain", "draw", RFEAT_DIM, TERM_DIM).values)
        z = np.asarray(
            posterior["z"].transpose("chain", "draw", SPEAKER_DIM, RFEAT_DIM, TERM_DIM).values
        )
        flat_sd = sd.reshape(n_c * n_d, *sd.shape[2:])
        flat_z = z.reshape(n_c * n_d, *z.shape[2:])
    spk = np.arange(codes.shape[0])[:, None]
    chip = np.arange(data.n_chips)[None, :]
    for d in range(flat_w.shape[0]):
        if hier:
            p = naming_probabilities(data.features, flat_w[d], flat_sd[d], flat_z[d])
            lp = np.log(np.clip(p[spk, chip, safe], 1e-300, 1))
        else:
            p = naming_probabilities(data.features, flat_w[d])
            lp = np.log(np.clip(p[chip, safe], 1e-300, 1))
        out[d] = np.where(ok, lp, 0.0).sum(0)
    return xr.Dataset(
        {TARGET: (("chain", "draw", CHIP_DIM), out.reshape(n_c, n_d, -1))},
        coords={
            "chain": posterior["chain"].values,
            "draw": posterior["draw"].values,
            CHIP_DIM: data.chips,
        },
    )


def _score_draws(
    post: xr.Dataset, draw_index: np.ndarray | None, n: int
) -> tuple[np.ndarray, np.ndarray]:
    """``(chain, draw)`` positions of the first ``n`` draws of the bundle thinning index (or
    ``n`` evenly spaced draws without one); the same draws for every request."""
    chains, draws = int(post.sizes["chain"]), int(post.sizes["draw"])
    if draw_index is not None and len(draw_index):
        pairs = np.asarray(draw_index, dtype=np.int64).reshape(-1, 2)
        pairs = pairs[(pairs[:, 0] < chains) & (pairs[:, 1] < draws)][:n]
        return pairs[:, 0], pairs[:, 1]
    flat = np.unique(np.linspace(0, chains * draws - 1, min(n, chains * draws)).round())
    flat = flat.astype(np.int64)
    return flat // draws, flat % draws


def _at(da: xr.DataArray, chains: np.ndarray, draws: np.ndarray) -> np.ndarray:
    """Values of ``da`` at the ``(chain, draw)`` pairs, sample axis first."""
    arr = np.asarray(da.transpose("chain", "draw", ...).values)
    out: np.ndarray = arr[chains, draws]
    return out


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
