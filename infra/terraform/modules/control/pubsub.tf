# Event transport (plan §8.7): supervisor stdout -> Cloud Logging (exec project) -> Logging
# sink (filter jsonPayload.mapi_event:*) -> Pub/Sub topic -> push subscription with an OIDC
# token -> events service (POST /internal/events) -> HMAC check, dedupe on (run, attempt, seq).
#
# Run identity comes from the execution's resource labels, never from the payload. Messages
# that keep failing go to a dead-letter topic with a pull subscription for inspection.

resource "google_project_service_identity" "pubsub" {
  provider = google-beta
  project  = var.project_id
  service  = "pubsub.googleapis.com"
}

locals {
  pubsub_agent = "serviceAccount:${google_project_service_identity.pubsub.email}"
}

resource "google_pubsub_topic" "run_events" {
  project                    = var.project_id
  name                       = "${local.prefix}-run-events"
  labels                     = var.labels
  message_retention_duration = "86400s"
}

resource "google_pubsub_topic" "run_events_dlq" {
  project                    = var.project_id
  name                       = "${local.prefix}-run-events-dlq"
  labels                     = var.labels
  message_retention_duration = "604800s"
}

resource "google_pubsub_subscription" "run_events_dlq" {
  project                    = var.project_id
  name                       = "${local.prefix}-run-events-dlq"
  topic                      = google_pubsub_topic.run_events_dlq.id
  labels                     = var.labels
  ack_deadline_seconds       = 60
  message_retention_duration = "604800s"

  expiration_policy {
    ttl = "" # never expires
  }
}

resource "google_pubsub_subscription" "run_events_push" {
  project                    = var.project_id
  name                       = "${local.prefix}-run-events-push"
  topic                      = google_pubsub_topic.run_events.id
  labels                     = var.labels
  ack_deadline_seconds       = var.pubsub.ack_deadline_seconds
  message_retention_duration = "604800s"

  push_config {
    push_endpoint = local.push_endpoint

    oidc_token {
      service_account_email = google_service_account.sa["push"].email
      audience              = local.push_audience
    }
  }

  retry_policy {
    minimum_backoff = var.pubsub.min_backoff
    maximum_backoff = var.pubsub.max_backoff
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.run_events_dlq.id
    max_delivery_attempts = var.pubsub.max_delivery_attempts
  }

  expiration_policy {
    ttl = ""
  }

  depends_on = [
    google_pubsub_topic_iam_member.dlq_publisher,
    google_service_account_iam_member.push_token_creator,
  ]
}

# Pub/Sub mints the push OIDC tokens as the push service account...
resource "google_service_account_iam_member" "push_token_creator" {
  service_account_id = google_service_account.sa["push"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.pubsub_agent
}

# ...and forwards undeliverable messages to the dead-letter topic.
resource "google_pubsub_topic_iam_member" "dlq_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.run_events_dlq.name
  role    = "roles/pubsub.publisher"
  member  = local.pubsub_agent
}

resource "google_pubsub_subscription_iam_member" "dlq_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.run_events_push.name
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_agent
}

# ------------------------------------------------------------------- Logging sinks (exec side)
# A sink lives in the project whose logs it routes, so there is one per exec project. Each
# writes as its own (unique) writer identity, which may only publish to this topic.

resource "google_logging_project_sink" "run_events" {
  for_each = var.exec_pools

  project                = each.value.project
  name                   = "${local.prefix}-run-events"
  description            = "Supervisor event lines of mapi Jobs -> control-plane events topic."
  destination            = "pubsub.googleapis.com/${google_pubsub_topic.run_events.id}"
  filter                 = local.event_log_filter
  unique_writer_identity = true
}

resource "google_pubsub_topic_iam_member" "sink_publisher" {
  for_each = var.exec_pools

  project = var.project_id
  topic   = google_pubsub_topic.run_events.name
  role    = "roles/pubsub.publisher"
  member  = google_logging_project_sink.run_events[each.key].writer_identity
}
