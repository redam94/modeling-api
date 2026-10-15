""".github/workflows/deploy.yml: manual only, keyless (WIF), approvals before apply."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from .conftest import ROOT

WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"


@pytest.fixture(scope="module")
def wf() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(data, dict)
    # YAML 1.1 reads the key `on` as True
    data["on"] = data.pop(True, data.get("on"))
    return data


def test_manual_dispatch_only(wf: dict[str, Any]) -> None:
    assert set(wf["on"]) == {"workflow_dispatch"}, "deploys never run on push or pull requests"
    env_input = wf["on"]["workflow_dispatch"]["inputs"]["environment"]
    assert env_input["type"] == "choice"
    assert env_input["options"] == ["dev", "staging", "prod"]


def test_least_privilege_token(wf: dict[str, Any]) -> None:
    assert wf["permissions"] == {"contents": "read"}
    for name, job in wf["jobs"].items():
        assert job["permissions"] == {"contents": "read", "id-token": "write"}, name


def test_one_deploy_per_environment_at_a_time(wf: dict[str, Any]) -> None:
    assert wf["concurrency"]["group"] == "deploy-${{ inputs.environment }}"
    assert wf["concurrency"]["cancel-in-progress"] is False, "never cancel an apply half-way"


def test_jobs_and_environments(wf: dict[str, Any]) -> None:
    jobs = wf["jobs"]
    assert list(jobs) == ["build", "plan", "apply"]
    assert jobs["build"]["environment"] == "${{ inputs.environment }}-build"
    assert jobs["plan"]["environment"] == "${{ inputs.environment }}-plan"
    assert jobs["apply"]["environment"]["name"] == "${{ inputs.environment }}"
    assert jobs["plan"]["needs"] == "build" and jobs["apply"]["needs"] == "plan"


def _step_uses(job: dict[str, Any]) -> list[str]:
    return [s["uses"] for s in job["steps"] if "uses" in s]


def test_keyless_auth_with_one_identity_per_job(wf: dict[str, Any]) -> None:
    expected = {"build": "GCP_CI_PUSH_SA", "plan": "GCP_PLANNER_SA", "apply": "GCP_DEPLOYER_SA"}
    for name, job in wf["jobs"].items():
        auth = [
            s for s in job["steps"] if s.get("uses", "").startswith("google-github-actions/auth@")
        ]
        assert len(auth) == 1, name
        with_ = auth[0]["with"]
        assert with_["workload_identity_provider"] == "${{ vars.GCP_WIF_PROVIDER }}"
        assert with_["service_account"] == f"${{{{ vars.{expected[name]} }}}}"
        assert "credentials_json" not in with_, "no service-account keys"


def test_apply_runs_exactly_the_reviewed_plan(wf: dict[str, Any]) -> None:
    plan_run = "\n".join(s.get("run", "") for s in wf["jobs"]["plan"]["steps"])
    assert "deploy/gcp/deploy.sh" in plan_run and "--plan-only" in plan_run
    apply_run = "\n".join(s.get("run", "") for s in wf["jobs"]["apply"]["steps"])
    assert "--apply-plan" in apply_run and "--yes" in apply_run
    assert "terraform apply" not in apply_run, (
        "apply goes through deploy.sh (migrations, smoke test)"
    )
    uses = _step_uses(wf["jobs"]["apply"])
    assert any(u.startswith("actions/download-artifact@") for u in uses)


def test_terraform_version_matches_the_constraint(wf: dict[str, Any]) -> None:
    version = wf["env"]["TF_VERSION"]
    major, minor, _ = (int(x) for x in version.split("."))
    assert (major, minor) >= (1, 11), "write-only arguments and ephemeral resources need 1.11+"
