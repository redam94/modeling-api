"""Shared set-up of the M3.5 live-stack verification (``test_m35_stack.py`` through the API
and the CLI, ``apps/web/e2e/m35-live.e2e.spec.ts`` in the browser via ``m35_browser_seed.py``).

Everything runs against a ``dev``-profile stack (``make dev``): the platform operator
(``admin@dev.local``, the tenant admin) creates a fresh project in the ``default`` tenant
(an ``internal`` tenant, so the local executor runs it) and adds ``analyst@dev.local`` as an
editor with the ``reviewer`` capability, the second person who approves validations.

The data are synthetic with known truth:

* ``weekly_sales``: 156 weeks of ``sales`` whose error is a **random walk** (a unit root)
  plus a ``price`` driver with a true effect of -3 per unit;
* ``signal`` / ``noise``: 160 weeks of ``sales`` with AR(0.5) errors and a strong ``price``
  effect, and the same table with ``sales`` replaced by pure noise;
* ``geo_panel``: the ``guided_mmm`` simulation at its tiny size (40 weeks, 3 regions,
  2 channels with known adstock and saturation);
* ``macro``: three weekly series ``value(series, time)``, two sharing a stochastic trend.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_client import Client
from mapi_client.oidc import dev_idp_login
from mapi_conformance.fixtures.guided_mmm.data import generate, schema
from mapi_spec.jobs import DATASET_SCHEMA_ATTR
from mapi_spec.localdev import LocalDev
from mapi_workflows.testing import multi_series, weekly_series

ROOT: Final = Path(__file__).resolve().parents[2]
TENANT: Final = "default"
OPERATOR: Final = "admin@dev.local"
REVIEWER: Final = "analyst@dev.local"
RUN_TIMEOUT_S: Final = 1500.0
TINY_MMM_FIT: Final = {"tune": 150, "draws": 150, "chains": 2, "seed": 1}
NULL_POLICY: Final[dict[str, Any]] = {
    "name": "Forecasts must beat their own null tests",
    "description": "Shuffled results and fake noise drivers; a second person signs off.",
    "applies_to": {"families": ["ts"]},
    "checks": [
        {"check": "null.permutation_target", "params": {"n_permutations": 19}},
        {"check": "null.noise_regressors", "params": {"n_noise": 3}},
    ],
    "require_approval": True,
    "require_validated_for_promotion": True,
}
"""The policy of the governance journey: ``families: [ts]`` as in the docs (it must cover
``ts.sarimax``, whose declared family is ``forecast``)."""


def api_url() -> str:
    return LocalDev.from_environ(os.environ).api_url.rstrip("/")


def data_dir() -> Path:
    d = Path(LocalDev.from_environ(os.environ).data_dir)
    return d if d.is_absolute() else ROOT / d


def sign_in(email: str, project: str | None = None) -> Client:
    """A client signed in through the dev IdP (an ID token: approvals need a person)."""
    url = api_url()
    client = Client(url, token=dev_idp_login(url, email))
    if project is not None:
        client.use(f"{TENANT}/{project}")
    return client


# ================================================================================== data


def unit_root_series() -> xr.Dataset:
    """Weekly ``sales`` with a random-walk error and a ``price`` driver (beta = -3)."""
    return weekly_series(156, random_walk=True, beta=3.0)


def signal_and_noise() -> tuple[xr.Dataset, xr.Dataset]:
    """The same weekly table with a real ``price`` effect, and with ``sales`` pure noise.
    The price moves from week to week (promotions): the permutation null test shuffles the
    price, not the sales, so its effect must show on the held-out weeks."""
    signal = weekly_series(160, phi=0.5, beta=6.0, seed=3)
    price = 10 + np.random.default_rng(4).normal(0, 1.0, signal.sizes["time"])
    signal["sales"] = signal["sales"] - 6.0 * (price - signal["price"])
    signal["price"] = ("time", price)
    noise = signal.copy()
    rng = np.random.default_rng(5)
    noise["sales"] = ("time", rng.normal(100, 1, signal.sizes["time"]))
    return signal, noise


def geo_panel() -> xr.Dataset:
    ds, _ = generate(0, "tiny")
    return ds.assign_attrs({DATASET_SCHEMA_ATTR: schema().model_dump_json()})


def macro_series() -> xr.Dataset:
    return multi_series(160)


# ================================================================================= specs


def arimax_spec(project: str, name: str, dataset: str) -> str:
    """``ts.sarimax`` AR(1) errors with the price driver; 16 held-out weeks scored first."""
    return f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: {name}, project: {project}}}
data:
  dataset: dataset://{project}/{dataset}@latest
  bind: {{dims: {{t: time}}, vars: {{y: sales}}}}
  split: {{kind: time_holdout, dim: t, last: 16}}
model:
  use: ts.sarimax@^1
  settings: {{order: [1, 0, 0], exog: [price]}}
  fit: {{mode: evaluate_then_refit, seed: 1}}
evaluate: {{metrics: [rmse, mape]}}
"""


def var_spec(project: str, name: str = "macro-var", dataset: str = "macro") -> str:
    """A VAR(2) over the three series (``variable`` bound to the ``series`` dimension)."""
    return f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: {name}, project: {project}}}
data:
  dataset: dataset://{project}/{dataset}@latest
  bind: {{dims: {{t: time, variable: series}}, vars: {{y: value}}}}
  split: {{kind: time_holdout, dim: t, last: 8}}
model:
  use: ts.var@^1
  settings: {{lags: 2}}
  fit: {{mode: evaluate_then_refit, seed: 1}}
evaluate: {{metrics: [rmse]}}
"""


def mmm_spec(project: str, name: str = "geo-mmm", dataset: str = "geo_panel") -> str:
    """``mmm.bayesian`` (mmm-framework) pooled over regions, tiny NUTS settings."""
    fit = ", ".join(f"{k}: {v}" for k, v in TINY_MMM_FIT.items())
    return f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: {name}, project: {project}}}
data:
  dataset: dataset://{project}/{dataset}@latest
  bind:
    dims: {{geo: region, channel: channel, time: week}}
    vars: {{y: revenue, spend: spend}}
  split: {{kind: time_holdout, dim: time, last: 6}}
model:
  use: mmm.bayesian@^1
  settings:
    adstock: {{kind: geometric, l_max: 4}}
    saturation: {{kind: hill}}
    trend: {{kind: linear}}
    hierarchy: {{pool_over: [geo]}}
  fit: {{mode: evaluate_then_refit, {fit}}}
outputs:
  derived: [contributions, response_curves, marginal_cost]
  ui_draws: 100
evaluate: {{metrics: [mape]}}
"""


# =============================================================================== project


@dataclass
class World:
    """A fresh project of the ``default`` tenant and what the journeys create in it."""

    project: str
    project_name: str
    tenant_name: str
    reviewer_id: str
    datasets: dict[str, str] = field(default_factory=dict)
    """name -> dataset id"""
    models: dict[str, str] = field(default_factory=dict)
    """spec name -> model id (version 1)"""
    runs: dict[str, str] = field(default_factory=dict)
    """journey -> workflow run id"""
    timings: dict[str, float] = field(default_factory=dict)

    def ref(self, dataset: str, version: str = "latest") -> str:
        return f"dataset://{self.project}/{dataset}@{version}"


def create_world(op: Client, prefix: str, run: str) -> World:
    """A new project ``<prefix>-<run>``; the reviewer is an editor with ``reviewer``."""
    tenant = op.tenants.get(TENANT)
    project = op.tenants.create_project(TENANT, f"{prefix}-{run}", f"M3.5 verification {run}")
    members = list(op.tenants.members(TENANT, "default"))
    reviewer = next(m for m in members if (m.email or "").lower() == REVIEWER)
    op.tenants.add_member(
        TENANT,
        "editor",
        project_id=project.id,
        principal_id=reviewer.principal_id,
        capabilities=["reviewer"],
    )
    op.use(f"{TENANT}/{project.id}")
    return World(
        project=project.id,
        project_name=project.name,
        tenant_name=tenant.name,
        reviewer_id=reviewer.principal_id,
    )


def upload(op: Client, world: World, name: str, ds: xr.Dataset) -> None:
    t0 = time.monotonic()
    version = op.datasets.upload(ds, dataset=name, timeout=RUN_TIMEOUT_S)
    assert version.status.value == "ready", version
    world.datasets[name] = version.dataset_id
    world.timings[f"upload.{name}"] = round(time.monotonic() - t0, 2)


def fit_all(op: Client, world: World, specs: Mapping[str, str]) -> None:
    """Submit every spec's fit, then wait for all of them (they run concurrently)."""
    started: dict[str, tuple[str, float]] = {}
    for name, text in specs.items():
        rev = op.specs.upsert(text)
        started[name] = (op.runs.fit(rev.spec_id, rev.revision).id, time.monotonic())
    for name, (run_id, t0) in started.items():
        final = op.runs.wait(run_id, timeout=RUN_TIMEOUT_S)
        assert final.state.value == "succeeded", (name, final.problem)
        assert final.output_model_version_id, name
        world.timings[f"fit.{name}"] = round(time.monotonic() - t0, 2)
    by_name = {m.name: m.id for m in op.models.list()}
    for name in specs:
        world.models[name] = by_name[name]


def cleanup(project: str) -> None:
    """Delete the project's endpoints and the project (best effort)."""
    import contextlib

    with sign_in(OPERATOR, project) as op:
        for ep in op.endpoints.list():
            with contextlib.suppress(Exception):
                op.endpoints.delete(ep.alias)
        with contextlib.suppress(Exception):
            op.tenants.delete_project(TENANT, project)
