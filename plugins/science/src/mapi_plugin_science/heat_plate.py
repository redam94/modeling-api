"""``sci.heat_plate``: calibrate a heated plate with the 2-D steady heat equation (E51).

Canonical inputs (bind dataset names onto them in the spec)::

    dims  run: categorical, policy new_levels (pooled) - one heater layout per run
          y, x: float, policy fixed - cell centres in cm (square cells, equal steps)
    vars  heater_power(run, y, x): covariate, W - electrical power put into each cell
          temperature(run, y, x): target, degC - thermocouple readings, NaN where there is
                                  no thermocouple (missing: allow)
          insulation(y, x): bool, True under back-face insulation (setting use_insulation)

The model (pymc-challenges E51, section 2). A thin plate of thickness ``delta`` conducts heat
in its plane (conductivity ``k``) and loses heat through its two faces (``h``) and its outer
edges (``h_edge``); a fraction ``eta`` of the heater power reaches the plate. At steady state
the rise ``u = T - T_room`` of each finite-volume cell solves the linear system::

    A(theta) u = eta q,
    A(theta) = -k delta L + diag(h (1 + m_back) dx^2 + h_edge delta dx n_edge)
    temperature[run, cell] ~ Normal(T_room + u[run, cell], sigma)

``L`` is the graph Laplacian of the grid (four neighbours, no flux across the outer edge in
``L``; the edge loss is the ``n_edge`` term), ``n_edge`` counts the outer edges of each cell
and ``m_back`` is 1 on the bare back face and ``insulated_back_loss`` under insulation. The
system is solved inside the model with ``pytensor.tensor.linalg.solve`` (gradients included),
once per run. Priors (the notebook's)::

    k ~ LogNormal(log 50, 0.5)   h ~ LogNormal(log 10, 0.5)   h_edge ~ LogNormal(log 10, 0.7)
    eta ~ Beta(40, 4)            sigma ~ HalfNormal(0.5)

**The field is linear in heater powers** (superposition). So the same posterior predicts the
field of any new heater layout: score a payload with new ``run`` labels and their
``heater_power`` (and ``insulation``). ``predict`` gives the expected field; ``simulate``
gives simulated thermocouple readings in every cell (the field plus sensor noise).

**Sparse targets.** The fit likelihood runs over the finite readings only (dim ``reading``,
in grid order), so the pointwise log-likelihood (``keep: {log_likelihood: true}``) has one
value per thermocouple reading; ``observed_data/temperature`` keeps the grid with NaN where
there is no thermocouple, so image recipes can lay the readings over the field.

``spread_length = sqrt(k delta / 2 h)`` (cm) is how far heat spreads from a heater before
the faces lose it (12.9 cm for the notebook plate).
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any, ClassVar

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import xarray as xr
from mapi_pymc.context import BuildContext
from mapi_pymc.model import PyMCModel
from mapi_sdk.models import FitContext, FitOutput, Fitted, ModelSettings, ScoreContext
from mapi_spec.capabilities import Capabilities, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field

__all__ = [
    "FIELD",
    "INSULATION",
    "OBS_DIM",
    "POWER",
    "RUN_DIM",
    "TARGET",
    "X_DIM",
    "Y_DIM",
    "HeatPlateModel",
    "HeatPlateSettings",
    "grid_laplacian",
    "heat_operator",
    "spread_length_cm",
    "steady_rise",
]

RUN_DIM = "run"
Y_DIM = "y"
X_DIM = "x"
POWER = "heater_power"
TARGET = "temperature"
INSULATION = "insulation"
FIELD = "field"
OBS_DIM = "reading"
"""Dim of the fit likelihood: the finite thermocouple readings, in (run, y, x) grid order."""

_PARAMS = ("k", "h", "h_edge", "eta")
_STEP_RTOL = 1e-6


class HeatPlateSettings(ModelSettings):
    """Settings of ``sci.heat_plate`` (k in W/m/K, h and h_edge in W/m^2/K)."""

    thickness_mm: float = Field(
        5.0, gt=0, le=1000, description="Thickness of the plate in mm (x-ui-level: basic)"
    )
    room_temperature: float = Field(
        20.0,
        ge=-100,
        le=1000,
        description="Temperature of the room in degC: the plate without heat (x-ui-level: basic)",
    )
    use_insulation: bool = Field(
        False,
        description="The data has a bool insulation(y, x): True where the back face is "
        "insulated (x-ui-level: basic)",
    )
    insulated_back_loss: float = Field(
        0.1,
        ge=0,
        le=1,
        description="Back-face loss under insulation as a fraction of the bare loss",
    )
    k_median: float = Field(50.0, gt=0, description="Prior median of the conductivity k")
    k_log_sd: float = Field(0.5, gt=0, description="Log-scale sd of the prior of k")
    h_median: float = Field(10.0, gt=0, description="Prior median of the face coefficient h")
    h_log_sd: float = Field(0.5, gt=0, description="Log-scale sd of the prior of h")
    h_edge_median: float = Field(10.0, gt=0, description="Prior median of the edge coefficient")
    h_edge_log_sd: float = Field(0.7, gt=0, description="Log-scale sd of the prior of h_edge")
    eta_alpha: float = Field(40.0, gt=0, description="Beta prior of the heater efficiency: alpha")
    eta_beta: float = Field(4.0, gt=0, description="Beta prior of the heater efficiency: beta")
    sigma_scale: float = Field(
        0.5, gt=0, description="Half-normal scale of the thermocouple noise sd (degC)"
    )


_TEMPLATE: dict[str, Any] = {
    "params": {"use_insulation": {"type": "boolean", "default": False}},
    "template": {
        "dims": {
            RUN_DIM: {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "pooled",
                "description": "One run of the plate with one heater layout (score new runs "
                "to predict new layouts)",
            },
            Y_DIM: {
                "kind": "float",
                "policy": "fixed",
                "description": "Centre of each row of cells (cm, equal steps)",
            },
            X_DIM: {
                "kind": "float",
                "policy": "fixed",
                "description": "Centre of each column of cells (cm, the same step as y)",
            },
        },
        "vars": {
            POWER: {
                "dims": [RUN_DIM, Y_DIM, X_DIM],
                "dtype": "float64",
                "role": "covariate",
                "units": "W",
                "bounds": [0, None],
                "description": "Electrical power put into each cell by the heaters",
            },
            TARGET: {
                "dims": [RUN_DIM, Y_DIM, X_DIM],
                "dtype": "float64",
                "role": "target",
                "units": "degC",
                "missing": "allow",
                "description": "Thermocouple reading of each cell (NaN where there is none)",
            },
        },
    },
    "cases": [
        {
            "when": {"use_insulation": True},
            "merge": {
                "vars": {
                    INSULATION: {
                        "dims": [Y_DIM, X_DIM],
                        "dtype": "bool",
                        "role": "covariate",
                        "description": "True where the back face of the cell is insulated",
                    }
                }
            },
        }
    ],
}


# ============================================================================== numerics


def grid_laplacian(ny: int, nx: int) -> tuple[np.ndarray, np.ndarray]:
    """``(L, n_edge)`` of an ``ny x nx`` grid of cells, cell index ``j * nx + i``
    (row-major over ``(y, x)``): the graph Laplacian with four neighbours and the number of
    outer edges of each cell (the E51 ``LAP`` and ``N_EDGE``)."""
    n = ny * nx
    lap = np.zeros((n, n))
    n_edge = np.zeros(n)
    for j in range(ny):
        for i in range(nx):
            a = j * nx + i
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if 0 <= i + di < nx and 0 <= j + dj < ny:
                    lap[a, a] -= 1
                    lap[a, (j + dj) * nx + i + di] += 1
                else:
                    n_edge[a] += 1
    return lap, n_edge


def heat_operator(
    k: float,
    h: float,
    h_edge: float,
    *,
    lap: np.ndarray,
    n_edge: np.ndarray,
    dx_m: float,
    thickness_m: float,
    back: np.ndarray | None = None,
) -> np.ndarray:
    """``A(theta)`` of the finite-volume heat balance (NumPy twin of :meth:`HeatPlateModel.
    build`); ``back`` is ``m_back`` per cell (1 bare, the insulated fraction under pads)."""
    m_back = np.ones(lap.shape[0]) if back is None else np.asarray(back, dtype=float)
    diag = h * (1.0 + m_back) * dx_m**2 + h_edge * thickness_m * dx_m * n_edge
    return np.asarray(-k * thickness_m * lap + np.diag(diag))


def steady_rise(
    power: np.ndarray,
    *,
    k: float,
    h: float,
    h_edge: float,
    eta: float,
    dx_m: float,
    thickness_m: float,
    back: np.ndarray | None = None,
) -> np.ndarray:
    """Steady temperature rise above the room (K) of ``power`` (W) on ``(..., ny, nx)``."""
    power = np.asarray(power, dtype=float)
    ny, nx = power.shape[-2:]
    lap, n_edge = grid_laplacian(ny, nx)
    a = heat_operator(
        k, h, h_edge, lap=lap, n_edge=n_edge, dx_m=dx_m, thickness_m=thickness_m,
        back=None if back is None else np.asarray(back, dtype=float).reshape(-1),
    )  # fmt: skip
    q = power.reshape(-1, ny * nx).T
    u = np.linalg.solve(a, eta * q).T
    return u.reshape(power.shape)


def spread_length_cm(k: Any, h: Any, thickness_m: float) -> Any:
    """``sqrt(k delta / 2 h)`` in cm: how far heat spreads before the faces lose it."""
    return 100.0 * np.sqrt(np.asarray(k) * thickness_m / (2.0 * np.asarray(h)))


# ================================================================================= model


class HeatPlateModel(PyMCModel[HeatPlateSettings]):
    """Steady heat equation on a plate, solved inside PyMC (pymc-challenges E51)."""

    id = "sci.heat_plate"
    version = "1.0.0"
    Settings = HeatPlateSettings
    description = (
        "Thermocouple readings of a heated plate as the 2-D steady heat equation: conduction "
        "k, face loss h, edge loss h_edge and heater efficiency eta, with a finite-volume "
        "linear system solved inside the model for each run. Outputs are the temperature "
        "field of each run (or of any new heater layout) and the heat-spreading length "
        "sqrt(k delta / 2h)."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "simulate", "evaluate"}),
        deterministic_given_seed=True,
        checkpointable=True,
        identified_quantities=["k", "h", "eta", "spread_length"],
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        FIELD: {
            "dims": [RUN_DIM, Y_DIM, X_DIM],
            "units_of": TARGET,
            "description": "Temperature of each cell (degC): the steady field, or a simulated "
            "thermocouple reading",
            "grid": {
                "y": Y_DIM,
                "x": X_DIM,
                "frame": RUN_DIM,
                "origin": "lower",
                "colormap": "magma",
            },
        },
        "spread_length": {
            "dims": [],
            "units": "cm",
            "description": "Heat-spreading length sqrt(k delta / 2h)",
        },
    }
    expected: ClassVar[dict[str, str]] = {FIELD: FIELD, "spread_length": "spread_length"}
    predictive: ClassVar[dict[str, str]] = {FIELD: TARGET}
    plots = (
        "core.convergence",
        "core.posterior_forest",
        "core.pair",
        "sci.field_map",
        "sci.image_residuals",
    )
    default_engine = "pymc"
    prior_overlap_vars = ("k", "h", "h_edge", "eta")

    # ------------------------------------------------------------------ geometry
    @staticmethod
    def geometry(ctx: BuildContext) -> tuple[float, int, int]:
        """``(dx in m, ny, nx)``: ``y`` and ``x`` must be equal steps of the same size."""
        steps: dict[str, float] = {}
        for dim in (Y_DIM, X_DIM):
            v = np.asarray(ctx.labels(dim), dtype=float)
            if v.size < 2:
                raise MapiError(
                    f"{dim!r} needs at least 2 cells", code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{dim}",
                )  # fmt: skip
            d = np.diff(v)
            step = float(d.mean())
            if step <= 0 or not np.allclose(d, step, rtol=_STEP_RTOL, atol=0.0):
                raise MapiError(
                    f"the positions of {dim!r} must increase in equal steps (a regular grid)",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{dim}",
                )
            steps[dim] = step
        if not math.isclose(steps[Y_DIM], steps[X_DIM], rel_tol=1e-4):
            raise MapiError(
                f"the cells must be square: the step of y is {steps[Y_DIM]:g} cm and the step "
                f"of x is {steps[X_DIM]:g} cm",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{Y_DIM}",
            )
        ny, nx = ctx.labels(Y_DIM).size, ctx.labels(X_DIM).size
        return steps[X_DIM] / 100.0, int(ny), int(nx)

    @staticmethod
    def back_factor(ds: xr.Dataset, s: HeatPlateSettings, ctx: BuildContext) -> Any:
        """``m_back`` per cell (flat, grid order): a tensor over ``insulation`` or ones."""
        if not s.use_insulation:
            return None
        if INSULATION not in ds:
            raise MapiError(
                f"use_insulation is set but the data has no {INSULATION!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{INSULATION}",
            )
        ins = ctx.data(INSULATION, dims=[Y_DIM, X_DIM])
        return 1.0 - (1.0 - s.insulated_back_loss) * ins.reshape((-1,))

    # ------------------------------------------------------------------ model
    def build(self, ds: xr.Dataset, s: HeatPlateSettings, ctx: BuildContext) -> pm.Model:
        dx, ny, nx = self.geometry(ctx)
        n = ny * nx
        n_run = int(ctx.labels(RUN_DIM).size)
        delta = s.thickness_mm / 1000.0
        lap, n_edge = grid_laplacian(ny, nx)
        with pm.Model(coords=ctx.coords()) as model:
            q = ctx.data(POWER, dims=[RUN_DIM, Y_DIM, X_DIM])
            back = self.back_factor(ds, s, ctx)
            k = pm.LogNormal("k", math.log(s.k_median), s.k_log_sd)
            h = pm.LogNormal("h", math.log(s.h_median), s.h_log_sd)
            h_edge = pm.LogNormal("h_edge", math.log(s.h_edge_median), s.h_edge_log_sd)
            eta = pm.Beta("eta", s.eta_alpha, s.eta_beta)
            sigma = pm.HalfNormal("sigma", s.sigma_scale)
            pm.Deterministic("spread_length", 100.0 * pt.sqrt(k * delta / (2.0 * h)))
            face = 2.0 * h if back is None else h * (1.0 + back)
            a = -k * delta * pt.as_tensor(lap) + pt.diag(
                face * dx**2 + h_edge * delta * dx * pt.as_tensor(n_edge)
            )
            u = pt.linalg.solve(a, eta * q.reshape((n_run, n)).T).T
            field = pm.Deterministic(
                FIELD,
                s.room_temperature + u.reshape((n_run, ny, nx)),
                dims=(RUN_DIM, Y_DIM, X_DIM),
            )
            y = ctx.observed(TARGET, dims=[RUN_DIM, Y_DIM, X_DIM])
            if ctx.is_score:
                pm.Normal(TARGET, field, sigma, observed=y, dims=(RUN_DIM, Y_DIM, X_DIM))
            else:
                flat = y.reshape(-1)
                idx = np.flatnonzero(np.isfinite(flat))
                if idx.size == 0:
                    raise MapiError(
                        f"the fit data has no finite {TARGET!r} reading",
                        code=ErrorCode.SCHEMA_INVALID,
                        pointer=f"/data/{TARGET}",
                    )
                model.add_coord(OBS_DIM, np.arange(idx.size))
                pm.Normal(
                    TARGET, field.reshape((-1,))[idx], sigma, observed=flat[idx], dims=OBS_DIM
                )
        return model

    # ------------------------------------------------------------------ fit / score
    def fit(
        self, ds: xr.Dataset, s: HeatPlateSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        out = super().fit(ds, s, cfg, ctx)
        post = out.groups["posterior"]
        flat = {p: np.asarray(post[p].values, dtype=float).ravel() for p in _PARAMS}
        corr: dict[str, Any] = {}
        for a, b in (("k", "h"), ("k", "eta"), ("h", "eta")):
            c = float(np.corrcoef(flat[a], flat[b])[0, 1]) if flat[a].size > 1 else float("nan")
            corr[f"{a}-{b}"] = round(c, 4) if math.isfinite(c) else 0.0
        target = np.asarray(ds[TARGET].values, dtype=float)
        out.learned["posterior_correlations"] = corr
        out.learned["n_readings"] = int(np.isfinite(target).sum())
        out.learned["cell_cm"] = float(np.diff(np.asarray(ds[X_DIM].values, dtype=float)).mean())
        out.learned["grid"] = [int(ds.sizes[Y_DIM]), int(ds.sizes[X_DIM])]
        return out

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        if ctx.mode == "simulate" and ctx.kind != "predictive":
            # simulate = new thermocouple readings in every cell (the field plus noise)
            ctx = dataclasses.replace(ctx, kind="predictive")
        return super().score(fitted, ds, req, ctx)
