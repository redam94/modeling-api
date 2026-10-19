---
description: Running self-service sign-up on modeling-api - e-mail verification and mailers, the disposable-domain blocklist, sign-up rate limits, Sybil heuristics and the operators' review queue, identity verification and cut line A (public tenants run built-ins and declarative specs only).
---

# Sign-up and abuse controls <span class="mapi-badge">v0.4</span>

With self-service sign-up, each person with an e-mail address can make a **public**
workspace. Public workspaces run untrusted *data* through trusted *code*: only built-in
models and declarative specs (cut line A). This page is for the person who operates the
platform. It tells how sign-up works, what stops abuse, and what operators examine. Each
variable on this page is in the
[configuration reference](configuration.md#mapi-server-api-and-worker). The HTTP operations
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

1. `POST /auth/signup` first refuses requests that can never sign up. These are: terms not
   accepted (`TERMS_NOT_ACCEPTED`), an address that is malformed or disposable
   (`EMAIL_NOT_ALLOWED`), and a failed bot check. Then it applies the rate limits below
   (`RATE_LIMITED` with `Retry-After`).
2. It keeps the sign-up with an HMAC of a new 200-bit token. It never keeps the token. It
   sends the link `MAPI_SIGNUP_VERIFY_URL` by e-mail. Only the newest link of an address
   works.
3. **The answer is the same for all addresses**, new or already registered. Thus nobody can
   use sign-up to find out who has an account. If an account that is already there verifies
   a new sign-up, it gets one more workspace.
4. `POST /auth/verify-email` uses the token. In one transaction, it makes these items:
    - The tenant (tier `public`, with an id from the organisation name).
    - Its `default` project.
    - The person as tenant admin. This is an invitation that the person gets at the first
      sign-in with that **verified** address.
    - The verification record of the tenant.

    A link that is expired, replaced or used answers `410 LINK_EXPIRED`.

The audit log records all events: `auth.signup`, `auth.signup_rejected`, `auth.email_verified`,
`tenant.create` and `auth.signup_flagged`. It records a limited number of
`auth.signup_rejected` events for each client in each minute, because each person can cause
them.

## Mailers

| `MAPI_MAILER` | Use | Notes |
|---|---|---|
| `smtp` | Production | `smtplib` with STARTTLS (necessary, never opportunistic) or implicit TLS (`MAPI_SMTP_TLS=ssl`, port 465). The mailer verifies certificates. Needs `MAPI_SMTP_HOST`. Credentials in `MAPI_SMTP_USERNAME`/`MAPI_SMTP_PASSWORD`. No third-party mail API |
| `outbox` | `dev` profile, end-to-end tests | One JSON file for each message (mode `0600`) under `MAPI_OUTBOX_DIR`. The `dev` profile serves the newest messages at `GET /dev/outbox?to=<address>`, with the token in `tags.token` |
| `console` | Single-user installations | Logs the message, with the link |
| `none` | | Sign-up is off |

The `multi-tenant` profile refuses `console` and `outbox` while sign-up is on. With these
mailers, each person who can read the logs or the disk could verify all addresses. Without an SMTP host,
sign-up is off (`POST /auth/signup` answers `403`). Messages are plain text. The mailer
refuses header values with line breaks. Thus nobody can inject a header through the name of
a sign-up.

```bash
# dev: follow a verification link without a mail server
curl -s -X POST localhost:8000/auth/signup -H 'Content-Type: application/json' \
  -d '{"email":"ana@acme.example","name":"Ana","accepted_terms":true,"terms_version":"2026-10"}'
TOKEN=$(curl -s 'localhost:8000/dev/outbox?to=ana@acme.example' | jq -r '.items[0].tags.token')
curl -s -X POST localhost:8000/auth/verify-email -H 'Content-Type: application/json' \
  -d "{\"token\":\"$TOKEN\"}"
```

## Disposable addresses

The built-in list (`mapi_spec.trust.DEFAULT_DISPOSABLE_DOMAINS`) is small on purpose. To add
domains, use `MAPI_SIGNUP_BLOCKED_DOMAINS` (comma-separated). You can also point
`MAPI_SIGNUP_BLOCKLIST_PATH` at a file (one domain on each line, `#` comments). For example,
use a public disposable-domain list that your deployment updates. A domain on the list also
blocks its subdomains. If a deployment wants a vendor check, it can add a
`mapi_spec.trust.DisposableEmailPolicy` to `TrustServices.email_policy`.

## Rate limits

| Limit | Default | Variable |
|---|---|---|
| Sign-ups for each client address in each hour | 5 | `MAPI_SIGNUP_PER_IP_PER_HOUR` |
| *Verified* sign-ups for each e-mail domain in each day | 25 | `MAPI_SIGNUP_PER_DOMAIN_PER_DAY` |
| *Verified* sign-ups on the whole platform in each hour | 500 | `MAPI_SIGNUP_GLOBAL_PER_HOUR` |
| Verification attempts for each client address in each minute | 20 | `MAPI_VERIFY_PER_IP_PER_MINUTE` |

The server counts the limits in Postgres from the sign-ups. Thus the limits apply across API
instances. Each request counts toward the limit for each address. This limit stops floods of
unverified sign-ups. The domain and platform caps count only sign-ups whose link was opened.
If not, a person could send 25 false `@gmail.com` (or `@your-customer.com`) addresses. That
domain could then not sign up for one day.

Large free-mail domains (`MAPI_SIGNUP_SHARED_EMAIL_DOMAINS`, by default `gmail.com`,
`outlook.com`, `yahoo.com`, `icloud.com`, `proton.me` and similar domains) have no cap for
each domain. Behind the Google Cloud load balancer,
[Cloud Armor](security-hardening.md#cloud-armor) adds rate *bans* for each IP in front of
`/auth/signup` and `/auth/*`. These bans apply before a request goes to the API. The server keeps client
addresses only as keyed digests.

The client address is the peer that connects, but not if that peer is a trusted proxy
(`MAPI_TRUSTED_PROXIES`). Then the client address is the right-most `X-Forwarded-For` hop
that is not a trusted proxy. Thus callers behind one load balancer keep separate limits. A
caller cannot forge its address when it sends the header itself. The multi-tenant profile
does not start without the setting (use `none` when clients connect to the API directly).

## Sybil heuristics and the review queue

When a sign-up is verified, the platform looks back `MAPI_SIGNUP_SYBIL_WINDOW_DAYS` (30) days
for workspaces that come from the same signals:

| Flag | Signal |
|---|---|
| `same_ip` | the same client address (keyed digest) |
| `same_device` | the same browser: a random, HttpOnly `mapi_device` cookie that the server sets at sign-up |
| `same_email` | the same e-mail address |
| `domain_burst` | many verified sign-ups from one domain in one hour (two times the threshold). Never for the shared free-mail domains |

At `MAPI_SIGNUP_SYBIL_THRESHOLD` (3) earlier workspaces, the server **flags** the new
sign-up. The workspace works, and it waits in the review queue (`auth.signup_flagged` in the
audit log). With `MAPI_SIGNUP_FLAGGED_SUSPEND=true`, the server suspends a flagged workspace
(read-only) until an operator approves it.

Operators examine flagged sign-ups:

```bash
# the queue, newest first
curl -s -b cookies "$API/v1/admin/signups?review=flagged"
# reject: the workspace is suspended (audited, with the reason)
curl -s -b cookies -H "X-CSRF-Token: $CSRF" -X POST "$API/v1/admin/signups/$ID/review" \
  -H 'Content-Type: application/json' -d '{"decision":"reject","reason":"device farm"}'
# approve: the flag is cleared, and a suspension the review caused is lifted
```

An approval removes only the suspensions that the review caused. It never removes a
suspension that an operator set. If you reject a sign-up that was never verified, its link
stops working.

## Identity verification and cut line A

Each tenant has a verification record. `GET /v1/tenants/{tenant_id}/verification` answers
`email_verified`, `identity_verified`, the method and `uploaded_code_allowed`, with a reason
in plain words. The rule (`mapi_spec.trust.uploaded_code_allowed`):

| Trust tier | Uploaded code (plugins with an overlay) |
|---|---|
| `internal`, `self-hosted-trusted` | allowed |
| `verified` | only after identity verification |
| `public` | never before cut line B (`MAPI_PUBLIC_CODE_UPLOAD_ENABLED`, planned). After cut line B, only with identity verification |

The server enforces the rule at **run admission**. If a tenant cannot run uploaded code, the
server refuses its runs of uploaded code with `403 IDENTITY_VERIFICATION_REQUIRED`. The audit
log records this as a denied `run.create`. The server always admits built-in models and
declarative specs. The plugin upload of M4 calls the same check
(`mapi_server.services.verification.require_uploaded_code_allowed`).

After a check outside the platform (a contract, an invoice), operators record the identity
verification:

```bash
curl -s -b cookies -H "X-CSRF-Token: $CSRF" -X PUT \
  "$API/v1/admin/tenants/acme/verification" -H 'Content-Type: application/json' \
  -d '{"identity_verified":true,"method":"manual","reason":"signed enterprise contract"}'
```

The request-path database role can only *read* verification records. Thus no tenant session
can mark its own tenant as verified. A card or document provider connects as a
`mapi_server.services.trust.IdentityVerifier`. A deterministic `FakeIdentityVerifier` is
available for tests and drills. No vendor is integrated yet.

## Bot checks

`SignupRequest.challenge` contains a bot-check token (for example Cloudflare Turnstile). The
server asks `TrustServices.challenge` (`mapi_server.services.trust.SignupChallenge`). The
default accepts all sign-ups. Then the rate limits, Cloud Armor and the Sybil review do the
work. A deployment that adds a vendor check implements the protocol.

## Runbook: a sign-up wave

1. Look at the queue (`GET /v1/admin/signups?review=flagged`) and the audit log
   (`auth.signup_rejected`, `auth.signup_flagged`).
2. Decrease the limits (`MAPI_SIGNUP_PER_IP_PER_HOUR`, `MAPI_SIGNUP_GLOBAL_PER_HOUR`), or set
   `MAPI_SIGNUP_FLAGGED_SUSPEND=true`.
3. Deploy the API again.
4. Block the domains (`MAPI_SIGNUP_BLOCKED_DOMAINS`).
5. At the edge, decrease `cloud_armor.rate_limits.signup_per_minute` in Terraform.
6. Reject the flagged sign-ups.
7. Suspend all other workspaces that you must stop with `PATCH /v1/admin/tenants/{id}`.
   Public workspaces never ran uploaded code, so their runs used only built-in models. The
   [kill switches](../guides/administration.md) and the spend cap still limit what they can
   spend.
8. To close sign-up fully, set `MAPI_SIGNUP_ENABLED=false`.
