"""``sci.network_latent_space``: who works with whom, a Bernoulli model of the dyads of a
graph (E21, the Lazega law firm).

Canonical inputs (bind dataset names onto them in the spec)::

    dims  node:  integer or categorical, policy fixed           (the actors)
          node_: the same labels as node, policy fixed          (the second end of a pair)
          cov:   categorical, policy fixed                      (settings.covariates only)
    vars  tie(node, node_):          target, float64, 0/1, NaN = not observed
          dyad_cov(node, node_, cov): covariates of each pair  (settings.covariates only)
          node_group(node):           int64 group code, kept for plots (settings.node_groups)

The model, over the pairs ``i < j`` whose two cells ``tie[i, j]`` and ``tie[j, i]`` are both
observed (a **dyad** is held out when either cell is missing)::

    tie[i, j] ~ Bernoulli(logit^-1(eta_ij))
    eta_ij = alpha + x_ij . beta                 (covariates; kind covariates)
             + s_i + s_j                         (+ sociality; kind sociality)
             - ||z_i - z_j||                     (+ latent space; kind latent_space)

with ``alpha ~ N(-2, 2)``, ``beta ~ N(0, 1)``, ``s = sigma_s s_raw``, ``sigma_s ~
HalfNormal(1)``, ``z = sigma_z z_raw`` (``z_raw ~ N(0, 1)`` in ``latent_dims`` dimensions),
``sigma_z ~ HalfNormal(2)`` - the priors of the notebook. ``covariates: false`` drops the
``x_ij . beta`` term (with kind covariates that is the Erdos-Renyi graph).

**Positions.** The distances do not change when the configuration is rotated, reflected or
translated, so the raw ``z_raw`` draws do not converge (r_hat far above 1) although every
distance does. After the fit, every draw is centred and rotated onto a common reference by
generalised Procrustes analysis (:func:`procrustes`). The aligned draws are the posterior
variable ``position(node, latent_dim)``; the reference configuration of the last pass is
kept as ``constant_data/position_ref``. The convergence diagnostics of the fit look at the
identified quantities (``position``, not ``z_raw``).

**Log-likelihood.** The observed variable is ``tie`` over the dim ``dyad`` (one entry per
observed pair, ``i < j``, row-major), so ``elpd_loo`` is leave-one-dyad-out. The pairs are
kept as ``constant_data/dyad_i`` and ``dyad_j`` (positions of the two nodes). Every kind on
the same data uses the same dyads in the same order, so a workflow can compare them.

**Scores** are computed in NumPy from the posterior (the twin of the PyMC graph):
``tie_prob`` and the expected ``tie`` are the tie probabilities of every pair of the score
data, ``kind: predictive`` and ``mode: simulate`` draw whole symmetric graphs (common random
numbers on the pair, so a subset of nodes gets the same ties), ``degree`` sums a row and
``graph_stat`` gives whole-graph statistics of every simulated graph (posterior predictive
checks: transitivity, shared partners, geodesics).
"""

from __future__ import annotations

import hashlib
from typing import Any, ClassVar, Literal

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import xarray as xr
from mapi_pymc.context import BuildContext
from mapi_pymc.diagnostics import convergence_diagnostics
from mapi_pymc.model import PyMCModel
from mapi_sdk.models import PREDICTIONS, FitContext, FitOutput, Fitted, ModelSettings, ScoreContext
from mapi_spec.capabilities import Capabilities, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field
from scipy.sparse.csgraph import shortest_path
from scipy.special import expit

__all__ = [
    "ADJACENCY",
    "COVARIATES",
    "COV_DIM",
    "DYAD_DIM",
    "DYAD_I",
    "DYAD_J",
    "GRAPH_STATS",
    "GROUP",
    "KINDS",
    "LATENT_DIM",
    "NODE_DIM",
    "PAIR_DIM",
    "POSITION_REF",
    "STAT_DIM",
    "TARGET",
    "NetworkLatentSpaceModel",
    "NetworkLatentSpaceSettings",
    "align_to",
    "dyad_eta",
    "dyads",
    "graph_stats",
    "pair_probabilities",
    "procrustes",
    "simulate_graph",
]

NODE_DIM = "node"
PAIR_DIM = "node_"
COV_DIM = "cov"
TARGET = "tie"
COVARIATES = "dyad_cov"
GROUP = "node_group"
DYAD_DIM = "dyad"
"""Dim of the likelihood: the observed pairs ``i < j``."""
Z_DIM = "z_axis"
"""Dim of the raw (not identified) latent coordinates ``z_raw``."""
LATENT_DIM = "latent_dim"
"""Dim of the Procrustes-aligned ``position``."""
STAT_DIM = "statistic"
DYAD_I = "dyad_i"
DYAD_J = "dyad_j"
ADJACENCY = "adjacency"
"""Constant data: the observed adjacency ``(node, node_)`` of the fit (NaN = held out)."""
POSITION_REF = "position_ref"
"""Constant data: the Procrustes reference configuration ``(node, latent_dim)``."""

KINDS = ("covariates", "sociality", "latent_space")
GRAPH_STATS = (
    "density",
    "sd_degree",
    "max_degree",
    "isolates",
    "triangles",
    "transitivity",
    "esp0",
    "mean_geodesic",
    "unreachable",
)
"""Statistics of ``graph_stat`` (``esp0``: ties whose two nodes share no neighbour)."""

_EPS = 1e-6
"""Added under the square root of the latent distance (as in the notebook)."""


class NetworkLatentSpaceSettings(ModelSettings):
    """Settings of ``sci.network_latent_space`` (all effects on the log-odds scale)."""

    kind: Literal["covariates", "sociality", "latent_space"] = Field(
        "latent_space",
        description="Model of the log odds: covariates only, + a sociality effect per node, "
        "+ latent positions (x-ui-level: basic)",
    )
    covariates: bool = Field(
        True,
        description="Use the pair covariates dyad_cov(node, node_, cov); false leaves them "
        "out (x-ui-level: basic)",
    )
    node_groups: bool = Field(
        False, description="The data has node_group(node), a group code kept for the plots"
    )
    latent_dims: int = Field(2, ge=1, le=3, description="Dimensions of the latent space")
    alpha_mu: float = Field(-2.0, description="Prior mean of the intercept alpha")
    alpha_sigma: float = Field(2.0, gt=0, description="Prior sd of the intercept alpha")
    beta_sigma: float = Field(1.0, gt=0, description="Prior sd of each covariate effect")
    sociality_scale: float = Field(
        1.0, gt=0, description="Scale of the HalfNormal prior of sigma_s"
    )
    latent_scale: float = Field(2.0, gt=0, description="Scale of the HalfNormal prior of sigma_z")
    procrustes_iters: int = Field(
        10, ge=1, le=100, description="Passes of generalised Procrustes alignment"
    )


_TEMPLATE: dict[str, Any] = {
    "params": {
        "covariates": {"type": "boolean", "default": True},
        "node_groups": {"type": "boolean", "default": False},
    },
    "template": {
        "dims": {
            NODE_DIM: {"kind": "integer", "policy": "fixed", "description": "The nodes (actors)"},
            PAIR_DIM: {
                "kind": "integer",
                "policy": "fixed",
                "description": "The second node of a pair (the same labels as node)",
            },
        },
        "vars": {
            TARGET: {
                "dims": [NODE_DIM, PAIR_DIM],
                "dtype": "float64",
                "role": "target",
                "missing": "allow",
                "bounds": [0, 1],
                "description": "1 when the two nodes have a tie, 0 when not, missing when not "
                "observed (symmetric)",
            }
        },
    },
    "cases": [
        {
            "when": {"covariates": True},
            "merge": {
                "dims": {
                    COV_DIM: {
                        "kind": "categorical",
                        "policy": "fixed",
                        "description": "The covariates of a pair",
                    }
                },
                "vars": {
                    COVARIATES: {
                        "dims": [NODE_DIM, PAIR_DIM, COV_DIM],
                        "dtype": "float64",
                        "description": "Covariates of each pair, for example 1 when the two "
                        "nodes are in the same group",
                    }
                },
            },
        },
        {
            "when": {"node_groups": True},
            "merge": {
                "vars": {
                    GROUP: {
                        "dims": [NODE_DIM],
                        "dtype": "int64",
                        "role": "constant",
                        "description": "A group code of each node, used for colours in plots",
                    }
                }
            },
        },
    ],
}


# ============================================================================== NumPy twins


def dyads(adjacency: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(I, J, y)``: the pairs ``i < j`` whose two cells are both observed (row-major), and
    their ties. Raises ``SCHEMA_INVALID`` for a value that is not 0/1 or an asymmetric pair."""
    a = np.asarray(adjacency, dtype=float)
    n = a.shape[0]
    i, j = np.triu_indices(n, 1)
    up, lo = a[i, j], a[j, i]
    seen = np.isfinite(up) & np.isfinite(lo)
    vals = a[np.isfinite(a)]
    if not np.isin(vals, (0.0, 1.0)).all():
        raise MapiError(
            f"{TARGET!r} must be 0 or 1 (or missing)",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{TARGET}",
        )
    if (up[seen] != lo[seen]).any():
        bad = int((up[seen] != lo[seen]).sum())
        raise MapiError(
            f"{TARGET!r} must be symmetric (an undirected graph): {bad} pair(s) have "
            "tie[i, j] != tie[j, i]",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{TARGET}",
        )
    return i[seen], j[seen], up[seen]


def dyad_eta(
    alpha: np.ndarray,
    i: np.ndarray,
    j: np.ndarray,
    *,
    x: np.ndarray | None = None,
    beta: np.ndarray | None = None,
    sociality: np.ndarray | None = None,
    z: np.ndarray | None = None,
) -> np.ndarray:
    """Log odds ``(samples, pairs)`` of the pairs ``(i, j)`` (the notebook's ``eta_logit``).

    ``alpha (S,)``, ``x (pairs, K)``, ``beta (S, K)``, ``sociality (S, n)``, ``z (S, n, d)``.
    """
    eta = np.asarray(alpha, dtype=float)[:, None] + np.zeros(len(i))
    if x is not None and beta is not None:
        eta = eta + np.asarray(beta) @ np.asarray(x, dtype=float).T
    if sociality is not None:
        eta = eta + sociality[:, i] + sociality[:, j]
    if z is not None:
        eta = eta - np.sqrt(((z[:, i] - z[:, j]) ** 2).sum(-1) + _EPS)
    return eta


def pair_probabilities(
    alpha: np.ndarray,
    ia: np.ndarray,
    ib: np.ndarray,
    *,
    x: np.ndarray | None = None,
    beta: np.ndarray | None = None,
    sociality: np.ndarray | None = None,
    z: np.ndarray | None = None,
    dtype: Any = np.float64,
    check: Any = None,
) -> np.ndarray:
    """Tie probabilities ``(samples, len(ia), len(ib))`` of every pair of the rows ``ia``
    and the columns ``ib`` (node positions), from :func:`dyad_eta` in blocks of draws.
    ``x`` is ``(len(ia), len(ib), K)``; the diagonal is not set to 0 here."""
    na, nb = len(ia), len(ib)
    ii, jj = np.meshgrid(ia, ib, indexing="ij")
    n_s = len(alpha)
    prob = np.empty((n_s, na, nb), dtype=dtype)
    xf = None if x is None else np.asarray(x, dtype=float).reshape(na * nb, -1)
    block = max(1, 2_000_000 // max(1, na * nb))
    for a in range(0, n_s, block):
        sl = slice(a, a + block)
        eta = dyad_eta(
            alpha[sl],
            ii.ravel(),
            jj.ravel(),
            x=xf,
            beta=None if beta is None else beta[sl],
            sociality=None if sociality is None else sociality[sl],
            z=None if z is None else z[sl],
        )
        prob[sl] = expit(eta).reshape(-1, na, nb)
        if check is not None:
            check()
    return prob


def align_to(z: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """Centre each draw ``z (samples, nodes, d)`` and rotate or reflect it onto ``ref``
    (``R = U V^T`` from the SVD of ``Z^T ref``)."""
    zc = z - z.mean(1, keepdims=True)
    u, _, vt = np.linalg.svd(np.einsum("snd,ne->sde", zc, ref))
    return np.asarray(np.einsum("snd,sde->sne", zc, u @ vt))


def procrustes(z: np.ndarray, iters: int = 10) -> tuple[np.ndarray, np.ndarray]:
    """Generalised Procrustes alignment of ``z (samples, nodes, d)`` (the notebook's
    ``procrustes``): start from the first draw, align, use the mean as the next reference.
    Returns ``(aligned draws, the reference of the last pass)``; ``align_to(z, ref)`` gives
    the same aligned draws again."""
    zc = z - z.mean(1, keepdims=True)
    ref = zc[0]
    aligned = zc
    for k in range(iters):
        aligned = align_to(zc, ref)
        if k < iters - 1:
            ref = aligned.mean(0)
    return aligned, np.asarray(ref)


def simulate_graph(p: np.ndarray, u: np.ndarray) -> np.ndarray:
    """A symmetric 0/1 graph from pair probabilities ``p (n, n)`` and uniforms ``u (n, n)``
    (only the upper triangle of each is read; the diagonal is 0)."""
    n = p.shape[0]
    i, j = np.triu_indices(n, 1)
    m = np.zeros((n, n))
    m[i, j] = u[i, j] < p[i, j]
    return m + m.T


def graph_stats(m: np.ndarray) -> np.ndarray:
    """:data:`GRAPH_STATS` of a symmetric 0/1 adjacency matrix (the notebook's
    ``graph_stats`` and the 0 bin of ``esp_hist``)."""
    m = np.asarray(m, dtype=float)
    n = m.shape[0]
    i, j = np.triu_indices(n, 1)
    d = m.sum(1)
    m2 = m @ m
    tri = float(np.trace(m2 @ m) / 6)
    two_paths = float((d * (d - 1) / 2).sum())
    geo = shortest_path(m, unweighted=True, directed=False)[i, j]
    finite = np.isfinite(geo)
    ties = m[i, j] == 1
    return np.array(
        [
            float(m[i, j].mean()) if i.size else np.nan,
            float(d.std()),
            float(d.max()) if n else np.nan,
            float((d == 0).sum()),
            tri,
            3 * tri / two_paths if two_paths else np.nan,
            float((m2[i, j][ties] == 0).sum()),
            float(geo[finite].mean()) if finite.any() else np.nan,
            float((~finite).mean()) if i.size else np.nan,
        ]
    )


def _uniforms(seed: int, n: int, size: int, offset: int) -> np.ndarray:
    """Frozen uniforms ``(size, n, n)`` for draws ``offset .. offset + size`` (seeded by the
    score seed and the draw, so a block of draws gets the same numbers in any call)."""
    digest = hashlib.sha256(f"{TARGET}:{n}".encode()).digest()
    key = int.from_bytes(digest[:8], "little")
    out = np.empty((size, n, n))
    for k in range(size):
        out[k] = np.random.default_rng([int(seed), key, offset + k]).uniform(size=(n, n))
    return out


# ============================================================================== the model


class NetworkLatentSpaceModel(PyMCModel[NetworkLatentSpaceSettings]):
    """Ties between the nodes of an undirected graph: covariates, sociality and a latent
    space, with Procrustes-aligned positions (E21)."""

    id = "sci.network_latent_space"
    version = "1.0.0"
    Settings = NetworkLatentSpaceSettings
    description = (
        "An undirected graph (who works with whom) as one Bernoulli tie for each pair of "
        "nodes. The log odds add pair covariates (homophily), a sociality effect per node "
        "and, for kind latent_space, minus the distance between latent positions. Outputs "
        "are the tie probability of every pair, the positions (aligned by Procrustes), the "
        "sociality, simulated graphs and their statistics."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "simulate", "evaluate"}),
        deterministic_given_seed=True,
        checkpointable=True,
        identified_quantities=["alpha", "beta", "sigma_s", "sigma_z", "position", "tie_prob"],
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "tie_prob": {
            "dims": [NODE_DIM, PAIR_DIM],
            "units": "probability",
            "description": "Probability of a tie between the two nodes (0 on the diagonal)",
            "grid": {"y": NODE_DIM, "x": PAIR_DIM, "origin": "upper", "colormap": "viridis"},
        },
        "tie": {
            "dims": [NODE_DIM, PAIR_DIM],
            "units_of": TARGET,
            "description": "The tie: its probability, or a simulated graph (kind predictive, "
            "mode simulate)",
            "grid": {"y": NODE_DIM, "x": PAIR_DIM, "origin": "upper", "colormap": "gray"},
        },
        "degree": {
            "dims": [NODE_DIM],
            "units": "ties",
            "description": "Ties of each node: expected, or in a simulated graph",
        },
        "sociality": {
            "dims": [NODE_DIM],
            "units": "log odds",
            "description": "How much more often the node has ties with anyone (kind sociality "
            "or latent_space)",
        },
        "position": {
            "dims": [NODE_DIM, LATENT_DIM],
            "units": "latent units",
            "description": "Latent position, aligned by Procrustes (kind latent_space)",
        },
        "graph_stat": {
            "dims": [STAT_DIM],
            "units": "statistic",
            "description": "Statistics of each simulated graph (density, sd_degree, "
            "max_degree, isolates, triangles, transitivity, esp0, mean_geodesic, unreachable)",
        },
    }
    plots = (
        "core.convergence",
        "core.posterior_forest",
        "net.graph",
        "sci.field_map",
    )
    resources = ResourceHint(min_class="S", default_class="M", est_fit_seconds=60)
    default_engine = "pymc"
    prior_overlap_vars = ("alpha", "beta", "sigma_s", "sigma_z")

    # ------------------------------------------------------------------ data
    @staticmethod
    def nodes(ds: xr.Dataset) -> np.ndarray:
        """The node labels; ``node_`` must hold the same labels (any order)."""
        if NODE_DIM not in ds.dims:
            raise MapiError(
                f"the data has no dim {NODE_DIM!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{NODE_DIM}",
            )
        nodes = (
            np.asarray(ds[NODE_DIM].values)
            if NODE_DIM in ds.coords
            else np.arange(ds.sizes[NODE_DIM])
        )
        if PAIR_DIM in ds.dims:
            pair = (
                np.asarray(ds[PAIR_DIM].values)
                if PAIR_DIM in ds.coords
                else np.arange(ds.sizes[PAIR_DIM])
            )
            if len(pair) != len(nodes) or set(pair.tolist()) != set(nodes.tolist()):
                raise MapiError(
                    f"{PAIR_DIM!r} must have the same labels as {NODE_DIM!r} (a square "
                    "matrix of pairs)",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{PAIR_DIM}",
                )
        return nodes

    @staticmethod
    def _square(da: xr.DataArray, nodes: np.ndarray, *rest: str) -> np.ndarray:
        """``da`` as ``(node, node_, *rest)`` with both node dims in the order ``nodes``."""
        if NODE_DIM in da.coords and PAIR_DIM in da.coords:
            da = da.sel({NODE_DIM: nodes, PAIR_DIM: nodes})
        return np.asarray(da.transpose(NODE_DIM, PAIR_DIM, *rest).values, dtype=float)

    def adjacency(self, ds: xr.Dataset, nodes: np.ndarray) -> np.ndarray:
        if TARGET not in ds:
            raise MapiError(
                f"the fit data has no {TARGET!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TARGET}",
            )
        return self._square(ds[TARGET], nodes)

    def covariates(
        self, ds: xr.Dataset, nodes: np.ndarray, labels: np.ndarray | None = None
    ) -> tuple[np.ndarray, np.ndarray]:
        """``(x (node, node_, cov), cov labels)``; ``labels`` puts ``cov`` in the fit order."""
        if COVARIATES not in ds:
            raise MapiError(
                f"the data has no {COVARIATES!r} (set covariates: false to fit without them)",
                code=ErrorCode.SCHEMA_INVALID
                if labels is None
                else ErrorCode.MISSING_FUTURE_COVARIATE,
                pointer=f"/data/{COVARIATES}",
            )
        da = ds[COVARIATES]
        if labels is not None and COV_DIM in da.coords:
            have = {str(v) for v in da[COV_DIM].values}
            missing = [str(v) for v in labels if str(v) not in have]
            if missing:
                raise MapiError(
                    f"{COVARIATES!r} has no covariate(s) {missing} of the fit",
                    code=ErrorCode.MISSING_FUTURE_COVARIATE,
                    pointer=f"/data/{COVARIATES}",
                )
            da = da.sel({COV_DIM: list(labels)})
        cov = (
            np.asarray(da[COV_DIM].values) if COV_DIM in da.coords else np.arange(da.sizes[COV_DIM])
        )
        return self._square(da, nodes, COV_DIM), cov

    # ------------------------------------------------------------------ model
    def build(self, ds: xr.Dataset, s: NetworkLatentSpaceSettings, ctx: BuildContext) -> pm.Model:
        nodes = self.nodes(ds)
        n = len(nodes)
        if n < 3:
            raise MapiError(
                f"the graph needs at least 3 nodes (got {n})",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{NODE_DIM}",
            )
        if ctx.is_fit:
            i, j, y = dyads(self.adjacency(ds, nodes))
            if y.size == 0 or y.min() == y.max():
                raise MapiError(
                    f"the fit needs observed pairs with and without a tie in {TARGET!r} "
                    f"(got {y.size} pairs, {int(y.sum())} ties)",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{TARGET}",
                )
        else:
            i, j = np.triu_indices(n, 1)
            y = np.zeros(i.size)
        coords: dict[str, Any] = {
            NODE_DIM: nodes.tolist(),
            DYAD_DIM: np.arange(i.size).tolist(),
        }
        x = cov = None
        if s.covariates:
            xs, cov = self.covariates(ds, nodes)
            x = xs[i, j]
            if not np.isfinite(x).all():
                raise MapiError(
                    f"{COVARIATES!r} has missing values on the observed pairs",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{COVARIATES}",
                )
            coords[COV_DIM] = cov.tolist()
        latent = s.kind == "latent_space"
        if latent:
            coords[Z_DIM] = [f"z{k + 1}" for k in range(s.latent_dims)]
        with pm.Model(coords=coords) as model:
            di = ctx.data(DYAD_I, i.astype(np.int64), dims=[DYAD_DIM])
            dj = ctx.data(DYAD_J, j.astype(np.int64), dims=[DYAD_DIM])
            eta: Any = pm.Normal("alpha", s.alpha_mu, s.alpha_sigma)
            if x is not None:
                beta = pm.Normal("beta", 0.0, s.beta_sigma, dims=COV_DIM)
                eta = eta + pt.dot(pt.as_tensor_variable(x), beta)
            if s.kind in ("sociality", "latent_space"):
                sigma_s = pm.HalfNormal("sigma_s", s.sociality_scale)
                s_raw = pm.Normal("s_raw", 0.0, 1.0, dims=NODE_DIM)
                soc = pm.Deterministic("sociality", sigma_s * s_raw, dims=NODE_DIM)
                eta = eta + soc[di] + soc[dj]
            if latent:
                sigma_z = pm.HalfNormal("sigma_z", s.latent_scale)
                z = sigma_z * pm.Normal("z_raw", 0.0, 1.0, dims=(NODE_DIM, Z_DIM))
                eta = eta - pt.sqrt(((z[di] - z[dj]) ** 2).sum(-1) + _EPS)
            pm.Bernoulli(TARGET, logit_p=eta, observed=y, dims=DYAD_DIM)
        ctx.observed_vars[TARGET] = (DYAD_DIM,)
        ctx.observed_values[TARGET] = y
        return model

    # ------------------------------------------------------------------ fit
    def fit(
        self, ds: xr.Dataset, s: NetworkLatentSpaceSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        out = super().fit(ds, s, cfg, ctx)
        nodes = self.nodes(ds)
        groups = out.groups
        for name, grp in list(groups.items()):
            if DYAD_DIM in grp.dims and DYAD_DIM not in grp.coords:
                groups[name] = grp.assign_coords({DYAD_DIM: np.arange(grp.sizes[DYAD_DIM])})
        post = groups["posterior"]
        const = groups.get("constant_data", xr.Dataset())
        adj = self.adjacency(ds, nodes)
        const = const.assign(
            {ADJACENCY: xr.DataArray(adj, dims=(NODE_DIM, PAIR_DIM),
                                     coords={NODE_DIM: nodes, PAIR_DIM: nodes})}
        )  # fmt: skip
        if s.node_groups and GROUP in ds:
            grp_da = ds[GROUP]
            if NODE_DIM in grp_da.coords:
                grp_da = grp_da.sel({NODE_DIM: nodes})
            const = const.assign(
                {GROUP: xr.DataArray(np.asarray(grp_da.values).astype(np.int64),
                                     dims=(NODE_DIM,), coords={NODE_DIM: nodes})}
            )  # fmt: skip
        free = [str(v) for v in post.data_vars if v in _FREE]
        if s.kind == "latent_space":
            z = _flat(post, "z_raw", NODE_DIM, Z_DIM) * _flat(post, "sigma_z")[:, None, None]
            aligned, ref = procrustes(z, s.procrustes_iters)
            labels = [f"z{k + 1}" for k in range(s.latent_dims)]
            chains, draws = post.sizes["chain"], post.sizes["draw"]
            post = post.assign(
                position=xr.DataArray(
                    aligned.reshape(chains, draws, len(nodes), s.latent_dims),
                    dims=("chain", "draw", NODE_DIM, LATENT_DIM),
                    coords={NODE_DIM: nodes, LATENT_DIM: labels},
                )
            )
            const = const.assign(
                {POSITION_REF: xr.DataArray(ref, dims=(NODE_DIM, LATENT_DIM),
                                            coords={NODE_DIM: nodes, LATENT_DIM: labels})}
            )  # fmt: skip
            free = [v for v in free if v != "z_raw"] + ["position"]
        post = post.assign(tie_prob=self._fit_probabilities(ds, s, nodes, post))
        groups["posterior"] = post
        groups["constant_data"] = const
        if "log_likelihood" not in groups:
            # always kept: leave-one-dyad-out elpd is how the kinds are compared
            groups["log_likelihood"] = self._log_likelihood(ds, s, nodes, post, const)
        if out.diagnostics:
            # r-hat and ESS on the identified quantities: the raw z_raw draws rotate freely
            out.diagnostics = convergence_diagnostics(
                post, groups.get("sample_stats"), var_names=free
            )
        obs = groups["observed_data"][TARGET].values
        out.learned.update(
            {
                "kind": s.kind,
                "covariates": bool(s.covariates),
                "n_nodes": len(nodes),
                "n_dyads": int(obs.size),
                "n_ties": int(obs.sum()),
                "n_dyads_held_out": len(nodes) * (len(nodes) - 1) // 2 - int(obs.size),
            }
        )
        return out

    def _fit_probabilities(
        self, ds: xr.Dataset, s: NetworkLatentSpaceSettings, nodes: np.ndarray, post: xr.Dataset
    ) -> xr.DataArray:
        """``tie_prob(chain, draw, node, node_)`` of every pair (float32, 0 on the diagonal):
        the field that the plots and the Model Explorer read from the bundle."""
        n = len(nodes)
        idx = np.arange(n)
        x = beta = None
        if s.covariates:
            x = self.covariates(ds, nodes)[0]
            beta = _flat(post, "beta", COV_DIM)
        soc = _flat(post, "sociality", NODE_DIM) if "sociality" in post else None
        z = None
        if "z_raw" in post:
            z = _flat(post, "z_raw", NODE_DIM, Z_DIM) * _flat(post, "sigma_z")[:, None, None]
        prob = pair_probabilities(
            _flat(post, "alpha"), idx, idx, x=x, beta=beta, sociality=soc, z=z,
            dtype=np.float32,
        )  # fmt: skip
        prob[:, idx, idx] = 0.0
        chains, draws = post.sizes["chain"], post.sizes["draw"]
        return xr.DataArray(
            prob.reshape(chains, draws, n, n),
            dims=("chain", "draw", NODE_DIM, PAIR_DIM),
            coords={NODE_DIM: nodes, PAIR_DIM: nodes},
        )

    def _log_likelihood(
        self,
        ds: xr.Dataset,
        s: NetworkLatentSpaceSettings,
        nodes: np.ndarray,
        post: xr.Dataset,
        const: xr.Dataset,
    ) -> xr.Dataset:
        """Pointwise ``log p(tie_ij)`` ``(chain, draw, dyad)`` from the NumPy twin."""
        i = np.asarray(const[DYAD_I].values, dtype=np.int64)
        j = np.asarray(const[DYAD_J].values, dtype=np.int64)
        adj = self.adjacency(ds, nodes)
        y = adj[i, j]
        x = beta = None
        if s.covariates:
            x = self.covariates(ds, nodes)[0][i, j]
            beta = _flat(post, "beta", COV_DIM)
        soc = _flat(post, "sociality", NODE_DIM) if "sociality" in post else None
        z = None
        if "z_raw" in post:
            z = _flat(post, "z_raw", NODE_DIM, Z_DIM) * _flat(post, "sigma_z")[:, None, None]
        eta = dyad_eta(_flat(post, "alpha"), i, j, x=x, beta=beta, sociality=soc, z=z)
        ll = -np.logaddexp(0.0, np.where(y == 1, -eta, eta))
        chains, draws = post.sizes["chain"], post.sizes["draw"]
        return xr.Dataset(
            {
                TARGET: xr.DataArray(
                    ll.reshape(chains, draws, -1),
                    dims=("chain", "draw", DYAD_DIM),
                    coords={
                        "chain": post["chain"].values,
                        "draw": post["draw"].values,
                        DYAD_DIM: np.arange(i.size),
                    },
                )
            }
        )

    # ------------------------------------------------------------------ score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        post = fitted.posterior
        known = fitted.coords.get(NODE_DIM)
        if known is None:
            known = fitted.group("constant_data")[ADJACENCY][NODE_DIM].values
        fit_nodes = np.asarray(known)
        index = {v: k for k, v in enumerate(fit_nodes.tolist())}
        rows = self._labels(ds, NODE_DIM, fit_nodes)
        cols = self._labels(ds, PAIR_DIM, rows)
        try:
            ia = np.array([index[v] for v in rows.tolist()], dtype=np.int64)
            ib = np.array([index[v] for v in cols.tolist()], dtype=np.int64)
        except KeyError as exc:
            raise MapiError(
                f"node {exc.args[0]!r} is not a node of the fit (the node set is fixed)",
                code=ErrorCode.COORD_NOT_ALLOWED,
                pointer=f"/data/{NODE_DIM}",
            ) from None
        chain, draw = post["chain"].values, post["draw"].values
        n_s = chain.size * draw.size
        alpha = _flat(post, "alpha")
        beta = _flat(post, "beta", COV_DIM) if s.covariates else None
        soc = _flat(post, "sociality", NODE_DIM) if "sociality" in post else None
        z = None
        if "z_raw" in post:
            z = _flat(post, "z_raw", NODE_DIM, Z_DIM) * _flat(post, "sigma_z")[:, None, None]
        x = None
        if s.covariates:
            fit_cov = np.asarray(post[COV_DIM].values)
            x = self._score_covariates(ds, rows, cols, fit_cov)
        na, nb = rows.size, cols.size
        same = rows[:, None] == cols[None, :]
        ii, jj = np.meshgrid(ia, ib, indexing="ij")
        prob = pair_probabilities(
            alpha, ia, ib, x=x, beta=beta, sociality=soc, z=z, check=ctx.check_cancelled
        )
        prob[:, same] = 0.0
        noisy = ctx.mode == "simulate" or ctx.kind == "predictive"
        shape = (chain.size, draw.size)
        dims = ("chain", "draw", NODE_DIM, PAIR_DIM)
        node_coords = {"chain": chain, "draw": draw, NODE_DIM: rows, PAIR_DIM: cols}
        expected = {"kind": "expected", "noise": "none"}
        arrays: dict[str, xr.DataArray] = {
            "tie_prob": xr.DataArray(
                prob.reshape(*shape, na, nb), dims=dims, coords=node_coords, attrs=expected
            )
        }
        stats = None
        if noisy:
            n_fit = fit_nodes.size
            ties = np.empty_like(prob)
            full = (
                na == n_fit
                and nb == n_fit
                and set(ia.tolist()) == set(range(n_fit))
                and bool(np.isfinite(prob).all())
            )
            stats = np.full((n_s, len(GRAPH_STATS)), np.nan) if full else None
            # uniforms of the unordered pair, in fit positions: subsets get the same ties
            lo, hi = np.minimum(ii, jj), np.maximum(ii, jj)
            gblock = 250
            for a in range(0, n_s, gblock):
                u = _uniforms(ctx.seed, n_fit, min(gblock, n_s - a), a)
                ub = u[:, lo, hi]
                ties[a : a + gblock] = (ub < prob[a : a + gblock]).astype(float)
                ctx.check_cancelled()
            ties[:, same] = 0.0
            if stats is not None:
                order = np.argsort(ia)
                for k in range(n_s):
                    m = ties[k][np.ix_(order, np.argsort(ib))]
                    stats[k] = graph_stats(m)
            tie = ties
            attrs = {"kind": "predictive", "noise": "crn_seeded"}
        else:
            tie, attrs = prob, expected
        arrays["tie"] = xr.DataArray(
            tie.reshape(*shape, na, nb), dims=dims, coords=node_coords, attrs=attrs
        )
        arrays["degree"] = xr.DataArray(
            np.nansum(tie, axis=-1).reshape(*shape, na),
            dims=("chain", "draw", NODE_DIM),
            coords={NODE_DIM: rows},
            attrs=dict(attrs),
        )
        if soc is not None:
            arrays["sociality"] = xr.DataArray(
                soc[:, ia].reshape(*shape, na),
                dims=("chain", "draw", NODE_DIM),
                coords={NODE_DIM: rows},
                attrs=dict(expected),
            )
        if "position" in post:
            arrays["position"] = (
                post["position"]
                .sel({NODE_DIM: rows})
                .transpose("chain", "draw", NODE_DIM, LATENT_DIM)
                .assign_attrs(expected)
            )
        if stats is not None:
            arrays["graph_stat"] = xr.DataArray(
                stats.reshape(*shape, len(GRAPH_STATS)),
                dims=("chain", "draw", STAT_DIM),
                coords={STAT_DIM: list(GRAPH_STATS)},
                attrs=dict(attrs),
            )
        pred = xr.Dataset(arrays, coords={"chain": chain, "draw": draw})
        return xr.DataTree.from_dict({PREDICTIONS: pred})

    @staticmethod
    def _labels(ds: xr.Dataset, dim: str, default: np.ndarray) -> np.ndarray:
        if dim in ds.coords:
            return np.asarray(ds[dim].values)
        if dim in ds.dims:
            return np.arange(ds.sizes[dim])
        return np.asarray(default)

    def _score_covariates(
        self, ds: xr.Dataset, rows: np.ndarray, cols: np.ndarray, fit_cov: np.ndarray
    ) -> np.ndarray:
        """``(node, node_, cov)`` covariates of the score data, in the fit order of ``cov``;
        missing values only in padding cells (not requested)."""
        if COVARIATES not in ds:
            raise MapiError(
                f"the score data has no {COVARIATES!r}, which the fit used",
                code=ErrorCode.MISSING_FUTURE_COVARIATE,
                pointer=f"/data/{COVARIATES}",
            )
        da = ds[COVARIATES]
        if COV_DIM in da.coords:
            have = {str(v) for v in da[COV_DIM].values}
            missing = [str(v) for v in fit_cov if str(v) not in have]
            if missing:
                raise MapiError(
                    f"{COVARIATES!r} has no covariate(s) {missing} of the fit",
                    code=ErrorCode.MISSING_FUTURE_COVARIATE,
                    pointer=f"/data/{COVARIATES}",
                )
            da = da.sel({COV_DIM: list(fit_cov)})
        flag = f"{COV_DIM}__requested"
        if flag in ds.coords and not bool(np.asarray(ds[flag].values, dtype=bool).all()):
            # the platform pads a left-out covariate with 0: never predict from that
            sent = np.asarray(ds[flag].values, dtype=bool)
            missing = [str(v) for v, ok in zip(ds[COV_DIM].values, sent, strict=True) if not ok]
            raise MapiError(
                f"{COVARIATES!r} must hold every covariate of the fit; missing {missing}",
                code=ErrorCode.MISSING_FUTURE_COVARIATE,
                pointer=f"/data/{COVARIATES}",
            )
        x = np.asarray(da.transpose(NODE_DIM, PAIR_DIM, COV_DIM).values, dtype=float)
        req = np.ones((rows.size, cols.size), dtype=bool)
        for k, dim in enumerate((NODE_DIM, PAIR_DIM)):
            flag = f"{dim}__requested"
            if flag in ds.coords:
                mask = np.asarray(ds[flag].values, dtype=bool)
                req &= mask[:, None] if k == 0 else mask[None, :]
        off = rows[:, None] != cols[None, :]
        bad = ~np.isfinite(x).all(-1) & req & off
        if bad.any():
            raise MapiError(
                f"{COVARIATES!r} has missing values for {int(bad.sum())} requested pair(s)",
                code=ErrorCode.MISSING_FUTURE_COVARIATE,
                pointer=f"/data/{COVARIATES}",
            )
        return x


_FREE = frozenset({"alpha", "beta", "sigma_s", "s_raw", "sigma_z", "z_raw"})
"""The free random variables of :meth:`NetworkLatentSpaceModel.build`."""


def _flat(post: xr.Dataset, name: str, *dims: str) -> np.ndarray:
    """Posterior draws of ``name`` as ``(samples, *dims)``."""
    da = post[name].transpose("chain", "draw", *dims)
    arr = np.asarray(da.values, dtype=float)
    return arr.reshape(-1, *arr.shape[2:])
