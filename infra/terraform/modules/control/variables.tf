variable "project_id" {
  description = "The control project (trusted; JSON only; never runs tenant code)."
  type        = string
}

variable "project_number" {
  description = "Number of the control project. Cloud Run service URLs are https://<name>-<number>.<region>.run.app, which lets services know each other's URLs without a dependency cycle."
  type        = string
}

variable "env" {
  description = "Environment name; becomes MAPI_ENV, which selects the bucket names mapi-{env}-*."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,15}$", var.env))
    error_message = "env must match ^[a-z][a-z0-9-]{0,15}$."
  }
}

variable "region" {
  description = "Region of every control-plane resource."
  type        = string
}

variable "labels" {
  description = "Labels added to every labelled resource."
  type        = map(string)
  default     = {}
}

variable "deletion_protection" {
  description = "Refuse to destroy the database, services and jobs."
  type        = bool
  default     = true
}

# --------------------------------------------------------------------------------- images

variable "images" {
  description = "Control-plane images pinned by digest: server (api, events, orchestrator, migrate) and web (app and plot sandbox). Null until build-push.sh has run; the services are only planned once both are set."
  type = object({
    server = optional(string)
    web    = optional(string)
  })
  default = {}

  validation {
    condition = alltrue([
      for img in [var.images.server, var.images.web] :
      img == null || can(regex("^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$", img))
    ])
    error_message = "Images must be pinned by digest (registry/path@sha256:<64 hex>)."
  }
}

# ------------------------------------------------------------------------- wiring from other modules

variable "buckets" {
  description = "Bucket names from modules/data (quarantine, bootstrap, runs, bundles, data)."
  type        = map(string)
}

variable "exec_pools" {
  description = "Exec pools (one per exec project). The orchestrator dispatches to dispatch_pool (MAPI_CLOUDRUN_PROJECT/REGION); a Logging sink in each exec project routes event lines to the events topic."
  type = map(object({
    project = string
    region  = string
    classes = list(string)
    jobs    = map(map(string))
    public  = bool
  }))
}

variable "dispatch_pool" {
  description = "The exec pool the orchestrator submits runs to (its Job inventory becomes MAPI_CLOUDRUN_PROJECT/REGION). Default: the first pool that is not public, else the first pool. One orchestrator serves one pool; per-tier placement across pools is M5."
  type        = string
  default     = null
}

variable "kms_key_version" {
  description = "Version of the bundle-signing key the server signs with (MAPI_KMS_KEY_VERSION). Bump after creating a new version; list the old one in kms_trusted_key_versions while bundles it signed are live (ops/runbooks/rotating-keys.md)."
  type        = number
  default     = 1
}

variable "kms_trusted_key_versions" {
  description = "Older versions of the bundle-signing key whose signatures still verify (MAPI_KMS_TRUSTED_KEY_VERSIONS on every server workload). Without them, every bundle signed before a rotation fails INTEGRITY_FAILURE."
  type        = list(number)
  default     = []

  validation {
    condition     = !contains(var.kms_trusted_key_versions, var.kms_key_version)
    error_message = "kms_trusted_key_versions lists retired versions; the active one is kms_key_version."
  }
}

# ----------------------------------------------------------------------------------- network

variable "network" {
  description = "Control VPC: the subnet Cloud Run services use for Direct VPC egress (to reach Cloud SQL's private IP), and the range reserved for private services access."
  type = object({
    subnet_cidr       = optional(string, "10.32.0.0/24")
    psa_prefix_length = optional(number, 20)
  })
  default = {}
}

# ----------------------------------------------------------------------------------- database

variable "database" {
  description = <<-EOT
    Cloud SQL for PostgreSQL 16, private IP only.
    Managed connection pooling needs the ENTERPRISE_PLUS edition. With pooling on, the API and
    events services connect through the pooler (transaction mode); LISTEN/NOTIFY and the
    procrastinate worker need session semantics, so they use the direct port.
  EOT
  type = object({
    edition                = optional(string, "ENTERPRISE_PLUS")
    tier                   = optional(string, "db-perf-optimized-N-2")
    availability_type      = optional(string, "REGIONAL")
    disk_size_gb           = optional(number, 20)
    connection_pooling     = optional(bool, true)
    pooler_port            = optional(number, 6432)
    max_pool_size          = optional(number, 200)
    backup_retained_count  = optional(number, 14)
    pitr_log_days          = optional(number, 7)
    maintenance_day        = optional(number, 7)
    maintenance_hour       = optional(number, 3)
    password_version       = optional(number, 1)
    query_insights_enabled = optional(bool, true)
  })
  default = {}

  validation {
    condition     = contains(["ENTERPRISE", "ENTERPRISE_PLUS"], var.database.edition)
    error_message = "database.edition must be ENTERPRISE or ENTERPRISE_PLUS."
  }

  validation {
    condition     = !var.database.connection_pooling || var.database.edition == "ENTERPRISE_PLUS"
    error_message = "Managed connection pooling needs database.edition = ENTERPRISE_PLUS."
  }

  validation {
    condition     = contains(["ZONAL", "REGIONAL"], var.database.availability_type)
    error_message = "database.availability_type must be ZONAL or REGIONAL."
  }
}

# ------------------------------------------------------------------------------------ secrets

variable "secret_versions" {
  description = "Bump a number to rotate that generated secret on the next apply (new value, new secret version). Rotating server_secret invalidates every API token."
  type = object({
    server_secret = optional(number, 1)
    metrics_token = optional(number, 1)
  })
  default = {}
}

variable "oidc" {
  description = "Browser sign-in (BFF, authorization code + PKCE). issuer defaults to Google; client_id comes from the OAuth client you register for the web origin."
  type = object({
    issuer                = optional(string, "https://accounts.google.com")
    client_id             = optional(string)
    client_secret_version = optional(number, 0)
  })
  default = {}
}

variable "oidc_client_secret" {
  description = "The OAuth client secret. Ephemeral: written to Secret Manager with a write-only argument and never stored in state or plan files. Required (once) when oidc.client_secret_version is raised."
  type        = string
  default     = null
  sensitive   = true
  ephemeral   = true
}

# ---------------------------------------------------------------------------- identity platform

variable "identity_platform" {
  description = "Identity Platform project config (Q12). authorized_domains defaults to the web origin host."
  type = object({
    enabled              = optional(bool, true)
    authorized_domains   = optional(list(string), [])
    email_signin_enabled = optional(bool, false)
  })
  default = {}
}

# ------------------------------------------------------------------------------------ signing

variable "kms" {
  description = "Bundle-manifest signing key (EC P-256, the same algorithm as the local PEM key)."
  type = object({
    protection_level = optional(string, "SOFTWARE")
  })
  default = {}

  validation {
    condition     = contains(["SOFTWARE", "HSM"], var.kms.protection_level)
    error_message = "kms.protection_level must be SOFTWARE or HSM."
  }
}

# ------------------------------------------------------------------------------------ services

variable "services" {
  description = "Sizing of the Cloud Run services and the orchestrator worker pool."
  type = object({
    api = optional(object({
      min_instances = optional(number, 1)
      max_instances = optional(number, 20)
      cpu           = optional(string, "2")
      memory        = optional(string, "2Gi")
      concurrency   = optional(number, 80)
    }), {})
    events = optional(object({
      min_instances = optional(number, 0)
      max_instances = optional(number, 10)
      cpu           = optional(string, "1")
      memory        = optional(string, "1Gi")
      concurrency   = optional(number, 80)
    }), {})
    web = optional(object({
      min_instances = optional(number, 0)
      max_instances = optional(number, 5)
      cpu           = optional(string, "1")
      memory        = optional(string, "512Mi")
      concurrency   = optional(number, 200)
    }), {})
    orchestrator = optional(object({
      instances = optional(number, 1)
      cpu       = optional(string, "2")
      memory    = optional(string, "2Gi")
    }), {})
  })
  default = {}
}

variable "events_args" {
  description = "Arguments of the events service container. Until mapi-server has a dedicated `events` command, it runs the API app; ingress is internal-only and only the Pub/Sub push identity may invoke it."
  type        = list(string)
  default     = ["api", "--host", "0.0.0.0", "--port", "8080"]
}

variable "metrics_collector" {
  description = "The metrics collector sidecar of api, events and orchestrator: an OpenTelemetry Collector (the Google-built otelcol-google, mirrored into Artifact Registry and pinned by digest) that scrapes each workload's /metrics over loopback with the metrics bearer and writes the series to Managed Service for Prometheus. Without an image no mapi_* series reach Cloud Monitoring: the PromQL alert policies and the dashboard stay empty."
  type = object({
    image           = optional(string)
    scrape_interval = optional(string, "30s")
    cpu             = optional(string, "1")
    memory          = optional(string, "512Mi")
  })
  default = {}

  validation {
    condition     = var.metrics_collector.image == null || can(regex("^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$", var.metrics_collector.image))
    error_message = "metrics_collector.image must be pinned by digest (registry/path@sha256:<64 hex>)."
  }
}

variable "extra_env" {
  description = "Additional plain environment variables per workload (api, events, orchestrator, migrate, web). Never put secrets here."
  type        = map(map(string))
  default     = {}
}

variable "pubsub" {
  description = "Event delivery: push retry back-off and dead-lettering."
  type = object({
    ack_deadline_seconds  = optional(number, 30)
    max_delivery_attempts = optional(number, 10)
    min_backoff           = optional(string, "1s")
    max_backoff           = optional(string, "60s")
  })
  default = {}
}

# ------------------------------------------------------------------------------- load balancer

variable "domains" {
  description = <<-EOT
    Public host names. When set, a global HTTPS load balancer serves the app origin (web, with
    /v1/* and /auth/* routed to the API, so the SPA needs no CORS) and the plot-sandbox origin
    (a different site, as plan §10.3 requires), and the API accepts traffic only from it.
    When null, the services answer on their run.app URLs.
  EOT
  type = object({
    web   = string
    plots = string
  })
  default = null

  validation {
    condition     = var.domains == null || try(var.domains.web != var.domains.plots, false)
    error_message = "The plot sandbox must be served from a different host than the app."
  }
}

variable "security_policy" {
  description = "Optional Cloud Armor policy (self link) attached to the load balancer backends (M3)."
  type        = string
  default     = null
}
