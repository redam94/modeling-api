variable "region" {
  description = "Region of the control plane, the Jobs and the registry."
  type        = string
}

variable "bucket_location" {
  description = "Location of the data buckets (region or multi-region)."
  type        = string
}

variable "projects" {
  description = "Project ids: control, data, build."
  type = object({
    control = string
    data    = string
    build   = string
  })
}

variable "exec_pools" {
  description = "Exec projects keyed by pool (paid, public)."
  type = map(object({
    project_id  = string
    public      = optional(bool, false)
    classes     = optional(list(string), ["S", "M", "L"])
    subnet_cidr = optional(string, "10.64.0.0/20")
  }))
}

variable "images" {
  description = "Digest-pinned images (the file build-push.sh writes). Empty for a foundation apply."
  type        = map(string)
  default     = {}
}

variable "access_policy_id" {
  description = "Access Context Manager policy id for the VPC-SC perimeter (required outside dev)."
  type        = string
}

variable "domains" {
  description = "App and plot-sandbox host names (global HTTPS load balancer); null serves run.app URLs."
  type = object({
    web   = string
    plots = string
  })
  default = null
}

variable "oidc_client_id" {
  description = "OAuth client id registered for the web origin (null: sign-in not configured yet)."
  type        = string
  default     = null
}

variable "oidc_client_secret_version" {
  description = "Raise to write var.oidc_client_secret as a new secret version (0 = none yet)."
  type        = number
  default     = 0
}

variable "oidc_client_secret" {
  description = "OAuth client secret (TF_VAR_oidc_client_secret). Ephemeral: never stored in state or plan files."
  type        = string
  default     = null
  sensitive   = true
  ephemeral   = true
}

variable "github_repository" {
  description = "owner/name of the GitHub repository whose Actions may deploy (null: no CI identities)."
  type        = string
  default     = null
}

variable "state_bucket" {
  description = "The Terraform state bucket (same value as in backend.hcl)."
  type        = string
  default     = null
}

variable "labels" {
  description = "Extra labels for every resource."
  type        = map(string)
  default     = {}
}

variable "metrics_collector_image" {
  description = "The OpenTelemetry Collector sidecar image (Google-built otelcol-google, mirrored into Artifact Registry) pinned by digest. It ships the mapi_* series to Managed Service for Prometheus; null: no metrics in Cloud Monitoring."
  type        = string
  default     = null
}
