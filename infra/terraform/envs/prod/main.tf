# prod: highly available database with managed connection pooling, HSM-backed signing key,
# enforced VPC-SC perimeter, a load balancer on real domains, deletion protection, and a
# LOCKED retention policy on the bundles bucket (irreversible: see modules/data).

locals {
  env = "prod"
}

provider "google" {
  region                = var.region
  billing_project       = var.projects.control
  user_project_override = true
}

provider "google-beta" {
  region                = var.region
  billing_project       = var.projects.control
  user_project_override = true
}

module "stack" {
  source = "../../modules/stack"

  providers = {
    google      = google
    google-beta = google-beta
  }

  env             = local.env
  region          = var.region
  bucket_location = var.bucket_location
  labels          = var.labels
  projects        = var.projects
  exec_pools      = var.exec_pools
  images          = var.images
  domains         = var.domains
  state_bucket    = var.state_bucket
  github          = var.github_repository == null ? null : { repository = var.github_repository }

  deletion_protection  = true
  binary_authorization = false # M5: switch on once every base digest is attested

  vpc_sc = {
    access_policy_id = var.access_policy_id
    dry_run          = false
  }

  control = {
    database = {
      edition               = "ENTERPRISE_PLUS"
      tier                  = "db-perf-optimized-N-4"
      availability_type     = "REGIONAL"
      connection_pooling    = true
      disk_size_gb          = 100
      backup_retained_count = 30
    }
    services = {
      api          = { min_instances = 2, max_instances = 50, cpu = "2", memory = "2Gi" }
      events       = { min_instances = 1, max_instances = 20 }
      web          = { min_instances = 1, max_instances = 10 }
      orchestrator = { instances = 1, cpu = "2", memory = "2Gi" }
    }
    oidc = {
      client_id             = var.oidc_client_id
      client_secret_version = var.oidc_client_secret_version
    }
    kms               = { protection_level = "HSM" }
    metrics_collector = { image = var.metrics_collector_image }
    # Cloud Armor: start in preview, read the logs, then set cloud_armor_waf_mode = "enforce"
    cloud_armor        = { waf_mode = var.cloud_armor_waf_mode }
    log_retention_days = 30
  }
  oidc_client_secret = var.oidc_client_secret

  data = {
    runs_ttl_days         = 30
    bundle_retention_days = 30
    lock_bundle_retention = true
    force_destroy         = false
  }
}
