# Bundle-manifest signatures (plan §8.6): the finalizer signs the manifest that binds tenant,
# run, digests, hashes and destination generations; the API verifies it before minting any
# URL for a bundle. EC P-256 / SHA-256 is the algorithm of the local PEM key, so signatures
# have the same shape in both environments.

resource "google_kms_key_ring" "control" {
  project  = var.project_id
  name     = local.prefix
  location = var.region
}

resource "google_kms_crypto_key" "bundle_signing" {
  name     = "bundle-signing"
  key_ring = google_kms_key_ring.control.id
  purpose  = "ASYMMETRIC_SIGN"
  labels   = var.labels

  version_template {
    algorithm        = "EC_SIGN_P256_SHA256"
    protection_level = var.kms.protection_level
  }

  # 30 days between "destroy" and the version actually being destroyed.
  destroy_scheduled_duration = "2592000s"

  lifecycle {
    prevent_destroy = true
  }
}

# Sign: the orchestrator (finalizer) only.
resource "google_kms_crypto_key_iam_member" "signer" {
  crypto_key_id = google_kms_crypto_key.bundle_signing.id
  role          = "roles/cloudkms.signer"
  member        = "serviceAccount:${google_service_account.sa["orchestrator"].email}"
}

# Verify: anyone who verifies needs only the public key.
resource "google_kms_crypto_key_iam_member" "public_key" {
  for_each = toset(["api", "orchestrator"])

  crypto_key_id = google_kms_crypto_key.bundle_signing.id
  role          = "roles/cloudkms.publicKeyViewer"
  member        = "serviceAccount:${google_service_account.sa[each.key].email}"
}
