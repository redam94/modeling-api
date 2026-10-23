"""``plugin_describe`` end to end (v0.19, D8, D17): the real supervisor fetches and verifies
the uploaded wheel, mounts it read-only, and the worker exports only that upload's entries
with the kit report and the recipe examples.

The upload is the ``mapi plugin init`` scaffold (model, step, recipe, node, template) renamed
into ``t_k3vq7m_acme`` (:func:`test_worker_overlay.build_upload`). It is loaded only by the
worker process the supervisor starts, never by this test process.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from mapi_runner.plugin_kits import KitTimeout, _limited, time_limit
from mapi_spec.executor import RunKind
from mapi_spec.jobs import PLUGIN_DESCRIBE_OUTPUT, PluginDescribeJob
from mapi_spec.plugins import DESCRIBE_MAX_BYTES, OverlayPin, PluginDescribe, check_describe
from mapi_spec.run import OverlayMount, RunnerExit
from runner_helpers import Result, make_run, run_supervisor
from shimgcs import ShimGcs
from test_worker_overlay import NS, PVID, build_upload, needs_upload_nodes, sha256_of

pytestmark = pytest.mark.integration


def describe_run(
    gcs: ShimGcs, wheel: Path, *, pin_sha: str | None = None, data_kits: bool = True
) -> Result:
    data = wheel.read_bytes()
    sha = sha256_of(wheel)
    name = f"t/default/plugins/plg_acme/{PVID}.whl"
    gcs.put(gcs.buckets.data, name, data, "application/zip")
    job = PluginDescribeJob(
        plugin_version_id=PVID, namespace=NS, overlay_sha256=sha, data_kits=data_kits
    )
    run = make_run(gcs, RunKind.PLUGIN_DESCRIBE, job.model_dump(mode="json"))
    mount = OverlayMount(
        pin=OverlayPin(plugin_version_id=PVID, namespace=NS, sha256=pin_sha or sha),
        get=gcs.sign_get(gcs.buckets.data, name),
        size=len(data),
    )
    run.rs = run.rs.model_copy(update={"overlays": [mount]})
    gcs.put(
        gcs.buckets.bootstrap, run.rs.nonce, run.rs.model_dump_json().encode(), "application/json"
    )
    return run_supervisor(run)


@pytest.fixture(scope="module")
def described(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    tmp = tmp_path_factory.mktemp("describe")
    gcs = ShimGcs(tmp / "gcs").start()
    try:
        wheel = build_upload(tmp)
        result = describe_run(gcs, wheel)
        errors = [e.payload for e in result.of("error")]
        assert result.code == RunnerExit.OK, (errors, result.stderr[-6000:])
        raw = result.output(PLUGIN_DESCRIBE_OUTPUT)
        return {
            "raw": raw,
            "describe": PluginDescribe.model_validate_json(raw),
            "manifest": result.manifest(),
            "sha": sha256_of(wheel),
        }
    finally:
        gcs.stop()


def test_the_manifest_lists_the_verified_overlay(described: dict[str, Any]) -> None:
    m = described["manifest"]
    assert m.overlay_hashes == [described["sha"]]
    assert m.overlay_hash is None


def test_describe_exports_only_the_upload(described: dict[str, Any]) -> None:
    d: PluginDescribe = described["describe"]
    ex = d.exports
    assert [m.id for m in ex.models] == [f"{NS}.group_mean"]
    assert [s.id for s in ex.steps] == [f"{NS}.winsorize"]
    assert [p.id for p in ex.plots] == [f"{NS}.weekly_total"]
    for entry in (*ex.models, *ex.steps, *ex.plots, *ex.workflow_templates):
        assert entry.provider == f"upload:{PVID}"
        assert entry.overlay_hash == described["sha"]
    assert d.plugin_version_id == PVID and d.overlay_sha256 == described["sha"]
    assert d.base_image_digest.startswith("sha256:")


def test_the_kit_report_is_static_pass_and_data_pass_or_skipped(
    described: dict[str, Any],
) -> None:
    kit = described["describe"].kit
    failed = [(c.kind, c.entry_id, c.check, c.message) for c in kit.checks if c.status == "fail"]
    node_rule = [f for f in failed if f[0] in ("node", "template") or "load." in f[2]]
    assert failed == node_rule  # only the (pending) node/template rule may fail here
    by = {(c.kind, c.check): c.status for c in kit.checks}
    assert by[("package", "exports")] == "pass"
    for kind in ("model", "step", "plot"):
        assert by[(kind, "namespace")] == "pass"
        assert by[(kind, "about")] == "pass"
    assert by[("plot", "example_renders")] == "pass"
    data = [c for c in kit.checks if c.check.startswith("data")]
    assert any(c.kind == "model" and c.status == "pass" for c in data)
    assert any(c.kind == "step" for c in data)
    assert all(c.status in ("pass", "skipped") for c in data if c.kind in ("model", "step"))


def test_the_recipe_example_is_rendered(described: dict[str, Any]) -> None:
    examples = described["describe"].plot_examples
    assert list(examples) == [f"{NS}.weekly_total"]
    assert examples[f"{NS}.weekly_total"]["id"] == f"{NS}.weekly_total"


def test_the_describe_result_is_small(described: dict[str, Any]) -> None:
    assert len(described["raw"]) <= DESCRIBE_MAX_BYTES


def test_the_control_plane_check_finds_nothing_but_the_node_rule(
    described: dict[str, Any],
) -> None:
    issues = check_describe(described["describe"], namespace=NS, builtin_titles=["Bayesian MMM"])
    assert {i.context.get("check") for i in issues} <= {"node_contract", "namespace"}


@needs_upload_nodes
def test_nodes_and_templates_are_described_and_pass(described: dict[str, Any]) -> None:
    d: PluginDescribe = described["describe"]
    assert [n.id for n in d.exports.workflow_nodes] == [f"test.{NS}_zero_share"]
    assert [t.id for t in d.exports.workflow_templates] == [f"{NS}_weekly_check"]
    assert d.kit.ok, [c for c in d.kit.checks if c.status == "fail"]
    assert check_describe(d, namespace=NS, builtin_titles=["Bayesian MMM"]) == []


def test_a_wheel_that_does_not_match_its_pin_never_runs(gcs: ShimGcs, tmp_path: Path) -> None:
    wheel = build_upload(tmp_path)
    result = describe_run(gcs, wheel, pin_sha="sha256:" + "ab" * 32)
    assert result.code == RunnerExit.INTEGRITY_ERROR
    assert result.of("error")[-1].payload["code"] == "INTEGRITY_FAILURE"
    states = [str(e.payload.get("message") or "") for e in result.of("state")]
    assert "mounting uploaded code" in states
    assert not any("static checks" in m for m in states)  # the worker never started


# ================================================================================ time limits


def test_a_data_check_over_its_time_limit_fails_as_too_long() -> None:
    def slow() -> list[Any]:
        time.sleep(5)
        return []

    started = time.monotonic()
    checks = _limited("step", f"{NS}.slow", 0.3, slow)
    assert time.monotonic() - started < 3
    assert [(c.status, c.check) for c in checks] == [("fail", "data")]
    assert "took too long" in checks[0].message


def test_the_time_limit_cannot_be_swallowed_by_except_exception() -> None:
    def stubborn() -> None:
        try:
            time.sleep(5)
        except Exception:  # plugin code that catches everything it can
            time.sleep(5)

    with pytest.raises(KitTimeout), time_limit(0.2):
        stubborn()
