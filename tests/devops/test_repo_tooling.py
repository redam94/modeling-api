"""scripts/image_pins.py, scripts/lock_closure.py, scripts/release_check.py and the Makefile."""

from __future__ import annotations

import io
import json
import re
import subprocess
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from .conftest import ROOT, load_script

DIGEST = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32


@pytest.fixture(scope="module")
def pins() -> ModuleType:
    return load_script("scripts/image_pins.py", "mapi_devtools_image_pins")


@pytest.fixture(scope="module")
def lc() -> ModuleType:
    return load_script("scripts/lock_closure.py", "mapi_devtools_lock_closure")


@pytest.fixture(scope="module")
def rc() -> ModuleType:
    return load_script("scripts/release_check.py", "mapi_devtools_release_check")


# ================================================================================ image pins


class TestImageRef:
    @pytest.mark.parametrize(
        ("ref", "registry", "repo", "tag", "digest"),
        [
            ("python:3.12-slim", "registry-1.docker.io", "library/python", "3.12-slim", None),
            (f"postgres:16@{DIGEST}", "registry-1.docker.io", "library/postgres", "16", DIGEST),
            (
                "nginxinc/nginx-unprivileged:1.30-alpine",
                "registry-1.docker.io",
                "nginxinc/nginx-unprivileged",
                "1.30-alpine",
                None,
            ),
            (f"ghcr.io/astral-sh/uv:0.5.13@{DIGEST}", "ghcr.io", "astral-sh/uv", "0.5.13", DIGEST),
            ("localhost:5000/team/img", "localhost:5000", "team/img", None, None),
            ("docker.io/library/node:22", "registry-1.docker.io", "library/node", "22", None),
        ],
    )
    def test_parse(
        self,
        pins: ModuleType,
        ref: str,
        registry: str,
        repo: str,
        tag: str | None,
        digest: str | None,
    ) -> None:
        image = pins.ImageRef.parse(ref)
        assert (image.registry, image.repository, image.tag, image.digest) == (
            registry,
            repo,
            tag,
            digest,
        )

    def test_display(self, pins: ModuleType) -> None:
        assert pins.ImageRef.parse("python:3.12").display(DIGEST) == f"python:3.12@{DIGEST}"
        ghcr = pins.ImageRef.parse(f"ghcr.io/astral-sh/uv:1@{DIGEST}")
        assert ghcr.display() == f"ghcr.io/astral-sh/uv:1@{DIGEST}"
        assert ghcr.display(digest="") == "ghcr.io/astral-sh/uv:1"

    @pytest.mark.parametrize("bad", ["", "a b", "python@sha256:xyz", "python:3@md5:00"])
    def test_rejects(self, pins: ModuleType, bad: str) -> None:
        with pytest.raises(ValueError):
            pins.ImageRef.parse(bad)


class TestPins:
    DOCKERFILE = (
        f"# syntax=docker/dockerfile:1.26@{DIGEST}\n"
        "ARG PYTHON_IMAGE=python:3.12-slim\n"
        f"ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.5.13@{DIGEST}\n"
        "ARG VERSION=1.0\n"
        "FROM ${PYTHON_IMAGE}\n"
    )
    COMPOSE = (
        "services:\n"
        f"  db:\n    image: postgres:16@{DIGEST}  # pinned\n"
        "  api:\n    image: mapi/server:${MAPI_VERSION:-0.1.0}\n"
        "  cache:\n    image: redis\n"
    )

    def test_find_and_check(self, pins: ModuleType) -> None:
        found = pins.find_pins(Path("images/x/Dockerfile"), self.DOCKERFILE)
        assert [(p.name, p.line) for p in found] == [
            ("syntax", 1),
            ("PYTHON_IMAGE", 2),
            ("UV_IMAGE", 3),
        ]
        problems = pins.check(found)
        assert len(problems) == 1 and "PYTHON_IMAGE" not in problems[0]
        assert "python:3.12-slim is not pinned" in problems[0]
        compose = pins.find_pins(Path("docker-compose.yml"), self.COMPOSE)
        assert [p.ref for p in compose] == [f"postgres:16@{DIGEST}", "redis"]
        assert any("redis has no tag" in p for p in pins.check(compose))

    def test_update_text_rewrites_only_digests(self, pins: ModuleType) -> None:
        new = pins.update_text(Path("Dockerfile"), self.DOCKERFILE, lambda image: OTHER)
        assert f"ARG PYTHON_IMAGE=python:3.12-slim@{OTHER}" in new
        assert f"ghcr.io/astral-sh/uv:0.5.13@{OTHER}" in new
        assert f"docker/dockerfile:1.26@{OTHER}" in new
        assert "ARG VERSION=1.0" in new and new.count("\n") == self.DOCKERFILE.count("\n")
        compose = pins.update_text(Path("c.yml"), self.COMPOSE, lambda image: OTHER)
        assert f"postgres:16@{OTHER}  # pinned" in compose
        assert "mapi/server:${MAPI_VERSION:-0.1.0}" in compose

    def test_repository_pins_are_complete(self, pins: ModuleType) -> None:
        assert pins.main(["check"]) == 0

    def test_resolve_digest_bearer_flow(self, pins: ModuleType) -> None:
        calls: list[tuple[str, str, str | None]] = []

        class Resp(io.BytesIO):
            def __init__(self, status: int, headers: dict[str, str], body: bytes = b"") -> None:
                super().__init__(body)
                self.status = status
                self.headers = Message()
                for k, v in headers.items():
                    self.headers[k] = v

        def opener(req: urllib.request.Request) -> Any:
            calls.append((req.get_method(), req.full_url, req.get_header("Authorization")))
            if "token" in req.full_url:
                return Resp(200, {}, json.dumps({"token": "t0k"}).encode())
            if req.get_header("Authorization") != "Bearer t0k":
                hdrs = Message()
                hdrs["WWW-Authenticate"] = (
                    'Bearer realm="https://auth.example/token",service="registry.example"'
                )
                raise urllib.error.HTTPError(req.full_url, 401, "unauth", hdrs, io.BytesIO())
            return Resp(200, {"Docker-Content-Digest": DIGEST})

        image = pins.ImageRef.parse("registry.example/team/app:1.2")
        assert pins.resolve_digest(image, opener=opener) == DIGEST
        assert calls[0][:2] == ("HEAD", "https://registry.example/v2/team/app/manifests/1.2")
        assert "scope=repository%3Ateam%2Fapp%3Apull" in calls[1][1]
        assert calls[2][2] == "Bearer t0k"

    def test_resolve_digest_refuses_http_realm(self, pins: ModuleType) -> None:
        def opener(req: urllib.request.Request) -> Any:
            hdrs = Message()
            hdrs["WWW-Authenticate"] = 'Bearer realm="http://evil/token"'
            raise urllib.error.HTTPError(req.full_url, 401, "unauth", hdrs, io.BytesIO())

        with pytest.raises(RuntimeError, match="non-https"):
            pins.resolve_digest(pins.ImageRef.parse("python:3"), opener=opener)


# ============================================================================== lock closure

SYNTH_LOCK: dict[str, Any] = {
    "package": [
        {
            "name": "app",
            "version": "1.0",
            "source": {"editable": "packages/app"},
            "dependencies": [
                {"name": "lib"},
                {"name": "winonly", "marker": "sys_platform == 'win32'"},
            ],
            "optional-dependencies": {"extra": [{"name": "heavy", "extra": ["fast"]}]},
        },
        {
            "name": "lib",
            "version": "2.0",
            "source": {"registry": "x"},
            "sdist": {"hash": "sha256:" + "11" * 32},
            "wheels": [{"hash": "sha256:" + "22" * 32}],
            "dependencies": [{"name": "Typing_Extensions"}],
        },
        {
            "name": "typing-extensions",
            "version": "4",
            "source": {"registry": "x"},
            "wheels": [{"hash": "sha256:" + "33" * 32}],
        },
        {
            "name": "heavy",
            "version": "3",
            "source": {"registry": "x"},
            "wheels": [{"hash": "sha256:" + "44" * 32}],
            "optional-dependencies": {
                "fast": [{"name": "numpy", "marker": "python_full_version >= '3.12'"}]
            },
        },
        {
            "name": "numpy",
            "version": "2",
            "source": {"registry": "x"},
            "wheels": [{"hash": "sha256:" + "55" * 32}],
        },
        {
            "name": "winonly",
            "version": "1",
            "source": {"registry": "x"},
            "wheels": [{"hash": "sha256:" + "66" * 32}],
        },
    ]
}


class TestLockClosure:
    def test_closure_follows_extras_and_markers(self, lc: ModuleType) -> None:
        lock = lc.Lock.from_mapping(SYNTH_LOCK)
        assert lc.closure(lock, [lc.Requirement.parse("app")]) == {
            "app",
            "lib",
            "typing-extensions",
        }
        assert lc.closure(lock, [lc.Requirement.parse("app[extra]")]) == {
            "app",
            "lib",
            "typing-extensions",
            "heavy",
            "numpy",
        }
        with pytest.raises(KeyError, match="no extra"):
            lc.closure(lock, [lc.Requirement.parse("app[nope]")])
        with pytest.raises(KeyError, match=r"not in uv\.lock"):
            lc.closure(lock, [lc.Requirement.parse("ghost")])

    def test_requirements_are_hash_pinned(self, lc: ModuleType) -> None:
        lock = lc.Lock.from_mapping(SYNTH_LOCK)
        text = lock.requirements({"lib", "typing-extensions"})
        assert text.splitlines() == [
            f"lib==2.0 --hash=sha256:{'11' * 32} --hash=sha256:{'22' * 32}",
            f"typing-extensions==4 --hash=sha256:{'33' * 32}",
        ]
        with pytest.raises(ValueError, match="workspace package"):
            lock.requirements({"app"})

    def test_real_lock(self, lc: ModuleType) -> None:
        lock = lc.Lock.load(ROOT / "uv.lock")
        names = lc.closure(lock, [lc.Requirement.parse("import-linter")])
        assert {"import-linter", "grimp"} <= names
        assert "--hash=sha256:" in lock.requirements(names)
        assert "mapi-server" in lock.workspace


# ============================================================================= release check


def _repo(tmp_path: Path, version: str = "1.2.3", member_version: str = "1.2.3") -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "root"\nversion = "{version}"\n'
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n'
    )
    pkg = tmp_path / "packages" / "a"
    (pkg / "src" / "a").mkdir(parents=True)
    (pkg / "pyproject.toml").write_text(f'[project]\nname = "a"\nversion = "{member_version}"\n')
    (pkg / "src" / "a" / "__init__.py").write_text(f'__version__ = "{member_version}"\n')
    (tmp_path / "apps" / "web").mkdir(parents=True)
    (tmp_path / "apps" / "web" / "package.json").write_text(json.dumps({"version": version}))
    (tmp_path / "docker-compose.yml").write_text(
        f"services:\n  x:\n    image: mapi/x:${{MAPI_VERSION:-{version}}}\n"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [Unreleased]\n\n## [{version}] — 2026-10-15\n\n- things\n"
    )
    return tmp_path


class TestReleaseCheck:
    def test_consistent_tree_passes(self, rc: ModuleType, tmp_path: Path) -> None:
        report = rc.run(_repo(tmp_path), ls_files=lambda specs: [])
        assert report.errors == []
        assert report.notes == ["version 1.2.3 in 5 places"]

    def test_version_drift(self, rc: ModuleType, tmp_path: Path) -> None:
        report = rc.run(_repo(tmp_path, member_version="1.2.4"), ls_files=lambda s: [])
        assert len(report.errors) == 2
        assert all("'1.2.4' != '1.2.3'" in e for e in report.errors)

    def test_changelog_rules(self, rc: ModuleType, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        (root / "CHANGELOG.md").write_text("## [1.2.3]\n\n## [1.2.2] — 2026-01-01\n- x\n")
        errors = rc.run(root, ls_files=lambda s: []).errors
        assert any("no YYYY-MM-DD date" in e for e in errors)
        assert any("section is empty" in e for e in errors)
        (root / "CHANGELOG.md").write_text("## [Unreleased]\n- x\n")
        assert any("no '## [1.2.3]" in e for e in rc.run(root, ls_files=lambda s: []).errors)

    def test_tracked_plan_files_fail(self, rc: ModuleType, tmp_path: Path) -> None:
        seen: list[tuple[str, ...]] = []

        def ls(specs: tuple[str, ...]) -> list[str]:
            seen.append(tuple(specs))
            return ["plans/BUILD-PROGRAM.md"]

        errors = rc.run(_repo(tmp_path), ls_files=ls).errors
        assert errors == [
            "plans/BUILD-PROGRAM.md: plan files must never be committed (git rm --cached it)"
        ]
        assert "docs/architecture-plan.md" in seen[0]

    def test_this_repository(self, rc: ModuleType) -> None:
        report = rc.run(ROOT)
        assert report.errors == [], report.errors


# ================================================================================== Makefile

REQUIRED_TARGETS = {
    "chaos",
    "load-test",
    "security-suite",
    "tf-validate",
    "dev",
    "run",
    "stop",
    "db-shell",
    "migrate",
    "seed",
    "docker-build",
    "compose-up",
    "compose-down",
    "compose-logs",
    "web-build",
    "web-test",
    "test-all",
    "e2e",
    "release-check",
    "clean-local",
    "docs-build",
    "docs-serve",
    "docs-catalog",
    "docs-test",
    "test",
    "lint",
    "typecheck",
    "help",
    "install",
}


class TestMakefile:
    def test_required_targets_are_documented(self) -> None:
        text = (ROOT / "Makefile").read_text()
        documented = set(re.findall(r"^([a-zA-Z0-9][\w.-]*):.*?## ", text, re.M))
        assert documented >= REQUIRED_TARGETS, REQUIRED_TARGETS - documented
        phony = set(re.findall(r"^\.PHONY: (\S+)", text, re.M))
        assert documented <= phony

    def test_help_is_grouped(self) -> None:
        out = subprocess.run(
            ["make", "-s", "help"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
        plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
        groups = [ln for ln in plain.splitlines() if ln and not ln.startswith((" ", "Usage"))]
        assert groups == [
            "setup",
            "local stack (no Docker)",
            "quality",
            "tests",
            "security",
            "web",
            "containers",
            "docs",
            "release",
            "housekeeping",
        ]
        assert "_web-check" not in plain and "_docker-check" not in plain

    def test_docker_build_tags_every_image(self) -> None:
        out = subprocess.run(
            ["make", "-n", "docker-build"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
        version = re.search(r'^version = "(.+)"', (ROOT / "pyproject.toml").read_text(), re.M)
        assert version is not None
        assert f"mapi/$img:{version.group(1)}" in out and f"mapi/worker:{version.group(1)}" in out
        images = ("base-bayes", "base-ml", "base-forecast", "base-dl", "server", "gcs-shim", "web")
        for image in images:
            assert image in out
