# Zero egress for executions (plan §8.1, §9). Every Job sends ALL its traffic into this VPC
# (Direct VPC egress, egress = ALL_TRAFFIC), where:
#
#   * the only route out is to the restricted Google APIs VIP 199.36.153.4/30;
#   * the firewall denies all egress except TCP 443 to that VIP and port 53 to Cloud DNS
#     (35.199.192.0/19, where the VPC's resolver answers from);
#   * the DNS response policy (dns.tf) answers every name locally except googleapis.com, which
#     a private zone points at the VIP;
#   * the VPC-SC perimeter (modules/perimeter) makes the VIP useless for reaching resources
#     outside the exec and data projects.

locals {
  prefix          = "mapi-${var.env}-exec-${var.pool}"
  restricted_vip  = "199.36.153.4/30"
  cloud_dns_range = "35.199.192.0/19"
  network_tag     = "mapi-exec"
}

resource "google_compute_network" "exec" {
  project                         = var.project_id
  name                            = local.prefix
  description                     = "Executions of tenant code. Deny-all egress; restricted VIP and Cloud DNS only."
  auto_create_subnetworks         = false
  routing_mode                    = "REGIONAL"
  delete_default_routes_on_create = true
}

resource "google_compute_subnetwork" "jobs" {
  project                  = var.project_id
  name                     = "${local.prefix}-jobs"
  region                   = var.region
  network                  = google_compute_network.exec.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true

  dynamic "log_config" {
    for_each = var.flow_logs.enabled ? [1] : []
    content {
      aggregation_interval = "INTERVAL_5_SEC"
      flow_sampling        = var.flow_logs.sampling
      metadata             = "INCLUDE_ALL_METADATA"
    }
  }
}

# The default internet route was never created; this is the only route that leaves the VPC.
resource "google_compute_route" "restricted_vip" {
  project          = var.project_id
  name             = "${local.prefix}-restricted-vip"
  network          = google_compute_network.exec.id
  dest_range       = local.restricted_vip
  next_hop_gateway = "default-internet-gateway"
  priority         = 100
  description      = "restricted.googleapis.com (VPC-SC aware); the only egress route."
}

# ---------------------------------------------------------------------------- firewall (egress)

resource "google_compute_firewall" "deny_all_egress" {
  project            = var.project_id
  name               = "${local.prefix}-deny-all-egress"
  network            = google_compute_network.exec.id
  direction          = "EGRESS"
  priority           = 65534
  destination_ranges = ["0.0.0.0/0"]
  description        = "Default deny: executions reach nothing unless a higher-priority rule allows it."

  deny {
    protocol = "all"
  }

  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_firewall" "deny_all_egress_ipv6" {
  project            = var.project_id
  name               = "${local.prefix}-deny-all-egress-v6"
  network            = google_compute_network.exec.id
  direction          = "EGRESS"
  priority           = 65534
  destination_ranges = ["::/0"]
  description        = "Default deny for IPv6 as well (defence in depth; the subnet is IPv4-only)."

  deny {
    protocol = "all"
  }

  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_firewall" "allow_restricted_vip" {
  project            = var.project_id
  name               = "${local.prefix}-allow-restricted-vip"
  network            = google_compute_network.exec.id
  direction          = "EGRESS"
  priority           = 1000
  destination_ranges = [local.restricted_vip]
  description        = "HTTPS to restricted.googleapis.com only (signed URLs to Cloud Storage)."

  allow {
    protocol = "tcp"
    ports    = ["443"]
  }

  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_firewall" "allow_cloud_dns" {
  project            = var.project_id
  name               = "${local.prefix}-allow-cloud-dns"
  network            = google_compute_network.exec.id
  direction          = "EGRESS"
  priority           = 1000
  destination_ranges = [local.cloud_dns_range]
  description        = "Port 53 only to the Cloud DNS resolver range, which applies the response policy."

  allow {
    protocol = "udp"
    ports    = ["53"]
  }

  allow {
    protocol = "tcp"
    ports    = ["53"]
  }
}

# ---------------------------------------------------------------------------- firewall (ingress)

resource "google_compute_firewall" "deny_all_ingress" {
  project       = var.project_id
  name          = "${local.prefix}-deny-all-ingress"
  network       = google_compute_network.exec.id
  direction     = "INGRESS"
  priority      = 65534
  source_ranges = ["0.0.0.0/0"]
  description   = "Explicit deny (the implied rule says the same; this one is logged)."

  deny {
    protocol = "all"
  }

  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}
