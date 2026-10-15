# Security invariants of the data buckets (plan §8.6, §9), checked on a mocked apply.

mock_provider "google" {
  source = "../../testing/mocks/google"
}

variables {
  project_id   = "acme-test-data"
  env          = "test"
  location     = "EU"
  cors_origins = ["https://app.example.com"]
  access = {
    orchestrator = "mapi-test-orch@acme-test-control.iam.gserviceaccount.com"
    api          = "mapi-test-api@acme-test-control.iam.gserviceaccount.com"
    url_signer   = "mapi-test-signer@acme-test-control.iam.gserviceaccount.com"
    url_uploader = "mapi-test-uploader@acme-test-control.iam.gserviceaccount.com"
    exec = {
      paid   = "mapi-test-exec@acme-test-exec-paid.iam.gserviceaccount.com"
      public = "mapi-test-exec@acme-test-exec-public.iam.gserviceaccount.com"
    }
  }
}

run "buckets_are_private_and_uniform" {
  command = apply

  assert {
    condition     = length(google_storage_bucket.this) == 5
    error_message = "Exactly the five buckets of plan §8.6."
  }

  assert {
    condition     = alltrue([for b in google_storage_bucket.this : b.public_access_prevention == "enforced"])
    error_message = "Every bucket must enforce public access prevention."
  }

  assert {
    condition     = alltrue([for b in google_storage_bucket.this : b.uniform_bucket_level_access])
    error_message = "Every bucket must use uniform bucket-level access (no ACLs)."
  }

  assert {
    condition     = alltrue([for b in google_storage_bucket.this : !b.versioning[0].enabled])
    error_message = "Object versioning stays off (write-once keys, generation pinning)."
  }

  assert {
    condition     = alltrue([for m in google_storage_bucket_iam_member.this : !contains(["allUsers", "allAuthenticatedUsers"], m.member)])
    error_message = "No bucket may be readable or writable by allUsers / allAuthenticatedUsers."
  }
}

run "bootstrap_is_read_once" {
  command = apply

  assert {
    condition     = google_storage_bucket.this["bootstrap"].soft_delete_policy[0].retention_duration_seconds == 0
    error_message = "A deleted bootstrap object must be gone, not soft-deleted (it holds signed URLs and the HMAC key)."
  }

  assert {
    condition     = one([for r in google_storage_bucket.this["bootstrap"].lifecycle_rule : one(r.condition).age if one(r.action).type == "Delete"]) == 1
    error_message = "Unclaimed bootstrap objects expire after one day."
  }

  assert {
    condition     = google_project_iam_custom_role.bootstrap_consumer.permissions == toset(["storage.objects.get", "storage.objects.delete"])
    error_message = "The execution role on bootstrap is get + delete, and nothing else (no list)."
  }

  assert {
    condition     = toset([for k, m in google_storage_bucket_iam_member.this : m.bucket if strcontains(m.member, "mapi-test-exec@")]) == toset(["mapi-test-bootstrap"])
    error_message = "Execution service accounts may touch the bootstrap bucket only."
  }

  assert {
    condition     = length([for k, m in google_storage_bucket_iam_member.this : m if strcontains(m.member, "mapi-test-exec@")]) == 2
    error_message = "One bootstrap grant per exec pool."
  }
}

run "quarantine_and_runs_expire" {
  command = apply

  assert {
    condition     = one([for r in google_storage_bucket.this["quarantine"].lifecycle_rule : one(r.condition).age if one(r.action).type == "Delete"]) == 7
    error_message = "Raw uploads are deleted after 7 days."
  }

  assert {
    condition     = one([for r in google_storage_bucket.this["runs"].lifecycle_rule : one(r.condition).age if one(r.action).type == "Delete"]) == 30
    error_message = "Run scratch expires."
  }
}

run "bundles_are_write_once" {
  command = apply

  variables {
    lock_bundle_retention = true
    force_destroy         = true
  }

  assert {
    condition     = google_storage_bucket.this["bundles"].retention_policy[0].is_locked
    error_message = "lock_bundle_retention must lock the bundles retention policy."
  }

  assert {
    condition     = !google_storage_bucket.this["bundles"].force_destroy
    error_message = "force_destroy never applies to bundles."
  }

  assert {
    condition     = length([for r in google_storage_bucket.this["bundles"].lifecycle_rule : r if one(r.action).type == "Delete"]) == 0
    error_message = "Bundles are never deleted by a lifecycle rule (live ModelVersions reference them)."
  }

  assert {
    condition     = length([for k, m in google_storage_bucket_iam_member.this : m if m.bucket == "mapi-test-bundles" && contains(["roles/storage.objectUser", "roles/storage.objectAdmin", "roles/storage.admin"], m.role)]) == 0
    error_message = "Nobody holds a role on bundles that can delete or overwrite objects."
  }
}

run "cmek_is_optional" {
  command = apply

  variables {
    kms_key_name = "projects/acme-test-data/locations/europe/keyRings/mapi/cryptoKeys/buckets"
  }

  assert {
    condition     = alltrue([for b in google_storage_bucket.this : b.encryption[0].default_kms_key_name == var.kms_key_name])
    error_message = "With a CMEK key every bucket uses it."
  }

  assert {
    condition     = length(google_kms_crypto_key_iam_member.gcs_cmek) == 1
    error_message = "The Cloud Storage service agent gets encrypt/decrypt on the CMEK key."
  }
}

run "env_must_match_bucket_naming" {
  command = plan

  variables {
    env = "Prod_1"
  }

  expect_failures = [var.env]
}
