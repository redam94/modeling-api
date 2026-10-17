"""FINALIZING: metadata-only verification, server-side copy, signed manifests, URL minting."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer, build_scorecard, dataset_sha
from mapi_server.services.runs import apply_transition, load_run
from mapi_server_testkit.builders import (
    CATALOG,
    TREND_SPEC,
    queued_fit,
    ready_dataset,
    simulate_outputs,
)
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_spec.api import ModelVersion
from mapi_spec.run import Actor, BundleManifest, RunState, RunTrigger, new_hmac_key
from mapi_spec.storage import bundle_prefix
from sqlalchemy import select


async def _finalizing(ctx: ServerContext, run_id: str | None = None) -> tuple[m.Run, m.RunAttempt]:
    run_id = run_id or await queued_fit(ctx)
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        att = await s.get(m.RunAttempt, (run_id, run.attempt))
        assert att is not None
        s.expunge_all()
    return run, att


async def _reload(ctx: ServerContext, run_id: str) -> m.Run:
    async with ctx.db.session() as s:
        return await load_run(s, None, run_id)


async def test_fit_is_verified_copied_signed_and_registered(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    run, att = await _finalizing(ctx)
    await simulate_outputs(ctx, run, att)
    state = await Finalizer(ctx).finalize(run.id, att.attempt)
    assert state is RunState.SUCCEEDED
    done = await _reload(ctx, run.id)
    assert done.output_model_version_id and done.finished_at is not None
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, done.output_model_version_id)
        model = await s.get(m.Model, mv.model_id) if mv else None
        plots = list((await s.execute(select(m.Plot))).scalars())
        warnings = list(
            (await s.execute(select(m.RunEvent).where(m.RunEvent.type == "warning"))).scalars()
        )
    assert mv is not None and model is not None and model.name == "trend"
    manifest = BundleManifest.model_validate(mv.manifest)
    assert ctx.signer.verify(manifest)
    assert manifest.signature is not None and manifest.signature.alg == "KMS-EC-P256-SHA256"
    prefix = bundle_prefix("default", model.id, 1)
    assert {o.key for o in manifest.objects.values()} == {
        prefix + n
        for n in (
            "bundle.zarr.zip",
            "diagnostics.json",
            "metrics.json",
            "plots.json",
            "coords.json",
            "ui/summaries.json",
            "scorecard.json",
        )
    }
    stored = ctx.store.snapshot()  # type: ignore[attr-defined]
    for obj in manifest.objects.values():
        assert stored[(ctx.buckets.bundles, obj.key)].generation == obj.generation
    assert (ctx.buckets.bundles, prefix + "manifest.json") in stored
    assert (ctx.buckets.bundles, prefix + "manifest.sig") in stored
    # the unsafe plot was dropped with a warning, the safe one kept (sanitized)
    assert [p.plot_id for p in plots] == ["core.ppc_fan"]
    assert [w.payload["code"] for w in warnings] == ["OUTPUT_DROPPED"]
    # the API view
    r = await client.get(f"/v1/models/{model.id}/versions/1")
    assert r.status_code == 200
    view = ModelVersion.model_validate(r.json())
    assert view.status == "ready" and view.metrics == {"rmse": 0.25}
    assert view.scorecard is not None and view.scorecard.overall == "pass"
    assert view.lineage.plugin == "ts.hsgp_trend@1.0.0" and view.lineage.seed == 7
    assert view.lineage.dataset_sha is not None and view.capabilities is not None
    link = (await client.get(f"/v1/models/{model.id}/versions/1/artifacts/bundle.zarr.zip")).json()
    assert link["generation"] == manifest.objects["bundle.zarr.zip"].generation
    ui = await client.get(
        f"/v1/models/{model.id}/versions/1/ui/summaries.json", follow_redirects=False
    )
    assert ui.status_code == 307 and ui.headers["location"].startswith(ctx.store.base_url)  # type: ignore[attr-defined]
    plots_page = (await client.get(f"/v1/models/{model.id}/versions/1/plots")).json()
    assert [p["id"] for p in plots_page["items"]] == ["core.ppc_fan"]
    plot = (await client.get(plots_page["items"][0]["href"])).json()
    assert plot["kind"] == "vega-lite" and plot["spec"]["data"] == {"name": "fan"}
    assert (await client.get(f"/v1/models/{model.id}/versions/1/plots/evil")).status_code == 404
    models = (await client.get("/v1/models")).json()
    assert [x["latest_version"] for x in models["items"]] == [1]
    # finalizing again is a no-op (idempotent)
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is None


async def test_lineage_records_the_effective_seed_when_the_spec_omits_it(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    """The runner fits with seed 0 when ``model.fit.seed`` is omitted; lineage must say so."""
    run_id = await queued_fit(ctx, yaml=TREND_SPEC.replace("fit: {seed: 7}", "fit: {}"))
    async with ctx.db.session() as s:
        queued = await load_run(s, None, run_id)
    assert queued.payload["model"]["fit"]["seed"] == 0  # pinned at submit, like `use:`
    run, att = await _finalizing(ctx, run_id)
    await simulate_outputs(ctx, run, att)
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    done = await _reload(ctx, run.id)
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, done.output_model_version_id)
    assert mv is not None and mv.lineage["seed"] == 0


async def test_lineage_records_the_side_table_versions_the_fit_read(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    run, att = await _finalizing(ctx)
    _, fx = await ready_dataset(ctx, "fx_daily")
    async with ctx.db.session() as s:
        row = await load_run(s, None, run.id, for_update=True)
        row.inputs = {**row.inputs, "side.fx": dict(fx.objects["data.zarr.zip"])}
    run = await _reload(ctx, run.id)  # the runner's manifest claims every pinned input
    await simulate_outputs(ctx, run, att)
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    done = await _reload(ctx, run.id)
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, done.output_model_version_id)
    assert mv is not None
    view = (await client.get(f"/v1/models/{mv.model_id}/versions/{mv.version}")).json()
    assert view["lineage"]["side_tables"] == {"fx": fx.sha256}


async def test_url_minting_refuses_a_tampered_manifest(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    run, att = await _finalizing(ctx)
    await simulate_outputs(ctx, run, att)
    await Finalizer(ctx).finalize(run.id, att.attempt)
    done = await _reload(ctx, run.id)
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, done.output_model_version_id)
        assert mv is not None
        model_id = mv.model_id
        forged = dict(mv.manifest)
        forged["tenant_id"] = "evil"
        mv.manifest = forged
    r = await client.get(f"/v1/models/{model_id}/versions/1/artifacts/bundle.zarr.zip")
    assert r.status_code == 500 and r.json()["code"] == "INTEGRITY_FAILURE"
    r = await client.get(f"/v1/models/{model_id}/versions/1/plots/core.ppc_fan")
    assert r.status_code == 500


async def test_overwritten_bundle_object_is_detected_at_minting(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    run, att = await _finalizing(ctx)
    await simulate_outputs(ctx, run, att)
    await Finalizer(ctx).finalize(run.id, att.attempt)
    done = await _reload(ctx, run.id)
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, done.output_model_version_id)
        assert mv is not None
    obj = BundleManifest.model_validate(mv.manifest).objects["bundle.zarr.zip"]
    await ctx.store.delete(obj.bucket, obj.key, generation=obj.generation)
    r = await client.get(f"/v1/models/{mv.model_id}/versions/1/artifacts/bundle.zarr.zip")
    assert r.status_code == 500 and r.json()["code"] == "INTEGRITY_FAILURE"


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"corrupt": "bundle.zarr.zip"}, "failed verification"),
        ({"extra": {"sneaky.bin": b"x"}}, "unexpected object"),
        ({"sign_key": new_hmac_key()}, "HMAC"),
    ],
)
async def test_integrity_failures(
    ctx: ServerContext, kwargs: dict[str, object], fragment: str
) -> None:
    run, att = await _finalizing(ctx)
    await simulate_outputs(ctx, run, att, **kwargs)  # type: ignore[arg-type]
    state = await Finalizer(ctx).finalize(run.id, att.attempt)
    assert state is RunState.FAILED
    failed = await _reload(ctx, run.id)
    assert failed.failure_class == "integrity"
    assert failed.problem is not None and failed.problem["code"] == "INTEGRITY_FAILURE"
    text = failed.problem["detail"] + json.dumps(failed.problem.get("errors", []))
    assert fragment in text
    async with ctx.db.session() as s:
        assert (await s.execute(select(m.ModelVersion))).first() is None
        errors = list(
            (await s.execute(select(m.RunEvent).where(m.RunEvent.type == "error"))).scalars()
        )
    assert errors and errors[0].payload["failure_class"] == "integrity"


@pytest.mark.parametrize("name", ["diagnostics.json", "metrics.json", "plots.json", "coords.json"])
async def test_deeply_nested_sidecar_is_an_integrity_failure(ctx: ServerContext, name: str) -> None:
    """Hostile nesting must not escape as RecursionError (retried, no security alert)."""
    run, att = await _finalizing(ctx)
    await simulate_outputs(ctx, run, att, replace={name: b"[" * 100_000 + b"]" * 100_000})
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.FAILED
    failed = await _reload(ctx, run.id)
    assert failed.failure_class == "integrity"
    assert failed.problem is not None and "nest" in failed.problem["detail"]


async def test_missing_manifest_and_missing_complete_event(ctx: ServerContext) -> None:
    run, att = await _finalizing(ctx)
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.FAILED
    assert "manifest.json does not exist" in (await _reload(ctx, run.id)).problem["detail"]  # type: ignore[index]


async def test_manifest_changed_after_complete_event(ctx: ServerContext) -> None:
    run, att = await _finalizing(ctx)
    await simulate_outputs(ctx, run, att)
    async with ctx.db.session() as s:
        row = await s.get(m.RunAttempt, (run.id, att.attempt))
        assert row is not None and row.complete_payload is not None
        row.complete_payload = {**row.complete_payload, "manifest_crc32c": "AAAAAA=="}
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.FAILED


async def test_partial_fit_without_bundle(ctx: ServerContext) -> None:
    run, att = await _finalizing(ctx)
    await simulate_outputs(ctx, run, att, mode="partial")
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.PARTIAL
    assert (await _reload(ctx, run.id)).output_model_version_id is None


async def test_finalize_gives_up_after_max_attempts(ctx: ServerContext) -> None:
    run, att = await _finalizing(ctx)
    ctx.settings.finalize_max_attempts = 0
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.FAILED
    assert (await _reload(ctx, run.id)).failure_class == "infra"


async def test_describe_run_registers_the_catalog(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mapi_server.services.submit import submit_describe

    image = "sha256:" + "cd" * 32
    ctx.catalogs.image_digest = image
    ctx.catalogs.invalidate()
    async with ctx.db.session() as s:
        run = await submit_describe(s, ctx)
        assert run is not None
        assert await submit_describe(s, ctx) is None  # one pending describe run at a time
        run_id = run.id
    monkeypatch.setenv("FAKE_RUNNER_CATALOG", str(CATALOG))
    run, att = await _finalizing(ctx, run_id)
    await simulate_outputs(ctx, run, att)
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    async with ctx.db.session() as s:
        snap = await ctx.catalogs.get(s)
    assert snap is not None and snap.source == "describe" and snap.image_digest == image


async def test_ingest_run_makes_the_dataset_version_ready(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ds = (await client.post("/v1/datasets", json={"name": "co2"})).json()
    up = (
        await client.post(
            f"/v1/datasets/{ds['id']}/uploads",
            json={"filename": "co2.csv", "content_type": "text/csv", "size_bytes": 100},
        )
    ).json()
    await ctx.store.put(ctx.buckets.quarantine, up["upload"]["name"], os.urandom(50))
    mapping = {
        "format": "csv",
        "dims": {"t": {"column": "date", "spec": {"kind": "time", "policy": "extend"}}},
        "vars": {"y": {"column": "co2", "role": "target"}},
    }
    dv = (
        await client.post(
            f"/v1/datasets/{ds['id']}/versions", json={"upload_id": up["id"], "mapping": mapping}
        )
    ).json()
    run, att = await _finalizing(ctx, dv["ingest_run_id"])
    await simulate_outputs(ctx, run, att)
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    got = (await client.get(f"/v1/datasets/{ds['id']}/versions/1")).json()
    assert got["status"] == "ready" and got["sha256"].startswith("sha256:")
    assert got["data_schema"]["vars"]["y"]["role"] == "target"
    assert got["preview"]["name"] == "preview.json" and got["summary"]["vars"] == ["y"]
    assert (await client.get(f"/v1/datasets/{ds['id']}")).json()["latest_version"] == 1
    r = await client.get(f"/v1/datasets/{ds['id']}/versions/1/preview", follow_redirects=False)
    assert r.status_code == 307


async def _ingest_version(
    client: httpx.AsyncClient, ctx: ServerContext, ds_id: str
) -> dict[str, Any]:
    up = (
        await client.post(
            f"/v1/datasets/{ds_id}/uploads",
            json={"filename": "co2.csv", "content_type": "text/csv", "size_bytes": 100},
        )
    ).json()
    await ctx.store.put(ctx.buckets.quarantine, up["upload"]["name"], os.urandom(50))
    mapping = {
        "format": "csv",
        "dims": {"t": {"column": "date", "spec": {"kind": "time", "policy": "extend"}}},
        "vars": {"y": {"column": "co2", "role": "target"}},
    }
    created = await client.post(
        f"/v1/datasets/{ds_id}/versions", json={"upload_id": up["id"], "mapping": mapping}
    )
    return dict(created.json())


async def test_identical_data_uploaded_again_gets_its_own_explorer_views(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    """The ingest's data and sidecars live under the data's content hash. Uploading the same
    data as the next version must succeed although its explorer views (timestamped plots,
    an index) differ byte for byte: each ingest keeps its own views under its run. A sidecar
    that differs from the stored one is still an integrity failure."""
    ds = (await client.post("/v1/datasets", json={"name": "co2"})).json()
    data = {"data.zarr.zip": b"the same data" * 300}
    stored = []
    for i in (1, 2):
        dv = await _ingest_version(client, ctx, ds["id"])
        run, att = await _finalizing(ctx, dv["ingest_run_id"])
        index = json.dumps({"format": "mapi.explorer-index/v1", "run": i}).encode()
        await simulate_outputs(
            ctx, run, att, replace=data, add={"ui/explore/index.json": (index, "application/json")}
        )
        assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
        async with ctx.db.session() as s:
            row = await s.get(m.DatasetVersion, (ds["id"], i))
            assert row is not None and row.status == "ready"
            stored.append(row.objects)
            obj = row.objects["ui/explore/index.json"]
            assert obj["name"].endswith(f"/runs/{run.id}/ui/explore/index.json")
            body = await ctx.store.get(obj["bucket"], obj["name"], generation=obj["generation"])
            assert json.loads(body)["run"] == i  # this ingest's own view
        r = await client.get(
            f"/v1/datasets/{ds['id']}/versions/{i}/artifacts/ui/explore/index.json"
        )
        assert r.status_code == 200 and r.json()["name"] == "ui/explore/index.json"
    first, second = stored
    assert first["data.zarr.zip"] == second["data.zarr.zip"]  # the data is stored once
    dv = await _ingest_version(client, ctx, ds["id"])
    run, att = await _finalizing(ctx, dv["ingest_run_id"])
    await simulate_outputs(ctx, run, att, replace={**data, "summary.json": b'{"vars": ["z"]}'})
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is not RunState.SUCCEEDED


async def test_failed_ingest_marks_the_version_failed(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ds = (await client.post("/v1/datasets", json={"name": "co2"})).json()
    up = (
        await client.post(
            f"/v1/datasets/{ds['id']}/uploads",
            json={"filename": "co2.csv", "content_type": "text/csv", "size_bytes": 100},
        )
    ).json()
    await ctx.store.put(ctx.buckets.quarantine, up["upload"]["name"], b"x")
    mapping = {"format": "zarr_zip"}
    dv = (
        await client.post(
            f"/v1/datasets/{ds['id']}/versions", json={"upload_id": up["id"], "mapping": mapping}
        )
    ).json()
    run, _att = await _finalizing(ctx, dv["ingest_run_id"])
    async with ctx.db.session() as s:
        row = await load_run(s, None, run.id, for_update=True)
        await apply_transition(s, row, RunTrigger.FAIL_INTEGRITY, Actor.FINALIZER)
    got = (await client.get(f"/v1/datasets/{ds['id']}/versions/1")).json()
    assert got["status"] == "failed" and got["issues"][0]["code"] == "INTEGRITY_FAILURE"


def test_scorecard_and_dataset_sha() -> None:
    from mapi_spec.capabilities import Diagnostic

    assert build_scorecard([]) is None
    card = build_scorecard(
        [
            Diagnostic(code="RHAT_OK", severity="info", message_expert="e", message_plain="p"),
            Diagnostic(code="ESS_LOW", severity="warning", message_expert="e", message_plain="p"),
        ]
    )
    assert card is not None and card.overall == "warn"
    assert [i.status for i in card.items] == ["pass", "warn"]
    from datetime import UTC, datetime

    from mapi_spec.storage import ObjectMeta

    meta = ObjectMeta(
        bucket="mapi-local-runs",
        name="x",
        generation=1,
        size=3,
        crc32c="AAAAAA==",
        md5_hash="1B2M2Y8AsgTpgAmY7PhCfg==",
        time_created=datetime.now(UTC),
    )
    assert len(dataset_sha(meta)) == 64 and dataset_sha(meta) == dataset_sha(meta)


async def test_concurrent_finalization_of_one_model_does_not_deadlock(ctx: ServerContext) -> None:
    """Fits of the same spec finalize at once: versions 1..n, no deadlock (the model row is
    locked before a version row takes FOR KEY SHARE on it)."""
    from mapi_server_testkit.builders import submit_fit, trend_spec

    spec_id = await trend_spec(ctx)
    pairs = []
    for _ in range(4):
        pairs.append(await _finalizing(ctx, await submit_fit(ctx, spec_id)))
    for run, att in pairs:
        await simulate_outputs(ctx, run, att)
    finalizer = Finalizer(ctx)
    states = await asyncio.gather(*(finalizer.finalize(r.id, a.attempt) for r, a in pairs))
    assert states == [RunState.SUCCEEDED] * 4
    async with ctx.db.session() as s:
        versions = sorted((await s.execute(select(m.ModelVersion.version))).scalars())
    assert versions == [1, 2, 3, 4]


async def test_finalization_retry_reuses_the_stored_signed_manifest(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finalization that stops after storing manifest.json/.sig is retried idempotently
    (re-signing would produce a different signature and signed_at)."""
    run, att = await _finalizing(ctx)
    await simulate_outputs(ctx, run, att)
    finalizer = Finalizer(ctx)
    real_commit = finalizer._commit
    calls = {"n": 0}

    async def flaky_commit(*args: Any, **kwargs: Any) -> RunState | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("lost the database connection")
        return await real_commit(*args, **kwargs)

    monkeypatch.setattr(finalizer, "_commit", flaky_commit)
    with pytest.raises(ConnectionError):
        await finalizer.finalize(run.id, att.attempt)
    snapshot = ctx.store.snapshot()  # type: ignore[attr-defined]
    sig_keys = [k for k in snapshot if k[1].endswith("/manifest.sig")]
    assert len(sig_keys) == 1
    assert await finalizer.finalize(run.id, att.attempt) is RunState.SUCCEEDED
    done = await _reload(ctx, run.id)
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, done.output_model_version_id)
    assert mv is not None
    manifest = BundleManifest.model_validate(mv.manifest)
    assert ctx.signer.verify(manifest)
    stored = await ctx.store.get(*sig_keys[0])
    assert manifest.signature is not None
    assert manifest.signature.value == json.loads(stored)["value"]


async def test_a_different_stored_manifest_is_an_integrity_failure(ctx: ServerContext) -> None:
    run, att = await _finalizing(ctx)
    await simulate_outputs(ctx, run, att)
    finalizer = Finalizer(ctx)
    model_id, version, _ = await finalizer._reserve_model(run)
    forged = {"schema_version": "mapi.bundle-manifest/v1", "tenant_id": "evil"}
    await ctx.store.put(
        ctx.buckets.bundles,
        bundle_prefix(run.tenant_id, model_id, version) + "manifest.json",
        json.dumps(forged).encode(),
    )
    assert await finalizer.finalize(run.id, att.attempt) is RunState.FAILED
    done = await _reload(ctx, run.id)
    assert done.failure_class == "integrity"
