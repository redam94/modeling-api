# Log retention and tenant deletion (plan §9 "Data lifecycle and privacy").
#
# Tenant deletion purges Postgres and every object-store prefix of the tenant, but NOT logs:
# Cloud Logging entries cannot be deleted one by one. The platform keeps tenant content out of
# logs (structured lines carry tenant, project and run ids, request ids and keyed digests of
# client addresses, never request bodies, data or tracebacks of tenant code), and the logs
# expire with their bucket's retention. This makes that retention explicit for the control
# project's _Default bucket (API, worker, events service and load-balancer request logs),
# so "logs are gone N days after a deletion" is a configured fact, not a default.
#
# The run_events sink (pubsub.tf) does not store anything: it forwards supervisor event lines
# to Pub/Sub, whose undelivered messages expire after the subscription's retention. The exec
# projects keep their own _Default buckets (Cloud Run's system logs of the Jobs; user output
# is excluded at the source, modules/exec/logging.tf) with the same default retention.

resource "google_logging_project_bucket_config" "default" {
  project        = var.project_id
  location       = "global"
  bucket_id      = "_Default"
  retention_days = var.log_retention_days
  description    = "mapi ${var.env}: control-plane logs; kept ${var.log_retention_days} days (tenant deletion does not purge logs)."
}
