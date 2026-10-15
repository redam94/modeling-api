# VPC Service Controls perimeter around the exec projects and the data project (plan §9:
# "deny-all egress; restricted VIP only; VPC-SC perimeter around exec and data").
#
# Executions can reach only restricted.googleapis.com (modules/exec). This perimeter is what
# makes that VIP safe: through it, they can reach only resources inside the perimeter, so an
# attacker-owned bucket (or topic, or dataset) is unreachable even with valid credentials.
#
# Traffic that legitimately crosses the boundary is listed explicitly:
#   ingress  control plane -> Cloud Storage in the data project (api, orchestrator, url-signer;
#            url-signer from any source, because signed URLs are used by browsers and Jobs)
#   ingress  orchestrator -> Cloud Run in the exec projects (run / cancel executions)
#   ingress  deployer -> everything inside (Terraform)
#   egress   exec Logging sinks -> Pub/Sub Publish on the control project's events topic
#   egress   exec Cloud Run service agents -> Artifact Registry in the build project (pulls)

locals {
  protected = [for n in values(var.protected_projects) : "projects/${n}"]
  exec      = [for n in values(var.exec_project_numbers) : "projects/${n}"]

  ingress = concat(
    [
      {
        title      = "control plane to Cloud Storage"
        identities = [for e in values(var.control_identities.storage) : "serviceAccount:${e}"]
        resources  = ["projects/${var.data_project_number}"]
        service    = "storage.googleapis.com"
      },
      {
        title      = "orchestrator to Cloud Run Jobs"
        identities = [for e in values(var.control_identities.run) : "serviceAccount:${e}"]
        resources  = local.exec
        service    = "run.googleapis.com"
      },
    ],
    var.deployer_identity == null ? [] : [
      {
        title      = "deployer (Terraform)"
        identities = ["serviceAccount:${var.deployer_identity}"]
        resources  = local.protected
        service    = "*"
      },
    ],
  )

  egress = [
    {
      title      = "event sinks to the control topic"
      identities = values(var.sink_writer_identities)
      resources  = ["projects/${var.control_project_number}"]
      service    = "pubsub.googleapis.com"
      method     = "google.pubsub.v1.Publisher.Publish"
    },
    {
      title      = "image pulls from the build project"
      identities = [for n in values(var.exec_project_numbers) : "serviceAccount:service-${n}@serverless-robot-prod.iam.gserviceaccount.com"]
      resources  = ["projects/${var.build_project_number}"]
      service    = "artifactregistry.googleapis.com"
      method     = "*"
    },
  ]
}

resource "google_access_context_manager_service_perimeter" "exec_data" {
  parent         = "accessPolicies/${var.access_policy_id}"
  name           = "accessPolicies/${var.access_policy_id}/servicePerimeters/mapi_${replace(var.env, "-", "_")}_exec_data"
  title          = "mapi-${var.env}-exec-data"
  description    = "mapi ${var.env}: exec projects + data project. Executions reach only resources inside."
  perimeter_type = "PERIMETER_TYPE_REGULAR"

  # Dry-run: the configuration lives in `spec` and is only evaluated (logged), not enforced.
  use_explicit_dry_run_spec = var.dry_run

  dynamic "status" {
    for_each = var.dry_run ? [] : [1]
    content {
      resources           = local.protected
      restricted_services = var.restricted_services
      access_levels       = var.access_levels

      vpc_accessible_services {
        enable_restriction = true
        allowed_services   = var.vpc_allowed_services
      }

      dynamic "ingress_policies" {
        for_each = local.ingress
        content {
          title = ingress_policies.value.title
          ingress_from {
            identities = ingress_policies.value.identities
            sources {
              access_level = "*"
            }
          }
          ingress_to {
            resources = ingress_policies.value.resources
            operations {
              service_name = ingress_policies.value.service
              dynamic "method_selectors" {
                for_each = ingress_policies.value.service == "*" ? [] : [1]
                content {
                  method = "*"
                }
              }
            }
          }
        }
      }

      dynamic "egress_policies" {
        for_each = local.egress
        content {
          title = egress_policies.value.title
          egress_from {
            identities = egress_policies.value.identities
          }
          egress_to {
            resources = egress_policies.value.resources
            operations {
              service_name = egress_policies.value.service
              method_selectors {
                method = egress_policies.value.method
              }
            }
          }
        }
      }
    }
  }

  dynamic "spec" {
    for_each = var.dry_run ? [1] : []
    content {
      resources           = local.protected
      restricted_services = var.restricted_services
      access_levels       = var.access_levels

      vpc_accessible_services {
        enable_restriction = true
        allowed_services   = var.vpc_allowed_services
      }

      dynamic "ingress_policies" {
        for_each = local.ingress
        content {
          title = ingress_policies.value.title
          ingress_from {
            identities = ingress_policies.value.identities
            sources {
              access_level = "*"
            }
          }
          ingress_to {
            resources = ingress_policies.value.resources
            operations {
              service_name = ingress_policies.value.service
              dynamic "method_selectors" {
                for_each = ingress_policies.value.service == "*" ? [] : [1]
                content {
                  method = "*"
                }
              }
            }
          }
        }
      }

      dynamic "egress_policies" {
        for_each = local.egress
        content {
          title = egress_policies.value.title
          egress_from {
            identities = egress_policies.value.identities
          }
          egress_to {
            resources = egress_policies.value.resources
            operations {
              service_name = egress_policies.value.service
              method_selectors {
                method = egress_policies.value.method
              }
            }
          }
        }
      }
    }
  }
}
