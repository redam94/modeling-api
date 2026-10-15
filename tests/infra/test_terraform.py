"""``terraform fmt``, ``terraform validate`` per environment, and the mocked-provider
``terraform test`` suites of every module. Never contacts Google Cloud: init runs with
``-backend=false``, and the module tests replace the Google providers with mocks.

Skipped when terraform is not installed (or the providers cannot be downloaded). Set
``TF_PLUGIN_CACHE_DIR`` to reuse downloaded providers across runs.
"""

from __future__ import annotations

import pytest

from .conftest import ENVS, MODULES, Terraform

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def test_fmt(terraform: Terraform) -> None:
    res = terraform(terraform.workdir, "fmt", "-check", "-recursive", "-diff", "-no-color")
    assert res.returncode == 0, f"terraform fmt would change files:\n{res.stdout}{res.stderr}"


@pytest.mark.parametrize("env", ENVS)
def test_validate(terraform: Terraform, env: str) -> None:
    cwd = terraform.workdir / "envs" / env
    terraform.init(cwd)
    res = terraform(cwd, "validate", "-no-color")
    assert res.returncode == 0, f"terraform validate failed for {env}:\n{res.stdout}{res.stderr}"
    assert "Success!" in res.stdout


@pytest.mark.parametrize("module", MODULES)
def test_module_suite(terraform: Terraform, module: str) -> None:
    cwd = terraform.workdir / "modules" / module
    terraform.init(cwd)
    res = terraform(cwd, "test", "-no-color")
    assert res.returncode == 0, (
        f"terraform test failed for modules/{module}:\n{res.stdout}{res.stderr}"
    )
    assert "0 failed" in res.stdout
