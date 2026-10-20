# Zero-egress invariants of an exec project (plan §8.1, §9), checked on a mocked apply.

mock_provider "google" {
  source = "../../testing/mocks/google"
}

mock_provider "google-beta" {
  source = "../../testing/mocks/google-beta"
}

variables {
  project_id = "acme-test-exec-paid"
  env        = "test"
  pool       = "paid"
  region     = "europe-west4"
  images = {
    base-bayes    = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/base-bayes@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    base-ml       = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/base-ml@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    base-forecast = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/base-forecast@sha256:5555555555555555555555555555555555555555555555555555555555555555"
  }
  bootstrap_url_template = "gs://mapi-test-bootstrap/{nonce}"
  orchestrator_sa_email  = "mapi-test-orch@acme-test-control.iam.gserviceaccount.com"
}

run "job_inventory" {
  command = apply

  assert {
    condition     = length(google_cloud_run_v2_job.runner) == 9
    error_message = "3 bases x {S, M, L} = 9 Jobs."
  }

  assert {
    condition     = alltrue([for j in google_cloud_run_v2_job.runner : j.template[0].template[0].max_retries == 0])
    error_message = "Every Job has max-retries 0: the reconciler owns retries (confirmed cancel first)."
  }

  assert {
    condition     = alltrue([for j in google_cloud_run_v2_job.runner : j.template[0].template[0].execution_environment == "EXECUTION_ENVIRONMENT_GEN2"])
    error_message = "Every Job runs in the gen2 microVM sandbox."
  }

  assert {
    condition     = alltrue([for j in google_cloud_run_v2_job.runner : j.template[0].template[0].vpc_access[0].egress == "ALL_TRAFFIC"])
    error_message = "All execution traffic must enter the deny-all VPC."
  }

  assert {
    condition     = alltrue([for j in google_cloud_run_v2_job.runner : [for e in j.template[0].template[0].containers[0].env : e.name] == ["MAPI_BOOTSTRAP_URL"]])
    error_message = "A Job's only static env is MAPI_BOOTSTRAP_URL: no secrets, no tokens."
  }

  assert {
    condition     = alltrue([for j in google_cloud_run_v2_job.runner : j.template[0].template[0].service_account == google_service_account.exec.email])
    error_message = "Every Job runs as the execution service account."
  }

  assert {
    condition     = google_cloud_run_v2_job.runner["base-bayes-m"].template[0].template[0].containers[0].resources[0].limits == tomap({ cpu = "4", memory = "16Gi" })
    error_message = "Class M is 4 vCPU / 16 GiB (plan §8.5)."
  }
}

run "gpu_job" {
  # [unverified: Cloud Run Jobs GPU (L4) needs a region with GPU quota; not applied or tested
  # against GCP] - only the mocked plan of the GPU Job is checked here.
  command = apply

  variables {
    images = {
      base-bayes    = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/base-bayes@sha256:3333333333333333333333333333333333333333333333333333333333333333"
      base-ml       = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/base-ml@sha256:4444444444444444444444444444444444444444444444444444444444444444"
      base-forecast = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/base-forecast@sha256:5555555555555555555555555555555555555555555555555555555555555555"
      base-dl       = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/base-dl@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    }
    gpu_jobs = true
  }

  assert {
    condition     = length(google_cloud_run_v2_job.runner) == 13
    error_message = "4 bases x {S, M, L} + the base-dl GPU Job = 13 Jobs."
  }

  assert {
    condition     = google_cloud_run_v2_job.runner["base-dl-gpu"].name == "mapi-test-base-dl-gpu"
    error_message = "The GPU Job is named mapi-{env}-base-dl-gpu (the orchestrator's convention)."
  }

  assert {
    condition     = google_cloud_run_v2_job.runner["base-dl-gpu"].template[0].template[0].containers[0].resources[0].limits == tomap({ cpu = "8", memory = "32Gi", "nvidia.com/gpu" = "1" })
    error_message = "The GPU class is 8 vCPU / 32 GiB / one GPU (mapi_spec.resources)."
  }

  assert {
    condition     = google_cloud_run_v2_job.runner["base-dl-gpu"].template[0].template[0].node_selector[0].accelerator == "nvidia-l4"
    error_message = "The GPU Job asks for an NVIDIA L4."
  }

  assert {
    condition     = google_cloud_run_v2_job.runner["base-dl-gpu"].template[0].template[0].gpu_zonal_redundancy_disabled
    error_message = "The GPU Job runs without zonal redundancy."
  }

  assert {
    condition     = length(google_cloud_run_v2_job.runner["base-dl-m"].template[0].template[0].node_selector) == 0 && google_cloud_run_v2_job.runner["base-dl-m"].template[0].template[0].containers[0].resources[0].limits == tomap({ cpu = "4", memory = "16Gi" })
    error_message = "CPU Jobs of base-dl get no GPU."
  }

  assert {
    condition = (
      google_cloud_run_v2_job.runner["base-dl-gpu"].template[0].template[0].max_retries == 0 &&
      google_cloud_run_v2_job.runner["base-dl-gpu"].template[0].template[0].vpc_access[0].egress == "ALL_TRAFFIC" &&
      google_cloud_run_v2_job.runner["base-dl-gpu"].template[0].template[0].service_account == google_service_account.exec.email
    )
    error_message = "The GPU Job has the sandbox of every Job (no retries, deny-all VPC, exec identity)."
  }

  assert {
    condition     = output.inventory.classes == tolist(["S", "M", "L", "GPU"]) && output.jobs["base-dl"]["GPU"] == "mapi-test-base-dl-gpu"
    error_message = "The inventory output offers the GPU class."
  }
}

run "gpu_job_is_off_by_default" {
  command = plan

  variables {
    images = {
      base-dl = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/base-dl@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    }
  }

  assert {
    condition     = length(google_cloud_run_v2_job.runner) == 3 && !contains(keys(google_cloud_run_v2_job.runner), "base-dl-gpu")
    error_message = "Without gpu_jobs there is no GPU Job."
  }

  assert {
    condition     = output.inventory.classes == tolist(["S", "M", "L"])
    error_message = "Without gpu_jobs the inventory offers the CPU classes only."
  }
}

run "egress_firewall" {
  command = apply

  assert {
    condition     = google_compute_network.exec.delete_default_routes_on_create
    error_message = "The exec VPC has no default internet route."
  }

  assert {
    condition     = google_compute_route.restricted_vip.dest_range == "199.36.153.4/30"
    error_message = "The only route out is the restricted VIP."
  }

  assert {
    condition = (
      google_compute_firewall.deny_all_egress.direction == "EGRESS" &&
      google_compute_firewall.deny_all_egress.destination_ranges == toset(["0.0.0.0/0"]) &&
      one(google_compute_firewall.deny_all_egress.deny[*].protocol) == "all"
    )
    error_message = "A deny-all egress rule must cover 0.0.0.0/0."
  }

  assert {
    condition = (
      google_compute_firewall.allow_restricted_vip.destination_ranges == toset(["199.36.153.4/30"]) &&
      one(google_compute_firewall.allow_restricted_vip.allow[*].ports) == tolist(["443"])
    )
    error_message = "HTTPS is allowed to the restricted VIP only."
  }

  assert {
    condition     = google_compute_firewall.allow_cloud_dns.destination_ranges == toset(["35.199.192.0/19"])
    error_message = "Port 53 is allowed to the Cloud DNS range only."
  }

  assert {
    condition     = google_compute_firewall.allow_restricted_vip.priority < google_compute_firewall.deny_all_egress.priority
    error_message = "Allow rules must take precedence over the deny-all rule."
  }
}

run "dns_policy" {
  command = apply

  assert {
    condition     = google_dns_response_policy_rule.sinkhole_all.dns_name == "*."
    error_message = "Every name is answered locally unless explicitly bypassed."
  }

  assert {
    condition     = toset(values(local.dns_bypass)) == toset(["googleapis.com.", "*.googleapis.com.", "metadata.google.internal."])
    error_message = "Only googleapis.com and the metadata server bypass the response policy."
  }

  assert {
    condition     = google_dns_record_set.googleapis_wildcard.rrdatas == tolist(["restricted.googleapis.com."])
    error_message = "*.googleapis.com resolves to the restricted VIP."
  }
}

run "least_privilege" {
  command = apply

  assert {
    condition     = length(setintersection(google_project_iam_custom_role.dispatcher.permissions, toset(["run.jobs.create", "run.jobs.update", "run.jobs.delete", "run.jobs.setIamPolicy"]))) == 0
    error_message = "The orchestrator can run executions but never create or change a Job."
  }

  assert {
    condition     = google_project_iam_member.dispatcher.member == "serviceAccount:mapi-test-orch@acme-test-control.iam.gserviceaccount.com"
    error_message = "The dispatcher role is granted to the orchestrator only."
  }
}

run "user_output_is_not_stored_in_the_exec_project" {
  command = apply

  assert {
    condition     = google_logging_project_exclusion.user_output.project == "acme-test-exec-paid"
    error_message = "The exclusion applies to this exec project's _Default sink."
  }

  assert {
    condition     = strcontains(google_logging_project_exclusion.user_output.filter, "jsonPayload.mapi_event.type=\"log\"") && strcontains(google_logging_project_exclusion.user_output.filter, "^mapi-test-")
    error_message = "User log events of this environment's Jobs are excluded (plan §8.7)."
  }
}

run "tagged_runner_images_are_refused" {
  command = plan

  variables {
    images = { base-bayes = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/base-bayes:2026.10" }
  }

  expect_failures = [var.images]
}
