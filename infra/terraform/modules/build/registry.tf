# Artifact Registry (plan §7, §12).
#
# images  Docker images of the platform: server, web, base-bayes, base-ml, base-forecast.
#         Tags are immutable, and everything is deployed by digest. No delete policy: a digest
#         referenced by a live ModelVersion must never disappear (scoring re-runs on the
#         original image), so garbage collection is a deliberate operator action.
# pypi    Remote repository proxying PyPI: the curated mirror the M4 overlay resolve runs read
#         from. A remote repository does not enforce an allow-list by itself; build-inspect
#         enforces the curated allow-list against the resolved lock (plan §7 step 2).

resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = "mapi-${var.env}-images"
  description   = "mapi platform images (deployed by digest)."
  format        = "DOCKER"
  labels        = var.labels

  docker_config {
    immutable_tags = true
  }

  cleanup_policy_dry_run = true

  cleanup_policies {
    id     = "keep-everything"
    action = "KEEP"

    condition {
      tag_state = "ANY"
    }
  }

  vulnerability_scanning_config {
    enablement_config = "INHERITED"
  }
}

resource "google_artifact_registry_repository" "pypi" {
  project       = var.project_id
  location      = var.region
  repository_id = "mapi-${var.env}-pypi"
  description   = "Curated PyPI mirror (remote). The allow-list is enforced by build-inspect."
  format        = "PYTHON"
  mode          = "REMOTE_REPOSITORY"
  labels        = var.labels

  remote_repository_config {
    description = "pypi.org"

    python_repository {
      public_repository = "PYPI"
    }
  }
}

resource "google_artifact_registry_repository_iam_member" "image_readers" {
  for_each = var.image_readers

  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = each.value
}

resource "google_artifact_registry_repository_iam_member" "pypi_readers" {
  for_each = var.pypi_readers

  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.pypi.name
  role       = "roles/artifactregistry.reader"
  member     = each.value
}
