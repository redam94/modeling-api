# Binary Authorization attestor (stub until M5).
#
# CI will attest each base-image digest it built and scanned with this KMS key; the exec
# projects' policy (M5) will then admit only attested digests on the Jobs
# (modules/exec: binary_authorization = true). For now the attestor, its note and its key
# exist so that the signing step can be wired into CI before enforcement is switched on.

resource "google_kms_key_ring" "build" {
  project  = var.project_id
  name     = "mapi-${var.env}-build"
  location = var.region
}

resource "google_kms_crypto_key" "attestor" {
  name     = "binauthz-attestor"
  key_ring = google_kms_key_ring.build.id
  purpose  = "ASYMMETRIC_SIGN"
  labels   = var.labels

  version_template {
    algorithm        = "EC_SIGN_P256_SHA256"
    protection_level = var.kms_protection_level
  }

  lifecycle {
    prevent_destroy = true
  }
}

data "google_kms_crypto_key_version" "attestor" {
  crypto_key = google_kms_crypto_key.attestor.id
}

resource "google_container_analysis_note" "attestor" {
  project = var.project_id
  name    = "mapi-${var.env}-built-by-ci"

  attestation_authority {
    hint {
      human_readable_name = "mapi ${var.env}: built, scanned and signed by CI"
    }
  }
}

resource "google_binary_authorization_attestor" "ci" {
  project     = var.project_id
  name        = "mapi-${var.env}-built-by-ci"
  description = "Attests base images built from the locked lockfile and scanned in CI (enforced from M5)."

  attestation_authority_note {
    note_reference = google_container_analysis_note.attestor.name

    public_keys {
      id = data.google_kms_crypto_key_version.attestor.id

      pkix_public_key {
        public_key_pem      = data.google_kms_crypto_key_version.attestor.public_key[0].pem
        signature_algorithm = data.google_kms_crypto_key_version.attestor.public_key[0].algorithm
      }
    }
  }
}

# CI signs attestations with the key (the ci-push identity builds the images).
resource "google_kms_crypto_key_iam_member" "attestor_signer" {
  count = var.github == null ? 0 : 1

  crypto_key_id = google_kms_crypto_key.attestor.id
  role          = "roles/cloudkms.signerVerifier"
  member        = "serviceAccount:${google_service_account.ci["ci-push"].email}"
}

resource "google_binary_authorization_attestor_iam_member" "attestor_attacher" {
  count = var.github == null ? 0 : 1

  project  = var.project_id
  attestor = google_binary_authorization_attestor.ci.name
  role     = "roles/binaryauthorization.attestorsVerifier"
  member   = "serviceAccount:${google_service_account.ci["ci-push"].email}"
}

# An attestation is a Container Analysis occurrence in this project.
resource "google_project_iam_member" "attestation_writer" {
  count = var.github == null ? 0 : 1

  project = var.project_id
  role    = "roles/containeranalysis.occurrences.editor"
  member  = "serviceAccount:${google_service_account.ci["ci-push"].email}"
}

resource "google_container_analysis_note_iam_member" "attestor_occurrences" {
  count = var.github == null ? 0 : 1

  project = var.project_id
  note    = google_container_analysis_note.attestor.name
  role    = "roles/containeranalysis.notes.attacher"
  member  = "serviceAccount:${google_service_account.ci["ci-push"].email}"
}
