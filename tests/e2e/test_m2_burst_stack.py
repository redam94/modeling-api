"""A burst of concurrent *real* fits through a running stack (opt-in).

``make load-test`` proves "100 concurrent fits without loss" and the p95 time-to-RUNNING
budget on the Cloud Run code path with a fake Admin API and synthetic executions. This suite
complements it with real executions: real runner processes (supervisor + worker, the
store-demand scikit-learn fit), the real dispatcher, reconciler, finalizer and cost ledger,
all submitted at once by one tenant.

Opt-in because it loads the executor: set ``MAPI_E2E_BURST_RUNS``.

* **Local dev stack** (``make dev``): ``MAPI_E2E_BURST_RUNS=16 uv run pytest -m e2e
  tests/e2e/test_m2_burst_stack.py``. The operator (dev IdP) creates a fresh ``internal``
  tenant for the burst. Each fit is a pair of processes on this host (about 0.5 GB of RAM).
  The ledger, attempt and model-version rows are also counted straight in Postgres.
* **A deployment** (for example Cloud Run Jobs in ``dev``): ``MAPI_API_URL=https://…
  MAPI_TOKEN=<API token> MAPI_PROJECT=<tenant>/<project> MAPI_E2E_BURST_RUNS=100
  uv run pytest -m e2e -s tests/e2e/test_m2_burst_stack.py``. The token needs the ``admin``
  scope (the burst uploads a dataset and saves a spec, which ``runs:create`` and
  ``datasets:write`` together do not cover). Everything is checked through the API.

Asserts: every run SUCCEEDED, exactly one attempt and one metered ledger entry per run, one
distinct model version per run (no lost or duplicated finalization), and p95
time-to-RUNNING (``started_at - queued_at``) under 90 s. The percentiles are printed.
"""

from __future__ import annotations

import json
import os
import statistics
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import psycopg
import pytest
from mapi_client import Client
from mapi_client.oidc import dev_idp_login
from mapi_conformance.runner import load_fixture
from mapi_conformance.stack import StackRefs, stack_spec_yaml
from mapi_spec.api import Run
from mapi_spec.jobs import DATASET_SCHEMA_ATTR
from mapi_spec.localdev import LocalDev

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.slow]

ROOT = Path(__file__).resolve().parents[2]
RUNS = int(os.environ.get("MAPI_E2E_BURST_RUNS", "0") or 0)
P95_BUDGET_S: Final = 90.0
OPERATOR: Final = "admin@dev.local"


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, round(q * (len(ordered) - 1))))]


@contextmanager
def _caller(api: str, token: str) -> Iterator[Client]:
    """``MAPI_TOKEN``/``MAPI_PROJECT`` against a deployment, else a fresh dev-stack tenant."""
    deployed = os.environ.get("MAPI_TOKEN", "").strip()
    if deployed:
        project = os.environ.get("MAPI_PROJECT", "").strip()
        if not project:
            pytest.fail("MAPI_TOKEN needs MAPI_PROJECT (<tenant>/<project>) for the burst")
        with Client(api, token=deployed, project=project) as client:
            yield client
        return
    tenant = f"burst-{token}"
    account = os.environ.get("MAPI_E2E_UI_ACCOUNT", "analyst@dev.local")
    with Client(api, token=dev_idp_login(api, OPERATOR)) as op:
        op.admin.create_tenant(
            {"id": tenant, "name": f"Burst {token}", "trust_tier": "internal",
             "initial_admin_email": account}
        )  # fmt: skip
    with Client(api, token=dev_idp_login(api, account)) as client:
        client.tenants.create_project(tenant, "growth", "Burst")
        client.use(f"{tenant}/growth")
        yield client


def _local_database(local: LocalDev) -> str | None:
    """The dev stack's database URL (``None`` against a deployment)."""
    if os.environ.get("MAPI_TOKEN", "").strip():
        return None
    data_dir = Path(local.data_dir)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    return local.database_url or f"postgresql://postgres:@/mapi?host={data_dir / 'pg'}"


@pytest.mark.skipif(RUNS <= 0, reason="opt-in: set MAPI_E2E_BURST_RUNS (e.g. 16)")
def test_concurrent_real_fits_without_loss() -> None:
    local = LocalDev.from_environ(os.environ)
    api = local.api_url.rstrip("/")
    token = uuid.uuid4().hex[:6]
    with _caller(api, token) as user:
        assert user.project, "no project selected"
        project = user.project.split("/", 1)[1]
        fx = load_fixture("sklearn_store_demand")
        data, _ = fx.generate(0, "tiny")
        version = user.datasets.upload(
            data.assign_attrs({DATASET_SCHEMA_ATTR: fx.raw_schema(data).model_dump_json()}),
            dataset=f"burst-{token}",
            timeout=600,
        )
        assert version.status.value == "ready", version
        doc = json.loads(
            stack_spec_yaml(fx, "tiny", StackRefs(data=version.ref), name=f"burst-{token}")
        )
        doc["metadata"]["project"] = project
        spec = user.specs.create(json.dumps(doc), message="burst")

        def submit(i: int) -> Run:
            return user.runs.fit(spec.id, 1, labels={"suite": "burst", "i": str(i)},
                                 idempotency_key=f"burst-{token}-{i}")  # fmt: skip

        with ThreadPoolExecutor(max_workers=min(RUNS, 16)) as pool:
            submitted = list(pool.map(submit, range(RUNS)))
        assert len({r.id for r in submitted}) == RUNS
        finals = [user.runs.wait(r.id, timeout=3600, max_poll_interval=5) for r in submitted]

    bad = [(r.id, r.state.value, r.problem) for r in finals if r.state.value != "succeeded"]
    assert not bad, bad
    # no retries, every attempt metered exactly once, one model version per fit
    assert [r.attempt for r in finals] == [1] * RUNS
    unbilled = [(r.id, r.cost) for r in finals if r.cost.estimated or r.cost.attempts_billed != 1]
    assert not unbilled, unbilled
    outputs = [r.output_model_version_id for r in finals]
    assert None not in outputs and len(set(outputs)) == RUNS, outputs
    ids = [r.id for r in finals]
    db = _local_database(local)
    if db is not None:
        with psycopg.connect(db) as conn:
            for table in ("cost_ledger", "model_versions", "run_attempts"):
                rows = conn.execute(
                    f"SELECT run_id, count(*) FROM {table} WHERE run_id = ANY(%s) GROUP BY run_id",
                    (ids,),
                ).fetchall()
                assert dict(rows) == dict.fromkeys(ids, 1), (table, rows)

    waits = [
        (r.started_at - r.queued_at).total_seconds()
        for r in finals
        if r.started_at is not None and r.queued_at is not None
    ]
    assert len(waits) == RUNS
    walls = [
        (r.finished_at - r.queued_at).total_seconds()
        for r in finals
        if r.finished_at is not None and r.queued_at is not None
    ]
    metrics = {
        "runs": RUNS,
        "time_to_running_p50_s": round(statistics.median(waits), 2),
        "time_to_running_p95_s": round(_pct(waits, 0.95), 2),
        "time_to_running_max_s": round(max(waits), 2),
        "wall_p95_s": round(_pct(walls, 0.95), 2),
        "cost_micros_total": sum(r.cost.amount_micros or 0 for r in finals),
    }
    print(f"\nburst metrics: {json.dumps(metrics)}")
    assert metrics["time_to_running_p95_s"] < P95_BUDGET_S, metrics
