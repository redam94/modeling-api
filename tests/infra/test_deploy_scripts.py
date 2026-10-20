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
    path: Path,
    names: tuple[str, ...] = ("server", "web", "base-bayes", "base-ml", "base-forecast", "base-dl"),
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
    # only read-only calls run: whether the state bucket exists decides what the preview shows
    assert calls(stubs) == [
        "gcloud storage buckets describe gs://acme-dev-build-mapi-tfstate --project acme-dev-build"
    ], "a dry run changes nothing"


def test_bootstrap_dry_run_previews_an_existing_state_bucket(
    bash: str, stubs: Path, tmp_path: Path
) -> None:
    """The preview matches what a real run would do: no create for a bucket that exists."""
    describe_ok = stubs / "gcloud"
    describe_ok.write_text(
        f'#!/bin/sh\necho "gcloud $*" >> "{stubs.parent / "calls.log"}"\n'
        'case "$1 $2 $3" in "storage buckets describe") exit 0 ;; esac\nexit 97\n'
    )
    res = script(bash, "bootstrap", *BOOTSTRAP, "--dry-run", stubs=stubs, out=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "gcloud storage buckets create" not in res.stdout
    assert "state bucket gs://acme-dev-build-mapi-tfstate exists" in res.stderr


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
    assert len(builds) == 6
    for name in ("server", "web", "base-bayes", "base-ml", "base-forecast", "base-dl"):
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
    # nothing ran, and the summary does not claim otherwise
    for claim in ("==> migrations applied", "==> release recorded as", "==> deployed 'staging'"):
        assert claim not in res.stderr, claim
    assert "dry run: nothing was deployed to 'staging'" in res.stderr


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
    assert res.returncode != 0
    assert "lacks: base-bayes, base-ml, base-forecast, base-dl" in res.stderr

    tagged = tmp_path / "tagged.json"
    tagged.write_text(
        json.dumps(
            {
                "images": {
                    n: f"{REPO}/{n}:latest"
                    for n in ("server", "web", "base-bayes", "base-ml", "base-forecast", "base-dl")
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
    assert "migrations applied" not in res.stderr and "dry run" in res.stderr


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


# ------------------------------------------------------------- the real (non-dry) deploy path

FAKE_TERRAFORM = """#!/bin/sh
echo "terraform $*" >> "{log}"
case " $* " in
  *" output -json "*) if [ -f "{outputs}" ]; then cat "{outputs}"; else echo '{{}}'; fi ;;
  *" plan "*) for a in "$@"; do case $a in -out=*) : > "${{a#-out=}}" ;; esac; done ;;
esac
exit 0
"""
FAKE_CURL = """#!/bin/sh
echo "curl $*" >> "{log}"
case " $* " in
  *"%{{http_code}}"*) printf '404' ;;
  *" -D - "*) printf 'HTTP/2 200\\r\\ncontent-security-policy: default-src none\\r\\n\\r\\n' ;;
esac
exit 0
"""


def _fake_cloud(tmp_path: Path, outputs: dict[str, object] | None) -> Path:
    """Stubs that succeed: terraform (canned outputs), gcloud, docker, curl."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    outputs_file = tmp_path / "tf-outputs.json"
    if outputs is not None:
        outputs_file.write_text(json.dumps({k: {"value": v} for k, v in outputs.items()}))
    bodies = {
        "terraform": FAKE_TERRAFORM.format(log=log, outputs=outputs_file),
        "curl": FAKE_CURL.format(log=log),
        "gcloud": f'#!/bin/sh\necho "gcloud $*" >> "{log}"\nexit 0\n',
        "docker": f'#!/bin/sh\necho "docker $*" >> "{log}"\nexit 0\n',
    }
    for name, body in bodies.items():
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    return bin_dir


def _outputs(services_deployed: bool) -> dict[str, object]:
    run_app = {k: f"https://mapi-staging-{k}-1.europe-west4.run.app" for k in ("api", "events")}
    return {
        "control_project": "acme-stg-control",
        "region": "europe-west4",
        "services_deployed": services_deployed,
        "urls": {
            "api": "https://app.example.com",
            "web": "https://app.example.com",
            "plots": "https://plots.example.net",
            "run_app": run_app,
        },
    }


def _deploy(bash: str, tmp_path: Path, bin_dir: Path, *extra: str) -> tuple[int, str, list[str]]:
    backend = tmp_path / "backend.hcl"
    backend.write_text('bucket = "acme-tfstate"\nprefix = "mapi/staging"\n')
    images = images_file(tmp_path / "images.json")
    res = script(
        bash,
        "deploy",
        "--env",
        "staging",
        "--images-file",
        str(images),
        "--backend-config",
        str(backend),
        "--plan-file",
        str(tmp_path / "tfplan"),
        *extra,
        stubs=bin_dir,
        out=tmp_path,
    )
    log = tmp_path / "calls.log"
    return res.returncode, res.stderr, log.read_text().splitlines() if log.exists() else []


def _position(lines: list[str], fragment: str) -> int:
    return next(i for i, line in enumerate(lines) if fragment in line)


def test_deploy_release_migrates_before_apply(bash: str, tmp_path: Path) -> None:
    bin_dir = _fake_cloud(tmp_path, _outputs(services_deployed=True))
    code, err, log = _deploy(bash, tmp_path, bin_dir, "--yes")
    assert code == 0, err
    update = _position(log, "gcloud run jobs update mapi-staging-migrate")
    assert f"--image {REPO}/server@{DIGEST}" in log[update]
    assert "--project acme-stg-control --quiet" in log[update]
    execute = _position(log, "gcloud run jobs execute mapi-staging-migrate")
    apply = _position(log, " apply -input=false")
    record = _position(log, "gcloud storage cp")
    assert update < execute < apply < record
    assert "gs://acme-tfstate/mapi/staging/releases/" in log[record]
    smoke = [line for line in log[record:] if line.startswith("curl")]
    assert any("https://app.example.com/healthz" in c for c in smoke)
    assert any("https://app.example.com/readyz" in c for c in smoke)
    events = "-X POST https://mapi-staging-events-1.europe-west4.run.app/internal/events"
    assert any(events in c for c in smoke)
    assert "smoke test passed" in err


def test_deploy_first_release_migrates_after_apply(bash: str, tmp_path: Path) -> None:
    bin_dir = _fake_cloud(tmp_path, _outputs(services_deployed=False))
    code, err, log = _deploy(bash, tmp_path, bin_dir, "--yes", "--skip-smoke")
    # the stubbed apply leaves services_deployed false, so the post-apply check refuses
    assert code != 0 and "did not include the Cloud Run workloads" in err
    apply = _position(log, " apply -input=false")
    execute = _position(log, "gcloud run jobs execute mapi-staging-migrate")
    assert apply < execute, "the migrate job only exists after the first apply"


def test_deploy_without_confirmation_stops_after_the_plan(bash: str, tmp_path: Path) -> None:
    bin_dir = _fake_cloud(tmp_path, _outputs(services_deployed=True))
    code, err, log = _deploy(bash, tmp_path, bin_dir)
    assert code != 0 and "pass --yes" in err
    assert any(" plan " in line for line in log)
    assert not any(" apply " in line or "gcloud run jobs" in line for line in log)


def test_deploy_refuses_a_public_events_service(bash: str, tmp_path: Path) -> None:
    bin_dir = _fake_cloud(tmp_path, _outputs(services_deployed=True))
    curl = bin_dir / "curl"
    curl.write_text(curl.read_text().replace("printf '404'", "printf '200'"))
    code, err, _ = _deploy(bash, tmp_path, bin_dir, "--yes")
    assert code != 0 and "accepted an unauthenticated request" in err


# ----------------------------------------------------------------- rollback --previous


RELEASES = DEPLOY / "lib" / "releases.py"


def _previous(*names: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-I", str(RELEASES), "previous"],
        input="\n".join(names) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )


def test_previous_skips_retries_and_replays_rollbacks() -> None:
    r0, r1, r2 = (
        "20261001T000000Z-aaaa.json",
        "20261002T000000Z-bbbb.json",
        "20261003T000000Z-cccc.json",
    )
    # a plain history: the release before the newest
    assert _previous(r2, r0, r1).stdout.strip() == r1
    # R2 deployed twice (a retried deploy): still R1, never the deployed release itself
    assert _previous(r0, r1, r2, "20261003T010000Z-cccc.json").stdout.strip() == r1
    # R2 was bad and rolled back to R1: "go back one more" is R0, not the bad R2
    rolled = (r0, r1, r2, "20261004T000000Z-bbbb-rollback.json")
    assert _previous(*rolled).stdout.strip() == r0
    # nothing earlier than what runs now
    lonely = _previous(r0, "20261005T000000Z-aaaa.json")
    assert lonely.returncode == 1 and "no earlier release" in lonely.stderr


def test_rollback_previous_picks_the_release_before_the_deployed_one(
    bash: str, tmp_path: Path
) -> None:
    """End to end over stubs: R1 good, R2 bad and deployed twice; --previous redeploys R1
    and records the rollback under R1's id, so a second --previous goes back to R0."""
    bin_dir = _fake_cloud(tmp_path, _outputs(services_deployed=True))
    listing = tmp_path / "releases.txt"
    names = [
        "20261001T000000Z-r0r0r0.json",
        "20261002T000000Z-r1r1r1.json",
        "20261003T000000Z-r2r2r2.json",
        "20261003T010000Z-r2r2r2.json",
    ]
    listing.write_text("".join(f"gs://acme-tfstate/mapi/staging/releases/{n}\n" for n in names))
    source = images_file(tmp_path / "source-images.json")
    log = tmp_path / "calls.log"
    (bin_dir / "gcloud").write_text(
        "#!/bin/sh\n"
        f'echo "gcloud $*" >> "{log}"\n'
        f'case "$1 $2" in "storage ls") cat "{listing}" ;;\n'
        f'  "storage cp") case "$3" in gs://*) cp "{source}" "$4" ;; esac ;;\n'
        "esac\nexit 0\n"
    )
    backend = tmp_path / "backend.hcl"
    backend.write_text('bucket = "acme-tfstate"\nprefix = "mapi/staging"\n')
    res = script(
        bash, "rollback", "--env", "staging", "--previous", "--backend-config", str(backend),
        "--yes", stubs=bin_dir, out=tmp_path,
    )  # fmt: skip
    assert res.returncode == 0, res.stderr
    lines = log.read_text().splitlines()
    fetched = [line for line in lines if "storage cp gs://" in line]
    assert fetched and fetched[0].split()[3].endswith("/20261002T000000Z-r1r1r1.json"), fetched
    recorded = [line for line in lines if "storage cp" in line and "releases/" in line.split()[-1]]
    assert recorded[-1].split()[-1].endswith("-r1r1r1-rollback.json"), recorded
    assert not any("gcloud run jobs execute" in line for line in lines), "no migrations"
    # the history now says R1 runs again: the next --previous is R0
    after = [*names, recorded[-1].split()[-1].rsplit("/", 1)[-1]]
    assert _previous(*after).stdout.strip() == "20261001T000000Z-r0r0r0.json"


def test_deploy_names_the_release_after_its_images(bash: str, tmp_path: Path) -> None:
    bin_dir = _fake_cloud(tmp_path, _outputs(services_deployed=True))
    code, err, log = _deploy(bash, tmp_path, bin_dir, "--yes")
    assert code == 0, err
    rid = subprocess.run(
        ["python3", "-I", str(RELEASES), "id", str(tmp_path / "images.json")],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    record = log[_position(log, "gcloud storage cp")]
    assert record.endswith(f"-{rid}.json"), record
    sha = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--short=12", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()  # fmt: skip
    assert not sha or sha not in record, "named after the images, not the operator's checkout"
