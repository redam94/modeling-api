---
description: Running mapi-server multi-tenant - sign-in with an OpenID provider (Google, any OIDC IdP, or the offline development IdP), API tokens, CSRF, operators, the two database roles behind row-level security, and Pub/Sub push authentication.
---

# Authentication and tenant isolation <span class="mapi-badge mapi-badge--soon">v0.3</span>

This page is for whoever runs `mapi-server`. The concepts (roles, scopes, project
selection) are in [Tenancy and authentication](../concepts/tenancy-and-auth.md); every
variable named here is in the [configuration reference](configuration.md).

## Profiles

| `MAPI_PROFILE` | Who may call the API | Sign-in |
|---|---|---|
| `single-tenant` (default) | Anyone who can reach it (it listens on loopback): credential-less requests act as the **local administrator** of the `default` tenant. Sessions and API tokens work too | Optional |
| `multi-tenant` | Only callers with a browser session or an API token; everything else is `401` | `MAPI_OIDC_*` |
| `dev` | As `multi-tenant` | The built-in development IdP (below) |

In every profile, each request is checked against the operation's rule in
`mapi_spec.api.ROUTE_ACCESS` (a route without one cannot start), the caller's roles are read
from memberships *at that moment*, and every database query of a request runs in a
transaction pinned to the request's tenant (row-level security, below).

## Browser sign-in (BFF)

The web app never sees an OAuth token. The API does the OpenID Connect work:

1. `GET /auth/login?return_to=/runs` stores a login transaction (state, nonce and PKCE
   verifier) server-side, sets a 10-minute `__Host-mapi_login` cookie and redirects to the
   provider (Authorization Code flow, PKCE S256, nonce).
2. `GET /auth/callback` consumes the transaction exactly once, compares `state` in constant
   time, exchanges the code with the verifier and verifies the ID token against the
   provider's JWKS: signature (asymmetric algorithms only; `alg` must match the key),
   issuer, audience (and `azp`), expiry, issued-at and nonce. Keys are cached for an hour and
   refetched when a token names an unknown key id (rotation).
3. It finds the user by `(issuer, subject)`. A first sign-in with a **verified** e-mail
   claims pending invitations for that address; an unverified e-mail claims nothing.
4. It starts a server-side session and sets two cookies:

    | Cookie | Attributes | Purpose |
    |---|---|---|
    | `__Host-mapi_session` | `HttpOnly; Secure; SameSite=Strict; Path=/` | The session (the server stores only its SHA-256). 2 h idle, 12 h absolute |
    | `__Host-mapi_csrf` | `Secure; SameSite=Strict; Path=/` (readable by the app) | The CSRF token |

Without TLS (`MAPI_COOKIE_SECURE=false`, the default for `http://` API URLs) the cookies are
`mapi_session` and `mapi_csrf` without `Secure`. Use that for local development only.

**CSRF.** Every cookie-authenticated `POST`, `PUT`, `PATCH` and `DELETE` (logout included)
must send the session's token in `X-CSRF-Token` (the `mapi_csrf` cookie, or the
`X-CSRF-Token` response header of `GET /auth/session`). Requests whose `Origin` is not the
web app or the API, or with `Sec-Fetch-Site: cross-site`, are refused too. API-token
requests are not cookie-authenticated and need no CSRF token.

`GET /auth/session` answers whether the browser is signed in; `POST /auth/logout` ends the
session. Failed sign-ins answer `401` with a generic message; the reason is in the audit
log (`auth.login_failed`).

### Google sign-in

1. In the Google Cloud console, create an OAuth client ID of type **Web application** with
   the authorized redirect URI `https://<api host>/auth/callback`.
2. Configure the API:

    ```bash
    MAPI_PROFILE=multi-tenant
    MAPI_API_URL=https://api.mapi.example
    MAPI_OIDC_ISSUER=https://accounts.google.com
    MAPI_OIDC_CLIENT_ID=1234-abc.apps.googleusercontent.com
    MAPI_OIDC_CLIENT_SECRET=...            # from Secret Manager, never in images
    MAPI_OPERATOR_EMAILS=oncall@mapi.example
    ```

    Google's ID tokens use the issuer `https://accounts.google.com` (older ones
    `accounts.google.com`); both are accepted. `MAPI_OIDC_EXTRA_PARAMS='{"hd": "acme.com"}'`
    pre-selects a Workspace domain in the account chooser. It is only a hint: who may do
    what is decided by memberships.

**Other providers** (Okta, Entra ID, Keycloak, Auth0, an Identity Platform tenant fronted by
an OIDC-compliant provider): set `MAPI_OIDC_ISSUER` to the issuer whose
`/.well-known/openid-configuration` lists an authorization endpoint, a token endpoint and a
JWKS. Identity Platform's own client-SDK tokens (issuer `securetoken.google.com/<project>`,
obtained without an authorization-code endpoint) are not accepted by the BFF yet.

### The development IdP (offline)

`MAPI_PROFILE=dev` mounts a small OpenID provider in the API at `/dev/idp`, so sign-in
works without a network:

- `/auth/login` redirects to `/dev/idp/authorize`, a page listing the accounts in
  `MAPI_DEVIDP_USERS` (default `admin@dev.local`, `analyst@dev.local`, `viewer@dev.local`);
- at start-up these accounts are invited: the first as admin of the `default` tenant and
  platform operator, the second as `runner` and the others as `viewer` of the `default`
  project;
- its ID tokens are RS256-signed with a key generated at start-up and go through exactly
  the same verification as a real provider's; PKCE is enforced and codes are single-use.

There are no passwords: anyone who can reach a `dev` API can sign in as any listed account.
The IdP refuses to mount in any other profile.

The CLI can use it too (see [The CLI](#the-cli)). Its issuer is the identifier
`https://dev-idp.mapi.localhost`, and the API serves its discovery document:

```bash
mapi login --issuer https://dev-idp.mapi.localhost \
           --discovery-url http://127.0.0.1:8000/dev/idp
```

## The CLI

`mapi login` signs in with the identity provider directly, using the authorization code flow
with PKCE and a one-off listener on `127.0.0.1`. It then sends the ID token as
`Authorization: Bearer`. The API accepts such a bearer when all of these hold:

- it is a JWT, not a `mapi_` API token;
- it is signed by the configured issuer (checked against its JWKS), and has not expired;
- its audience is one of `MAPI_OIDC_CLI_CLIENT_IDS` (default `mapi-cli`). The BFF's own client
  id is not accepted.

The caller is then the same user as in a browser session. The first use claims a pending
invitation, exactly like `/auth/callback`. No cookie is involved, so no CSRF token is needed,
and `GET /v1/me` reports `auth: session`. Set `MAPI_OIDC_CLI_CLIENT_IDS=` (empty) to accept
API tokens only. Register the CLI as a public "installed app" client with your provider; it
needs a loopback redirect URI, PKCE and the `openid email` scopes.

## API tokens

Programs send `Authorization: Bearer mapi_<prefix>_<secret><checksum>`. A token is minted
for its caller in the selected project (`POST /v1/tokens`, shown once), acts as its owner
narrowed by its scopes, and is bound to that project:

- the server stores only HMAC-SHA256 of the token under a pepper derived from
  `MAPI_SERVER_SECRET` and compares in constant time; a typo is caught by the checksum
  before any database lookup, and no error ever echoes the presented value;
- expired, revoked or orphaned tokens (owner disabled) answer `401`; a wrong secret for a
  known prefix, expiry and revocation are audited as `token.auth_failed`;
- a disabled account stays disabled: signing in again is refused (a generic `401`, audited
  as `auth.login_failed`), and only an invitation becomes active at its first sign-in;
- a token never carries operator powers, even if its owner is an operator;
- per-token `rate_limit_per_minute` (`RATE_LIMITED`, `Retry-After`) and a vCPU-second
  budget per UTC day or month apply in addition to the tenant's quotas.

Rotate the root secret only together with every token: all existing tokens stop working.

## Operators

Users whose **verified** e-mail is in `MAPI_OPERATOR_EMAILS` are platform operators (the
flag follows the configuration at every sign-in). With a browser session they may use
`/v1/admin/*` (tenants, quota policies, kill switches, executor health, the platform audit
log) and read metadata of every tenant: tenants, members, token listings, runs and usage.
No role, flag or token lets them read datasets, specs, models or plots. Every change and
every refused attempt at an operator endpoint is audited.

## Row-level security

`mapi-server migrate` creates two cluster-wide roles and turns on `FORCE ROW LEVEL SECURITY`
for every table that holds tenant data, with a fail-closed policy on
`current_setting('app.tenant_id', false)`:

| Role | Used by | Row-level security |
|---|---|---|
| `mapi_app` | Request handlers (one transaction per request, pinned with `set_config('app.tenant_id', <tenant>, true)`) | Applies: it sees and writes only that tenant's rows. Owns no table |
| `mapi_system` | Dispatcher, reconciler, finalizer, events, authentication lookups (which token is this, which tenants does this user belong to) and operator endpoints | `BYPASSRLS` where the migrating login is a superuser, plus an explicit policy, so it also works on managed Postgres and for logins that are members of it |

A transaction that forgot to pin a tenant fails closed: on a fresh connection every query
errors, on a pooled connection the setting is empty and matches no row. The test suite runs
deliberately unscoped queries for two tenants over one pooled connection to prove it.

With a single `DATABASE_URL` (local development, the default Compose file) both pools log
in as the migrating user and select their role with the start-up option `role=...`
(`RESET ROLE` returns to that role). In production give each its own login:

```sql
CREATE ROLE mapi_api LOGIN PASSWORD '...' IN ROLE mapi_app;
CREATE ROLE mapi_orchestrator LOGIN PASSWORD '...' IN ROLE mapi_system;
```

and set `MAPI_DATABASE_APP_URL` (API) and `MAPI_DATABASE_SYSTEM_URL` (API and worker).
`DATABASE_URL` (the owner) is then needed by `mapi-server migrate` and procrastinate only.

## Pub/Sub push

In the cloud, run events reach `POST /internal/events` from a Pub/Sub push subscription
with OIDC authentication. Configure the subscription with a dedicated service account and
the audience `https://<api host>/internal/events`, then set

```bash
MAPI_PUBSUB_PUSH_AUDIENCE=https://api.mapi.example/internal/events
MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT=pubsub-push@mapi-prod-control.iam.gserviceaccount.com
# optional: only accept pushes from these subscriptions
MAPI_PUBSUB_PUSH_SUBSCRIPTIONS=projects/mapi-prod-control/subscriptions/mapi-run-events-push
```

Each push must carry a Google-signed ID token (RS256, checked against
`MAPI_GOOGLE_JWKS_URL`) whose issuer is Google, audience the configured one, `email` the
service account with `email_verified: true`, and which has not expired (a valid Google token for another audience or account gets 403).
With `MAPI_PUBSUB_PUSH_SUBSCRIPTIONS` set, the envelope must also come from one of those
subscriptions. Without these
settings `MAPI_INTERNAL_TOKEN` is required when set, and a multi-tenant deployment with
neither refuses every push. Refused pushes are audited as `event.rejected`.
