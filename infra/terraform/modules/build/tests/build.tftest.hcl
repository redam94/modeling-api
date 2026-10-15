# Supply-chain and CI-identity invariants, checked on a mocked apply.

mock_provider "google" {
  source = "../../testing/mocks/google"
}

variables {
  project_id      = "acme-test-build"
  env             = "test"
  region          = "europe-west4"
  github          = { repository = "acme/modeling-api" }
  state_bucket    = "acme-tfstate"
  deploy_projects = { control = "acme-test-control", data = "acme-test-data" }
}

run "registry" {
  command = apply

  assert {
    condition     = google_artifact_registry_repository.images.docker_config[0].immutable_tags
    error_message = "Image tags are immutable; deploys use digests."
  }

  assert {
    condition     = alltrue([for p in google_artifact_registry_repository.images.cleanup_policies : p.action == "KEEP"])
    error_message = "No automatic deletion: digests referenced by live model versions must survive."
  }

  assert {
    condition     = google_artifact_registry_repository.pypi.mode == "REMOTE_REPOSITORY"
    error_message = "The curated PyPI mirror is a remote repository."
  }
}

run "github_federation" {
  command = apply

  assert {
    condition     = google_iam_workload_identity_pool_provider.github[0].attribute_condition == "assertion.repository == \"acme/modeling-api\""
    error_message = "Only tokens from the configured repository are accepted."
  }

  assert {
    condition     = endswith(google_service_account_iam_member.wif["deployer"].member, "/attribute.environment/test")
    error_message = "The deployer trusts only the protected <env> GitHub environment."
  }

  assert {
    condition     = endswith(google_service_account_iam_member.wif["planner"].member, "/attribute.environment/test-plan")
    error_message = "The planner trusts only <env>-plan."
  }

  assert {
    condition     = length([for k, m in google_project_iam_member.ci : m if startswith(k, "planner|") && !contains(["roles/viewer", "roles/iam.securityReviewer"], m.role)]) == 0
    error_message = "The planner is read-only."
  }

  assert {
    condition     = length([for k, m in google_project_iam_member.ci : m if m.role == "roles/owner" || m.role == "roles/editor"]) == 0
    error_message = "No basic owner/editor roles for CI."
  }
}

run "no_ci_without_github" {
  command = apply

  variables {
    github = null
  }

  assert {
    condition     = length(google_iam_workload_identity_pool.github) == 0 && length(google_service_account.ci) == 0
    error_message = "Without a repository there is no identity pool and no CI account."
  }
}
