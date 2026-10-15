# Log volume (plan §8.7): user stdout above the platform's needs is excluded at the sink.
#
# Executions write only the supervisor's signed {"mapi_event": ...} lines to stdout. User
# output reaches it as `log` events (capped per line and per run by the supervisor), and
# progress as coalesced `progress` events. Both are routed to the control plane's events
# topic by the run_events sink (modules/control/pubsub.tf), which an exclusion on the
# _Default sink does not affect. Here they are kept out of this (hostile tenants') project's
# _Default bucket, where every line would otherwise be ingested and billed. State changes,
# warnings, errors, completions, the supervisor's own stderr and Cloud Run's system logs stay.

resource "google_logging_project_exclusion" "user_output" {
  project     = var.project_id
  name        = "mapi-${var.env}-user-output"
  description = "mapi ${var.env}: user log and progress events of the runner Jobs (routed to the control plane, not stored here)."
  filter = join(" AND ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.job_name=~\"^mapi-${var.env}-\"",
    "(jsonPayload.mapi_event.type=\"log\" OR jsonPayload.mapi_event.type=\"progress\")",
  ])
}
