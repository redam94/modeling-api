# DNS exfiltration control (plan §9, Appendix A #1).
#
# A response policy is consulted before any zone. Its wildcard rule '*.' answers every name
# locally with a sinkhole record, so a query for <secret>.attacker.example never reaches the
# attacker's authoritative server. Three names bypass the policy:
#
#   googleapis.com, *.googleapis.com -> the private zone below -> restricted VIP
#   metadata.google.internal         -> the Cloud Run metadata server (execution SA token)
#
# Cloud DNS response policies can answer with local data or bypass; they cannot synthesise
# NXDOMAIN. The plan's "'*.' -> NXDOMAIN" is therefore implemented as "'*.' -> local sinkhole
# answer", which has the same security property (no recursion). Whether the Direct VPC egress
# resolver of Cloud Run Jobs honours response policies at all is a [verify M0] item; the
# red-team gate in docs/operations/security-hardening.md checks it against authoritative logs.

resource "google_dns_managed_zone" "googleapis" {
  project     = var.project_id
  name        = "${local.prefix}-googleapis"
  dns_name    = "googleapis.com."
  description = "Every Google API name resolves to the restricted (VPC-SC aware) VIP."
  visibility  = "private"
  labels      = var.labels

  private_visibility_config {
    networks {
      network_url = google_compute_network.exec.id
    }
  }
}

resource "google_dns_record_set" "restricted" {
  project      = var.project_id
  managed_zone = google_dns_managed_zone.googleapis.name
  name         = "restricted.googleapis.com."
  type         = "A"
  ttl          = 300
  rrdatas      = ["199.36.153.4", "199.36.153.5", "199.36.153.6", "199.36.153.7"]
}

resource "google_dns_record_set" "googleapis_wildcard" {
  project      = var.project_id
  managed_zone = google_dns_managed_zone.googleapis.name
  name         = "*.googleapis.com."
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["restricted.googleapis.com."]
}

resource "google_dns_response_policy" "egress" {
  project              = var.project_id
  response_policy_name = "${local.prefix}-egress"
  description          = "Answer every name locally except googleapis.com and the metadata server."

  networks {
    network_url = google_compute_network.exec.id
  }
}

resource "google_dns_response_policy_rule" "sinkhole_all" {
  project         = var.project_id
  response_policy = google_dns_response_policy.egress.response_policy_name
  rule_name       = "sinkhole-all"
  dns_name        = "*."

  local_data {
    local_datas {
      name    = "*."
      type    = "A"
      ttl     = var.dns_sinkhole.ttl
      rrdatas = [var.dns_sinkhole.ipv4]
    }
    local_datas {
      name    = "*."
      type    = "AAAA"
      ttl     = var.dns_sinkhole.ttl
      rrdatas = [var.dns_sinkhole.ipv6]
    }
  }
}

locals {
  dns_bypass = {
    googleapis-apex     = "googleapis.com."
    googleapis-wildcard = "*.googleapis.com."
    metadata-server     = "metadata.google.internal."
  }
}

resource "google_dns_response_policy_rule" "bypass" {
  provider = google-beta
  for_each = local.dns_bypass

  project         = var.project_id
  response_policy = google_dns_response_policy.egress.response_policy_name
  rule_name       = each.key
  dns_name        = each.value
  behavior        = "bypassResponsePolicy"
}
