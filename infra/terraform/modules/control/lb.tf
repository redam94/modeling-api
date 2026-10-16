# Optional global HTTPS load balancer (var.domains). Two hosts, two origins:
#
#   https://<domains.web>     /v1/*, /auth/*, /healthz, /readyz -> api ;  everything else -> web
#   https://<domains.plots>   -> plots (the isolated plot renderer, a different site)
#
# HTTP only redirects to HTTPS. TLS 1.2+ with the MODERN profile. Every backend carries the
# Cloud Armor policy of armor.tf (or var.security_policy when one is supplied).

locals {
  lb   = local.lb_enabled && local.services_ready ? 1 : 0
  negs = local.lb == 1 ? toset(["api", "web", "plots"]) : toset([])
}

resource "google_compute_global_address" "lb" {
  count   = local.lb
  project = var.project_id
  name    = "${local.prefix}-lb"
}

resource "google_compute_managed_ssl_certificate" "lb" {
  count   = local.lb
  project = var.project_id
  name    = "${local.prefix}-lb"

  managed {
    domains = [var.domains.web, var.domains.plots]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "google_compute_ssl_policy" "lb" {
  count           = local.lb
  project         = var.project_id
  name            = "${local.prefix}-tls"
  profile         = "MODERN"
  min_tls_version = "TLS_1_2"
}

resource "google_compute_region_network_endpoint_group" "run" {
  for_each = local.negs

  project               = var.project_id
  name                  = "${local.prefix}-${each.key}"
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = each.key == "api" ? google_cloud_run_v2_service.api[0].name : google_cloud_run_v2_service.web[each.key].name
  }
}

resource "google_compute_backend_service" "run" {
  for_each = local.negs

  project               = var.project_id
  name                  = "${local.prefix}-${each.key}"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTPS"
  security_policy       = local.security_policy

  backend {
    group = google_compute_region_network_endpoint_group.run[each.key].id
  }

  log_config {
    enable      = true
    sample_rate = 1
  }
}

resource "google_compute_url_map" "lb" {
  count           = local.lb
  project         = var.project_id
  name            = "${local.prefix}-lb"
  default_service = google_compute_backend_service.run["web"].id

  host_rule {
    hosts        = [var.domains.web]
    path_matcher = "app"
  }

  host_rule {
    hosts        = [var.domains.plots]
    path_matcher = "plots"
  }

  path_matcher {
    name            = "app"
    default_service = google_compute_backend_service.run["web"].id

    path_rule {
      paths   = ["/v1", "/v1/*", "/auth/*", "/healthz", "/readyz"]
      service = google_compute_backend_service.run["api"].id
    }
  }

  path_matcher {
    name            = "plots"
    default_service = google_compute_backend_service.run["plots"].id
  }
}

resource "google_compute_target_https_proxy" "lb" {
  count            = local.lb
  project          = var.project_id
  name             = "${local.prefix}-lb"
  url_map          = google_compute_url_map.lb[0].id
  ssl_certificates = [google_compute_managed_ssl_certificate.lb[0].id]
  ssl_policy       = google_compute_ssl_policy.lb[0].id
}

resource "google_compute_global_forwarding_rule" "https" {
  count                 = local.lb
  project               = var.project_id
  name                  = "${local.prefix}-https"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.lb[0].id
  port_range            = "443"
  target                = google_compute_target_https_proxy.lb[0].id
}

resource "google_compute_url_map" "redirect" {
  count   = local.lb
  project = var.project_id
  name    = "${local.prefix}-http-redirect"

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "redirect" {
  count   = local.lb
  project = var.project_id
  name    = "${local.prefix}-http-redirect"
  url_map = google_compute_url_map.redirect[0].id
}

resource "google_compute_global_forwarding_rule" "http" {
  count                 = local.lb
  project               = var.project_id
  name                  = "${local.prefix}-http"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.lb[0].id
  port_range            = "80"
  target                = google_compute_target_http_proxy.redirect[0].id
}
