# Identity Platform (plan Q12: the auth provider at launch; enterprise SSO after M5).
#
# The API is a backend-for-frontend: it runs the authorization-code flow with PKCE, keeps the
# tokens server-side and gives the browser only an HttpOnly, Secure, SameSite=Strict session
# cookie (mapi_spec.auth). Identity providers (Google, SAML, OIDC) are added to this config in
# the console or with google_identity_platform_*_idp_config resources; their client secrets do
# not belong in this module's state.

resource "google_identity_platform_config" "this" {
  count = var.identity_platform.enabled ? 1 : 0

  project = var.project_id
  authorized_domains = distinct(concat(
    length(var.identity_platform.authorized_domains) > 0 ? var.identity_platform.authorized_domains : [local.host.web],
  ))
  autodelete_anonymous_users = true

  sign_in {
    allow_duplicate_emails = false

    anonymous {
      enabled = false
    }

    email {
      enabled           = var.identity_platform.email_signin_enabled
      password_required = true
    }
  }
}
