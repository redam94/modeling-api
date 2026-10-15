variable "env" {
  description = "Environment name (dev, staging, prod). Becomes MAPI_ENV and the bucket prefix mapi-{env}-."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,15}$", var.env))
    error_message = "env must match ^[a-z][a-z0-9-]{0,15}$."
  }
}

variable "region" {
  description = "Region of the control plane, the Jobs and the registry."
  type        = string
}

variable "bucket_location" {
  description = "Location of the data buckets (region or multi-region; follow data residency)."
  type        = string
}

variable "labels" {
  description = "Labels added to every labelled resource (mapi-env is always added)."
  type        = map(string)
  default     = {}
}

variable "projects" {
  description = "Project ids of the control, data and build projects (plan §8.1: one project per trust zone)."
  type = object({
    control = string
    data    = string
    build   = string
  })
}

variable "exec_pools" {
  description = "Exec projects keyed by pool name (paid, public). public = true marks the pool that hosts public-tier tenants (exec-public, plan Q7)."
  type = map(object({
    project_id  = string
    public      = optional(bool, false)
    classes     = optional(list(string), ["S", "M", "L"])
    subnet_cidr = optional(string, "10.64.0.0/20")
  }))

  validation {
    condition     = length(var.exec_pools) > 0
    error_message = "At least one exec pool is needed."
  }
}

variable "images" {
  description = "Digest-pinned images from deploy/gcp/build-push.sh: server, web, base-bayes, base-ml, base-forecast. Empty for a foundation apply."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for k in keys(var.images) : contains(["server", "web", "base-bayes", "base-ml", "base-forecast"], k)])
    error_message = "images keys must be among server, web, base-bayes, base-ml, base-forecast."
  }

  validation {
    condition     = alltrue([for img in values(var.images) : can(regex("@sha256:[0-9a-f]{64}$", img))])
    error_message = "Every image must be pinned by digest (...@sha256:<64 hex>). Tags are never deployed."
  }
}

variable "vpc_sc" {
  description = "VPC Service Controls. access_policy_id null disables the perimeter (dev without an organisation only; a check warns). dry_run logs violations without blocking."
  type = object({
    access_policy_id = optional(string)
    dry_run          = optional(bool, false)
    access_levels    = optional(list(string), [])
  })
  default = {}
}

variable "domains" {
  description = "App and plot-sandbox host names behind a global HTTPS load balancer; null serves the run.app URLs."
  type = object({
    web   = string
    plots = string
  })
  default = null
}

variable "security_policy" {
  description = "Optional Cloud Armor policy for the load balancer backends."
  type        = string
  default     = null
}

variable "control" {
  description = "Settings passed to modules/control (database, services, network, pubsub, kms, kms_key_version, identity_platform, secret_versions, oidc, extra_env, events_args, dispatch_pool); see that module's variables."
  type        = any
  default     = {}
}

variable "oidc_client_secret" {
  description = "OAuth client secret, written to Secret Manager write-only (never in state). Only read when control.oidc.client_secret_version is raised."
  type        = string
  default     = null
  sensitive   = true
  ephemeral   = true
}

variable "data" {
  description = "Settings passed to modules/data (kms_key_name, quarantine_ttl_days, runs_ttl_days, bundle_retention_days, lock_bundle_retention, force_destroy)."
  type        = any
  default     = {}
}

variable "exec" {
  description = "Settings applied to every exec pool (class_specs, dns_sinkhole, flow_logs)."
  type        = any
  default     = {}
}

variable "github" {
  description = "GitHub repository for Workload Identity Federation ({repository = \"owner/name\"}); null disables CI identities."
  type = object({
    repository = string
  })
  default = null
}

variable "state_bucket" {
  description = "Terraform state bucket (bootstrap.sh); the CI identities get object access to it."
  type        = string
  default     = null
}

variable "deletion_protection" {
  description = "Protect the database, services and Jobs against destroy."
  type        = bool
  default     = true
}

variable "binary_authorization" {
  description = "Enforce the exec projects' Binary Authorization policy on the Jobs (M5)."
  type        = bool
  default     = false
}

variable "manage_apis" {
  description = "Enable the required APIs on every project (bootstrap.sh enables the same list; tests keep the two in sync)."
  type        = bool
  default     = true
}
