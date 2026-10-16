---
description: Running self-service sign-up on modeling-api - e-mail verification and mailers, the disposable-domain blocklist, sign-up rate limits, Sybil heuristics and the operators' review queue, identity verification and cut line A (public tenants run built-ins and declarative specs only).
---

# Sign-up and abuse controls <span class="mapi-badge">v0.4</span>

Self-service sign-up lets anyone with an e-mail address create a **public** workspace. Public
workspaces run untrusted *data* through trusted *code*: built-in models and declarative specs
only (cut line A). This page is for whoever runs the platform: how sign-up works, what stops
abuse, and what operators review. Every variable named here is in the
[configuration reference](configuration.md#mapi-server-api-and-worker); the HTTP operations
are in the [HTTP API reference](../reference/http-api.md#sign-up-verification-deletion-and-export).

## How sign-up works

```mermaid
sequenceDiagram
    participant B as Browser
    participant API as API (/auth)
    participant M as Mailer
    B->>API: POST /auth/signup {email, name, organisation, accepted_terms}
    API->>API: terms, blocklist, bot check, rate limits
    API->>M: verification link (single use, 24 h)
    API-->>B: 202 {"status": "check_email"}
    B->>API: POST /auth/verify-email {token}
    API->>API: create tenant (public), project "default", admin grant; Sybil checks
    API-->>B: 200 {tenant_id, login_url}
    B->>API: GET /auth/login (OpenID sign-in with that address)
```

1. `POST /auth/signup` refuses early what can never sign up: terms not accepted
   (`TERMS_NOT_ACCEPTED`), a malformed or disposable address (`EMAIL_NOT_ALLOWED`), a failed
   bot check. Then it applies the rate limits below (`RATE_LIMITED` with `Retry-After`).
2. It stores the sign-up with an HMAC of a fresh 200-bit token (never the token itself) and
   e-mails the link `MAPI_SIGNUP_VERIFY_URL`. Only the newest link of an address works.
3. **The answer is the same for every address**, new or already registered, so sign-up
   cannot be used to find out who has an account. An existing account that verifies a new
   sign-up gets one more workspace.
4. `POST /auth/verify-email` consumes the token and, in one transaction, creates the tenant
   (tier `public`, id from the organisation name), its `default` project, the person as
   tenant admin (an invitation claimed at their first sign-in with that **verified** address)
   and the tenant's verification record. An expired, superseded or used link answers
   `410 LINK_EXPIRED`.

Everything is audited: `auth.signup`, `auth.signup_rejected` (bounded per client and minute,
since anyone can cause them), `auth.email_verified`, `tenant.create` and
`auth.signup_flagged`.

## Mailers

| `MAPI_MAILER` | Use | Notes |
|---|---|---|
| `smtp` | Production | `smtplib` with STARTTLS (required, never opportunistic) or implicit TLS (`MAPI_SMTP_TLS=ssl`, port 465); certificates are verified. Needs `MAPI_SMTP_HOST`; credentials in `MAPI_SMTP_USERNAME`/`MAPI_SMTP_PASSWORD`. No third-party mail API |
| `outbox` | `dev` profile, end-to-end tests | One JSON file per message (mode `0600`) under `MAPI_OUTBOX_DIR`. The `dev` profile serves the newest at `GET /dev/outbox?to=<address>`, with the token in `tags.token` |
| `console` | Single-user installs | Logs the message, link included |
| `none` | | Sign-up is off |

The `multi-tenant` profile refuses `console` and `outbox` while sign-up is on: whoever can
read the logs or the disk could verify any address. Without an SMTP host sign-up is simply
off (`POST /auth/signup` answers `403`). Messages are plain text; header values with line
breaks are refused (no header injection through a sign-up's name).

```bash
# dev: follow a verification link without a mail server
curl -s -X POST localhost:8000/auth/signup -H 'Content-Type: application/json' \
  -d '{"email":"ana@acme.example","name":"Ana","accepted_terms":true,"terms_version":"2026-10"}'
TOKEN=$(curl -s 'localhost:8000/dev/outbox?to=ana@acme.example' | jq -r '.items[0].tags.token')
curl -s -X POST localhost:8000/auth/verify-email -H 'Content-Type: application/json' \
  -d "{\"token\":\"$TOKEN\"}"
```

## Disposable addresses

The built-in list (`mapi_spec.trust.DEFAULT_DISPOSABLE_DOMAINS`) is deliberately small. Add
domains with `MAPI_SIGNUP_BLOCKED_DOMAINS` (comma-separated) or point
`MAPI_SIGNUP_BLOCKLIST_PATH` at a file (one domain per line, `#` comments), for example a
maintained public disposable-domain list refreshed by your deployment. A listed domain also
blocks its subdomains. Deployments that want a vendor check plug a
`mapi_spec.trust.DisposableEmailPolicy` into `TrustServices.email_policy`.

## Rate limits

| Limit | Default | Variable |
|---|---|---|
| Sign-ups per client address and hour | 5 | `MAPI_SIGNUP_PER_IP_PER_HOUR` |
| Sign-ups per e-mail domain and day | 25 | `MAPI_SIGNUP_PER_DOMAIN_PER_DAY` |
| Sign-ups on the whole platform per hour | 500 | `MAPI_SIGNUP_GLOBAL_PER_HOUR` |
| Verification attempts per client address and minute | 20 | `MAPI_VERIFY_PER_IP_PER_MINUTE` |

They are counted in Postgres from the sign-ups themselves, so they hold across API
instances. Behind the Google Cloud load balancer, [Cloud Armor](security-hardening.md#cloud-armor)
adds per-IP rate *bans* in front of `/auth/signup` and `/auth/*` before a request reaches the
API. Client addresses are stored only as keyed digests.

The client address is the connecting peer unless that peer is a trusted proxy
(`MAPI_TRUSTED_PROXIES`): then it is the right-most `X-Forwarded-For` hop that is not a
trusted proxy, so callers behind one load balancer keep separate limits and a caller cannot
forge its address by sending the header itself. The multi-tenant profile refuses to start
without the setting (`none` when clients reach the API directly).

## Sybil heuristics and the review queue

When a sign-up is verified, the platform looks back `MAPI_SIGNUP_SYBIL_WINDOW_DAYS` (30) days
for workspaces created from the same signals:

| Flag | Signal |
|---|---|
| `same_ip` | the same client address (keyed digest) |
| `same_device` | the same browser: a random, HttpOnly `mapi_device` cookie set at sign-up |
| `same_email` | the same e-mail address |
| `domain_burst` | many sign-ups from one domain within an hour (twice the threshold) |

At `MAPI_SIGNUP_SYBIL_THRESHOLD` (3) earlier workspaces the new sign-up is **flagged**: the
workspace works, and it waits in the review queue (`auth.signup_flagged` in the audit log).
With `MAPI_SIGNUP_FLAGGED_SUSPEND=true` a flagged workspace is suspended (read-only) until an
operator approves it.

Operators review flagged sign-ups:

```bash
# the queue, newest first
curl -s -b cookies "$API/v1/admin/signups?review=flagged"
# reject: the workspace is suspended (audited, with the reason)
curl -s -b cookies -H "X-CSRF-Token: $CSRF" -X POST "$API/v1/admin/signups/$ID/review" \
  -H 'Content-Type: application/json' -d '{"decision":"reject","reason":"device farm"}'
# approve: the flag is cleared, and a suspension the review caused is lifted
```

An approval only lifts suspensions the review itself caused (never an operator's own
suspension). Rejecting a sign-up that was never verified kills its link.

## Identity verification and cut line A

Every tenant has a verification record: `GET /v1/tenants/{tenant_id}/verification` answers
`email_verified`, `identity_verified`, the method and `uploaded_code_allowed` with a reason in
plain words. The rule (`mapi_spec.trust.uploaded_code_allowed`):

| Trust tier | Uploaded code (plugins with an overlay) |
|---|---|
| `internal`, `self-hosted-trusted` | allowed |
| `verified` | only once identity is verified |
| `public` | never before cut line B (`MAPI_PUBLIC_CODE_UPLOAD_ENABLED`, M5); then only with identity verification |

The rule is enforced at **run admission**: a run of uploaded code for a tenant that may not
run it is refused with `403 IDENTITY_VERIFICATION_REQUIRED` (audited as a denied
`run.create`). Built-in models and declarative specs are always admitted. The plugin upload
of M4 calls the same check (`mapi_server.services.verification.require_uploaded_code_allowed`).

Operators record identity verification after an out-of-band check (a contract, an invoice):

```bash
curl -s -b cookies -H "X-CSRF-Token: $CSRF" -X PUT \
  "$API/v1/admin/tenants/acme/verification" -H 'Content-Type: application/json' \
  -d '{"identity_verified":true,"method":"manual","reason":"signed enterprise contract"}'
```

The request-path database role may only *read* verification records, so no tenant session can
mark its own tenant verified. A card or document provider plugs in as a
`mapi_server.services.trust.IdentityVerifier` (a deterministic `FakeIdentityVerifier` exists
for tests and drills); no vendor is integrated yet.

## Bot checks

`SignupRequest.challenge` carries a bot-check token (for example Cloudflare Turnstile). The
server consults `TrustServices.challenge` (`mapi_server.services.trust.SignupChallenge`); the
default accepts every sign-up, leaving the rate limits, Cloud Armor and the Sybil review to
do the work. A deployment that adds a vendor check implements the protocol.

## Runbook: a sign-up wave

1. Look at the queue (`GET /v1/admin/signups?review=flagged`) and the audit log
   (`auth.signup_rejected`, `auth.signup_flagged`).
2. Lower the limits (`MAPI_SIGNUP_PER_IP_PER_HOUR`, `MAPI_SIGNUP_GLOBAL_PER_HOUR`) or set
   `MAPI_SIGNUP_FLAGGED_SUSPEND=true`, and redeploy the API.
3. Block the domains (`MAPI_SIGNUP_BLOCKED_DOMAINS`) and, at the edge, lower
   `cloud_armor.rate_limits.signup_per_minute` in Terraform.
4. Reject the flagged sign-ups; suspend any other workspace with
   `PATCH /v1/admin/tenants/{id}`. Public workspaces never ran uploaded code, so their runs
   used built-in models only; the [kill switches](../guides/administration.md) and the spend
   cap still bound what they can spend.
5. To close sign-up entirely: `MAPI_SIGNUP_ENABLED=false`.
