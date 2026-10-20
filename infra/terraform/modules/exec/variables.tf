variable "project_id" {
  description = "The exec project (hostile: runs tenant code). One instance of this module per exec project (exec-paid, exec-public)."
  type        = string
}

variable "env" {
  description = "Environment name (dev, staging, prod)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,15}$", var.env))
    error_message = "env must match ^[a-z][a-z0-9-]{0,15}$."
  }
}

variable "pool" {
  description = "Name of this exec pool, e.g. paid or public. Used in names and labels; placement code picks the pool by trust tier."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{0,9}$", var.pool))
    error_message = "pool must be 1-10 lowercase letters or digits, starting with a letter."
  }
}

variable "region" {
  description = "Region of the Jobs, the subnet and the response policy."
  type        = string
}

variable "labels" {
  description = "Labels added to every labelled resource."
  type        = map(string)
  default     = {}
}

variable "subnet_cidr" {
  description = "Subnet the executions get their Direct VPC egress addresses from. Each running task holds one address, so size it for peak concurrency (a /20 gives about 4,000)."
  type        = string
  default     = "10.64.0.0/20"
}

variable "images" {
  description = "Runner image per base, pinned by digest: {base-bayes = \"...@sha256:...\", base-ml = ..., base-forecast = ..., base-dl = ...}. Tags are refused."
  type        = map(string)

  validation {
    condition     = alltrue([for img in values(var.images) : can(regex("^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$", img))])
    error_message = "Every runner image must be pinned by digest (registry/path@sha256:<64 hex>)."
  }

  validation {
    condition     = length(setsubtract(keys(var.images), ["base-bayes", "base-ml", "base-forecast", "base-dl"])) == 0
    error_message = "images keys must be among base-bayes, base-ml, base-forecast and base-dl."
  }
}

variable "gpu_jobs" {
  description = "Create the GPU Job of base-dl (one NVIDIA L4, resource class GPU; v0.15). Needs images[\"base-dl\"] and Cloud Run GPU quota in the region (L4 GPUs are offered in some regions only). [unverified: Cloud Run Jobs GPU (L4) needs a region with GPU quota; not applied or tested against GCP]"
  type        = bool
  default     = false
}

variable "gpu_class_spec" {
  nullable    = false
  description = "CPU, memory, task timeout and GPU of the GPU class (mapi_spec.resources.RESOURCE_CLASSES[GPU]: 8 vCPU, 32 GiB, one nvidia-l4). Cloud Run needs at least 4 vCPU and 16 GiB per GPU."
  type = object({
    cpu         = string
    memory      = string
    timeout     = string
    accelerator = string
    gpus        = number
  })
  default = {
    cpu         = "8"
    memory      = "32Gi"
    timeout     = "3600s"
    accelerator = "nvidia-l4"
    gpus        = 1
  }

  validation {
    condition     = var.gpu_class_spec.gpus == 1
    error_message = "A Cloud Run Job task gets one GPU."
  }
}

variable "classes" {
  description = "Resource classes (plan §8.5) to create Jobs for. XL is not a Cloud Run class (GKE, M6)."
  type        = list(string)
  default     = ["S", "M", "L"]

  validation {
    condition     = length(var.classes) > 0 && alltrue([for c in var.classes : contains(["S", "M", "L"], c)])
    error_message = "classes must be a non-empty subset of S, M and L."
  }
}

variable "class_specs" {
  nullable    = false
  description = "CPU, memory and task timeout per class. Cloud Run's filesystem lives in memory, so memory includes the scratch budget (mapi_spec.resources.RESOURCE_CLASSES)."
  type = map(object({
    cpu     = string
    memory  = string
    timeout = string
  }))
  default = {
    S = { cpu = "2", memory = "8Gi", timeout = "3600s" }
    M = { cpu = "4", memory = "16Gi", timeout = "3600s" }
    L = { cpu = "8", memory = "32Gi", timeout = "3600s" }
  }
}

variable "bootstrap_url_template" {
  description = "MAPI_BOOTSTRAP_URL baked into every Job: gs://mapi-{env}-bootstrap/{nonce}. The only per-execution overrides are MAPI_RUN_ID and MAPI_NONCE."
  type        = string

  validation {
    condition     = can(regex("^gs://[a-z0-9._-]+/\\{nonce\\}$", var.bootstrap_url_template))
    error_message = "bootstrap_url_template must look like gs://<bucket>/{nonce}."
  }
}

variable "orchestrator_sa_email" {
  description = "The control-plane orchestrator service account. It gets a custom role here: run and cancel executions of existing Jobs, nothing else (it cannot change a Job's image, resources or service account)."
  type        = string
}

variable "deletion_protection" {
  description = "Refuse `terraform destroy` on the Jobs."
  type        = bool
  default     = true
}

variable "binary_authorization" {
  description = "Require the project's Binary Authorization policy on every Job (plan M5; needs an attestation for each base image)."
  type        = bool
  default     = false
}

variable "dns_sinkhole" {
  nullable    = false
  description = "Local answers for every name the response policy does not let through. Answering locally (instead of recursing) is what stops DNS exfiltration: the query never reaches an authoritative server. Defaults are non-routable (RFC 5737, RFC 6666), and egress to them is denied anyway."
  type = object({
    ipv4 = string
    ipv6 = string
    ttl  = number
  })
  default = {
    ipv4 = "192.0.2.1"
    ipv6 = "100::1"
    ttl  = 300
  }
}

variable "flow_logs" {
  nullable    = false
  description = "VPC flow logs on the exec subnet (a red-team gate reads them). Sampling 0-1."
  type = object({
    enabled  = bool
    sampling = number
  })
  default = {
    enabled  = true
    sampling = 0.5
  }
}
