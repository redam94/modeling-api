# Control-plane identities, one per workload, each with only what it uses.
#
#   api           API service: reads DB (RLS-bound role), reads bucket metadata, mints signed URLs
#                 (signBlob as url-signer), verifies bundle signatures (KMS public key).
#   events        events service: verifies pushes, upserts run progress. DB only.
#   orchestrator  dispatcher/reconciler/finalizer worker pool: runs Jobs in the exec projects
#                 (custom role, granted by modules/exec), writes bootstrap objects, copies
#                 verified outputs, signs bundle manifests with KMS.
#   migrate       the one-shot migration job: owner role on the database. Nothing else.
#   web           nginx serving the SPA and the plot sandbox. No roles at all.
#   push          the identity Pub/Sub mints OIDC tokens for when pushing to events; the only
#                 principal allowed to invoke the events service.
#   signer        holds the bucket permissions that signed URLs carry. Nobody has a key for it;
#                 api and orchestrator may only call signBlob on it.
#   uploader      signs the create-only upload policies (every run's outputs). GCS POST forms
#                 carry no generation precondition, so "create-only" means "signed by an
#                 account without storage.objects.delete": this one may only create objects.
#                 Keyless as well; api and orchestrator may only call signBlob on it.
#
# No workload gets roles/cloudsql.client: Cloud SQL is reached over private IP with password
# auth, so the Cloud SQL Admin API is not needed at runtime.

locals {
  service_accounts = {
    api          = { id = "${local.prefix}-api", title = "API service" }
    events       = { id = "${local.prefix}-events", title = "events service (Pub/Sub push)" }
    orchestrator = { id = "${local.prefix}-orch", title = "orchestrator: dispatcher, reconciler, finalizer" }
    migrate      = { id = "${local.prefix}-migrate", title = "database migrations" }
    web          = { id = "${local.prefix}-web", title = "web app and plot sandbox (nginx)" }
    push         = { id = "${local.prefix}-push", title = "Pub/Sub push identity (invokes events)" }
    url-signer   = { id = "${local.prefix}-signer", title = "signed URL identity (signBlob only, no keys)" }
    url-uploader = { id = "${local.prefix}-uploader", title = "create-only upload policy identity (signBlob only, no keys)" }
  }

  # Telemetry for the workloads that emit it.
  telemetry_roles = ["roles/logging.logWriter", "roles/monitoring.metricWriter", "roles/cloudtrace.agent"]
  telemetry_grants = {
    for pair in setproduct(["api", "events", "orchestrator"], local.telemetry_roles) :
    "${pair[0]}|${pair[1]}" => { sa = pair[0], role = pair[1] }
  }
}

resource "google_service_account" "sa" {
  for_each = local.service_accounts

  project      = var.project_id
  account_id   = each.value.id
  display_name = "mapi ${var.env} ${each.key}"
  description  = each.value.title
}

resource "google_project_iam_member" "telemetry" {
  for_each = local.telemetry_grants

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.sa[each.value.sa].email}"
}

# ------------------------------------------------------------------------------- URL signing

resource "google_project_iam_custom_role" "url_signer" {
  project     = var.project_id
  role_id     = "mapi_${replace(var.env, "-", "_")}_url_signer"
  title       = "mapi ${var.env} URL signer"
  description = "signBlob only: mint V4 signed URLs and POST policies without a key file, and without being able to impersonate the signer."
  permissions = ["iam.serviceAccounts.signBlob"]
}

resource "google_service_account_iam_member" "url_signer" {
  for_each = {
    for pair in setproduct(["url-signer", "url-uploader"], ["api", "orchestrator"]) :
    "${pair[0]}|${pair[1]}" => { signer = pair[0], caller = pair[1] }
  }

  service_account_id = google_service_account.sa[each.value.signer].name
  role               = google_project_iam_custom_role.url_signer.id
  member             = "serviceAccount:${google_service_account.sa[each.value.caller].email}"
}
