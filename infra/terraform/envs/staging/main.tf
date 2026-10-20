# staging: production shape at a smaller size. Enterprise Plus with managed connection
# pooling (the same connection path as prod), single zone, enforced VPC-SC perimeter, deletion
# protection on. The bundles retention policy is set but not locked, so staging can be rebuilt.

locals {
  env = "staging"
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
  binary_authorization = false
  gpu_jobs             = false # v0.15: the base-dl GPU Job [unverified: no GCP GPU quota tested]

  vpc_sc = {
    access_policy_id = var.access_policy_id
    dry_run          = false
  }

  control = {
    database = {
      edition            = "ENTERPRISE_PLUS"
      tier               = "db-perf-optimized-N-2"
      availability_type  = "ZONAL"
      connection_pooling = true
      disk_size_gb       = 20
    }
    services = {
      api          = { min_instances = 1, max_instances = 10 }
      events       = { min_instances = 0, max_instances = 5 }
      web          = { min_instances = 0, max_instances = 3 }
      orchestrator = { instances = 1 }
    }
    oidc = {
      client_id             = var.oidc_client_id
      client_secret_version = var.oidc_client_secret_version
    }
    kms               = { protection_level = "SOFTWARE" }
    metrics_collector = { image = var.metrics_collector_image }
  }
  oidc_client_secret = var.oidc_client_secret

  data = {
    runs_ttl_days         = 14
    bundle_retention_days = 7
    lock_bundle_retention = false
    force_destroy         = false
  }
}
