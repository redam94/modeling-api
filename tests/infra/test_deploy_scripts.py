"""deploy/gcp/*.sh: help, argument checking, dry runs and the confirmation guard.

``terraform``, ``gcloud``, ``docker`` and ``curl`` are replaced by stubs that record every
invocation and fail, so a dry run is proven to execute nothing. Each script runs under
``/bin/bash`` (3.2 on macOS) and the ``bash`` on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from .conftest import DEPLOY, ROOT

SCRIPTS = ("bootstrap", "build-push", "deploy", "migrate", "rollback")
DIGEST = "sha256:" + "ab" * 32
REPO = "europe-west4-docker.pkg.dev/acme-dev-build/mapi-dev-images"
STUBBED = ("terraform", "gcloud", "docker", "curl")


def _interpreters() -> list[str]:
    found = [p for p in ("/bin/bash", shutil.which("bash")) if p and Path(p).is_file()]
    return list(dict.fromkeys(os.path.realpath(p) for p in found))


@pytest.fixture(params=_interpreters(), ids=lambda p: Path(p).name + ":" + p.replace("/", "_"))
def bash(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def stubs(tmp_path: Path) -> Path:
    """A directory of failing stubs that log their argv to calls.log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for tool in STUBBED:
        stub = bin_dir / tool
        stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\nexit 97\n')
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def calls(stubs: Path) -> list[str]:
    log = stubs.parent / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def script(
    bash: str, name: str, *args: str, stubs: Path, out: Path
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "HOME": str(out),
        "MAPI_DEPLOY_OUT": str(out / "deploy-out"),
        "LC_ALL": "C",
    }
    return subprocess.run(
        [bash, str(DEPLOY / f"{name}.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        cwd=ROOT,
        timeout=120,
        check=False,
    )


def images_file(
    path: Path, names: tuple[str, ...] = ("server", "web", "base-bayes", "base-ml", "base-forecast")
) -> Path:
    path.write_text(json.dumps({"images": {n: f"{REPO}/{n}@{DIGEST}" for n in names}}))
    return path


# ----------------------------------------------------------------------------- all scripts


@pytest.mark.parametrize("name", SCRIPTS)
def test_strict_mode_and_executable(name: str) -> None:
    path = DEPLOY / f"{name}.sh"
    assert os.access(path, os.X_OK), f"{name}.sh is not executable"
    lines = path.read_text().splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    code = [line for line in lines[1:] if line.strip() and not line.lstrip().startswith("#")]
    assert code[0] == "set -euo pipefail", "strict mode before anything else runs"
    assert 'source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"' in path.read_text()


@pytest.mark.parametrize("name", SCRIPTS)
def test_help(bash: str, stubs: Path, tmp_path: Path, name: str) -> None:
    res = script(bash, name, "--help", stubs=stubs, out=tmp_path)
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("Usage: deploy/gcp/")
    assert "--dry-run" in res.stdout
    assert calls(stubs) == []


@pytest.mark.parametrize("name", SCRIPTS)
def test_unknown_argument(bash: str, stubs: Path, tmp_path: Path, name: str) -> None:
    res = script(bash, name, "--frobnicate", stubs=stubs, out=tmp_path)
    assert res.returncode != 0
    assert "unknown argument: --frobnicate" in res.stderr


@pytest.mark.parametrize("name", SCRIPTS)
def test_env_is_validated(bash: str, stubs: Path, tmp_path: Path, name: str) -> None:
    res = script(bash, name, "--env", "Prod_1", "--dry-run", stubs=stubs, out=tmp_path)
    assert res.returncode != 0
    assert "--env must match" in res.stderr


def test_shellcheck() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    files = [str(DEPLOY / f"{n}.sh") for n in SCRIPTS] + [str(DEPLOY / "lib" / "common.sh")]
    res = subprocess.run(
        [shellcheck, "-x", "-P", "SCRIPTDIR", "--severity=style", *files],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr


# ----------------------------------------------------------------------------------- bootstrap

BOOTSTRAP = (
    "--env", "dev", "--region", "europe-west4",
    "--control-project", "acme-dev-control", "--data-project", "acme-dev-data",
    "--build-project", "acme-dev-build", "--exec-project", "paid=acme-dev-exec-paid",
    "--exec-project", "public=acme-dev-exec-public", "--state-project", "acme-dev-build",
)  # fmt: skip


def test_bootstrap_dry_run(bash: str, stubs: Path, tmp_path: Path) -> None:
    res = script(bash, "bootstrap", *BOOTSTRAP, "--dry-run", stubs=stubs, out=tmp_path)
    assert res.returncode == 0, res.stderr
    out = res.stdout
    for project in (
        "acme-dev-control",
        "acme-dev-data",
        "acme-dev-build",
        "acme-dev-exec-paid",
        "acme-dev-exec-public",
    ):
        assert f"--project {project} --quiet" in out, project
    assert "gcloud services enable" in out and "sqladmin.googleapis.com" in out
    create = next(line for line in out.splitlines() if "gcloud storage buckets create" in line)
    assert "gs://acme-dev-build-mapi-tfstate" in create
    assert "--uniform-bucket-level-access" in create and "--public-access-prevention" in create
    assert "gcloud storage buckets update gs://acme-dev-build-mapi-tfstate --versioning" in out
    assert 'bucket = "acme-dev-build-mapi-tfstate"' in out and 'prefix = "mapi/dev"' in out
    assert calls(stubs) == [], "a dry run executes nothing"


def test_bootstrap_needs_an_exec_project(bash: str, stubs: Path, tmp_path: Path) -> None:
    args = [
        a
        for a in BOOTSTRAP
        if a not in ("--exec-project", "paid=acme-dev-exec-paid", "public=acme-dev-exec-public")
    ]
    res = script(bash, "bootstrap", *args, "--dry-run", stubs=stubs, out=tmp_path)
    assert res.returncode != 0
    assert "at least one --exec-project" in res.stderr


def test_bootstrap_rejects_malformed_exec_project(bash: str, stubs: Path, tmp_path: Path) -> None:
    res = script(
        bash,
        "bootstrap",
        *BOOTSTRAP,
        "--exec-project",
        "acme-exec",
        "--dry-run",
        stubs=stubs,
        out=tmp_path,
    )
    assert res.returncode != 0
    assert "POOL=PROJECT_ID" in res.stderr


def test_bootstrap_refuses_without_confirmation(bash: str, stubs: Path, tmp_path: Path) -> None:
    res = script(bash, "bootstrap", *BOOTSTRAP, stubs=stubs, out=tmp_path)
    assert res.returncode != 0
    assert "pass --yes" in res.stderr
    assert calls(stubs) == [], "nothing runs before the operator confirms"


# ---------------------------------------------------------------------------------- build-push


def test_build_push_dry_run(bash: str, stubs: Path, tmp_path: Path) -> None:
    res = script(
        bash,
        "build-push",
        "--env",
        "dev",
        "--repository",
        REPO,
        "--tag",
        "0.3.0-test",
        "--allow-dirty",
        "--dry-run",
        stubs=stubs,
        out=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    builds = [line for line in res.stdout.splitlines() if "docker buildx build" in line]
    assert len(builds) == 5
    for name in ("server", "web", "base-bayes", "base-ml", "base-forecast"):
        line = next(b for b in builds if f"images/{name}/Dockerfile" in b)
        assert "--platform linux/amd64" in line and "--target runtime" in line and "--push" in line
        assert f"--tag {REPO}/{name}:0.3.0-test" in line
    assert f"gcloud auth configure-docker {REPO.split('/')[0]} --quiet" in res.stdout
    assert f'"server": "{REPO}/server@sha256:<digest of server>"' in res.stdout
    assert calls(stubs) == []


def test_build_push_subset_and_bad_names(bash: str, stubs: Path, tmp_path: Path) -> None:
    ok = script(
        bash,
        "build-push",
        "--env",
        "dev",
        "--repository",
        REPO,
        "--images",
        "server web",
        "--allow-dirty",
        "--dry-run",
        stubs=stubs,
        out=tmp_path,
    )
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.count("docker buildx build") == 2
    bad = script(
        bash,
        "build-push",
        "--env",
        "dev",
        "--repository",
        REPO,
        "--images",
        "gcs-shim",
        "--dry-run",
        stubs=stubs,
        out=tmp_path,
    )
    assert bad.returncode != 0 and "unknown image 'gcs-shim'" in bad.stderr


def test_build_push_validates_the_repository(bash: str, stubs: Path, tmp_path: Path) -> None:
    res = script(
        bash,
        "build-push",
        "--env",
        "dev",
        "--repository",
        "docker.io/acme/mapi",
        "--allow-dirty",
        "--dry-run",
        stubs=stubs,
        out=tmp_path,
    )
    assert res.returncode != 0
    assert "REGION-docker.pkg.dev/PROJECT/REPO" in res.stderr


# -------------------------------------------------------------------------------------- deploy


def test_deploy_dry_run(bash: str, stubs: Path, tmp_path: Path) -> None:
    images = images_file(tmp_path / "images.json")
    res = script(
        bash,
        "deploy",
        "--env",
        "staging",
        "--images-file",
        str(images),
        "--dry-run",
        stubs=stubs,
        out=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()

    def index(fragment: str) -> int:
        return next(i for i, line in enumerate(lines) if fragment in line)

    staging = str(ROOT / "infra" / "terraform" / "envs" / "staging")
    assert f"terraform -chdir={staging} init -input=false -reconfigure" in res.stdout
    plan = lines[index(" plan -input=false")]
    assert f"-var-file={images}" in plan
    # migrations run with the new server image BEFORE the apply, then the release is recorded
    migrate = index("gcloud run jobs update mapi-staging-migrate")
    assert f"--image {REPO}/server@{DIGEST}" in lines[migrate]
    assert index("gcloud run jobs execute mapi-staging-migrate") > migrate
    apply = index(" apply -input=false")
    assert migrate < apply < index("gcloud storage cp")
    assert "/mapi/staging/releases/" in lines[index("gcloud storage cp")]
    assert "would smoke-test" in res.stdout
    assert calls(stubs) == []


def test_deploy_plan_only_stops_after_plan(bash: str, stubs: Path, tmp_path: Path) -> None:
    images = images_file(tmp_path / "images.json")
    res = script(
        bash,
        "deploy",
        "--env",
        "dev",
        "--images-file",
        str(images),
        "--plan-only",
        "--plan-file",
        str(tmp_path / "tfplan"),
        "--dry-run",
        stubs=stubs,
        out=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    assert f"-out={tmp_path / 'tfplan'}" in res.stdout
    assert " apply " not in res.stdout and "migrate" not in res.stdout


def test_deploy_foundation_needs_no_images(bash: str, stubs: Path, tmp_path: Path) -> None:
    res = script(
        bash, "deploy", "--env", "dev", "--foundation", "--dry-run", stubs=stubs, out=tmp_path
    )
    assert res.returncode == 0, res.stderr
    assert (
        "-var-file=" not in next(line for line in res.stdout.splitlines() if " plan " in line)
        or "images" not in res.stdout
    )
    assert "gcloud run jobs" not in res.stdout, "no migrations in a foundation apply"
    clash = script(
        bash,
        "deploy",
        "--env",
        "dev",
        "--foundation",
        "--images-file",
        "x.json",
        "--dry-run",
        stubs=stubs,
        out=tmp_path,
    )
    assert clash.returncode != 0 and "--foundation deploys no images" in clash.stderr


def test_deploy_refuses_incomplete_or_tagged_releases(
    bash: str, stubs: Path, tmp_path: Path
) -> None:
    partial = images_file(tmp_path / "partial.json", ("server", "web"))
    res = script(
        bash,
        "deploy",
        "--env",
        "dev",
        "--images-file",
        str(partial),
        "--dry-run",
        stubs=stubs,
        out=tmp_path,
    )
    assert res.returncode != 0 and "lacks: base-bayes, base-ml, base-forecast" in res.stderr

    tagged = tmp_path / "tagged.json"
    tagged.write_text(
        json.dumps(
            {
                "images": {
                    n: f"{REPO}/{n}:latest"
                    for n in ("server", "web", "base-bayes", "base-ml", "base-forecast")
                }
            }
        )
    )
    res = script(
        bash,
        "deploy",
        "--env",
        "dev",
        "--images-file",
        str(tagged),
        "--dry-run",
        stubs=stubs,
        out=tmp_path,
    )
    assert res.returncode != 0 and "not pinned by digest" in res.stderr
    assert calls(stubs) == []


def test_deploy_unknown_environment(bash: str, stubs: Path, tmp_path: Path) -> None:
    res = script(
        bash, "deploy", "--env", "qa", "--foundation", "--dry-run", stubs=stubs, out=tmp_path
    )
    assert res.returncode != 0
    assert "no Terraform environment 'qa'" in res.stderr


# ------------------------------------------------------------------------------------- migrate


def test_migrate_dry_run(bash: str, stubs: Path, tmp_path: Path) -> None:
    image = f"{REPO}/server@{DIGEST}"
    res = script(
        bash,
        "migrate",
        "--env",
        "prod",
        "--project",
        "acme-prod-control",
        "--region",
        "europe-west4",
        "--image",
        image,
        "--dry-run",
        stubs=stubs,
        out=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    assert (
        f"gcloud run jobs update mapi-prod-migrate --image {image} --region europe-west4 "
        "--project acme-prod-control --quiet"
    ) in res.stdout
    assert (
        "gcloud run jobs execute mapi-prod-migrate --region europe-west4 "
        "--project acme-prod-control --wait"
    ) in res.stdout
    assert calls(stubs) == []


def test_migrate_requires_a_digest(bash: str, stubs: Path, tmp_path: Path) -> None:
    res = script(
        bash,
        "migrate",
        "--env",
        "prod",
        "--project",
        "p",
        "--region",
        "r",
        "--image",
        f"{REPO}/server:0.3.0",
        "--dry-run",
        stubs=stubs,
        out=tmp_path,
    )
    assert res.returncode != 0 and "pinned by digest" in res.stderr


def test_migrate_refuses_without_confirmation(bash: str, stubs: Path, tmp_path: Path) -> None:
    res = script(
        bash,
        "migrate",
        "--env",
        "prod",
        "--project",
        "acme-prod-control",
        "--region",
        "europe-west4",
        stubs=stubs,
        out=tmp_path,
    )
    assert res.returncode != 0 and "pass --yes" in res.stderr
    assert calls(stubs) == []


# ------------------------------------------------------------------------------------ rollback


def test_rollback_traffic_only_dry_run(bash: str, stubs: Path, tmp_path: Path) -> None:
    res = script(
        bash,
        "rollback",
        "--env",
        "prod",
        "--traffic-only",
        "--project",
        "acme-prod-control",
        "--region",
        "europe-west4",
        "--dry-run",
        stubs=stubs,
        out=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    shifted = [
        line for line in res.stdout.splitlines() if "gcloud run services update-traffic" in line
    ]
    assert [line.split()[5] for line in shifted] == [
        f"mapi-prod-{s}" for s in ("api", "events", "web", "plots")
    ]
    assert calls(stubs) == []


def test_rollback_previous_redeploys_without_migrations(
    bash: str, stubs: Path, tmp_path: Path
) -> None:
    res = script(
        bash, "rollback", "--env", "staging", "--previous", "--dry-run", stubs=stubs, out=tmp_path
    )
    assert res.returncode == 0, res.stderr
    assert "gcloud storage cp gs://" in res.stdout and "/mapi/staging/releases/" in res.stdout
    assert " apply -input=false" in res.stdout, "the release is redeployed through deploy.sh"
    assert "gcloud run jobs" not in res.stdout, "schema migrations are forward-only"
    assert calls(stubs) == []


def test_rollback_needs_exactly_one_mode(bash: str, stubs: Path, tmp_path: Path) -> None:
    none = script(bash, "rollback", "--env", "dev", "--dry-run", stubs=stubs, out=tmp_path)
    assert none.returncode != 0 and "choose one of" in none.stderr
    two = script(
        bash,
        "rollback",
        "--env",
        "dev",
        "--previous",
        "--traffic-only",
        "--dry-run",
        stubs=stubs,
        out=tmp_path,
    )
    assert two.returncode != 0 and "choose one of" in two.stderr
    bad = script(
        bash,
        "rollback",
        "--env",
        "dev",
        "--release",
        "../../etc/passwd",
        "--dry-run",
        stubs=stubs,
        out=tmp_path,
    )
    assert bad.returncode != 0 and "--release must look like" in bad.stderr
