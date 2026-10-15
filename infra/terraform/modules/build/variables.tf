variable "project_id" {
  description = "The build project: Artifact Registry, the curated PyPI mirror, the Binary Authorization attestor and the CI identity pool."
  type        = string
}

variable "env" {
  description = "Environment name."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,15}$", var.env))
    error_message = "env must match ^[a-z][a-z0-9-]{0,15}$."
  }
}

variable "region" {
  description = "Artifact Registry location (keep it next to the Jobs: image pulls are on the cold-start path)."
  type        = string
}

variable "labels" {
  description = "Labels added to every labelled resource."
  type        = map(string)
  default     = {}
}

variable "image_readers" {
  description = "Members (\"serviceAccount:...\") that pull images: the Cloud Run service agents of the control and exec projects. Keyed by a static name."
  type        = map(string)
  default     = {}
}

variable "pypi_readers" {
  description = "Members allowed to resolve wheels from the curated PyPI remote (the M4 overlay resolve runs). Keyed by a static name."
  type        = map(string)
  default     = {}
}

variable "github" {
  description = <<-EOT
    GitHub Actions through Workload Identity Federation (no service-account keys, plan §9).
    repository: "owner/name". Tokens are accepted only from that repository, and each service
    account only from its GitHub environment:
      <env>-build  -> ci-push   (push images)
      <env>-plan   -> planner   (read-only: terraform plan)
      <env>        -> deployer  (terraform apply; protect this environment with required reviewers)
    Null disables the pool.
  EOT
  type = object({
    repository = string
  })
  default = null

  validation {
    condition     = var.github == null || can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github.repository))
    error_message = "github.repository must be owner/name."
  }
}

variable "deploy_projects" {
  description = "Projects the deployer and planner act on (control, exec..., data, build), keyed by a static name."
  type        = map(string)
  default     = {}
}

variable "deployer_roles" {
  description = "Project roles the deployer gets on every deploy project. Terraform manages IAM, so the set is necessarily powerful: guard the GitHub environment with required reviewers."
  type        = list(string)
  default = [
    "roles/artifactregistry.admin",
    "roles/binaryauthorization.attestorsAdmin",
    "roles/cloudkms.admin",
    "roles/cloudsql.admin",
    "roles/compute.loadBalancerAdmin",
    "roles/compute.networkAdmin",
    "roles/compute.securityAdmin",
    "roles/containeranalysis.notes.editor",
    "roles/dns.admin",
    "roles/iam.roleAdmin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser",
    "roles/iam.workloadIdentityPoolAdmin",
    "roles/identityplatform.admin",
    "roles/logging.configWriter",
    "roles/pubsub.admin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/run.admin",
    "roles/secretmanager.admin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/storage.admin",
  ]
}

variable "planner_roles" {
  description = "Read-only project roles for terraform plan."
  type        = list(string)
  default     = ["roles/viewer", "roles/iam.securityReviewer"]
}

variable "state_bucket" {
  description = "Terraform state bucket (created by deploy/gcp/bootstrap.sh). The deployer and planner get object access to it (the planner needs it for the state lock)."
  type        = string
  default     = null
}

variable "kms_protection_level" {
  description = "Protection level of the attestation signing key."
  type        = string
  default     = "SOFTWARE"
}
