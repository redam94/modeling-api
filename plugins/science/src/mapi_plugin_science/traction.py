"""``sci.traction_force``: traction force microscopy as a Gaussian inverse problem (E60).

A cell on a soft gel pulls on the gel surface; beads in the gel move. This model recovers
the 2-D traction field ``t`` (Pa) under the cell from the measured displacement field ``u``
(um) of the gel surface.

Canonical inputs::

    dims  component: categorical, fixed (levels "x", "y")
          row, col:  integer, fixed (the raster; x runs along col, y along row)
    vars  displacement(component, row, col): target, float64, um, no missing values
          force_mask(row, col): mask, bool (optional, setting ``force_mask``): the region
            that strain energy and contractility are summed over

**Physics.** The gel is an elastic half-space (Young's modulus ``E``, Poisson ratio ``nu``).
The Boussinesq Green's function is diagonal in Fourier space once every vector mode is split
into its longitudinal part (along ``k``) and its transverse part::

    u_L = g_L(k) t_L,  g_L = 2 (1 + nu)(1 - nu) / (E k)
    u_T = g_T(k) t_T,  g_T = 2 (1 + nu) / (E k)

so on a periodic grid (unitary FFT) the problem is ``2 (N - 1)`` independent scalar problems.
The ``k = 0`` mode is image drift (and a cell exerts no net force); it is dropped and the
drift is the per-component mean of the data.

**Model.** A stationary Gaussian prior on each traction polarisation with spectrum ``s2(k)``
(``prior``: ``white`` = Tikhonov, ``smooth`` = squared exponential, ``matern``:
``s0^2 (1 + k^2 l^2)^-alpha``) and white noise ``sigma`` on the displacements. With the field
integrated out the displacement modes are independent, ``U ~ N(0, g^2 s2 + sigma^2)``, so
the exact log marginal likelihood is a sum of per-mode terms over the FFT of the data. The
FFT is computed once in NumPy (the data are fixed); the pytensor graph holds only the
per-mode quadratic forms. NUTS (``mapi_pymc.engines``) samples the 2-4 hyperparameters
``noise_sd``, ``traction_sd``, ``lengthscale``, ``smoothness``.

**Field draws.** Given one hyperparameter draw the traction field is Gaussian, mode by mode::

    E[t | U] = g s2 / (g^2 s2 + sigma^2) U,   Var[t | U] = s2 sigma^2 / (g^2 s2 + sigma^2)

so an exact joint draw of the whole field costs two FFTs. The bundle stores only the
hyperparameter draws and the data (``observed_data``); ``score`` draws ``field_draws``
fields on demand from evenly spaced posterior draws of each chain. Each field draw uses a
generator seeded by ``(request seed, chain, draw)``, so scores are deterministic given the
seed and do not depend on how many draws are requested.

**Outputs** (representation ``draws``):

* ``traction(component, row, col)`` - joint posterior draws of the field, Pa (in both
  ``expected`` and ``predictive`` kinds: the field is latent, not observed);
* ``displacement_hat(component, row, col)`` - ``G t`` plus the drift (``expected``), or plus
  white noise with sd ``noise_sd`` (``predictive``), um;
* ``strain_energy`` - ``1/2 sum t . u dA`` over the force mask (whole grid without one), fJ;
* ``contractility`` - forces projected towards the force epicentre and summed (pyTFM's
  definition), uN. The epicentre is found once per score from the posterior mean field.

``young_cv > 0`` adds the uncertainty of the gel stiffness: tractions scale exactly with
``E`` and the displacements carry no information about it, so each posterior draw gets a
lognormal ``young_pa`` draw (stored in the posterior at fit) that scales its field, strain
energy and contractility.

The scores ignore any displacement in the payload: the field is the posterior given the fit
data, and score coordinates select pixels and components of it. Missing displacement values
are not supported (the Fourier diagonalisation needs the full raster), so cell-level splits
do not apply; fit without a split and evaluate in sample. Pointwise LOO is not offered: the
likelihood is a potential over Fourier modes, not over pixels.
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
)
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field
from scipy.optimize import minimize

__all__ = [
    "COL_DIM",
    "COMPONENTS",
    "COMPONENT_DIM",
    "FORCE_MASK",
    "HYPERPARAMETERS",
    "ROW_DIM",
    "TARGET",
    "FourierGrid",
    "TractionForceModel",
    "TractionForceSettings",
    "contractility",
    "epicentre",
    "forward",
    "from_modes",
    "green",
    "prior_spectrum",
    "strain_energy",
    "to_modes",
]

COMPONENT_DIM = "component"
ROW_DIM = "row"
COL_DIM = "col"
COMPONENTS = ("x", "y")
TARGET = "displacement"
FORCE_MASK = "force_mask"
HYPERPARAMETERS = ("noise_sd", "traction_sd", "lengthscale", "smoothness")
YOUNG = "young_pa"
FIELD_DIMS = (COMPONENT_DIM, ROW_DIM, COL_DIM)
PriorKind = Literal["white", "smooth", "matern"]


# ============================================================================== Fourier maths


@dataclass(frozen=True, slots=True)
class FourierGrid:
    """Wave vectors of an ``ny x nx`` grid with spacing ``h`` (um).

    ``k`` is ``|k|`` (1/um), ``cx``/``cy`` the unit vector ``k / |k|`` (0 at ``k = 0``) and
    ``keep`` is False only for the ``k = 0`` mode.
    """

    k: np.ndarray
    cx: np.ndarray
    cy: np.ndarray
    keep: np.ndarray
    h: float

    @classmethod
    def of(cls, ny: int, nx: int, h: float) -> FourierGrid:
        kx, ky = np.meshgrid(2 * np.pi * np.fft.fftfreq(nx, h), 2 * np.pi * np.fft.fftfreq(ny, h))
        k = np.hypot(kx, ky)
        safe = np.where(k == 0, 1.0, k)
        return cls(k=k, cx=kx / safe, cy=ky / safe, keep=k > 0, h=float(h))


def to_modes(fx: np.ndarray, fy: np.ndarray, g: FourierGrid) -> tuple[np.ndarray, np.ndarray]:
    """Real vector field (``..., ny, nx``) -> longitudinal and transverse modes (unitary)."""
    ffx = np.fft.fft2(fx, norm="ortho")
    ffy = np.fft.fft2(fy, norm="ortho")
    return g.cx * ffx + g.cy * ffy, g.cy * ffx - g.cx * ffy


def from_modes(fl: np.ndarray, ft: np.ndarray, g: FourierGrid) -> tuple[np.ndarray, np.ndarray]:
    """Longitudinal and transverse modes -> real vector field (the ``k = 0`` mode dropped)."""
    fl = np.where(g.keep, fl, 0)
    ft = np.where(g.keep, ft, 0)
    ffx = g.cx * fl + g.cy * ft
    ffy = g.cy * fl - g.cx * ft
    return np.fft.ifft2(ffx, norm="ortho").real, np.fft.ifft2(ffy, norm="ortho").real


def green(g: FourierGrid, young: float, poisson: float) -> tuple[np.ndarray, np.ndarray]:
    """Response of an elastic half-space (um per Pa) to longitudinal / transverse modes."""
    safe = np.where(g.keep, g.k, 1.0)
    a = 2.0 * (1.0 + poisson) / young
    return (
        np.where(g.keep, a * (1.0 - poisson) / safe, 0.0),
        np.where(g.keep, a / safe, 0.0),
    )


def prior_spectrum(
    k: Any,
    prior: PriorKind,
    traction_sd: Any,
    lengthscale: Any = None,
    smoothness: Any = None,
    *,
    lib: Any = np,
) -> Any:
    """Prior variance of each traction mode (NumPy, or pytensor with ``lib=pt``)."""
    if prior == "white":
        return traction_sd**2 + 0.0 * k
    if prior == "smooth":
        return traction_sd**2 * lib.exp(-0.5 * (k * lengthscale) ** 2)
    return traction_sd**2 * (1.0 + (k * lengthscale) ** 2) ** (-smoothness)


def forward(
    tx: np.ndarray, ty: np.ndarray, g: FourierGrid, young: float, poisson: float
) -> tuple[np.ndarray, np.ndarray]:
    """Displacement field (um) of a traction field (Pa) on a periodic grid."""
    gl, gt = green(g, young, poisson)
    tl, tt = to_modes(tx, ty, g)
    return from_modes(gl * tl, gt * tt, g)


def strain_energy(
    tx: np.ndarray, ty: np.ndarray, ux: np.ndarray, uy: np.ndarray, mask: np.ndarray, h: float
) -> np.ndarray:
    """``U = 1/2 sum t . u dA`` over ``mask`` in fJ (Pa um^3 = 1e-18 J), per leading index."""
    w = (tx * ux + ty * uy) * mask
    return 0.5 * w.reshape(*w.shape[:-2], -1).sum(-1) * h**2 * 1e-3


def contractility(
    tx: np.ndarray,
    ty: np.ndarray,
    mask: np.ndarray,
    h: float,
    centre: tuple[float, float],
) -> np.ndarray:
    """Forces projected on the direction to ``centre`` (um), summed over ``mask``, in uN."""
    ny, nx = mask.shape
    x, y = np.meshgrid(np.arange(nx) * h, np.arange(ny) * h)
    dx, dy = centre[0] - x, centre[1] - y
    proj = (tx * dx + ty * dy) / (np.hypot(dx, dy) + 1e-9) * mask
    return proj.reshape(*proj.shape[:-2], -1).sum(-1) * h**2 * 1e-6


def epicentre(tx: np.ndarray, ty: np.ndarray, mask: np.ndarray, h: float) -> tuple[float, float]:
    """The point (um) towards which the projected forces are largest (pyTFM)."""
    ny, nx = mask.shape
    x, y = np.meshgrid(np.arange(nx) * h, np.arange(ny) * h)
    start = [float(x[mask].mean()), float(y[mask].mean())]
    res = minimize(
        lambda c: -float(contractility(tx, ty, mask, h, (c[0], c[1]))),
        start,
        method="Nelder-Mead",
        options={"xatol": 1e-3 * h, "fatol": 1e-9, "maxiter": 400},
    )
    return float(res.x[0]), float(res.x[1])


# ============================================================================== settings


class TractionForceSettings(ModelSettings):
    """Settings of ``sci.traction_force``: the gel, the prior spectrum and the draws."""

    grid_um: float = Field(1.0, gt=0, description="Grid spacing of the displacement raster (um)")
    young_pa: float = Field(10_000.0, gt=0, description="Young's modulus of the gel (Pa)")
    poisson: float = Field(0.49, ge=0, lt=0.5, description="Poisson ratio of the gel")
    young_cv: float = Field(
        0.0,
        ge=0,
        le=1,
        description="Relative uncertainty of Young's modulus (lognormal sd); 0 = known exactly",
    )
    prior: PriorKind = Field(
        "matern",
        description="Prior spectrum of the tractions: white (Tikhonov), smooth (squared "
        "exponential) or matern ((1 + k^2 l^2)^-alpha)",
    )
    noise_um: float = Field(0.01, gt=0, description="Prior median of the noise sd (um)")
    traction_pa: float = Field(300.0, gt=0, description="Prior median of the traction scale (Pa)")
    lengthscale_um: float = Field(
        5.0, gt=0, description="Prior median of the length scale (um; smooth and matern)"
    )
    field_draws: int = Field(
        100, ge=1, le=1000, description="Traction fields drawn per score (over all chains)"
    )
    force_mask: bool = Field(
        False,
        description="The data has a force_mask(row, col) variable: strain energy and "
        "contractility are summed inside it (else over the whole grid)",
    )


_TEMPLATE: dict[str, Any] = {
    "params": {"force_mask": {"type": "boolean", "default": False}},
    "template": {
        "dims": {
            COMPONENT_DIM: {"kind": "categorical", "policy": "fixed"},
            ROW_DIM: {"kind": "integer", "policy": "fixed"},
            COL_DIM: {"kind": "integer", "policy": "fixed"},
        },
        "vars": {
            TARGET: {
                "dims": list(FIELD_DIMS),
                "dtype": "float64",
                "role": "target",
                "description": "Displacement of the gel surface (x and y components)",
            },
        },
    },
    "cases": [
        {
            "when": {"force_mask": True},
            "merge": {
                "vars": {
                    FORCE_MASK: {
                        "dims": [ROW_DIM, COL_DIM],
                        "dtype": "bool",
                        "role": "mask",
                        "description": "Region that strain energy and contractility cover",
                    }
                }
            },
        }
    ],
}

_GRID = {"y": ROW_DIM, "x": COL_DIM, "component": COMPONENT_DIM, "origin": "lower"}


# ============================================================================== the model


class TractionForceModel(CustomModel[TractionForceSettings]):
    """Bayesian Fourier-transform traction cytometry with exact field draws (E60)."""

    id = "sci.traction_force"
    version = "1.0.0"
    Settings = TractionForceSettings
    description = (
        "Traction force microscopy: recovers the 2-D traction field under a cell from the "
        "displacement field of the gel. A Gaussian prior in Fourier space (white, squared "
        "exponential or Matern spectrum); NUTS on the hyperparameters with the field "
        "integrated out, then exact posterior draws of the field."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "evaluate"}),
        loo_valid=False,
        loo_invalid_reason="the likelihood is a potential over Fourier modes of the whole "
        "raster; pixels are not conditionally independent, so pointwise leave-one-out is "
        "not defined",
        identified_quantities=["noise_sd", "traction_sd", "lengthscale", "smoothness"],
        deterministic_given_seed=True,
        checkpointable=True,
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "traction": {
            "dims": list(FIELD_DIMS),
            "units": "Pa",
            "description": "Joint posterior draws of the traction field",
            "grid": {**_GRID, "colormap": "magma"},
        },
        "displacement_hat": {
            "dims": list(FIELD_DIMS),
            "units_of": TARGET,
            "description": "Displacement of each traction draw (expected), plus noise (predictive)",
            "grid": {**_GRID, "colormap": "viridis"},
        },
        "strain_energy": {
            "dims": [],
            "units": "fJ",
            "description": "Elastic energy the cell put into the gel, inside the force mask",
        },
        "contractility": {
            "dims": [],
            "units": "uN",
            "description": "Sum of the forces towards the force epicentre, inside the mask",
        },
    }
    plots = ("core.convergence", "core.posterior_forest", "sci.vector_field")
    # a 189 x 193 PIV field: ~2.5 min of NUTS (73k modes per gradient), ~1 s per 100 fields
    resources = ResourceHint(est_fit_seconds=180)
    default_engine: ClassVar[str] = "pymc"

    # ================================================================== fit
    def build_model(self, data: _Data, s: TractionForceSettings) -> pm.Model:
        """The hyperparameter model: the traction field is integrated out exactly."""
        g = data.grid
        gl, gt = green(g, s.young_pa, s.poisson)
        keep = g.keep
        gk = np.concatenate([gl[keep], gt[keep]])
        kk = np.concatenate([g.k[keep], g.k[keep]])
        a2 = np.concatenate([np.abs(data.ul[keep]) ** 2, np.abs(data.ut[keep]) ** 2])
        with pm.Model() as model:
            noise = pm.LogNormal("noise_sd", np.log(s.noise_um), 1.0)
            scale = pm.LogNormal("traction_sd", np.log(s.traction_pa), 1.5)
            ell = (
                pm.LogNormal("lengthscale", np.log(s.lengthscale_um), 1.0)
                if s.prior != "white"
                else None
            )
            alpha = pm.LogNormal("smoothness", 0.0, 0.7) if s.prior == "matern" else None
            s2 = prior_spectrum(pt.as_tensor(kk), s.prior, scale, ell, alpha, lib=pt)
            c = pt.as_tensor(gk**2) * s2 + noise**2
            pm.Potential(
                "log_marginal_likelihood",
                -0.5 * pt.sum(pt.as_tensor(a2) / c + pt.log(2.0 * np.pi * c)),
            )
        return model

    def fit(
        self, ds: xr.Dataset, s: TractionForceSettings, cfg: FitConfig, ctx: FitContext
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
                ctx.progress(0.9 * fraction, message)

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
        names = [n for n in HYPERPARAMETERS if n in posterior]
        posterior = posterior[names]
        if s.young_cv > 0:
            # E is not identified by the displacements: its posterior is its prior
            z = _rng(seed, YOUNG).standard_normal(
                (posterior.sizes["chain"], posterior.sizes["draw"])
            )
            posterior[YOUNG] = (("chain", "draw"), s.young_pa * np.exp(s.young_cv * z))
        groups: dict[str, xr.Dataset] = {"posterior": _clean(posterior, result.engine)}
        stats = result.group("sample_stats")
        if stats is not None:
            keep = [
                v
                for v in stats.data_vars
                if stats[v].dtype.kind in "biuf" and set(stats[v].dims) <= {"chain", "draw"}
            ]
            groups["sample_stats"] = _clean(stats[keep], result.engine)
        groups["observed_data"] = _clean(xr.Dataset({TARGET: data.field}), result.engine)
        groups["constant_data"] = _clean(xr.Dataset({FORCE_MASK: data.mask_da}), result.engine)
        with _quiet():
            prior = pm.sample_prior_predictive(
                draws=200, model=model, var_names=names, random_seed=seed
            )
        groups["prior"] = _clean(prior["prior"].to_dataset(), result.engine)
        ctx.progress(1.0, "posterior ready")
        learned: dict[str, Any] = {
            "engine": result.engine,
            "tune": result.plan.tune,
            "draws": result.plan.draws,
            "chains": result.plan.chains,
            "resumed_from": result.resumed_from,
            "checkpoints": result.checkpoints,
            "prior": s.prior,
            "modes": int(2 * data.grid.keep.sum()),
            "drift_um": [float(v) for v in data.drift],
            "has_force_mask": bool(data.has_mask),
        }
        if cfg.keep.log_likelihood:
            learned["notes"] = [
                "log_likelihood not stored: the marginal likelihood is a potential over "
                "Fourier modes, so pointwise LOO is not valid"
            ]
        return FitOutput(groups=groups, learned=learned)

    def diagnostics(self, fitted: Fitted, ds: xr.Dataset) -> list[Diagnostic]:
        post = fitted.groups.get("posterior")
        if post is None:
            return []
        names = [n for n in HYPERPARAMETERS if n in post]
        out = convergence_diagnostics(post, fitted.groups.get("sample_stats"), var_names=names)
        check = self._spectrum_check(fitted)
        if check is not None:
            out.append(check)
        return out

    def _spectrum_check(self, fitted: Fitted) -> Diagnostic | None:
        """Data power vs expected power in the finest quarter of the modes (a free PPC).

        Correlated noise (PIV windows that overlap) shows as much less power at high ``k``
        than the white-noise model expects; the posterior widths are then too narrow.
        """
        s = self.parse_settings(fitted.settings)
        data = _Data.from_fitted(fitted, s)
        g = data.grid
        hyper = _hyper_means(fitted.posterior)
        gl, gt = green(g, s.young_pa, s.poisson)
        s2 = _spectrum(g.k, s.prior, hyper)
        expected = (gl**2 + gt**2) / 2 * s2 + hyper["noise_sd"] ** 2
        observed = (np.abs(data.ul) ** 2 + np.abs(data.ut) ** 2) / 2
        fine = g.keep & (g.k >= np.quantile(g.k[g.keep], 0.75))
        ratio = float(observed[fine].mean() / expected[fine].mean())
        if 0.5 <= ratio <= 2.0:
            return None
        return Diagnostic(
            code="NOISE_SPECTRUM_MISMATCH",
            severity="warning",
            metric="fine_mode_power_ratio",
            value=ratio,
            threshold=0.5 if ratio < 0.5 else 2.0,
            message_expert=f"observed / expected displacement power in the finest quarter of "
            f"the Fourier modes is {ratio:.2f}: the noise is not white (for example "
            "overlapping PIV windows), so posterior widths are too narrow",
            message_plain="The noise in the displacements is not the same at all scales. "
            "The ranges of the tractions are possibly too narrow.",
            suggested_action="Use the ranges with caution. Compare the results of more than "
            "one prior.",
        )

    # ================================================================== score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        data = _Data.from_fitted(fitted, s)
        post = fitted.posterior
        chains, draws = _field_draw_index(post, s.field_draws)
        draws_out = self._draw_fields(data, post, s, chains, draws, ctx)
        ctx.check_cancelled()
        coords = {
            "chain": np.asarray(post["chain"].values)[chains],
            "draw": np.asarray(post["draw"].values)[draws],
            COMPONENT_DIM: np.asarray(data.field[COMPONENT_DIM].values),
            ROW_DIM: np.asarray(data.field[ROW_DIM].values),
            COL_DIM: np.asarray(data.field[COL_DIM].values),
        }
        dims5 = ("chain", "draw", *FIELD_DIMS)
        noise = "seeded_white" if ctx.kind == "predictive" else "none"
        pred = xr.Dataset(
            {
                "traction": xr.DataArray(
                    draws_out.traction, dims=dims5, attrs={"kind": "posterior", "units": "Pa"}
                ),
                "displacement_hat": xr.DataArray(
                    draws_out.displacement, dims=dims5, attrs={"kind": ctx.kind, "noise": noise}
                ),
                "strain_energy": xr.DataArray(
                    draws_out.energy,
                    dims=("chain", "draw"),
                    attrs={"kind": "posterior", "units": "fJ"},
                ),
                "contractility": xr.DataArray(
                    draws_out.contractility,
                    dims=("chain", "draw"),
                    attrs={
                        "kind": "posterior",
                        "units": "uN",
                        "epicentre_x_um": draws_out.centre[0],
                        "epicentre_y_um": draws_out.centre[1],
                    },
                ),
            },
            coords=coords,
        )
        pred = pred.sel({d: np.asarray(ds[d].values) for d in FIELD_DIMS if d in ds.coords})
        return xr.DataTree.from_dict({PREDICTIONS: pred})

    def _draw_fields(
        self,
        data: _Data,
        post: xr.Dataset,
        s: TractionForceSettings,
        chains: np.ndarray,
        draws: np.ndarray,
        ctx: ScoreContext,
    ) -> _FieldDraws:
        """Exact joint field draws for each kept ``(chain, draw)``, with their summaries.

        Fields are stored as float32 ``(chain, draw, component, row, col)``; strain energy
        and contractility are computed per draw in float64. The epicentre comes from the
        conditional mean field at the posterior-mean hyperparameters, so it does not depend
        on which draws are kept.
        """
        g = data.grid
        h = s.grid_um
        gl, gt = green(g, s.young_pa, s.poisson)
        ny, nx = g.k.shape
        values = {
            n: np.asarray(post[n].transpose("chain", "draw").values)
            for n in (*HYPERPARAMETERS, YOUNG)
            if n in post
        }
        mean = _hyper_means(post)
        s2m, sig2m = _spectrum(g.k, s.prior, mean), mean["noise_sd"] ** 2
        zero = np.zeros_like(data.ul)
        mx, my = from_modes(
            _conditional(data.ul, gl, s2m, sig2m, zero),
            _conditional(data.ut, gt, s2m, sig2m, zero),
            g,
        )
        centre = epicentre(mx, my, data.mask, h)
        u0x, u0y = data.centred
        shape = (chains.size, draws.size, 2, ny, nx)
        traction = np.empty(shape, dtype=np.float32)
        disp = np.empty(shape, dtype=np.float32)
        energy = np.empty((chains.size, draws.size))
        contract = np.empty((chains.size, draws.size))
        for i, c in enumerate(chains.tolist()):
            for j, d in enumerate(draws.tolist()):
                ctx.check_cancelled()
                hyper = {n: float(v[c, d]) for n, v in values.items()}
                s2 = _spectrum(g.k, s.prior, hyper)
                sig2 = hyper["noise_sd"] ** 2
                rng = _rng(ctx.seed, f"field:{c}:{d}")
                zl, zt = to_modes(rng.standard_normal((ny, nx)), rng.standard_normal((ny, nx)), g)
                fl = _conditional(data.ul, gl, s2, sig2, zl)
                ft = _conditional(data.ut, gt, s2, sig2, zt)
                tx, ty = from_modes(fl, ft, g)
                ux, uy = from_modes(gl * fl, gt * ft, g)
                if ctx.kind == "predictive":
                    eps = _rng(ctx.seed, f"noise:{c}:{d}").standard_normal((2, ny, nx))
                    ux = ux + np.sqrt(sig2) * eps[0]
                    uy = uy + np.sqrt(sig2) * eps[1]
                if YOUNG in hyper:
                    # tractions scale exactly with E; the displacements do not change
                    factor = hyper[YOUNG] / s.young_pa
                    tx, ty = tx * factor, ty * factor
                traction[i, j, 0], traction[i, j, 1] = tx, ty
                disp[i, j, 0], disp[i, j, 1] = ux + data.drift[0], uy + data.drift[1]
                energy[i, j] = strain_energy(tx, ty, u0x, u0y, data.mask, h)
                contract[i, j] = contractility(tx, ty, data.mask, h, centre)
        return _FieldDraws(traction, disp, energy, contract, centre)


# ============================================================================== helpers


@dataclass(frozen=True, slots=True)
class _FieldDraws:
    traction: np.ndarray
    displacement: np.ndarray
    energy: np.ndarray
    contractility: np.ndarray
    centre: tuple[float, float]


@dataclass(slots=True)
class _Data:
    """The displacement raster as the model needs it (fit order, centred, in modes)."""

    field: xr.DataArray  # displacement(component, row, col), components in COMPONENTS order
    mask: np.ndarray  # (row, col) bool
    has_mask: bool
    grid: FourierGrid
    ul: np.ndarray
    ut: np.ndarray
    drift: np.ndarray  # (2,) per-component mean (the k = 0 mode)
    centred: tuple[np.ndarray, np.ndarray]

    @property
    def mask_da(self) -> xr.DataArray:
        return xr.DataArray(
            self.mask,
            dims=(ROW_DIM, COL_DIM),
            coords={d: self.field[d].values for d in (ROW_DIM, COL_DIM)},
        )

    @classmethod
    def build(cls, field: xr.DataArray, mask: np.ndarray | None, h: float) -> _Data:
        labels = [str(v) for v in np.asarray(field[COMPONENT_DIM].values).tolist()]
        if sorted(labels) != sorted(COMPONENTS):
            raise MapiError(
                f"dim {COMPONENT_DIM!r} must hold the levels {list(COMPONENTS)}, got {labels}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data",
            )
        field = field.sel({COMPONENT_DIM: list(COMPONENTS)}).transpose(*FIELD_DIMS)
        values = np.asarray(field.values, dtype=np.float64)
        if not np.isfinite(values).all():
            raise MapiError(
                "the displacement field has missing or non-finite values; the Fourier model "
                "needs the full raster (do not use a cell-level split)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data",
            )
        ny, nx = values.shape[1:]
        if ny < 4 or nx < 4:
            raise MapiError(
                f"the raster is {ny} x {nx}; the model needs at least 4 x 4 pixels",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data",
            )
        has_mask = mask is not None
        m = np.ones((ny, nx), dtype=bool) if mask is None else np.asarray(mask, dtype=bool)
        if not m.any():
            raise MapiError(
                f"{FORCE_MASK!r} selects no pixel", code=ErrorCode.SCHEMA_INVALID, pointer="/data"
            )
        g = FourierGrid.of(ny, nx, h)
        drift = values.mean(axis=(1, 2))
        cx, cy = values[0] - drift[0], values[1] - drift[1]
        ul, ut = to_modes(cx, cy, g)
        field = field.copy(data=values)
        return cls(field, m, has_mask, g, ul, ut, drift, (cx, cy))

    @classmethod
    def from_dataset(cls, ds: xr.Dataset, s: TractionForceSettings) -> _Data:
        for d in FIELD_DIMS:
            if d not in ds.dims:
                raise MapiError(f"the data has no dim {d!r}", code=ErrorCode.SCHEMA_INVALID)
        if TARGET not in ds:
            raise MapiError(f"the data has no variable {TARGET!r}", code=ErrorCode.SCHEMA_INVALID)
        field = ds[TARGET].sortby([ROW_DIM, COL_DIM])
        mask = None
        if s.force_mask and FORCE_MASK in ds:
            mask = ds[FORCE_MASK].sortby([ROW_DIM, COL_DIM]).transpose(ROW_DIM, COL_DIM).values
        return cls.build(field, mask, s.grid_um)

    @classmethod
    def from_fitted(cls, fitted: Fitted, s: TractionForceSettings) -> _Data:
        obs = fitted.groups.get("observed_data")
        if obs is None or TARGET not in obs:
            raise MapiError(
                "the fitted model has no observed displacement field; refit the model",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        const = fitted.groups.get("constant_data")
        has_mask = bool(fitted.learned.get("has_force_mask"))
        mask = None
        if has_mask and const is not None and FORCE_MASK in const:
            mask = np.asarray(const[FORCE_MASK].transpose(ROW_DIM, COL_DIM).values, dtype=bool)
        return cls.build(obs[TARGET], mask, s.grid_um)


def _conditional(
    u: np.ndarray, gk: np.ndarray, s2: np.ndarray, sig2: float, z: np.ndarray
) -> np.ndarray:
    """One exact draw of the traction modes given displacement modes ``u``."""
    den = gk**2 * s2 + sig2
    return gk * s2 / den * u + np.sqrt(s2 * sig2 / den) * z


def _spectrum(k: np.ndarray, prior: PriorKind, hyper: dict[str, float]) -> np.ndarray:
    return np.asarray(
        prior_spectrum(
            k, prior, hyper["traction_sd"], hyper.get("lengthscale"), hyper.get("smoothness")
        )
    )


def _hyper_means(post: xr.Dataset) -> dict[str, float]:
    return {n: float(post[n].mean()) for n in HYPERPARAMETERS if n in post}


def _field_draw_index(post: xr.Dataset, n: int) -> tuple[np.ndarray, np.ndarray]:
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
