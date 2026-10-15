"""Configuration resolution: arguments > environment > config file profile > defaults."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from mapi_client import Client, ClientConfig, ConfigError, load_config
from mapi_client.config import DEFAULT_URL, config_path, is_loopback


def test_defaults_point_at_the_local_api() -> None:
    cfg = load_config()
    assert cfg.base_url == DEFAULT_URL == "http://127.0.0.1:8000"
    assert cfg.token is None
    assert cfg.sources["url"] == "default"


def test_environment_beats_file_and_args_beat_environment(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_config.write_text('url = "http://file.test"\n')
    assert load_config().base_url == "http://file.test"
    monkeypatch.setenv("MAPI_URL", "http://env.test/")
    cfg = load_config()
    assert cfg.base_url == "http://env.test"  # trailing slash trimmed
    assert cfg.sources["url"] == "env:MAPI_URL"
    assert load_config(url="http://arg.test").base_url == "http://arg.test"


def test_local_dev_variable_is_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPI_API_URL", "http://localdev.test")
    assert load_config().base_url == "http://localdev.test"
    monkeypatch.setenv("MAPI_URL", "http://primary.test")
    assert load_config().base_url == "http://primary.test"


def test_profiles(isolated_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolated_config.write_text(
        'profile = "a"\n'
        '[profiles.a]\nurl = "https://a.test"\ntimeout = 5\nmax_retries = 0\n'
        '[profiles.b]\nurl = "https://b.test"\ntoken = "tok-b"\n'
    )
    os.chmod(isolated_config, 0o600)
    a = load_config()
    assert (a.base_url, a.timeout, a.max_retries, a.profile) == ("https://a.test", 5.0, 0, "a")
    monkeypatch.setenv("MAPI_PROFILE", "b")
    b = load_config()
    assert b.base_url == "https://b.test" and b.token == "tok-b"
    assert "tok-b" not in repr(b)
    with pytest.raises(ConfigError, match="profile 'c' not found"):
        load_config(profile="c")


def test_invalid_files_are_rejected(isolated_config: Path) -> None:
    isolated_config.write_text("url = [\n")
    with pytest.raises(ConfigError, match="invalid config file"):
        load_config()
    isolated_config.write_text('[profiles.x]\nurl = "http://x"\npassword = "nope"\n')
    with pytest.raises(ConfigError, match="unknown key"):
        load_config()


def test_world_readable_token_file_warns(isolated_config: Path) -> None:
    isolated_config.write_text('token = "secret"\n')
    os.chmod(isolated_config, 0o644)
    with pytest.warns(UserWarning, match="chmod 600"):
        load_config()


def test_urls_are_validated() -> None:
    for bad in ("ftp://x", "127.0.0.1:8000", "http://u:p@host", "http://h/?q=1"):
        with pytest.raises(ConfigError):
            ClientConfig(base_url=bad)


def test_tokens_are_not_sent_over_plain_http_to_remote_hosts() -> None:
    with pytest.raises(ConfigError, match="plain http"):
        ClientConfig(base_url="http://api.example.com", token="t")
    ClientConfig(base_url="http://127.0.0.1:8000", token="t")
    ClientConfig(base_url="https://api.example.com", token="t")
    ClientConfig(base_url="http://api.example.com", token="t", allow_insecure_token=True)


def test_loopback_detection() -> None:
    assert is_loopback("localhost") and is_loopback("127.0.0.1") and is_loopback("[::1]")
    assert not is_loopback("example.com") and not is_loopback(None)


def test_config_path_honours_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    assert config_path(env) == tmp_path / "mapi" / "config.toml"
    assert config_path({"MAPI_CONFIG": str(tmp_path / "c.toml")}) == tmp_path / "c.toml"


def test_client_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPI_URL", "http://127.0.0.1:9999")
    with Client() as c:
        assert c.config.base_url == "http://127.0.0.1:9999"
