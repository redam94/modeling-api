"""v0.20 WP3: the chat and MCP settings (plan I-7): defaults, the provider rules, the secret."""

from __future__ import annotations

from typing import Any

import pytest
from mapi_server.settings import (
    GEMINI_DEFAULT_ENDPOINT,
    ExecutorName,
    ObjectStoreKind,
    Profile,
    Settings,
)
from pydantic import SecretStr, ValidationError

KEY = "AIzaTEST-settings-key-41d0"


@pytest.fixture(autouse=True)
def _no_cloud_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("K_SERVICE", "CLOUD_RUN_JOB", "MAPI_CHAT_PROVIDER", "MAPI_GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def multi_tenant(**kwargs: Any) -> Settings:
    return Settings(profile=Profile.MULTI_TENANT, trusted_proxies="none", **kwargs)


def test_defaults() -> None:
    s = Settings()
    assert s.chat_provider == "off"
    assert s.gemini_api_key is None
    assert s.gemini_endpoint == GEMINI_DEFAULT_ENDPOINT
    assert s.chat_model == "gemini-3.1-flash-lite"
    assert s.chat_thinking_level == "omit"  # probe (c): the generateContent field is unconfirmed
    assert s.chat_function_mode == "VALIDATED"
    assert (s.chat_max_output_tokens, s.chat_temperature) == (1024, 0.2)
    assert (s.chat_request_timeout_s, s.chat_turn_deadline_s) == (20.0, 45.0)
    assert s.chat_allowed_residencies == ["us"]
    assert (s.chat_price_in_micros_per_mtok, s.chat_price_out_micros_per_mtok) == (
        250_000,
        1_500_000,
    )
    assert s.chat_price_version == "gemini-3.1-flash-lite@2026-10-22"
    assert (
        s.chat_member_turns_per_minute,
        s.chat_share_turns_per_minute,
        s.chat_share_ip_turns_per_minute,
    ) == (10, 6, 12)
    assert s.chat_transcript_ttl_days == 30
    assert s.mcp_enabled is True
    assert s.mcp_allowed_origins == []
    assert s.mcp_rate_per_minute == 120
    assert (s.dashboard_read_token_max_days, s.dashboard_read_tokens_per_user) == (90, 5)


def test_key_is_read_from_the_environment_as_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPI_CHAT_PROVIDER", "gemini")
    monkeypatch.setenv("MAPI_GEMINI_API_KEY", KEY)
    s = Settings()
    assert isinstance(s.gemini_api_key, SecretStr)
    assert s.gemini_api_key.get_secret_value() == KEY


def test_settings_repr_no_key() -> None:
    s = Settings(chat_provider="gemini", gemini_api_key=KEY)
    for text in (
        repr(s),
        str(s),
        s.model_dump_json(),
        str(s.model_dump()),
        str(s.model_dump(mode="json")),
        repr(s.gemini_api_key),
    ):
        assert KEY not in text
    assert "gemini_api_key" not in repr(s)


def test_gemini_needs_a_key() -> None:
    with pytest.raises(ValidationError, match="MAPI_GEMINI_API_KEY"):
        Settings(chat_provider="gemini")
    with pytest.raises(ValidationError, match="MAPI_GEMINI_API_KEY"):
        Settings(chat_provider="gemini", gemini_api_key="   ")
    assert Settings(chat_provider="gemini", gemini_api_key=KEY).chat_provider == "gemini"


def test_fake_is_allowed_locally() -> None:
    assert Settings(chat_provider="fake").chat_provider == "fake"
    assert Settings(chat_provider="fake", profile=Profile.DEV).chat_provider == "fake"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"profile": Profile.MULTI_TENANT, "trusted_proxies": "none"}, "multi-tenant"),
        ({"executor": ExecutorName.CLOUDRUN_JOBS}, "cloudrun-jobs"),
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
def test_fake_is_refused_in_cloud_deployments(kwargs: dict[str, Any], reason: str) -> None:
    with pytest.raises(ValidationError, match=f"MAPI_CHAT_PROVIDER=fake.*{reason}"):
        Settings(chat_provider="fake", **kwargs)


@pytest.mark.parametrize("var", ["K_SERVICE", "CLOUD_RUN_JOB"])
def test_fake_is_refused_on_cloud_run(monkeypatch: pytest.MonkeyPatch, var: str) -> None:
    monkeypatch.setenv(var, "mapi-api")
    with pytest.raises(ValidationError, match=f"Cloud Run \\({var} is set\\)"):
        Settings(chat_provider="fake")
    assert Settings(chat_provider="off").chat_provider == "off"


def test_dev_untrusted_refusal_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # the shared helper keeps v0.19's refusal text
    monkeypatch.setenv("K_SERVICE", "mapi-api")
    with pytest.raises(ValidationError, match="MAPI_DEV_ALLOW_LOCAL_UNTRUSTED is for local"):
        Settings(dev_allow_local_untrusted=True)


def test_endpoint_override() -> None:
    local = Settings(gemini_endpoint="http://127.0.0.1:9999")
    assert local.gemini_endpoint == "http://127.0.0.1:9999"
    assert multi_tenant().gemini_endpoint == GEMINI_DEFAULT_ENDPOINT
    assert multi_tenant(gemini_endpoint=GEMINI_DEFAULT_ENDPOINT + "/").chat_provider == "off"
    with pytest.raises(ValidationError, match="multi-tenant profile allows only the default"):
        multi_tenant(gemini_endpoint="https://gemini-proxy.example.com")


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://generativelanguage.googleapis.com",
        "https://generativelanguage.googleapis.com/v1beta",
        "https://generativelanguage.googleapis.com?key=x",
        "https://user:pw@generativelanguage.googleapis.com",
        "https://",
        "generativelanguage.googleapis.com",
    ],
)
def test_endpoint_must_be_a_base_url(endpoint: str) -> None:
    with pytest.raises(ValidationError, match="MAPI_GEMINI_ENDPOINT must be a base URL"):
        Settings(gemini_endpoint=endpoint)


@pytest.mark.parametrize("model", ["Gemini", "-x", "a", "gemini/../x", "gemini 3", "g" * 64])
def test_model_id_pattern(model: str) -> None:
    with pytest.raises(ValidationError):
        Settings(chat_model=model)


def test_limits() -> None:
    with pytest.raises(ValidationError, match="must not exceed"):
        Settings(chat_request_timeout_s=50, chat_turn_deadline_s=45)
    with pytest.raises(ValidationError):
        Settings(dashboard_read_token_max_days=91)
    with pytest.raises(ValidationError):
        Settings(chat_temperature=2.5)
    with pytest.raises(ValidationError):
        Settings(chat_thinking_level="high")
    with pytest.raises(ValidationError):
        Settings(chat_function_mode="ANY")
    with pytest.raises(ValidationError):
        Settings(chat_provider="openai")


def test_residencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPI_CHAT_ALLOWED_RESIDENCIES", "us, eu,us-central1")
    s = Settings()
    assert s.chat_allowed_residencies == ["us", "eu", "us-central1"]
    assert s.chat_residency_allowed("us") and s.chat_residency_allowed("us-central1")
    monkeypatch.delenv("MAPI_CHAT_ALLOWED_RESIDENCIES")
    default = Settings()
    assert default.chat_residency_allowed("us")
    assert not default.chat_residency_allowed("eu")
    assert not default.chat_residency_allowed("us-east1")  # exact match only
    assert Settings(chat_allowed_residencies=[]).chat_residency_allowed("us") is False
    with pytest.raises(ValidationError, match="MAPI_CHAT_ALLOWED_RESIDENCIES"):
        Settings(chat_allowed_residencies=["US"])


def test_mcp_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPI_MCP_ALLOWED_ORIGINS", "https://app.example.com,http://127.0.0.1:6274")
    assert Settings().mcp_allowed_origins == ["https://app.example.com", "http://127.0.0.1:6274"]
    monkeypatch.delenv("MAPI_MCP_ALLOWED_ORIGINS")
    for bad in ("*", "https://app.example.com/", "app.example.com", "https://a.example?x=1"):
        with pytest.raises(ValidationError, match="MAPI_MCP_ALLOWED_ORIGINS"):
            Settings(mcp_allowed_origins=[bad])
