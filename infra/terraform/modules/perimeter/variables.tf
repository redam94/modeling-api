variable "access_policy_id" {
  description = "Numeric id of the organisation's Access Context Manager policy (gcloud access-context-manager policies list --organization ORG_ID)."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.access_policy_id))
    error_message = "access_policy_id is the numeric policy id."
  }
}

variable "env" {
  description = "Environment name."
  type        = string
}

variable "dry_run" {
  description = "Put the configuration in the dry-run spec only: violations are logged, nothing is blocked. Roll out with true, read the violation logs, then switch to false."
  type        = bool
  default     = false
}

variable "protected_projects" {
  description = "Project numbers inside the perimeter: every exec project and the data project, keyed by a static name."
  type        = map(string)
}

variable "control_project_number" {
  description = "Number of the control project (outside the perimeter; the event sinks publish into it)."
  type        = string
}

variable "build_project_number" {
  description = "Number of the build project (outside; Jobs pull images from it)."
  type        = string
}

variable "exec_project_numbers" {
  description = "Exec project numbers (subset of protected_projects), keyed by pool."
  type        = map(string)
}

variable "data_project_number" {
  description = "Data project number (in protected_projects)."
  type        = string
}

variable "control_identities" {
  description = <<-EOT
    Control-plane service account e-mails allowed in.
    storage: may use Cloud Storage in the data project (api, orchestrator, url-signer). Signed
             URLs are requests made as url-signer from anywhere (browsers, executions), so that
             ingress rule allows any source.
    run:     may run and cancel executions in the exec projects (orchestrator).
  EOT
  type = object({
    storage = map(string)
    run     = map(string)
  })
}

variable "sink_writer_identities" {
  description = "Writer identities (\"serviceAccount:...\") of the exec projects' Logging sinks, keyed by pool. They may publish to the control project's topic and nothing else."
  type        = map(string)
}

variable "deployer_identity" {
  description = "Service account e-mail of the deployer (Terraform). It may administer resources inside the perimeter from outside. Null: add your own ingress rule."
  type        = string
  default     = null
}

variable "restricted_services" {
  description = "APIs the perimeter restricts. Restrict every API that can move data: an unrestricted API reachable through the VIP is an exfiltration path to an attacker's project."
  type        = list(string)
  default = [
    "storage.googleapis.com",
    "run.googleapis.com",
    "pubsub.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "bigquery.googleapis.com",
    "bigtable.googleapis.com",
    "spanner.googleapis.com",
    "datastore.googleapis.com",
    "firestore.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudkms.googleapis.com",
    "artifactregistry.googleapis.com",
    "containerregistry.googleapis.com",
    "cloudfunctions.googleapis.com",
    "cloudbuild.googleapis.com",
    "dns.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
  ]
}

variable "vpc_allowed_services" {
  description = "The only APIs a VPC network inside the perimeter (the executions) may call. Executions need Cloud Storage (signed URLs) and nothing else; the metadata server is not an API call."
  type        = list(string)
  default     = ["storage.googleapis.com"]
}

variable "access_levels" {
  description = "Access levels (accessPolicies/<id>/accessLevels/<name>) admitted to the perimeter, e.g. a break-glass level for operators."
  type        = list(string)
  default     = []
}
