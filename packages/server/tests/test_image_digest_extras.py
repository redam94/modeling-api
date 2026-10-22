"""The image digest folds in the plugins an operator installed (v0.18): the extensions lock
of an extended image and the out-of-tree plugins of a source checkout. Both sides (api and
worker) derive it with ``Settings.resolve_image_digest``; it is never a literal."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from mapi_server.settings import (
    CHECKOUT_EXTRA_LIST,
    ExecutorName,
    Settings,
    checkout_image_digest,
    local_image_digest,
)

ROOT = Path(__file__).resolve().parents[3]


def _checkout(root: Path) -> Path:
    """A minimal source checkout: ``uv.lock`` and one workspace package."""
    (root / "packages" / "core" / "src" / "core").mkdir(parents=True)
    (root / "packages" / "core" / "src" / "core" / "__init__.py").write_text("X = 1\n")
    lock = root / "uv.lock"
    lock.write_text("version = 1\n")
    return lock


def _plugin(path: Path, body: str = "def fit(): ...\n") -> Path:
    (path / "src" / "acme").mkdir(parents=True, exist_ok=True)
    (path / "src" / "acme" / "__init__.py").write_text(body)
    (path / "src" / "acme" / "__pycache__").mkdir(exist_ok=True)
    (path / "src" / "acme" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    return path


def _list(root: Path, *lines: str) -> None:
    path = root / CHECKOUT_EXTRA_LIST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


# ======================================================================== extensions lock


def test_the_extensions_lock_is_folded_into_the_lock_digest(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_bytes(b"version = 1\n")
    ext = tmp_path / "extensions.lock"
    ext.write_bytes(b"acme-models==1.0.0 --hash=sha256:00\n")
    plain = local_image_digest(lock)
    assert plain == "sha256:" + hashlib.sha256(lock.read_bytes()).hexdigest()  # unchanged
    extended = local_image_digest(lock, ext)
    expected = hashlib.sha256(lock.read_bytes() + b"\0ext\0" + ext.read_bytes()).hexdigest()
    assert extended == "sha256:" + expected
    assert extended != plain
    ext.write_bytes(b"acme-models==1.0.1 --hash=sha256:01\n")
    assert local_image_digest(lock, ext) not in (plain, extended)


def test_both_sides_resolve_the_same_extended_digest(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_bytes(b"version = 1\n")
    ext = tmp_path / "extensions.lock"
    ext.write_bytes(b"acme\n")
    api = Settings(uv_lock_path=lock, extensions_lock_path=ext)
    worker = Settings(uv_lock_path=lock, extensions_lock_path=ext, executor=ExecutorName.DOCKER)
    assert api.resolve_image_digest() == worker.resolve_image_digest()
    assert api.resolve_image_digest() == local_image_digest(lock, ext)
    assert Settings(uv_lock_path=lock).resolve_image_digest() == local_image_digest(lock)
    # a literal digest wins (and is the operator's to keep fresh): nothing is folded in
    literal = "sha256:" + "ab" * 32
    assert Settings(image_digest=literal, extensions_lock_path=ext).resolve_image_digest() == (
        literal
    )
    missing = Settings(uv_lock_path=lock, extensions_lock_path=tmp_path / "nope.lock")
    with pytest.raises(RuntimeError, match="MAPI_EXTENSIONS_LOCK_PATH"):
        missing.resolve_image_digest()


def test_the_extend_image_never_bakes_a_literal_digest() -> None:
    dockerfile = ROOT / "images" / "extend" / "Dockerfile"
    if not dockerfile.is_file():
        pytest.skip("images/extend is not there yet (WP-X6)")
    text = dockerfile.read_text()
    assert "MAPI_IMAGE_DIGEST" not in text
    assert "MAPI_EXTENSIONS_LOCK_PATH" in text or "extensions.lock" in text


# ================================================================ checkout extra plugins


def test_listed_out_of_tree_plugins_change_the_checkout_digest(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    lock = _checkout(root)
    base = checkout_image_digest(lock)
    acme = _plugin(tmp_path / "acme")
    _list(root, "# plugins installed with make plugin-dev", "", "../acme")
    with_acme = checkout_image_digest(lock)
    assert with_acme != base
    # a change to the plugin's source changes it again; bytecode does not
    (acme / "src" / "acme" / "__pycache__" / "x.pyc").write_bytes(b"\1")
    assert checkout_image_digest(lock) == with_acme
    _plugin(acme, "def fit(): return 1\n")
    changed = checkout_image_digest(lock)
    assert changed != with_acme
    # duplicates and order do not matter; removing the entry restores the digest
    _list(root, "../acme", "../acme")
    assert checkout_image_digest(lock) == changed
    _list(root, "# nothing")
    assert checkout_image_digest(lock) == base


def test_a_listed_plugin_that_is_gone_still_changes_the_digest(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    lock = _checkout(root)
    base = checkout_image_digest(lock)
    _list(root, "../gone")
    assert checkout_image_digest(lock) != base


def test_the_digest_does_not_depend_on_where_the_checkout_lives(tmp_path: Path) -> None:
    one = tmp_path / "a"
    lock = _checkout(one / "repo")
    _plugin(one / "acme")
    _list(one / "repo", "../acme")
    two = tmp_path / "b"
    shutil.copytree(one, two)
    assert checkout_image_digest(lock) == checkout_image_digest(two / "repo" / "uv.lock")


def test_extra_plugins_alone_make_a_checkout_digest(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text("version = 1\n")
    assert checkout_image_digest(lock) == local_image_digest(lock)
    _plugin(tmp_path / "acme")
    _list(tmp_path, str(tmp_path / "acme"))  # an absolute entry works too
    assert checkout_image_digest(lock) != local_image_digest(lock)


def test_the_extensions_lock_is_folded_into_the_checkout_digest(tmp_path: Path) -> None:
    lock = _checkout(tmp_path)
    ext = tmp_path / "extensions.lock"
    ext.write_text("acme\n")
    assert checkout_image_digest(lock, ext) != checkout_image_digest(lock)


# ===================================================================== v0.18 settings


def test_live_recipe_previews_are_off_by_default_on_cloud_run_only() -> None:
    assert Settings().plot_preview_enabled is True
    assert Settings(executor=ExecutorName.DOCKER).plot_preview_enabled is True
    assert Settings(executor=ExecutorName.CLOUDRUN_JOBS).plot_preview_enabled is False
    on = Settings(executor=ExecutorName.CLOUDRUN_JOBS, plot_preview_enabled=True)
    assert on.plot_preview_enabled is True


def test_the_library_and_sample_limits_have_their_defaults() -> None:
    s = Settings()
    assert (s.library_items_per_tenant, s.library_revisions_per_item) == (500, 200)
    assert (s.library_validate_per_minute, s.library_chart_preview_per_minute) == (60, 60)
    assert (s.samples_enabled, s.sample_max_attempts) == (True, 3)
    assert (s.plot_preview_per_minute, s.plot_preview_per_day, s.plot_preview_wait_s) == (
        20,
        200,
        15.0,
    )
