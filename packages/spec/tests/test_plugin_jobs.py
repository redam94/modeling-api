"""The two pipeline run kinds of an uploaded version (v0.19): jobs, outputs, storage keys."""

from __future__ import annotations

import pytest
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import PLUGIN_KINDS, RUN_KIND_MAX_LEN, RunKind
from mapi_spec.jobs import (
    PLUGIN_DESCRIBE_OUTPUT,
    PLUGIN_INPUT,
    PLUGIN_INSPECTION_OUTPUT,
    PluginDescribeJob,
    PluginInspectJob,
    parse_job,
)
from mapi_spec.plugins import ArchiveLimits
from mapi_spec.storage import (
    BucketPurpose,
    Buckets,
    plugin_key,
    plugin_quarantine_key,
    plugin_quarantine_prefix,
)

pytestmark = pytest.mark.unit

NS = "t_k3vq7m_acme"
PLV = "plv_01jbz3q2x8m9v7k5h4g3f2e1d1"
SHA = "sha256:" + "ab" * 32

INSPECT = {
    "kind": "plugin_inspect",
    "plugin_version_id": PLV,
    "namespace": NS,
    "dist_name": "t-k3vq7m-acme",
    "version": "0.1.0",
    "expected_sha256": SHA,
}
DESCRIBE = {"kind": "plugin_describe", "plugin_version_id": PLV, "namespace": NS,
            "overlay_sha256": SHA}  # fmt: skip


def test_plugin_run_kinds() -> None:
    assert RunKind.PLUGIN_INSPECT.value == "plugin_inspect"
    assert RunKind.PLUGIN_DESCRIBE.value == "plugin_describe"
    assert frozenset({RunKind.PLUGIN_INSPECT, RunKind.PLUGIN_DESCRIBE}) == PLUGIN_KINDS
    for kind in PLUGIN_KINDS:
        assert len(kind.value) <= RUN_KIND_MAX_LEN
        assert not kind.is_parent


def test_parse_job_branches() -> None:
    job = parse_job(RunKind.PLUGIN_INSPECT, dict(INSPECT))
    assert isinstance(job, PluginInspectJob)
    assert job.limits == ArchiveLimits()
    assert job.limits.max_bytes == 20_971_520 and job.limits.max_members == 2000
    d = parse_job(RunKind.PLUGIN_DESCRIBE, dict(DESCRIBE))
    assert isinstance(d, PluginDescribeJob)
    assert d.data_kits and d.data_check_timeout_s == 120
    custom = parse_job(RunKind.PLUGIN_INSPECT, {**INSPECT, "limits": {"max_members": 10}})
    assert isinstance(custom, PluginInspectJob) and custom.limits.max_members == 10


@pytest.mark.parametrize(
    ("kind", "spec"),
    [
        (RunKind.PLUGIN_INSPECT, {**INSPECT, "dist_name": "acme"}),  # not the namespace's
        (RunKind.PLUGIN_INSPECT, {**INSPECT, "namespace": "acme", "dist_name": "acme"}),
        (RunKind.PLUGIN_INSPECT, {**INSPECT, "expected_sha256": "abc"}),
        (RunKind.PLUGIN_INSPECT, {**INSPECT, "extra": 1}),
        (RunKind.PLUGIN_INSPECT, {**INSPECT, "limits": {"max_members": 0}}),
        (RunKind.PLUGIN_DESCRIBE, {**DESCRIBE, "plugin_version_id": "run_01"}),
        (RunKind.PLUGIN_DESCRIBE, {**DESCRIBE, "data_check_timeout_s": 0}),
        (RunKind.PLUGIN_DESCRIBE, {**DESCRIBE, "kind": "plugin_inspect"}),
    ],
)
def test_parse_job_refuses_bad_specs(kind: RunKind, spec: dict[str, object]) -> None:
    with pytest.raises(MapiError) as exc:
        parse_job(kind, spec)
    assert exc.value.code is ErrorCode.SCHEMA_INVALID


def test_io_names() -> None:
    assert PLUGIN_INPUT == "plugin.whl"
    assert PLUGIN_INSPECTION_OUTPUT == "plugin/inspection.json"
    assert PLUGIN_DESCRIBE_OUTPUT == "plugin/describe.json"


def test_plugin_storage_keys_and_bucket() -> None:
    assert BucketPurpose.PLUGINS.value == "plugins"
    b = Buckets("local")
    assert b.plugins == "mapi-local-plugins"
    assert b.all()[BucketPurpose.PLUGINS] == "mapi-local-plugins"
    assert len(b.all()) == 6
    upload = "pup_01jbz3q2x8m9v7k5h4g3f2e1d2"
    assert plugin_quarantine_prefix("acme", upload) == f"t/acme/plugins/{upload}/"
    assert plugin_quarantine_key("acme", upload) == f"t/acme/plugins/{upload}/plugin.whl"
    assert plugin_key("acme", "plg_01jbz3q2x8m9v7k5h4g3f2e1d0", PLV) == (
        f"t/acme/plugins/plg_01jbz3q2x8m9v7k5h4g3f2e1d0/{PLV}.whl"
    )
    for bad in ("../x", "a/b", ""):
        with pytest.raises(ValueError):
            plugin_quarantine_key("acme", bad)
        with pytest.raises(ValueError):
            plugin_key(bad, "plg_x", PLV)
