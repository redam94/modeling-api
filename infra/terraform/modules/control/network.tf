# The control VPC exists for two things: Cloud SQL's private IP (private services access) and
# the Direct VPC egress of the Cloud Run services that reach it (egress = PRIVATE_RANGES_ONLY,
# so calls to Google APIs and identity providers do not go through the VPC).

resource "google_compute_network" "control" {
  project                 = var.project_id
  name                    = "${local.prefix}-control"
  description             = "Control plane: Cloud Run services to Cloud SQL (private IP)."
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

resource "google_compute_subnetwork" "run" {
  project                  = var.project_id
  name                     = "${local.prefix}-run"
  region                   = var.region
  network                  = google_compute_network.control.id
  ip_cidr_range            = var.network.subnet_cidr
  private_ip_google_access = true
}

resource "google_compute_global_address" "psa" {
  project       = var.project_id
  name          = "${local.prefix}-psa"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = var.network.psa_prefix_length
  network       = google_compute_network.control.id
  description   = "Private services access range (Cloud SQL private IP)."
}

resource "google_service_networking_connection" "psa" {
  network                 = google_compute_network.control.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.psa.name]
  deletion_policy         = "ABANDON"
}

# Nothing outside the VPC may open connections into it. (Cloud Run services are reached
# through their own front ends, not through this network.)
resource "google_compute_firewall" "deny_all_ingress" {
  project       = var.project_id
  name          = "${local.prefix}-control-deny-all-ingress"
  network       = google_compute_network.control.id
  direction     = "INGRESS"
  priority      = 65534
  source_ranges = ["0.0.0.0/0"]

  deny {
    protocol = "all"
  }
}
