"""A burst of concurrent *real* fits through the running stack (opt-in).

``make load-test`` proves "100 concurrent fits without loss" and the p95 time-to-RUNNING
budget on the Cloud Run code path with a fake Admin API and synthetic executions. This suite
complements it on the local stack: real runner processes (supervisor + worker, the
store-demand scikit-learn fit), the real dispatcher, reconciler, finalizer and cost ledger,
all submitted at once by one tenant.

Opt-in because it loads the machine: ``MAPI_E2E_BURST_RUNS=16 make e2e`` (or
``uv run pytest -m e2e tests/e2e/test_m2_burst_stack.py``). Each fit is a separate pair of
processes on this host, so size the burst to the machine (about 0.5 GB of RAM per fit).

Asserts: every run SUCCEEDED, exactly one attempt and one ledger entry per run, one model
version per run (no lost or duplicated finalization), and p95 time-to-RUNNING
(``started_at - queued_at``) under 90 s. The measured percentiles are printed.
"""

from __future__ import annotations

import json
import os
import statistics
import uuid
from concurrent.futures import ThreadPoolExecutor
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


@pytest.mark.skipif(RUNS <= 0, reason="opt-in: set MAPI_E2E_BURST_RUNS (e.g. 16)")
def test_concurrent_real_fits_without_loss(record_property: object) -> None:
    local = LocalDev.from_environ(os.environ)
    api = local.api_url.rstrip("/")
    data_dir = Path(local.data_dir) if Path(local.data_dir).is_absolute() else ROOT / local.data_dir
    db_url = local.database_url or f"postgresql://postgres:@/mapi?host={data_dir / 'pg'}"
    token = uuid.uuid4().hex[:6]
    tenant = f"burst-{token}"
    account = os.environ.get("MAPI_E2E_UI_ACCOUNT", "analyst@dev.local")
    with Client(api, token=dev_idp_login(api, OPERATOR)) as op:
        op.admin.create_tenant(
            {"id": tenant, "name": f"Burst {token}", "trust_tier": "internal",
             "initial_admin_email": account}
        )  # fmt: skip
    with Client(api, token=dev_idp_login(api, account)) as user:
        user.tenants.create_project(tenant, "growth", "Burst")
        user.use(f"{tenant}/growth")
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
        doc["metadata"]["project"] = "growth"
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
    assert all(r.attempt == 1 for r in finals)
    ids = [r.id for r in finals]
    with psycopg.connect(db_url) as conn:
        ledger = conn.execute(
            "SELECT run_id, count(*) FROM cost_ledger WHERE run_id = ANY(%s) GROUP BY run_id",
            (ids,),
        ).fetchall()
        versions = conn.execute(
            "SELECT run_id, count(*) FROM model_versions WHERE run_id = ANY(%s) GROUP BY run_id",
            (ids,),
        ).fetchall()
        attempts = conn.execute(
            "SELECT run_id, count(*) FROM run_attempts WHERE run_id = ANY(%s) GROUP BY run_id",
            (ids,),
        ).fetchall()
    assert dict(ledger) == dict.fromkeys(ids, 1), ledger
    assert dict(versions) == dict.fromkeys(ids, 1), versions
    assert dict(attempts) == dict.fromkeys(ids, 1), attempts
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
    }
    print(f"\nburst metrics: {json.dumps(metrics)}")
    assert metrics["time_to_running_p95_s"] < P95_BUDGET_S, metrics
