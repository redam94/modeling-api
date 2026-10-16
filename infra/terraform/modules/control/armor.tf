# Cloud Armor in front of the external HTTPS load balancer (plan §9 "Compute abuse",
# §14 M3 "Cloud Armor"). Created with the load balancer (var.domains) unless a policy is
# supplied (var.security_policy) or var.cloud_armor.enabled is false, and attached to every
# backend in lb.tf (api, web, plots).
#
#   priority 100       allow var.cloud_armor.trusted_ip_ranges (CI, office) past the rules below
#   priority 1000-1004 preconfigured OWASP CRS rules: sqli, xss, lfi, rce, scanner detection;
#                      var.cloud_armor.waf_mode "preview" logs matches without blocking, then
#                      "enforce" denies them with 403 once the logs show no false positives
#   priority 2000-2003 rate-based bans per client IP: sign-up, /auth/*, public embeds and
#                      endpoint scoring (429 over the threshold, then a ban for ban_duration_sec)
#   default            allow
#
# Request bodies: Cloud Armor inspects only the first request_body_inspection_size bytes of a
# body (8 KB unless raised, at most 64 KB). The API caps bodies itself (spec documents,
# inline XrJSON score payloads up to 5 MB): a larger body is not blocked by Cloud Armor, it is
# only inspected partially. Cloud Armor never sees the signed-URL uploads, which go to Cloud
# Storage directly.
#
# Adaptive Protection (Layer 7 DDoS defense) learns the normal traffic of each backend and
# alerts on anomalies, with suggested rules; it never blocks by itself.

locals {
  armor_enabled = local.lb == 1 && var.security_policy == null && var.cloud_armor.enabled
  armor         = local.armor_enabled ? 1 : 0
  security_policy = (
    var.security_policy != null ? var.security_policy
    : local.armor_enabled ? google_compute_security_policy.edge[0].self_link
    : null
  )

  waf_rules = {
    sqli    = { priority = 1000, rule = "sqli-v33-stable", description = "SQL injection (OWASP CRS)" }
    xss     = { priority = 1001, rule = "xss-v33-stable", description = "Cross-site scripting (OWASP CRS)" }
    lfi     = { priority = 1002, rule = "lfi-v33-stable", description = "Local file inclusion (OWASP CRS)" }
    rce     = { priority = 1003, rule = "rce-v33-stable", description = "Remote code execution (OWASP CRS)" }
    scanner = { priority = 1004, rule = "scannerdetection-v33-stable", description = "Security scanners (OWASP CRS)" }
  }

  # Paths are matched on the web host's path (the API shares the app origin behind the LB).
  rate_rules = {
    signup = {
      priority    = 2000
      expression  = "request.path.startsWith('/auth/signup') || request.path.startsWith('/v1/signup')"
      count       = var.cloud_armor.rate_limits.signup_per_minute
      description = "Self-service sign-up: per-IP rate ban"
    }
    auth = {
      priority    = 2001
      expression  = "request.path.startsWith('/auth/')"
      count       = var.cloud_armor.rate_limits.auth_per_minute
      description = "Sign-in, callback and e-mail verification: per-IP rate ban"
    }
    embeds = {
      priority    = 2002
      expression  = "request.path.startsWith('/v1/embeds/')"
      count       = var.cloud_armor.rate_limits.embed_per_minute
      description = "Public embed links: per-IP rate ban"
    }
    score = {
      priority    = 2003
      expression  = "request.path.matches('^/v1/endpoints/[^/]+/score$')"
      count       = var.cloud_armor.rate_limits.score_per_minute
      description = "Endpoint scoring: per-IP rate ban"
    }
  }
}

resource "google_compute_security_policy" "edge" {
  count       = local.armor
  project     = var.project_id
  name        = "${local.prefix}-edge"
  description = "mapi ${var.env}: OWASP CRS rules, per-IP rate bans and Adaptive Protection for the load balancer."
  type        = "CLOUD_ARMOR"

  advanced_options_config {
    json_parsing                 = "STANDARD"
    log_level                    = var.cloud_armor.verbose_logs ? "VERBOSE" : "NORMAL"
    request_body_inspection_size = var.cloud_armor.request_body_inspection_size
  }

  adaptive_protection_config {
    layer_7_ddos_defense_config {
      enable          = var.cloud_armor.adaptive_protection
      rule_visibility = "STANDARD"
    }
  }

  dynamic "rule" {
    for_each = length(var.cloud_armor.trusted_ip_ranges) > 0 ? [1] : []
    content {
      priority    = 100
      action      = "allow"
      description = "Trusted address ranges (CI, office): skip the rules below"
      match {
        versioned_expr = "SRC_IPS_V1"
        config {
          src_ip_ranges = var.cloud_armor.trusted_ip_ranges
        }
      }
    }
  }

  dynamic "rule" {
    for_each = local.waf_rules
    content {
      priority    = rule.value.priority
      action      = "deny(403)"
      preview     = var.cloud_armor.waf_mode == "preview"
      description = rule.value.description
      match {
        expr {
          expression = "evaluatePreconfiguredWaf('${rule.value.rule}', {'sensitivity': ${var.cloud_armor.waf_sensitivity}})"
        }
      }
    }
  }

  dynamic "rule" {
    for_each = local.rate_rules
    content {
      priority    = rule.value.priority
      action      = "rate_based_ban"
      preview     = var.cloud_armor.rate_limit_mode == "preview"
      description = rule.value.description
      match {
        expr {
          expression = rule.value.expression
        }
      }
      rate_limit_options {
        conform_action   = "allow"
        exceed_action    = "deny(429)"
        enforce_on_key   = "IP"
        ban_duration_sec = var.cloud_armor.rate_limits.ban_duration_sec
        rate_limit_threshold {
          count        = rule.value.count
          interval_sec = 60
        }
        ban_threshold {
          count        = rule.value.count * 3
          interval_sec = 300
        }
      }
    }
  }

  rule {
    priority    = 2147483647
    action      = "allow"
    description = "Default: allow"
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}
