# The five buckets of plan §8.6. One resource for all of them, so the invariants every bucket
# must satisfy (uniform access, public access prevention, no versioning) are written once.
#
#   quarantine  raw uploads (signed PUT from browsers)        deleted after 7 days
#   bootstrap   RunSpec objects keyed by a 256-bit nonce      exec SA get+delete, no list; 1 day
#   runs        per-attempt scratch, written via signed URLs  deleted after runs_ttl_days
#   bundles     verified, write-once model bundles            exec SA has NO access; retention lock
#   data        content-addressed dataset versions            exec SA has NO access

locals {
  # Soft delete keeps deleted objects restorable. For bootstrap that would defeat the
  # read-then-delete of the RunSpec (it holds signed URLs and the per-run HMAC key), and runs/
  # and quarantine/ hold tenant data whose deletion must be real. 604800 s = the GCS default.
  buckets = {
    quarantine = {
      delete_after_days   = var.quarantine_ttl_days
      soft_delete_seconds = 0
      retention_days      = null
      cors_methods        = ["PUT", "POST", "OPTIONS"]
    }
    bootstrap = {
      delete_after_days   = 1
      soft_delete_seconds = 0
      retention_days      = null
      cors_methods        = []
    }
    runs = {
      delete_after_days   = var.runs_ttl_days
      soft_delete_seconds = 0
      retention_days      = null
      cors_methods        = []
    }
    bundles = {
      delete_after_days   = null
      soft_delete_seconds = 604800
      retention_days      = var.bundle_retention_days
      cors_methods        = ["GET", "HEAD"]
    }
    data = {
      delete_after_days   = null
      soft_delete_seconds = 604800
      retention_days      = null
      cors_methods        = ["GET", "HEAD"]
    }
  }

  bucket_names = { for purpose in keys(local.buckets) : purpose => "mapi-${var.env}-${purpose}" }
}

data "google_storage_project_service_account" "gcs" {
  count   = var.kms_key_name == null ? 0 : 1
  project = var.project_id
}

# CMEK: the Cloud Storage service agent of the data project must be able to use the key.
resource "google_kms_crypto_key_iam_member" "gcs_cmek" {
  count         = var.kms_key_name == null ? 0 : 1
  crypto_key_id = var.kms_key_name
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${data.google_storage_project_service_account.gcs[0].email_address}"
}

resource "google_storage_bucket" "this" {
  for_each = local.buckets

  project       = var.project_id
  name          = local.bucket_names[each.key]
  location      = var.location
  storage_class = "STANDARD"
  labels        = merge(var.labels, { mapi-bucket = each.key })

  # No ACLs, no public access, ever. Access is IAM on the bucket plus signed URLs.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # Keys are write-once (ifGenerationMatch=0) and pinned by generation, so object versioning
  # adds nothing; a retention policy cannot coexist with it anyway.
  versioning {
    enabled = false
  }

  # Never delete a bundle bucket with Terraform, even in dev.
  force_destroy = var.force_destroy && each.key != "bundles"

  soft_delete_policy {
    retention_duration_seconds = each.value.soft_delete_seconds
  }

  dynamic "lifecycle_rule" {
    for_each = each.value.delete_after_days == null ? [] : [each.value.delete_after_days]
    content {
      action {
        type = "Delete"
      }
      condition {
        age = lifecycle_rule.value
      }
    }
  }

  # Resumable and XML multipart uploads that never finish still cost storage.
  lifecycle_rule {
    action {
      type = "AbortIncompleteMultipartUpload"
    }
    condition {
      age = 1
    }
  }

  dynamic "retention_policy" {
    for_each = each.value.retention_days == null ? [] : [each.value.retention_days]
    content {
      retention_period = tostring(retention_policy.value * 86400)
      is_locked        = var.lock_bundle_retention
    }
  }

  dynamic "encryption" {
    for_each = var.kms_key_name == null ? [] : [var.kms_key_name]
    content {
      default_kms_key_name = encryption.value
    }
  }

  dynamic "cors" {
    for_each = length(var.cors_origins) > 0 && length(each.value.cors_methods) > 0 ? [1] : []
    content {
      origin          = var.cors_origins
      method          = each.value.cors_methods
      response_header = ["Content-Type", "Content-Length", "Content-Range", "Range", "ETag", "x-goog-generation", "x-goog-hash", "x-goog-resumable"]
      max_age_seconds = 3600
    }
  }

  depends_on = [google_kms_crypto_key_iam_member.gcs_cmek]
}
