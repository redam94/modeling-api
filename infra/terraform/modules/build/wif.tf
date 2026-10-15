# GitHub Actions -> Google Cloud without long-lived keys (Workload Identity Federation).
#
# The provider accepts tokens only from var.github.repository. Each service account trusts
# exactly one GitHub environment, so the job's environment decides what it can do:
#
#   environment <env>-build  -> ci-push   push images to the images repository, sign attestations
#   environment <env>-plan   -> planner   read-only plan (+ state lock)
#   environment <env>        -> deployer  apply; protect it with required reviewers in GitHub

locals {
  ci_accounts = var.github == null ? {} : {
    ci-push  = { id = "mapi-${var.env}-ci-push", environment = "${var.env}-build", title = "CI: push images" }
    planner  = { id = "mapi-${var.env}-planner", environment = "${var.env}-plan", title = "CI: terraform plan (read-only)" }
    deployer = { id = "mapi-${var.env}-deployer", environment = var.env, title = "CI: terraform apply" }
  }
}

resource "google_iam_workload_identity_pool" "github" {
  count = var.github == null ? 0 : 1

  project                   = var.project_id
  workload_identity_pool_id = "mapi-${var.env}-github"
  display_name              = "mapi ${var.env} GitHub"
  description               = "GitHub Actions of ${var.github.repository}"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count = var.github == null ? 0 : 1

  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub Actions"

  attribute_mapping = {
    "google.subject"             = "assertion.sub"
    "attribute.repository"       = "assertion.repository"
    "attribute.repository_owner" = "assertion.repository_owner"
    "attribute.environment"      = "assertion.environment"
    "attribute.ref"              = "assertion.ref"
    "attribute.workflow_ref"     = "assertion.job_workflow_ref"
  }

  # Reject every token that is not from this repository, before any binding is consulted.
  attribute_condition = "assertion.repository == \"${var.github.repository}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "ci" {
  for_each = local.ci_accounts

  project      = var.project_id
  account_id   = each.value.id
  display_name = "mapi ${var.env} ${each.key}"
  description  = each.value.title
}

resource "google_service_account_iam_member" "wif" {
  for_each = local.ci_accounts

  service_account_id = google_service_account.ci[each.key].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.environment/${each.value.environment}"
}

resource "google_artifact_registry_repository_iam_member" "ci_push" {
  count = var.github == null ? 0 : 1

  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.ci["ci-push"].email}"
}

locals {
  deployer_grants = var.github == null ? {} : merge(
    {
      for pair in setproduct(keys(var.deploy_projects), var.deployer_roles) :
      "deployer|${pair[0]}|${pair[1]}" => { project = var.deploy_projects[pair[0]], role = pair[1], sa = "deployer" }
    },
    {
      for pair in setproduct(keys(var.deploy_projects), var.planner_roles) :
      "planner|${pair[0]}|${pair[1]}" => { project = var.deploy_projects[pair[0]], role = pair[1], sa = "planner" }
    },
  )
}

resource "google_project_iam_member" "ci" {
  for_each = local.deployer_grants

  project = each.value.project
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.ci[each.value.sa].email}"
}

resource "google_storage_bucket_iam_member" "state" {
  for_each = var.github != null && var.state_bucket != null ? {
    deployer = "roles/storage.objectAdmin"
    planner  = "roles/storage.objectUser" # the plan takes (and releases) the state lock
  } : {}

  bucket = var.state_bucket
  role   = each.value
  member = "serviceAccount:${google_service_account.ci[each.key].email}"
}
