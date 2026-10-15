"""Tests for the security probe suite (plan §9, §13).

Marked ``sandbox``. They run the corpus in-process (the local-process / dev environment) and
assert the structure the suite guarantees: every probe produces a result, the expected-outcome
matrix is enforced correctly, platform probes are BLOCKED everywhere, and an enforcing
executor would fail on an ALLOWED sandbox probe.
"""

from __future__ import annotations

import json

import pytest
from mapi_security.matrix import acceptable, is_unexpected
from mapi_security.outcomes import Category, Outcome
from mapi_security.probes import ALL_PROBES
from mapi_security.report import build_report
from mapi_security.runner import evaluate, isolation_for, run_probes
from mapi_spec.executor import Isolation

pytestmark = pytest.mark.sandbox


def test_every_probe_runs_and_classifies() -> None:
    results = run_probes()
    assert len(results) == len(ALL_PROBES)
    for r in results:
        assert r.outcome in set(Outcome)
        assert r.detail
        # the suite's own probes must never error out on a dev host
        assert r.outcome is not Outcome.ERROR, (r.probe_id, r.detail)


def test_platform_probes_blocked_at_every_level() -> None:
    results = {r.probe_id: r for r in run_probes()}
    platform = [p for p in ALL_PROBES if p.category is Category.PLATFORM]
    assert platform, "expected platform probes in the corpus"
    for probe in platform:
        got = results[probe.probe_id].outcome
        assert got is Outcome.BLOCKED, f"{probe.probe_id} was {got}, platform defences must block"
        for iso in (Isolation.PROCESS, Isolation.GVISOR, Isolation.MICROVM):
            assert acceptable(probe.probe_id, Category.PLATFORM, iso) == frozenset(
                {Outcome.BLOCKED, Outcome.SKIPPED}
            )


def test_local_process_report_passes() -> None:
    report = build_report("local-process", isolation_for("local-process"), run_probes())
    assert report.ok, [r.probe_id for r in report.unexpected]
    # the corpus names every §13 attack
    ids = {r.probe_id for r in report.rows}
    for required in (
        "metadata_token",
        "http_egress",
        "dns_egress",
        "write_outside_scratch",
        "reuse_foreign_url",
        "read_proc1_environ",
        "read_supervisor_mem",
        "pth_injection",
        "fork_bomb",
        "memory_bomb",
        "disk_bomb",
        "log_flood",
        "forged_stdout_event",
        "tampered_manifest",
        "redos_schema",
        "plot_xss_exfil",
    ):
        assert required in ids, f"missing probe {required}"


def test_enforcing_executor_flags_an_allowed_sandbox_probe() -> None:
    # On gVisor/microVM an ALLOWED isolation probe is a finding; a BLOCKED one is fine.
    assert is_unexpected("pth_injection", Category.ISOLATION, Isolation.GVISOR, Outcome.ALLOWED)
    assert not is_unexpected("pth_injection", Category.ISOLATION, Isolation.GVISOR, Outcome.BLOCKED)
    # At PROCESS (dev) an ALLOWED filesystem probe is expected, not a finding.
    assert not is_unexpected(
        "pth_injection", Category.ISOLATION, Isolation.PROCESS, Outcome.ALLOWED
    )


def test_report_json_and_metrics_round_trip(tmp_path) -> None:
    report = build_report("docker-gvisor", Isolation.GVISOR, run_probes())
    jpath = tmp_path / "report.json"
    mpath = tmp_path / "metrics.prom"
    report.write_json(jpath)
    report.write_metrics(mpath)
    doc = json.loads(jpath.read_text())
    assert doc["executor"] == "docker-gvisor" and "rows" in doc
    metrics = mpath.read_text()
    assert "mapi_security_probe_unexpected" in metrics
    assert 'executor="docker-gvisor"' in metrics


def test_docker_executors_skip_isolation_without_a_daemon() -> None:
    # No Docker daemon here: isolation probes must come back SKIPPED (never ERROR), and the
    # platform probes still run and block. So the suite is usable on a laptop.
    results = {r.probe_id: r for r in evaluate("docker-gvisor")}
    iso = [r for r in results.values() if r.category is Category.ISOLATION]
    assert iso and all(r.outcome in (Outcome.SKIPPED, Outcome.BLOCKED) for r in iso)
    assert results["forged_stdout_event"].outcome is Outcome.BLOCKED


def test_malicious_plugin_runs_probes_but_is_not_registered() -> None:
    import importlib.metadata as md

    import mapi_security_plugin as plugin

    assert [m.id for m in plugin.PLUGINS] == ["security.evil"]
    out = plugin.EvilModel().fit(
        None,
        plugin.model.EvilSettings(),
        None,
        type("C", (), {"info": staticmethod(lambda m: None)})(),
    )
    assert set(out.learned["probe_outcomes"]) >= {"fork_bomb", "pth_injection"}
    # it must NOT be discoverable through the mapi.models entry point
    registered = {ep.value for ep in md.entry_points(group="mapi.models")}
    assert not any("mapi_security_plugin" in v for v in registered)
