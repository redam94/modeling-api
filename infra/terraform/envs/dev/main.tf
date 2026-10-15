# dev: smallest footprint that still exercises every control. Single-zone database without the
# pooler, scale-to-zero services, nothing protected against destroy, the VPC-SC perimeter in
# dry-run (when an access policy is given). Environment policy lives here, in code review;
# project ids and names live in terraform.tfvars.

locals {
  env = "dev"
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

  deletion_protection  = false
  binary_authorization = false

  vpc_sc = {
    access_policy_id = var.access_policy_id
    dry_run          = true
  }

  control = {
    database = {
      edition            = "ENTERPRISE"
      tier               = "db-custom-1-3840"
      availability_type  = "ZONAL"
      connection_pooling = false
      disk_size_gb       = 10
    }
    services = {
      api          = { min_instances = 0, max_instances = 3, cpu = "1", memory = "1Gi" }
      events       = { min_instances = 0, max_instances = 2 }
      web          = { min_instances = 0, max_instances = 2 }
      orchestrator = { instances = 1, cpu = "1", memory = "1Gi" }
    }
    oidc = {
      client_id             = var.oidc_client_id
      client_secret_version = var.oidc_client_secret_version
    }
    kms = { protection_level = "SOFTWARE" }
  }
  oidc_client_secret = var.oidc_client_secret

  data = {
    runs_ttl_days         = 7
    bundle_retention_days = 1
    lock_bundle_retention = false
    force_destroy         = true
  }
}
