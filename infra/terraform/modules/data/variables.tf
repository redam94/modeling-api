variable "project_id" {
  description = "The data project: holds the five buckets (plan §8.6). It sits inside the VPC-SC perimeter with the exec projects."
  type        = string
}

variable "env" {
  description = "Environment name. Buckets are named mapi-{env}-{purpose}, exactly as mapi_spec.storage.bucket_name() builds them, so the server finds them from MAPI_ENV alone."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,15}$", var.env))
    error_message = "env must match ^[a-z][a-z0-9-]{0,15}$ (the rule mapi_spec.storage.bucket_name enforces)."
  }
}

variable "location" {
  description = "Bucket location: a region (e.g. europe-west4) or a multi-region (EU, US). Pick it from the tenants' data residency."
  type        = string
}

variable "labels" {
  description = "Labels added to every bucket."
  type        = map(string)
  default     = {}
}

variable "kms_key_name" {
  description = "Optional CMEK: a Cloud KMS key (projects/.../cryptoKeys/...) that becomes the default key of every bucket. Keep it in a project inside the perimeter, or add a perimeter egress rule for cloudkms."
  type        = string
  default     = null
}

variable "cors_origins" {
  description = "Browser origins allowed to use signed URLs directly (the web app origin). Uploads go to quarantine by signed PUT; downloads come from data and bundles by signed GET."
  type        = list(string)
  default     = []
}

variable "quarantine_ttl_days" {
  description = "Raw uploads are deleted after this many days (plan §8.6: 7)."
  type        = number
  default     = 7

  validation {
    condition     = var.quarantine_ttl_days >= 1 && var.quarantine_ttl_days <= 30
    error_message = "quarantine_ttl_days must be between 1 and 30."
  }
}

variable "runs_ttl_days" {
  description = "Per-attempt scratch under runs/ is deleted after this many days. Verified outputs have been copied to bundles/ or data/ long before."
  type        = number
  default     = 30

  validation {
    condition     = var.runs_ttl_days >= 1
    error_message = "runs_ttl_days must be at least 1."
  }
}

variable "bundle_retention_days" {
  description = "Retention period of the bundles bucket: no object can be deleted or overwritten before it is this old."
  type        = number
  default     = 30

  validation {
    condition     = var.bundle_retention_days >= 1
    error_message = "bundle_retention_days must be at least 1."
  }
}

variable "lock_bundle_retention" {
  description = "Lock the bundles retention policy. IRREVERSIBLE: a locked policy can never be shortened or removed, and the bucket cannot be deleted until every object has aged out. Enable in production only."
  type        = bool
  default     = false
}

variable "force_destroy" {
  description = "Let `terraform destroy` delete non-empty buckets (dev only). Never applies to bundles."
  type        = bool
  default     = false
}

variable "access" {
  description = <<-EOT
    Who may touch which bucket (service account e-mails, no "serviceAccount:" prefix).
    - orchestrator: dispatcher, reconciler and finalizer (writes bootstrap objects, verifies and copies outputs).
    - api: the API service (metadata reads, upload bookkeeping).
    - url_signer: the identity whose signBlob signs V4 URLs; signed URLs act with its permissions.
    - exec: pool name => execution service account of that exec project (get+delete on bootstrap only).
  EOT
  type = object({
    orchestrator = string
    api          = string
    url_signer   = string
    exec         = map(string)
  })
}
