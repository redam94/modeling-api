"""v0.19 WP6 (D10): the uploaded code a run used is the control plane's ``runs.overlays``.

The supervisor reports the overlay hashes it verified and mounted; the finalizer refuses an
attempt whose report differs from the pins (order-insensitive) with ``INTEGRITY_FAILURE``.
A model version's ``Lineage.overlays`` and its bundle manifest's ``overlay_hashes`` come from
``runs.overlays`` (never from the runner); runs without pins keep byte-identical manifests.
"""

from __future__ import annotations

import hashlib

from _v019_pipeline import (
    NS,
    WHEEL_SHA,
    finalize,
    pinned_fit,
    queued_fit_once,
    seed_version,
    to_finalizing,
    write_outputs,
)
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import load_run
from mapi_server_testkit.fake_runner import outputs_for
from mapi_spec.executor import RunKind
from mapi_spec.run import BundleManifest, RunSpec, RunState
from sqlalchemy import update

OTHER_SHA = "sha256:" + hashlib.sha256(b"another wheel").hexdigest()


def _fit_outputs() -> dict[str, tuple[bytes, str]]:
    return outputs_for(RunSpec.model_construct(kind=RunKind.FIT), "ok")


async def _model_version(ctx: ServerContext, run_id: str) -> m.ModelVersion:
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id)
        assert run.output_model_version_id is not None
        mv = await s.get(m.ModelVersion, run.output_model_version_id)
        assert mv is not None
        s.expunge(mv)
        return mv


async def test_lineage_and_bundle_name_the_pins(ctx: ServerContext) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    run_id = await pinned_fit(ctx, vid)
    run, att = await to_finalizing(ctx, run_id)
    await write_outputs(ctx, run, att, _fit_outputs(), overlay_hashes=[WHEEL_SHA])
    assert await finalize(ctx, run, att) is RunState.SUCCEEDED
    mv = await _model_version(ctx, run_id)
    assert mv.lineage["overlays"] == [
        {"plugin_version_id": vid, "namespace": NS, "sha256": WHEEL_SHA}
    ]
    assert mv.lineage.get("overlay_hash") is None
    manifest = BundleManifest.model_validate(mv.manifest)
    assert manifest.overlay_hashes == [WHEEL_SHA] and manifest.overlay_hash is None
    assert ctx.signer.verify(manifest)


async def test_the_supervisor_must_report_exactly_the_pins(ctx: ServerContext) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    for claimed in ([], [OTHER_SHA], [WHEEL_SHA, OTHER_SHA]):
        run_id = await pinned_fit(ctx, vid)
        run, att = await to_finalizing(ctx, run_id)
        await write_outputs(ctx, run, att, _fit_outputs(), overlay_hashes=claimed)
        assert await finalize(ctx, run, att) is RunState.FAILED, claimed
        async with ctx.db.session() as s:
            failed = await load_run(s, None, run_id)
        assert failed.problem is not None and failed.problem["code"] == "INTEGRITY_FAILURE"
        assert failed.output_model_version_id is None


async def test_the_comparison_ignores_order(ctx: ServerContext) -> None:
    _, vid = await seed_version(ctx, state="approved", overlay=True)
    _, other = await seed_version(
        ctx, state="approved", overlay=True, short="beta", wheel=b"another wheel"
    )
    run_id = await pinned_fit(ctx, vid)
    pins = [
        {"plugin_version_id": vid, "namespace": NS, "sha256": WHEEL_SHA},
        {"plugin_version_id": other, "namespace": "t_abcdef_beta", "sha256": OTHER_SHA},
    ]
    async with ctx.db.session() as s:
        await s.execute(update(m.Run).where(m.Run.id == run_id).values(overlays=pins))
    run, att = await to_finalizing(ctx, run_id)
    await write_outputs(ctx, run, att, _fit_outputs(), overlay_hashes=[OTHER_SHA, WHEEL_SHA])
    assert await finalize(ctx, run, att) is RunState.SUCCEEDED
    mv = await _model_version(ctx, run_id)
    assert [p["plugin_version_id"] for p in mv.lineage["overlays"]] == [vid, other]


async def test_a_run_without_pins_may_not_claim_uploaded_code(ctx: ServerContext) -> None:
    run_id = await queued_fit_once(ctx)
    run, att = await to_finalizing(ctx, run_id)
    await write_outputs(ctx, run, att, _fit_outputs(), overlay_hashes=[WHEEL_SHA])
    assert await finalize(ctx, run, att) is RunState.FAILED


async def test_runs_without_pins_keep_their_manifests_unchanged(ctx: ServerContext) -> None:
    run_id = await queued_fit_once(ctx)
    run, att = await to_finalizing(ctx, run_id)
    await write_outputs(ctx, run, att, _fit_outputs())
    assert await finalize(ctx, run, att) is RunState.SUCCEEDED
    mv = await _model_version(ctx, run_id)
    assert "overlays" not in mv.lineage and "overlay_hashes" not in mv.manifest
    assert mv.lineage.get("overlay_hash") is None
