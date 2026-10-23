"""v0.19: the shim serves the ``plugins`` bucket, also from a root created before it existed."""

from __future__ import annotations

from pathlib import Path

from mapi_gcs_shim.config import (
    config_path,
    default_config,
    init_root,
    load_config,
    save_config,
    with_default_buckets,
)
from mapi_spec.storage import BucketPurpose, Buckets


def test_default_config_has_one_bucket_per_purpose() -> None:
    cfg = default_config("local")
    assert {b.name for b in cfg.buckets} == set(Buckets("local").all().values())
    plugins = next(b for b in cfg.buckets if b.name == Buckets("local").plugins)
    assert plugins.retention_period_seconds is None and plugins.lifecycle_age_seconds is None


def test_an_existing_root_gains_missing_default_buckets(tmp_path: Path) -> None:
    old = default_config("local", bundle_retention_seconds=86400)
    plugins = Buckets("local").plugins
    legacy = old.model_copy(update={"buckets": tuple(b for b in old.buckets if b.name != plugins)})
    (tmp_path / "_shim" / "keys").mkdir(parents=True)
    save_config(config_path(tmp_path), legacy)
    cfg = init_root(tmp_path, env="local")
    assert plugins in {b.name for b in cfg.buckets}
    # persisted, and nothing else changed (secrets, the locked bundles policy)
    reloaded = load_config(config_path(tmp_path))
    assert reloaded == cfg
    assert reloaded.principals == legacy.principals
    bundles = next(b for b in reloaded.buckets if b.name == Buckets("local").bundles)
    assert bundles.retention_period_seconds == 86400 and bundles.retention_locked
    assert with_default_buckets(reloaded) is reloaded
    assert BucketPurpose.PLUGINS.value == "plugins"
