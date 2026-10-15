"""The ops/ dashboards, alert rules and runbooks are well-formed and consistent.

Guards against the dashboards/alerts drifting from the metric names the server actually
exposes at /metrics, and against runbook links that point nowhere.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "ops"

# Series produced outside the server process (security suite Pushgateway, Cloud Run native).
_EXTERNAL_METRICS = {"mapi_security_probe_unexpected"}


def _server_metric_names() -> set[str]:
    from mapi_server.observability import metrics as obs_metrics

    m = obs_metrics.Metrics()
    # touch a few label sets so every family appears in the exposition
    m.runs_total.labels(kind="fit", state="succeeded").inc()
    m.runs_active.labels(state="running").set(0)
    m.run_transitions_total.labels(state="queued").inc()
    m.events_ingested_total.labels(result="accepted").inc()
    m.finalize_outcomes_total.labels(kind="fit", outcome="succeeded").inc()
    m.integrity_failures_total.labels(tenant_id="t").inc()
    m.sandbox_violations_total.labels(reason="bad_hmac").inc()
    m.quota_rejections_total.labels(scope="tenant", code="QUOTA_EXCEEDED").inc()
    m.cost_micros_total.labels(tenant_id="t").inc()
    m.http_requests_total.labels(method="GET", route="/x", status="200").inc()
    m.http_request_duration.labels(method="GET", route="/x").observe(0.1)
    names: set[str] = set()
    for line in m.render().decode().splitlines():
        if line.startswith("# HELP "):
            names.add(line.split()[2])
    return names


def _referenced_metrics(exprs: list[str]) -> set[str]:
    """Bare ``mapi_metric_*`` names in PromQL expressions, stripping histogram suffixes.

    Only real metric series are matched (``mapi_<word>_<word>...``); single-segment tokens
    like ``mapi_server`` or ``mapi_security`` that appear in prose are not metric names.
    """
    found = set()
    for text in exprs:
        for raw in re.findall(r"\bmapi_[a-z0-9]+(?:_[a-z0-9]+)+\b", text):
            found.add(re.sub(r"_(bucket|count|sum)$", "", raw))
    return found


def _promql_exprs() -> list[str]:
    """Every PromQL string referenced by the alert rules and both dashboards."""
    import yaml

    exprs: list[str] = []
    rules = yaml.safe_load((OPS / "alerts" / "prometheus-rules.yml").read_text())
    for group in rules["groups"]:
        for rule in group["rules"]:
            exprs.append(str(rule.get("expr", "")))
    cm_alerts = [
        p for p in yaml.safe_load_all((OPS / "alerts" / "cloud-monitoring-alerts.yaml").read_text())
        if p
    ]
    for policy in cm_alerts:
        for cond in policy["conditions"]:
            pql = cond.get("conditionPrometheusQueryLanguage")
            if pql:
                exprs.append(str(pql["query"]))
    grafana = json.loads((OPS / "dashboards" / "grafana-mapi-overview.json").read_text())
    for panel in grafana["panels"]:
        for t in panel.get("targets", []):
            exprs.append(str(t.get("expr", "")))
    cm = json.loads((OPS / "dashboards" / "cloud-monitoring-mapi-overview.json").read_text())
    for tile in cm["mosaicLayout"]["tiles"]:
        widget = tile["widget"]
        chart = widget.get("xyChart")
        if chart:
            for dataset in chart["dataSets"]:
                exprs.append(dataset["timeSeriesQuery"]["prometheusQuery"])
        sc = widget.get("scorecard")
        if sc:
            exprs.append(sc["timeSeriesQuery"]["prometheusQuery"])
    return exprs


def test_grafana_dashboard_is_valid_json() -> None:
    doc = json.loads((OPS / "dashboards" / "grafana-mapi-overview.json").read_text())
    assert doc["title"] == "modeling-api — control plane"
    assert doc["panels"], "dashboard has no panels"
    assert any(p.get("type") == "timeseries" for p in doc["panels"])


def test_cloud_monitoring_dashboard_is_valid_json() -> None:
    doc = json.loads((OPS / "dashboards" / "cloud-monitoring-mapi-overview.json").read_text())
    assert doc["displayName"]
    assert doc["mosaicLayout"]["tiles"], "dashboard has no tiles"


def test_alert_and_dashboard_metrics_exist_in_the_exposition() -> None:
    pytest.importorskip("yaml")
    server = _server_metric_names()
    assert "mapi_runs_total" in server  # sanity: the helper found families
    referenced = _referenced_metrics(_promql_exprs())
    assert referenced, "no metric references were extracted"
    unknown = referenced - server - _EXTERNAL_METRICS
    assert not unknown, f"ops files reference metrics the server does not export: {sorted(unknown)}"


def test_prometheus_rules_parse_as_yaml() -> None:
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load((OPS / "alerts" / "prometheus-rules.yml").read_text())
    groups = {g["name"]: g for g in doc["groups"]}
    assert "mapi.security" in groups and "mapi.cost" in groups
    alerts = [r["alert"] for g in doc["groups"] for r in g["rules"] if "alert" in r]
    for required in (
        "MapiIntegrityFailure",
        "MapiSecurityProbeFailure",
        "MapiJobsRun429",
        "MapiGlobalSpendCap70",
        "MapiGlobalSpendCap90",
        "MapiStalledRateHigh",
        "MapiQuota70",
    ):
        assert required in alerts, f"missing required alert {required}"


def test_cloud_monitoring_alerts_parse_as_yaml() -> None:
    yaml = pytest.importorskip("yaml")
    text = (OPS / "alerts" / "cloud-monitoring-alerts.yaml").read_text()
    policies = [p for p in yaml.safe_load_all(text) if p]
    assert len(policies) >= 7
    for p in policies:
        assert p["displayName"].startswith("mapi —")
        assert p["conditions"], f"{p['displayName']} has no conditions"


def test_runbook_links_resolve() -> None:
    runbooks = OPS / "runbooks"
    files = {p.name for p in runbooks.glob("*.md")}
    assert {"incident-response.md", "sandbox-escape.md", "stuck-runs.md",
            "db-failover.md", "rotating-keys.md"} <= files
    # every ops/runbooks/<x>.md reference in the alert files points to a file that exists
    for blob in (OPS / "alerts").glob("*"):
        for ref in re.findall(r"ops/runbooks/([a-z-]+\.md)", blob.read_text()):
            assert ref in files, f"{blob.name} links missing runbook {ref}"
    # intra-runbook relative links resolve too
    for rb in runbooks.glob("*.md"):
        for ref in re.findall(r"\]\((?!https?:)([a-z0-9-]+\.md)[)#]", rb.read_text()):
            assert (runbooks / ref).exists(), f"{rb.name} links missing {ref}"
