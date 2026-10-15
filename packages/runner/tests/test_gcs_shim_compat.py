"""The runner against the real local GCS emulator (``mapi_gcs_shim``), when it is installed.

Skipped until the gcs-shim package (feat/m1-gcs-shim) is part of the workspace; the other
runner tests use ``tests/fakegcs.py``. Everything here goes through the contract's async
``ObjectStore`` protocol and the shim's *exec* principal, exactly as the local executor will.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mapi_conformance.fixtures.xgb_store_demand import data as xgb
from mapi_spec.api import DimMapping, IngestMapping, VarMapping
from mapi_spec.executor import RunKind
from mapi_spec.loader import canonical_json, load_model_spec, spec_hash
from mapi_spec.resources import ResourceClass
from mapi_spec.run import (
    OutputManifest,
    RunLimits,
    RunnerEnv,
    RunnerExit,
    RunSpec,
    new_hmac_key,
    new_nonce,
    verify_output_manifest,
    verify_output_objects,
)
from mapi_spec.storage import NotFound, ObjectMeta, run_prefix
from runner_helpers import Result, Run, assert_signed_stream, run_supervisor

shim_server = pytest.importorskip("mapi_gcs_shim.server")
shim_config = pytest.importorskip("mapi_gcs_shim.config")

pytestmark = pytest.mark.integration
TTL = timedelta(hours=1)


@pytest.fixture
def shim(tmp_path: Path) -> Iterator[Any]:
    server = shim_server.ShimServer(tmp_path / "shim")
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _prepare(
    shim: Any, kind: RunKind, spec: dict[str, Any], inputs: dict[str, tuple[str, str]]
) -> Run:
    b = shim.buckets
    run_id = "run_" + secrets.token_hex(6)
    nonce = new_nonce()
    prefix = run_prefix("default", run_id, 1)
    async with shim.store() as store:
        outputs = {
            "manifest": await store.sign_put(
                b.runs, prefix + "manifest.json", expires_in=TTL, max_bytes=1 << 20
            ),
            "artifacts": await store.post_policy(b.runs, prefix, expires_in=TTL, max_bytes=1 << 30),
        }
        signed = {
            name: await store.sign_get(bucket, obj, expires_in=TTL)
            for name, (bucket, obj) in inputs.items()
        }
        now = datetime.now(UTC)
        rs = RunSpec(
            run_id=run_id,
            attempt=1,
            nonce=nonce,
            kind=kind,
            tenant_id="default",
            project_id="default",
            spec=spec,
            spec_hash=spec_hash(spec),
            catalog_digest="sha256:" + "0" * 64,
            inputs=signed,
            outputs=outputs,
            hmac_key=new_hmac_key(),
            limits=RunLimits.for_class(ResourceClass.S),
            resource_class=ResourceClass.S,
            deadline=now + timedelta(minutes=15),
            created_at=now,
        )
        await store.put(b.bootstrap, nonce, rs.model_dump_json().encode())
    env = RunnerEnv(
        run_id=run_id,
        nonce=nonce,
        bootstrap_url=f"{shim.endpoint}/storage/v1/b/{b.bootstrap}/o/{{nonce}}",
        exec_token=shim.token(shim_config.PRINCIPAL_EXEC),
    ).to_environ()
    return Run(gcs=None, rs=rs, env=env, prefix=prefix)  # type: ignore[arg-type]


async def _verify(shim: Any, result: Result) -> dict[str, bytes]:
    assert result.code == RunnerExit.OK, ([e.payload for e in result.of("error")], result.stderr)
    assert_signed_stream(result)
    b, prefix = shim.buckets, result.run.prefix
    async with shim.store() as store:
        with pytest.raises(NotFound):
            await store.stat(b.bootstrap, result.run.rs.nonce)
        manifest = OutputManifest.model_validate_json(
            await store.get(b.runs, prefix + "manifest.json")
        )
        assert verify_output_manifest(manifest, result.run.key)
        listing: list[ObjectMeta] = []
        token: str | None = None
        while True:
            page = await store.list(b.runs, prefix=prefix, page_token=token)
            listing += page.items
            token = page.next_page_token
            if not token:
                break
        check = verify_output_objects(manifest, listing, expected_prefix=prefix)
        assert check.ok, check.issues
        return {name: await store.get(b.runs, o.key) for name, o in manifest.objects.items()}


def test_csv_ingest_fit_and_score_through_the_shim(shim: Any) -> None:
    b = shim.buckets
    data, _ = xgb.generate(0, "tiny")
    schema = xgb.schema()
    csv = data.to_dataframe().reset_index().to_csv(index=False).encode()

    async def upload() -> None:
        async with shim.store() as store:
            await store.put(b.quarantine, "t/default/uploads/upl_1", csv, content_type="text/csv")

    _run(upload())
    mapping = IngestMapping(
        format="csv",
        dims={d: DimMapping(column=d, spec=s) for d, s in schema.dims.items()},
        vars={
            v: VarMapping(column=v, role=s.role, missing=s.missing) for v, s in schema.vars.items()
        },
    )
    run = _run(
        _prepare(
            shim,
            RunKind.INGEST,
            mapping.model_dump(mode="json"),
            {"upload": (b.quarantine, "t/default/uploads/upl_1")},
        )
    )
    ingest = run_supervisor(run)
    outputs = _run(_verify(shim, ingest))
    assert set(outputs) == {"data.zarr.zip", "schema.json", "profile.json", "preview.json"}

    text = (Path(xgb.__file__).parent / "spec.yaml").read_text()
    spec = json.loads(canonical_json(load_model_spec(text)))
    spec["model"]["settings"]["estimator"]["params"] = {"n_estimators": 30, "max_depth": 3}
    fit_run = _run(
        _prepare(
            shim,
            RunKind.FIT,
            spec,
            {"dataset": (b.runs, ingest.run.prefix + "data.zarr.zip")},
        )
    )
    fit = run_supervisor(fit_run)
    fit_outputs = _run(_verify(shim, fit))
    assert {"bundle.zarr.zip", "bundle.manifest.json", "plots.json"} <= set(fit_outputs)

    request = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "endpoint://default/store-demand",
        "mode": "predict",
    }
    score_run = _run(
        _prepare(
            shim,
            RunKind.SCORE,
            request,
            {
                "bundle": (b.runs, fit.run.prefix + "bundle.zarr.zip"),
                "bundle.manifest": (b.runs, fit.run.prefix + "bundle.manifest.json"),
                "dataset": (b.runs, ingest.run.prefix + "data.zarr.zip"),
            },
        )
    )
    score = run_supervisor(score_run)
    assert "score.zarr.zip" in _run(_verify(shim, score))
