"""v0.3 configuration: project selection, OIDC settings and the private config writer."""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

import pytest
from mapi_client import ClientConfig, ConfigError, load_config
from mapi_client.config import (
    check_issuer,
    credentials_path,
    read_config_file,
    update_profile,
    write_config_file,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_project_resolution(isolated_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolated_config.write_text('[profiles.a]\nproject = "acme/file"\n')
    assert load_config(profile="a").project == "acme/file"
    monkeypatch.setenv("MAPI_PROJECT", "acme/env")
    cfg = load_config(profile="a")
    assert cfg.project == "acme/env" and cfg.sources["project"] == "env:MAPI_PROJECT"
    assert load_config(profile="a", project="growth").project == "growth"
    with pytest.raises(ConfigError, match="invalid project"):
        load_config(project="ACME/../x")
    assert ClientConfig(project="  ").project is None


def test_oidc_settings(isolated_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolated_config.write_text(
        'oidc_issuer = "https://idp.example/"\noidc_client_id = "cli"\n'
        'oidc_client_secret = "not-really-secret"\n'
    )
    os.chmod(isolated_config, 0o600)
    cfg = load_config()
    assert cfg.oidc_issuer == "https://idp.example"
    assert cfg.oidc_client_id == "cli"
    assert "not-really-secret" not in repr(cfg)
    monkeypatch.setenv("MAPI_OIDC_ISSUER", "http://127.0.0.1:9100/dev-idp")
    assert load_config().oidc_issuer == "http://127.0.0.1:9100/dev-idp"
    with pytest.raises(ConfigError, match="https"):
        check_issuer("http://idp.example")
    with pytest.raises(ConfigError):
        check_issuer("https://idp.example/?x=1")


def test_credentials_path_follows_the_config(tmp_path: Path) -> None:
    env = {"MAPI_CONFIG": str(tmp_path / "c" / "config.toml")}
    assert credentials_path(env) == tmp_path / "c" / "credentials.json"
    env["MAPI_CREDENTIALS"] = str(tmp_path / "creds.json")
    assert credentials_path(env) == tmp_path / "creds.json"
    assert load_config().credentials_file == credentials_path()
    assert ClientConfig().credentials_file is None


def test_writer_is_private_atomic_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "new" / "dir" / "config.toml"
    data = {
        "profile": "prod",
        "url": "http://127.0.0.1:8000",
        "profiles": {
            "prod": {
                "url": "https://api.example",
                "token": 'tok"with\\quotes\nand newline',
                "timeout": 12.5,
                "max_retries": 3,
                "verify": False,
            },
            "weird name": {"project": "acme/growth"},
        },
    }
    write_config_file(path, data)
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    assert tomllib.loads(path.read_text()) == data
    assert read_config_file(path) == data
    assert not [p for p in path.parent.iterdir() if p.name != "config.toml"]  # no temp files
    with pytest.raises(ConfigError, match="unknown"):
        write_config_file(path, {"profiles": {"x": {"password": "nope"}}})
    with pytest.raises(ConfigError, match="cannot store"):
        write_config_file(path, {"url": ["a"]})
    assert tomllib.loads(path.read_text()) == data  # unchanged after the failures


def test_update_profile(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('url = "http://127.0.0.1:8000"\n[profiles.a]\nurl = "https://a.test"\n')
    os.chmod(path, 0o644)
    update_profile(path, "a", set_values={"project": "acme/growth", "token": "t"})
    assert _mode(path) == 0o600  # rewritten private, even though it was 0644
    data = read_config_file(path)
    assert data["profiles"]["a"] == {
        "url": "https://a.test",
        "project": "acme/growth",
        "token": "t",
    }
    assert data["url"] == "http://127.0.0.1:8000"
    update_profile(path, "b", set_values={"url": "https://b.test"}, select=True)
    data = read_config_file(path)
    assert data["profile"] == "b" and data["profiles"]["b"] == {"url": "https://b.test"}
    update_profile(path, "a", unset=["token"], set_values={"project": None})
    assert read_config_file(path)["profiles"]["a"] == {"url": "https://a.test"}
    update_profile(path, None, set_values={"project": "acme/x"})
    assert read_config_file(path)["project"] == "acme/x"
    with pytest.raises(ConfigError, match="unknown config key"):
        update_profile(path, "a", set_values={"colour": "red"})
    with pytest.raises(ConfigError, match="profile names"):
        update_profile(path, "a b", set_values={"url": "https://x"})


def test_secret_keys_trigger_the_permission_warning(isolated_config: Path) -> None:
    isolated_config.write_text('oidc_client_secret = "s"\n')
    os.chmod(isolated_config, 0o640)
    with pytest.warns(UserWarning, match="chmod 600"):
        load_config()
