"""v0.19 WP6 (D7, T19.24): ``MAPI_DEV_ALLOW_LOCAL_UNTRUSTED`` is a development allowance.

It lets the local-process executor take uploaded code (no sandbox), and the settings refuse
it wherever other people's code or data could be reached: the multi-tenant profile, the
cloudrun-jobs executor, the gcs object store, or a server running on Cloud Run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mapi_server.executors import LocalProcessExecutor, build_executor
from mapi_server.settings import ExecutorName, ObjectStoreKind, Profile, Settings
from pydantic import ValidationError


def _settings(tmp_path: Path, **kw: object) -> Settings:
    return Settings(data_dir=tmp_path, exec_token="t", **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"profile": Profile.MULTI_TENANT, "trusted_proxies": "none"}, "multi-tenant"),
        (
            {"executor": ExecutorName.CLOUDRUN_JOBS, "cloudrun_project": "p"},
            "cloudrun-jobs",
        ),
        (
            {
                "object_store": ObjectStoreKind.GCS,
                "url_signer": "signer@p.iam.gserviceaccount.com",
                "url_uploader": "uploader@p.iam.gserviceaccount.com",
            },
            "gcs",
        ),
    ],
)
def test_the_flag_is_refused_outside_local_development(
    tmp_path: Path, kw: dict[str, object], reason: str
) -> None:
    _settings(tmp_path, **kw)  # fine without the flag
    with pytest.raises(ValidationError) as err:
        _settings(tmp_path, dev_allow_local_untrusted=True, **kw)
    text = str(err.value)
    assert "MAPI_DEV_ALLOW_LOCAL_UNTRUSTED" in text and reason in text


@pytest.mark.parametrize("var", ["K_SERVICE", "CLOUD_RUN_JOB"])
def test_the_flag_is_refused_on_cloud_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    monkeypatch.setenv(var, "mapi-api")
    with pytest.raises(ValidationError) as err:
        _settings(tmp_path, dev_allow_local_untrusted=True)
    assert var in str(err.value)


def test_the_flag_is_read_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAPI_DEV_ALLOW_LOCAL_UNTRUSTED", "1")
    s = _settings(tmp_path, profile=Profile.DEV)
    assert s.dev_allow_local_untrusted and s.dev_untrusted_code
    monkeypatch.setenv("MAPI_PROFILE", "multi-tenant")
    monkeypatch.setenv("MAPI_TRUSTED_PROXIES", "none")
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, exec_token="t")


def test_off_by_default_and_only_local_process_honours_it(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    assert not s.dev_allow_local_untrusted and not s.dev_untrusted_code
    docker = _settings(tmp_path, dev_allow_local_untrusted=True, executor=ExecutorName.DOCKER)
    assert not docker.dev_untrusted_code  # docker runs uploaded code anyway, in a container


def test_build_executor_passes_the_flag_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    plain = build_executor(_settings(tmp_path))
    assert isinstance(plain, LocalProcessExecutor) and not plain.caps().runs_untrusted_code
    with caplog.at_level("WARNING"):
        dev = build_executor(_settings(tmp_path, dev_allow_local_untrusted=True))
    caps = dev.caps()
    assert caps.runs_untrusted_code and caps.untrusted_dev_only
    assert any("MAPI_DEV_ALLOW_LOCAL_UNTRUSTED" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("kw", "warns"),
    [
        ({}, True),  # compose: the shim's namespace routes out
        ({"executor": ExecutorName.DOCKER_GVISOR}, True),  # gVisor does not stop egress
        ({"docker_network": "mapi-sandbox", "docker_network_isolated": True}, False),
        ({"plugins_enabled": False}, False),
    ],
)
def test_docker_with_uploads_on_a_shared_network_warns_about_egress(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, kw: dict[str, object], warns: bool
) -> None:
    """Live finding F1: uploaded code on the docker executor has the run network's internet
    egress unless the network is declared isolated; the server says so at start-up."""
    settings = _settings(
        tmp_path,
        **{"executor": ExecutorName.DOCKER, "docker_network": "container:mapi-gcs-shim", **kw},
    )
    with caplog.at_level("WARNING"):
        build_executor(settings)
    found = [
        r.getMessage() for r in caplog.records if "MAPI_DOCKER_NETWORK_ISOLATED" in r.getMessage()
    ]
    assert bool(found) is warns, found
    if warns:
        assert "reach the internet" in found[0] and "developer machine" in found[0]


def test_plugin_settings_defaults_match_the_plan(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    assert s.plugins_enabled
    assert (s.plugin_upload_max_bytes, s.plugin_uncompressed_max_bytes) == (20_971_520, 104_857_600)
    assert (s.plugin_member_max_bytes, s.plugin_max_members, s.plugin_max_ratio) == (
        20_971_520,
        2000,
        100,
    )
    assert s.plugin_uploads_per_user_per_day == 20
    assert s.plugin_pipeline_concurrency_per_tenant == 2
    assert (s.plugin_inspect_timeout_s, s.plugin_describe_timeout_s) == (60, 600)
    assert s.plugin_data_check_timeout_s == 120 and s.plugin_ticket_ttl_s == 900
    assert s.plugin_retention_days == 30 and s.plugin_approve_max_auth_age_s == 900
    assert s.plugin_approver_min_membership_age_days == 7
    assert s.estimate_wall_s["plugin_inspect"] == 15.0
    assert s.estimate_wall_s["plugin_describe"] == 60.0
    with pytest.raises(ValidationError):
        _settings(tmp_path, plugin_upload_max_bytes=20_971_521)  # never above the DB cap
