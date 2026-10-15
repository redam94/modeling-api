# `terraform test` with mocked Google providers: every resource is "applied" against mocks,
# so all expressions, for_each keys, preconditions and module wiring are evaluated without any
# cloud call or credentials. (random is real: it runs locally.)

mock_provider "google" {
  source = "../../testing/mocks/google"
}

mock_provider "google-beta" {
  source = "../../testing/mocks/google-beta"
}

variables {
  env             = "test"
  region          = "europe-west4"
  bucket_location = "EU"
  projects = {
    control = "acme-test-control"
    data    = "acme-test-data"
    build   = "acme-test-build"
  }
  exec_pools = {
    paid   = { project_id = "acme-test-exec-paid" }
    public = { project_id = "acme-test-exec-public", public = true, classes = ["S"], subnet_cidr = "10.80.0.0/20" }
  }
  images = {
    server        = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/server@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    web           = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/web@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    base-bayes    = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/base-bayes@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    base-ml       = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/base-ml@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    base-forecast = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/base-forecast@sha256:5555555555555555555555555555555555555555555555555555555555555555"
  }
  vpc_sc       = { access_policy_id = "123456789012" }
  domains      = { web = "app.example.com", plots = "plots.example.net" }
  github       = { repository = "acme/modeling-api" }
  state_bucket = "acme-tfstate"
  control = {
    oidc = { client_id = "mock-client.apps.googleusercontent.com", client_secret_version = 1 }
  }
  oidc_client_secret = "mock-oauth-client-secret"
}

run "full_environment" {
  command = apply

  assert {
    condition     = output.services_deployed
    error_message = "With images, the Cloud Run workloads must be part of the configuration."
  }

  assert {
    condition     = output.buckets == { quarantine = "mapi-test-quarantine", bootstrap = "mapi-test-bootstrap", runs = "mapi-test-runs", bundles = "mapi-test-bundles", data = "mapi-test-data" }
    error_message = "Bucket names must be exactly mapi-{env}-{purpose} (mapi_spec.storage.bucket_name)."
  }

  assert {
    condition     = length(output.exec_pools["paid"].jobs) == 3 && alltrue([for b, classes in output.exec_pools["paid"].jobs : keys(classes) == ["L", "M", "S"]])
    error_message = "The paid pool must have 3 bases x {S, M, L} Jobs."
  }

  assert {
    condition     = output.exec_pools["public"].jobs["base-bayes"] == { S = "mapi-test-base-bayes-s" }
    error_message = "The public pool must only have the classes it was given."
  }

  assert {
    condition     = output.perimeter != null && output.perimeter.enforced
    error_message = "The perimeter must exist and be enforced when an access policy is given."
  }

  assert {
    condition     = output.urls.api == "https://app.example.com" && output.urls.plots == "https://plots.example.net"
    error_message = "Behind the load balancer the API shares the app origin and the plot sandbox has its own."
  }

  assert {
    condition     = strcontains(output.events.sink_filter, "jsonPayload.mapi_event:*")
    error_message = "The Logging sink must route only supervisor event lines."
  }

  assert {
    condition     = output.github != null && length(output.github.service_accounts) == 3
    error_message = "CI identities: ci-push, planner, deployer."
  }

  assert {
    condition     = length([for k, p in google_project_service.this : p if p.disable_on_destroy]) == 0
    error_message = "Destroying the stack must never disable an API."
  }
}

run "foundation_without_images" {
  command = apply

  variables {
    images  = {}
    vpc_sc  = {}
    domains = null
    github  = null
  }

  expect_failures = [check.perimeter_enabled]

  assert {
    condition     = !output.services_deployed
    error_message = "Without images no Cloud Run workload may be planned."
  }

  assert {
    condition     = alltrue([for pool, inv in output.exec_pools : length(inv.jobs) == 0])
    error_message = "Without runner images there are no Jobs."
  }

  assert {
    condition     = output.perimeter == null
    error_message = "No access policy, no perimeter."
  }

  assert {
    condition     = output.urls.api == "https://mapi-test-api-111111111111.europe-west4.run.app"
    error_message = "Without a load balancer the API answers on its deterministic run.app URL."
  }
}

run "partial_runner_images_warn" {
  command = plan

  variables {
    images = {
      base-bayes = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/base-bayes@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    }
  }

  expect_failures = [check.runner_images_complete]
}

run "tag_images_are_refused" {
  command = plan

  variables {
    images = {
      server = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/server:latest"
    }
  }

  expect_failures = [var.images]
}

run "unknown_image_keys_are_refused" {
  command = plan

  variables {
    images = {
      gcs-shim = "europe-west4-docker.pkg.dev/acme-test-build/mapi-test-images/gcs-shim@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    }
  }

  expect_failures = [var.images]
}
