# One environment of the platform (plan §8.1 topology):
#
#   control  trusted: API, events, orchestrator, Cloud SQL, secrets, KMS, Pub/Sub, identity
#   exec-*   hostile: Cloud Run Jobs in a zero-egress VPC (one project per pool)
#   data     buckets; inside the VPC-SC perimeter with the exec projects
#   build    Artifact Registry, curated PyPI, Binary Authorization attestor, CI identities

locals {
  labels = merge(var.labels, { mapi-env = var.env, managed-by = "terraform" })

  runner_bases  = ["base-bayes", "base-ml", "base-forecast"]
  runner_images = { for k, v in var.images : k => v if contains(local.runner_bases, k) }
}

data "google_project" "this" {
  for_each   = local.project_roles
  project_id = each.value.project
}

# Cloud Run service agents (they pull images) exist only once requested.
resource "google_project_service_identity" "run" {
  provider = google-beta
  for_each = { for k, p in local.project_roles : k => p.project if contains(["control", "exec"], p.role) }

  project = each.value
  service = "run.googleapis.com"

  depends_on = [google_project_service.this]
}

# ---------------------------------------------------------------------------------- build

module "build" {
  source = "../build"

  project_id   = var.projects.build
  env          = var.env
  region       = var.region
  labels       = local.labels
  github       = var.github
  state_bucket = var.state_bucket

  image_readers = {
    for k, sa in google_project_service_identity.run : "run-agent-${k}" => "serviceAccount:${sa.email}"
  }
  deploy_projects = { for k, p in local.project_roles : k => p.project }

  depends_on = [google_project_service.this]
}

# -------------------------------------------------------------------------------- control

module "control" {
  source = "../control"

  project_id          = var.projects.control
  project_number      = data.google_project.this["control"].number
  env                 = var.env
  region              = var.region
  labels              = local.labels
  deletion_protection = var.deletion_protection

  images = {
    server = lookup(var.images, "server", null)
    web    = lookup(var.images, "web", null)
  }

  buckets = module.data.bucket_names
  exec_pools = {
    for pool, m in module.exec : pool => merge(m.inventory, { public = var.exec_pools[pool].public })
  }

  domains            = var.domains
  security_policy    = var.security_policy
  oidc_client_secret = var.oidc_client_secret

  network                  = lookup(var.control, "network", {})
  database                 = lookup(var.control, "database", {})
  secret_versions          = lookup(var.control, "secret_versions", {})
  oidc                     = lookup(var.control, "oidc", {})
  identity_platform        = lookup(var.control, "identity_platform", {})
  kms                      = lookup(var.control, "kms", {})
  services                 = lookup(var.control, "services", {})
  pubsub                   = lookup(var.control, "pubsub", {})
  extra_env                = lookup(var.control, "extra_env", {})
  metrics_collector        = lookup(var.control, "metrics_collector", {})
  dispatch_pool            = lookup(var.control, "dispatch_pool", null)
  kms_key_version          = lookup(var.control, "kms_key_version", 1)
  kms_trusted_key_versions = lookup(var.control, "kms_trusted_key_versions", [])
  events_args              = lookup(var.control, "events_args", ["api", "--host", "0.0.0.0", "--port", "8080"])

  depends_on = [google_project_service.this]
}

# ----------------------------------------------------------------------------------- exec

module "exec" {
  source   = "../exec"
  for_each = var.exec_pools

  project_id             = each.value.project_id
  env                    = var.env
  pool                   = each.key
  region                 = var.region
  labels                 = local.labels
  subnet_cidr            = each.value.subnet_cidr
  classes                = each.value.classes
  images                 = local.runner_images
  bootstrap_url_template = module.data.bootstrap_url_template
  orchestrator_sa_email  = module.control.service_accounts["orchestrator"]
  deletion_protection    = var.deletion_protection
  binary_authorization   = var.binary_authorization

  class_specs  = lookup(var.exec, "class_specs", null) == null ? null : var.exec.class_specs
  dns_sinkhole = lookup(var.exec, "dns_sinkhole", null) == null ? null : var.exec.dns_sinkhole
  flow_logs    = lookup(var.exec, "flow_logs", null) == null ? null : var.exec.flow_logs

  providers = {
    google      = google
    google-beta = google-beta
  }

  depends_on = [google_project_service.this]
}

# ----------------------------------------------------------------------------------- data

module "data" {
  source = "../data"

  project_id   = var.projects.data
  env          = var.env
  location     = var.bucket_location
  labels       = local.labels
  cors_origins = [module.control.urls.web]

  access = {
    orchestrator = module.control.service_accounts["orchestrator"]
    api          = module.control.service_accounts["api"]
    url_signer   = module.control.service_accounts["url-signer"]
    url_uploader = module.control.service_accounts["url-uploader"]
    exec         = { for pool, m in module.exec : pool => m.exec_sa_email }
  }

  kms_key_name          = lookup(var.data, "kms_key_name", null)
  quarantine_ttl_days   = lookup(var.data, "quarantine_ttl_days", 7)
  runs_ttl_days         = lookup(var.data, "runs_ttl_days", 30)
  bundle_retention_days = lookup(var.data, "bundle_retention_days", 30)
  lock_bundle_retention = lookup(var.data, "lock_bundle_retention", false)
  force_destroy         = lookup(var.data, "force_destroy", false)

  depends_on = [google_project_service.this]
}

# ------------------------------------------------------------------------------ perimeter

module "perimeter" {
  source = "../perimeter"
  count  = var.vpc_sc.access_policy_id == null ? 0 : 1

  access_policy_id = var.vpc_sc.access_policy_id
  env              = var.env
  dry_run          = var.vpc_sc.dry_run
  access_levels    = var.vpc_sc.access_levels

  protected_projects = merge(
    { data = data.google_project.this["data"].number },
    { for pool in keys(var.exec_pools) : "exec-${pool}" => data.google_project.this["exec-${pool}"].number },
  )
  exec_project_numbers   = { for pool in keys(var.exec_pools) : pool => data.google_project.this["exec-${pool}"].number }
  data_project_number    = data.google_project.this["data"].number
  control_project_number = data.google_project.this["control"].number
  build_project_number   = data.google_project.this["build"].number

  control_identities = {
    storage = {
      api          = module.control.service_accounts["api"]
      orchestrator = module.control.service_accounts["orchestrator"]
      url-signer   = module.control.service_accounts["url-signer"]
      url-uploader = module.control.service_accounts["url-uploader"]
    }
    run = {
      orchestrator = module.control.service_accounts["orchestrator"]
    }
  }
  sink_writer_identities = module.control.events.sink_writers
  deployer_identity      = try(module.build.github.service_accounts["deployer"], null)
}

check "perimeter_enabled" {
  assert {
    condition     = var.vpc_sc.access_policy_id != null
    error_message = "No VPC Service Controls perimeter (vpc_sc.access_policy_id is null): executions can reach any Google API resource through the restricted VIP. Acceptable only for a dev environment without an organisation."
  }
}

check "runner_images_complete" {
  assert {
    condition     = length(local.runner_images) == 0 || length(local.runner_images) == length(local.runner_bases)
    error_message = "Only some runner bases have images: the Job inventory is incomplete."
  }
}
