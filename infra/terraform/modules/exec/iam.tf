# Identities of the exec project.
#
# The execution service account holds NO project roles. Its only grant is a custom role on the
# bootstrap bucket in the data project (storage.objects.get + delete, no list), made by
# modules/data. It holds no secrets and cannot reach anything else; the run's data comes and
# goes through object-scoped signed URLs minted by the control plane.

resource "google_service_account" "exec" {
  project      = var.project_id
  account_id   = "mapi-${var.env}-exec"
  display_name = "mapi ${var.env} execution (${var.pool})"
  description  = "Identity of tenant executions. Bootstrap get+delete only; no project roles."
}

# The orchestrator (control project) starts, inspects and cancels executions of the existing
# Jobs. It cannot create or update Jobs, so it cannot change an image, a class or the
# execution identity; that stays with Terraform.
resource "google_project_iam_custom_role" "dispatcher" {
  project     = var.project_id
  role_id     = "mapi_${replace(var.env, "-", "_")}_dispatcher"
  title       = "mapi ${var.env} dispatcher"
  description = "Run (with env overrides), list, inspect and cancel executions of existing Cloud Run Jobs."
  permissions = [
    "run.jobs.get",
    "run.jobs.list",
    "run.jobs.run",
    "run.jobs.runWithOverrides",
    "run.executions.get",
    "run.executions.list",
    "run.executions.cancel",
    "run.executions.delete",
    "run.tasks.get",
    "run.tasks.list",
    "run.operations.get",
  ]
}

resource "google_project_iam_member" "dispatcher" {
  project = var.project_id
  role    = google_project_iam_custom_role.dispatcher.id
  member  = "serviceAccount:${var.orchestrator_sa_email}"
}
