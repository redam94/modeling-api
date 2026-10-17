---
description: Running mapi-server multi-tenant - sign-in with an OpenID provider (Google, any OIDC IdP, or the offline development IdP), API tokens, CSRF, operators, the two database roles behind row-level security, and Pub/Sub push authentication.
---

# Authentication and tenant isolation <span class="mapi-badge">v0.3</span>

This page is for the person who operates `mapi-server`. The concepts (roles, scopes, project
selection) are in [Tenancy and authentication](../concepts/tenancy-and-auth.md). Each
variable on this page is in the [configuration reference](configuration.md).

## Profiles

| `MAPI_PROFILE` | Who can call the API | Sign-in |
|---|---|---|
| `single-tenant` (default) | All persons who can get access to the API (it listens on loopback). Requests without credentials act as the **local administrator** of the `default` tenant. Sessions and API tokens also work | Optional |
| `multi-tenant` | Only callers with a browser session or an API token. All other requests get `401` | `MAPI_OIDC_*` |
| `dev` | The same as `multi-tenant` | The built-in development IdP (below) |

These rules apply in all profiles:

- The server does a check of each request against the rule of the operation in
  `mapi_spec.api.ROUTE_ACCESS`. A route without a rule cannot start.
- The server reads the roles of the caller from the memberships *at that moment*.
- Each database query of a request runs in a transaction that is pinned to the tenant of the
  request (row-level security, below).

## Browser sign-in (BFF)

The web app never sees an OAuth token. The API does the OpenID Connect work:

1. `GET /auth/login?return_to=/runs` keeps a login transaction (state, nonce and PKCE
   verifier) on the server. It sets a `__Host-mapi_login` cookie for 10 minutes. Then it
   redirects to the provider (Authorization Code flow, PKCE S256, nonce).
2. `GET /auth/callback` uses the transaction one time only. It compares `state` in constant
   time and exchanges the code with the verifier. Then it verifies the ID token against the
   JWKS of the provider. It verifies these items:
    - The signature. Only asymmetric algorithms are permitted, and `alg` must agree with the
      key.
    - The issuer, the audience (and `azp`), the expiry, the issue time and the nonce.

    The server keeps the keys in a cache for one hour. If a token names a key id that the
    server does not know (rotation), the server gets the keys again.
3. The server finds the user by `(issuer, subject)`. At the first sign-in with a **verified**
   e-mail, the user gets the pending invitations for that address. An e-mail that is not
   verified gets no invitations.
4. The server starts a session on the server and sets two cookies:

    | Cookie | Attributes | Purpose |
    |---|---|---|
    | `__Host-mapi_session` | `HttpOnly; Secure; SameSite=Strict; Path=/` | The session (the server keeps only its SHA-256). 2 h idle, 12 h absolute |
    | `__Host-mapi_csrf` | `Secure; SameSite=Strict; Path=/` (the app can read it) | The CSRF token |

Without TLS, the cookies are `mapi_session` and `mapi_csrf` without `Secure`. This occurs
with `MAPI_COOKIE_SECURE=false`, which is the default for `http://` API URLs. Use this
configuration only for local development.

**CSRF.** Each `POST`, `PUT`, `PATCH` and `DELETE` that uses cookie authentication must send
the token of the session in `X-CSRF-Token`. This includes logout. The token is in the
`mapi_csrf` cookie, or in the `X-CSRF-Token` response header of `GET /auth/session`. The
server also refuses requests with `Sec-Fetch-Site: cross-site`. It also refuses requests
whose `Origin` is not the web app or the API. API-token requests do not use cookie
authentication, and they do not need a CSRF token.

`GET /auth/session` tells if the browser is signed in. `POST /auth/logout` stops the
session. If a sign-in fails, the server sends `401` with a general message. The reason is in
the audit log (`auth.login_failed`).

### Google sign-in

1. In the Google Cloud console, make an OAuth client ID of type **Web application**. Set the
   authorized redirect URI to `https://<api host>/auth/callback`.
2. Configure the API:

    ```bash
    MAPI_PROFILE=multi-tenant
    MAPI_API_URL=https://api.mapi.example
    MAPI_OIDC_ISSUER=https://accounts.google.com
    MAPI_OIDC_CLIENT_ID=1234-abc.apps.googleusercontent.com
    MAPI_OIDC_CLIENT_SECRET=...            # from Secret Manager, never in images
    MAPI_OPERATOR_EMAILS=oncall@mapi.example
    ```

    Google ID tokens use the issuer `https://accounts.google.com`. Older tokens use
    `accounts.google.com`. The server accepts the two issuers.
    `MAPI_OIDC_EXTRA_PARAMS='{"hd": "acme.com"}'` selects a Workspace domain in the account
    chooser before the user selects. It is only a hint. The memberships control who can do
    which actions.

**Other providers** include Okta, Entra ID, Keycloak, Auth0, and an Identity Platform tenant
behind an OIDC-compliant provider. For these providers, set `MAPI_OIDC_ISSUER` to the
issuer. The `/.well-known/openid-configuration` of the issuer must list an authorization
endpoint, a token endpoint and a JWKS. The BFF does not accept the client-SDK tokens of
Identity Platform yet. These tokens have the issuer `securetoken.google.com/<project>`, and
you get them without an authorization-code endpoint.

### The development IdP (offline)

`MAPI_PROFILE=dev` mounts a small OpenID provider in the API at `/dev/idp`. Thus sign-in
works without a network:

- `/auth/login` redirects to `/dev/idp/authorize`. This page lists the accounts in
  `MAPI_DEVIDP_USERS`. The default accounts are `admin@dev.local`, `analyst@dev.local` and
  `viewer@dev.local`.
- At start-up, the server invites these accounts. The first account becomes the admin of the
  `default` tenant and a platform operator. The second account becomes `runner` of the
  `default` project, and the other accounts become `viewer`.
- The IdP signs its ID tokens with RS256 and a key that it makes at start-up. These tokens go
  through the same verification as the tokens of a real provider. The IdP enforces PKCE, and
  you can use each code only one time.

There are no passwords. Each person who can get access to a `dev` API can sign in as each
listed account. The IdP does not mount in other profiles.

The CLI can also use the IdP (refer to [The CLI](#the-cli)). Its issuer is the identifier
`https://dev-idp.mapi.localhost`. The API serves its discovery document:

```bash
mapi login --issuer https://dev-idp.mapi.localhost \
           --discovery-url http://127.0.0.1:8000/dev/idp
```

## The CLI

`mapi login` signs in with the identity provider directly. It uses the authorization code
flow with PKCE and a temporary listener on `127.0.0.1`. Then it sends the ID token as
`Authorization: Bearer`. The API accepts this bearer only when all of these conditions are
true:

- It is a JWT, not a `mapi_` API token.
- The configured issuer signed it (the server does a check against the JWKS of the issuer),
  and it is not expired.
- Its audience is one of `MAPI_OIDC_CLI_CLIENT_IDS` (default `mapi-cli`). The server does not
  accept the client id of the BFF.

The caller is then the same user as in a browser session. At the first use, the user gets a
pending invitation, in the same way as with `/auth/callback`. The CLI does not use a cookie.
Thus it does not need a CSRF token, and `GET /v1/me` reports `auth: session`. To accept only
API tokens, set `MAPI_OIDC_CLI_CLIENT_IDS=` (empty). Register the CLI with your provider as a
public "installed app" client. The client needs a loopback redirect URI, PKCE and the
`openid email` scopes.

## API tokens

Programs send `Authorization: Bearer mapi_<prefix>_<secret><checksum>`. The server mints a
token for its caller in the selected project (`POST /v1/tokens`). The server shows the token
one time only. The token acts as its owner, with the limits of its scopes, and it is bound to
that project. These rules apply:

- The server keeps only the HMAC-SHA256 of the token, with a pepper from
  `MAPI_SERVER_SECRET`. It compares in constant time. The checksum finds a typing error
  before the server looks in the database. No error message shows the value that the caller
  sent.
- Tokens that are expired, revoked or orphaned (the owner is disabled) get `401`. The audit
  log records a wrong secret for a known prefix, an expiry and a revocation as
  `token.auth_failed`. It records a maximum of five of these events for each token prefix in
  each minute. Each person who knows a prefix can cause them. An unknown prefix and
  a wrong secret get the same answer, `the API token is invalid`.
- A disabled account stays disabled. If the user signs in again, the server refuses it with a
  general `401` and records `auth.login_failed`. Only an invitation becomes active at its
  first sign-in.
- A token never has operator powers, also when its owner is an operator.
- A token cannot mint tokens, with all scopes (also `admin`). A child token would not have
  the expiry, budget, rate limit and revocation of its parent. Thus `POST /v1/tokens` needs a
  signed-in session (`mapi login` or the browser).
- Each token has a `rate_limit_per_minute` (`RATE_LIMITED`, `Retry-After`) and a budget of
  vCPU-seconds for each UTC day or month. These limits apply in addition to the quotas of the
  tenant.

Change the root secret only together with all tokens. When you change it, all the tokens that
you have now stop working.

## Operators

Users whose **verified** e-mail is in `MAPI_OPERATOR_EMAILS` are platform operators. The
server decides this for each request from the current configuration. Thus, when you remove
an address, the change applies immediately for browser sessions and `mapi login` ID tokens.
Nobody must sign in again. With a browser session, operators can use `/v1/admin/*`
(tenants, quota policies, kill switches, executor health, the platform audit log). They can
also read the metadata of all tenants: tenants, members, token lists, runs and usage.

No role, flag or token lets operators read datasets, specs, models or plots. The audit log
records each change and each refused attempt at an operator endpoint.

## Row-level security

`mapi-server migrate` makes three roles for the full cluster. It turns on `FORCE ROW LEVEL
SECURITY` for each table that holds tenant data. The policy is fail-closed, on
`current_setting('app.tenant_id', false)`:

| Role | Used by | Row-level security |
|---|---|---|
| `mapi_app` | Request handlers (one transaction for each request, pinned with `set_config('app.tenant_id', <tenant>, true)`) | Applies: the role sees and writes only the rows of that tenant. It owns no table |
| `mapi_gateway` | The look-ups that the API and the events service do before they know the tenant: authentication (which token is this, which tenants this user is a member of), the administration of tenants and members, operator endpoints, and the routing of a pushed event to the tenant of its run | Applies, with explicit policies: tenants, projects, memberships, API tokens (read, and touch `last_used_at`) and audit events (read, append). Also some metadata columns of `runs` and `cost_ledger` (state, estimate, attempt, executor, amounts) for global quotas, the spend cap and executor health. No access to datasets, uploads, specs, models, plots, attempts, events or logs |
| `mapi_system` | Only the orchestrator: dispatcher, reconciler, finalizer, housekeeper (plan §9) | `BYPASSRLS` if the login that migrates is a superuser, and also an explicit policy. Thus the role also works on managed Postgres and for logins that are members of it |

Each API request runs its platform sessions as `mapi_gateway`, independently of what the
handler does. In a cloud deployment, the API and events services cannot read the URL of the
login of the orchestrator. Thus, if a handler makes an authorization error before it knows
the tenant, row-level security still contains the error.

If a transaction does not pin a tenant, it fails closed. On a new connection, each query
gives an error. On a pooled connection, the setting is empty and agrees with no row. To prove
this, the test suite runs unscoped queries for two tenants over one pooled connection.

The audit log (`audit_events`) and the cost ledger (`cost_ledger`) are append-only. The
runtime roles cannot `UPDATE` or `DELETE` them. A trigger refuses all changes to a row that
is there now, for all users. Thus a bug or an injection cannot change the audit trail or the
billing history of a tenant. Only the table owner can purge them (tenant deletion,
retention). Before the purge, the owner must drop the `*_append_only` trigger.

With one `DATABASE_URL` (local development, the default Compose file), the two pools log in
as the user that migrates. They select their role with the start-up option `role=...`
(`RESET ROLE` goes back to that role). In production, give each pool its own login:

```sql
CREATE ROLE mapi_api LOGIN PASSWORD '...' IN ROLE mapi_app;
CREATE ROLE mapi_api_gateway LOGIN PASSWORD '...' IN ROLE mapi_gateway;
CREATE ROLE mapi_orchestrator LOGIN PASSWORD '...' IN ROLE mapi_system;
```

Then set `MAPI_DATABASE_APP_URL` (API and worker), `MAPI_DATABASE_GATEWAY_URL` (API) and
`MAPI_DATABASE_SYSTEM_URL` (only worker). After this, only `mapi-server migrate` and
procrastinate need `DATABASE_URL` (the owner).

## Pub/Sub push

In the cloud, run events go to `POST /internal/events` from a Pub/Sub push subscription with
OIDC authentication. To configure it, do these steps:

1. Configure the subscription with a dedicated service account and the audience
   `https://<api host>/internal/events`.
2. Set these variables:

    ```bash
    MAPI_PUBSUB_PUSH_AUDIENCE=https://api.mapi.example/internal/events
    MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT=pubsub-push@mapi-prod-control.iam.gserviceaccount.com
    # optional: only accept pushes from these subscriptions
    MAPI_PUBSUB_PUSH_SUBSCRIPTIONS=projects/mapi-prod-control/subscriptions/mapi-run-events-push
    ```

Each push must have an ID token that Google signed (RS256, with a check against
`MAPI_GOOGLE_JWKS_URL`). The token must agree with these conditions:

- The issuer is Google.
- The audience is the configured audience.
- `email` is the service account, with `email_verified: true`.
- The token is not expired.

A valid Google token for a different audience or account gets 403. If you set
`MAPI_PUBSUB_PUSH_SUBSCRIPTIONS`, the envelope must also come from one of those
subscriptions. Without these settings, the server needs `MAPI_INTERNAL_TOKEN` if it is set.
A multi-tenant deployment with neither of the two refuses all pushes. The audit log records
refused pushes as `event.rejected`.
