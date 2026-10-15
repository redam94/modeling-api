# Bucket-level IAM only: no principal gets a storage role on the data project itself.
#
#                 quarantine          bootstrap           runs                bundles             data
#   orchestrator  objectUser          objectUser          objectUser          creator + viewer    creator + viewer
#   api           objectUser          -                   objectViewer        objectViewer        objectViewer
#   url_signer    reader + writer     -                   reader + writer     reader              reader
#                                                         + delete under .../checkpoints/ only
#   url_uploader  writer              -                   writer              -                   -
#   exec SAs      -                   get + delete        -                   -                   -
#
# Segmented sampling rewrites the same checkpoint names every segment through a POST policy
# signed as url_signer, and GCS needs storage.objects.delete to overwrite an object. So
# url_signer may delete in the runs bucket, but only under t/*/runs/*/a*/[tasks/*/]checkpoints/
# (an IAM condition): manifests and outputs stay write-once.
#
# objectUser = get, list, create, update, delete. Nothing above can delete a bundle (and the
# retention policy would refuse anyway). The exec service accounts cannot list the bootstrap
# bucket, so knowing one nonce never reveals another.

locals {
  role_suffix = replace(var.env, "-", "_")
}

resource "google_project_iam_custom_role" "bootstrap_consumer" {
  project     = var.project_id
  role_id     = "mapi_${local.role_suffix}_bootstrap_consumer"
  title       = "mapi ${var.env} bootstrap consumer"
  description = "Read and delete one bootstrap object by name. No list: a nonce is the only way in."
  permissions = ["storage.objects.get", "storage.objects.delete"]
}

resource "google_project_iam_custom_role" "object_reader" {
  project     = var.project_id
  role_id     = "mapi_${local.role_suffix}_object_reader"
  title       = "mapi ${var.env} object reader"
  description = "Read objects by name (signed GET URLs). No list."
  permissions = ["storage.objects.get"]
}

resource "google_project_iam_custom_role" "checkpoint_overwriter" {
  project     = var.project_id
  role_id     = "mapi_${local.role_suffix}_checkpoint_overwriter"
  title       = "mapi ${var.env} checkpoint overwriter"
  description = "Delete (and so overwrite) objects. Granted only with a condition limiting it to checkpoint objects."
  permissions = ["storage.objects.delete"]
}

resource "google_project_iam_custom_role" "object_writer" {
  project     = var.project_id
  role_id     = "mapi_${local.role_suffix}_object_writer"
  title       = "mapi ${var.env} object writer"
  description = "Create objects (signed PUT URLs, V4 POST policies, resumable uploads). No list, no delete."
  permissions = [
    "storage.objects.create",
    "storage.multipartUploads.create",
    "storage.multipartUploads.abort",
    "storage.multipartUploads.listParts",
  ]
}

locals {
  # Keys are static names (known at plan time); e-mails and custom-role ids are only values.
  roles = {
    objectUser         = "roles/storage.objectUser"
    objectViewer       = "roles/storage.objectViewer"
    objectCreator      = "roles/storage.objectCreator"
    reader             = google_project_iam_custom_role.object_reader.id
    writer             = google_project_iam_custom_role.object_writer.id
    bootstrap_consumer = google_project_iam_custom_role.bootstrap_consumer.id
  }

  principals = merge(
    {
      orchestrator = var.access.orchestrator
      api          = var.access.api
      url_signer   = var.access.url_signer
      url_uploader = var.access.url_uploader
    },
    { for pool, email in var.access.exec : "exec-${pool}" => email },
  )

  grants = concat(
    [
      ["quarantine", "objectUser", "orchestrator"],
      ["bootstrap", "objectUser", "orchestrator"],
      ["runs", "objectUser", "orchestrator"],
      ["bundles", "objectCreator", "orchestrator"],
      ["bundles", "objectViewer", "orchestrator"],
      ["data", "objectCreator", "orchestrator"],
      ["data", "objectViewer", "orchestrator"],

      ["quarantine", "objectUser", "api"],
      ["runs", "objectViewer", "api"],
      ["bundles", "objectViewer", "api"],
      ["data", "objectViewer", "api"],

      ["quarantine", "reader", "url_signer"],
      ["quarantine", "writer", "url_signer"],
      ["runs", "reader", "url_signer"],
      ["runs", "writer", "url_signer"],
      ["bundles", "reader", "url_signer"],
      ["data", "reader", "url_signer"],

      # create-only upload policies: GCS POST forms carry no generation precondition, so only
      # a signer without storage.objects.delete makes them create-only
      ["quarantine", "writer", "url_uploader"],
      ["runs", "writer", "url_uploader"],
    ],
    [for pool in keys(var.access.exec) : ["bootstrap", "bootstrap_consumer", "exec-${pool}"]],
  )
}

resource "google_storage_bucket_iam_member" "this" {
  for_each = { for g in local.grants : join("|", g) => { bucket = g[0], role = g[1], who = g[2] } }

  bucket = google_storage_bucket.this[each.value.bucket].name
  role   = local.roles[each.value.role]
  member = "serviceAccount:${local.principals[each.value.who]}"
}

# Checkpoint overwrites (see the header): delete for url_signer, only on checkpoint objects of
# the runs bucket. extract() returns "" when the template does not match the object name.
resource "google_storage_bucket_iam_member" "checkpoint_overwrite" {
  bucket = google_storage_bucket.this["runs"].name
  role   = google_project_iam_custom_role.checkpoint_overwriter.id
  member = "serviceAccount:${var.access.url_signer}"

  condition {
    title       = "checkpoints-only"
    description = "Segmented sampling rewrites its checkpoints under t/{tenant}/runs/{run}/a{n}/[tasks/{i}/]checkpoints/."
    expression  = "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.this["runs"].name}/objects/t/\") && resource.name.extract(\"/objects/t/{run}/checkpoints/\") != \"\""
  }
}
