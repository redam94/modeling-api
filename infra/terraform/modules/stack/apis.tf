# APIs per project role. deploy/gcp/bootstrap.sh enables exactly these lists (so the first
# plan can run); tests/infra/test_terraform.py fails if the two drift apart.

locals {
  apis = {
    control = [
      "accesscontextmanager.googleapis.com",
      "cloudkms.googleapis.com",
      "cloudresourcemanager.googleapis.com",
      "cloudtrace.googleapis.com",
      "compute.googleapis.com",
      "iam.googleapis.com",
      "iamcredentials.googleapis.com",
      "identitytoolkit.googleapis.com",
      "logging.googleapis.com",
      "monitoring.googleapis.com",
      "pubsub.googleapis.com",
      "run.googleapis.com",
      "secretmanager.googleapis.com",
      "servicenetworking.googleapis.com",
      "serviceusage.googleapis.com",
      "sqladmin.googleapis.com",
    ]
    exec = [
      "binaryauthorization.googleapis.com",
      "cloudresourcemanager.googleapis.com",
      "compute.googleapis.com",
      "dns.googleapis.com",
      "iam.googleapis.com",
      "logging.googleapis.com",
      "run.googleapis.com",
      "serviceusage.googleapis.com",
    ]
    data = [
      "cloudkms.googleapis.com",
      "cloudresourcemanager.googleapis.com",
      "iam.googleapis.com",
      "serviceusage.googleapis.com",
      "storage.googleapis.com",
    ]
    build = [
      "artifactregistry.googleapis.com",
      "binaryauthorization.googleapis.com",
      "cloudkms.googleapis.com",
      "cloudresourcemanager.googleapis.com",
      "containeranalysis.googleapis.com",
      "containerscanning.googleapis.com",
      "iam.googleapis.com",
      "iamcredentials.googleapis.com",
      "serviceusage.googleapis.com",
      "sts.googleapis.com",
    ]
  }

  project_roles = merge(
    {
      control = { project = var.projects.control, role = "control" }
      data    = { project = var.projects.data, role = "data" }
      build   = { project = var.projects.build, role = "build" }
    },
    { for pool, p in var.exec_pools : "exec-${pool}" => { project = p.project_id, role = "exec" } },
  )

  project_apis = var.manage_apis ? merge([
    for key, p in local.project_roles : {
      for api in local.apis[p.role] : "${key}|${api}" => { project = p.project, api = api }
    }
  ]...) : {}
}

resource "google_project_service" "this" {
  for_each = local.project_apis

  project                    = each.value.project
  service                    = each.value.api
  disable_on_destroy         = false
  disable_dependent_services = false
}
