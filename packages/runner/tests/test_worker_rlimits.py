"""Worker resource limits (v0.19, D18, G9; WP0 probe P1).

``RLIMIT_NOFILE`` and ``RLIMIT_FSIZE`` apply to every worker; ``RLIMIT_NPROC`` only where the
worker's uid is private to the execution (Cloud Run Jobs), because it counts every process
of the uid on the kernel. A tiny fit through the real supervisor still succeeds with them.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
from pathlib import Path

import pytest
from mapi_conformance.fixtures.xgb_store_demand import data as xgb
from mapi_runner.sandbox import (
    NOFILE_LIMIT,
    NPROC_LIMITS,
    IsolationPlan,
    private_uid,
    worker_rlimits,
)
from mapi_sdk.wire import write_zarr_zip
from mapi_spec.executor import RunKind
from mapi_spec.loader import canonical_json, load_model_spec
from mapi_spec.resources import RESOURCE_CLASSES, ResourceClass
from mapi_spec.run import RunnerExit
from runner_helpers import make_run, run_supervisor
from shimgcs import ShimGcs


def test_every_worker_gets_nofile_and_fsize() -> None:
    limits = worker_rlimits(ResourceClass.S, private=False)
    soft, hard = limits[resource.RLIMIT_NOFILE]
    assert soft == hard and soft <= NOFILE_LIMIT
    scratch = RESOURCE_CLASSES[ResourceClass.S].scratch_bytes
    assert scratch is not None
    assert limits[resource.RLIMIT_FSIZE][0] <= scratch
    assert resource.RLIMIT_NPROC not in limits


@pytest.mark.parametrize(
    ("cls", "nproc"),
    [(ResourceClass.S, 256), (ResourceClass.M, 512), (ResourceClass.L, 1024)],
)
def test_nproc_only_for_a_private_uid(cls: ResourceClass, nproc: int) -> None:
    assert NPROC_LIMITS[cls] == nproc
    limits = worker_rlimits(cls, private=True)
    assert limits[resource.RLIMIT_NPROC][0] <= nproc
    assert resource.RLIMIT_NPROC not in worker_rlimits(cls, private=False)


def test_an_unbounded_class_has_no_fsize_limit() -> None:
    assert resource.RLIMIT_FSIZE not in worker_rlimits(ResourceClass.XL, private=False)


def test_a_private_uid_is_only_cloud_run() -> None:
    assert private_uid({"CLOUD_RUN_EXECUTION": "exec-1"})
    assert not private_uid({})
    assert not private_uid({"K_SERVICE": "api"})


_REPORT = (
    "import json, resource; "
    "print(json.dumps({n: resource.getrlimit(getattr(resource, n))[0] "
    "for n in ('RLIMIT_NOFILE', 'RLIMIT_FSIZE', 'RLIMIT_CORE')}))"
)


def test_the_preexec_applies_the_limits() -> None:
    limits = worker_rlimits(ResourceClass.S, private=False)
    plan = IsolationPlan(drop_privileges=False, no_new_privs=False).with_rlimits(limits)
    out = subprocess.run(
        [sys.executable, "-I", "-c", _REPORT],
        capture_output=True,
        text=True,
        check=True,
        preexec_fn=plan.preexec(),
    )
    got = json.loads(out.stdout)
    assert got["RLIMIT_NOFILE"] == limits[resource.RLIMIT_NOFILE][0]
    assert got["RLIMIT_FSIZE"] == limits[resource.RLIMIT_FSIZE][0]
    assert got["RLIMIT_CORE"] == 0


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or os.geteuid() != 0,
    reason="RLIMIT_NPROC counts every process of the uid: only meaningful for a fresh uid "
    "(root dropping to an unused uid, as in a container); see WP0 probe P1",
)
def test_nproc_stops_a_fork_bomb_in_a_fresh_uid() -> None:
    uid = 54321  # an unused uid: nothing else runs under it
    plan = IsolationPlan(drop_privileges=False, no_new_privs=False).with_rlimits(
        {resource.RLIMIT_NPROC: (8, 8)}
    )
    script = (
        "import os, sys\n"
        "n = 0\n"
        "try:\n"
        "    for _ in range(64):\n"
        "        if os.fork() == 0:\n"
        "            import time; time.sleep(5); os._exit(0)\n"
        "        n += 1\n"
        "except BlockingIOError:\n"
        "    print('LIMITED', n); sys.exit(0)\n"
        "print('UNLIMITED', n)\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        user=uid,
        group=uid,
        extra_groups=[],
        preexec_fn=plan.preexec(),
        timeout=60,
    )
    assert out.stdout.startswith("LIMITED")


@pytest.mark.integration
def test_a_tiny_fit_passes_with_the_worker_limits(gcs: ShimGcs) -> None:
    data, _ = xgb.generate(0, "tiny")
    data.attrs["mapi_schema"] = xgb.schema().model_dump_json()
    d = gcs.buckets.data
    gcs.put(d, "t/default/datasets/rl/data.zarr.zip", write_zarr_zip(data) or b"")
    text = (Path(xgb.__file__).parent / "spec.yaml").read_text()
    spec = json.loads(canonical_json(load_model_spec(text)))
    spec["model"]["settings"]["estimator"]["params"] = {"n_estimators": 10, "max_depth": 2}
    spec["model"]["fit"]["mode"] = "train_only"
    spec.pop("evaluate", None)
    result = run_supervisor(
        make_run(
            gcs, RunKind.FIT, spec, inputs={"dataset": (d, "t/default/datasets/rl/data.zarr.zip")}
        )
    )
    errors = [e.payload for e in result.of("error")]
    assert result.code == RunnerExit.OK, (errors, result.stderr[-4000:])
