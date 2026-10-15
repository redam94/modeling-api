---
description: Tenants, projects, roles, API tokens and browser sign-in in the multi-tenant platform, and how every request is authorized.
---

# Tenancy and authentication <span class="mapi-badge mapi-badge--soon">v0.3</span>

From v0.3 one deployment serves many organisations. Each is a **tenant**; inside it, work is
organised in **projects**; people and programs get **roles** in projects. Every request is
authenticated (who is calling), scoped (in which project) and authorized (may they do this),
by one pure function that the API, the web app and the tests share. The contracts live in
`mapi_spec.tenancy` and `mapi_spec.tokens`; the endpoints are listed in
[HTTP API: Multi-tenant API](../reference/http-api.md#multi-tenant-api).

!!! note "v0.2 is single-tenant"
    A local v0.2 install has one tenant and one project, both called `default`, and no
    sign-in. Every row and storage key already carries the tenant and project, so the
    multi-tenant release needs no data migration.

## Tenants and projects

```mermaid
flowchart TB
    T["Tenant <b>acme</b><br/>trust tier · status · data residency<br/><i>billing, isolation and quota boundary</i>"]
    T --> P1["Project <b>growth</b><br/>datasets · specs · runs · models"]
    T --> P2["Project <b>retail</b>"]
    U1(["Ana: runner in growth"]) -.-> P1
    U2(["IT: admin of acme (tenant-wide)"]) -.-> T
    K(["token mapi_7k2m…: bound to growth"]) -.-> P1
```

A **tenant** is the unit of billing, isolation and quotas. Postgres enforces it with row-level
security on `tenant_id` in every table, and every storage key starts with `t/{tenant}/`. A
tenant has:

| Field | Values | Effect |
|---|---|---|
| Trust tier | `internal`, `verified`, `public`, `self-hosted-trusted` | Where its runs may execute, and its default quotas |
| Status | `active`, `suspended`, `deleting` | `suspended` tenants can read but not change or run anything; `deleting` tenants are inaccessible |
| Data residency | A multi-region (`eu`, `us`, `asia`) or one region (`europe-west1`) | Runs are only placed on executors in a matching region |

The trust tier decides placement. This is enforced by the dispatcher, never by tenant
configuration:

| Tier | Who | Executor must provide |
|---|---|---|
| `internal` | The platform team | Process isolation or better |
| `self-hosted-trusted` | A single organisation running its own install | Process isolation or better |
| `verified` | Known customers (identity or card verified) | gVisor or a microVM, with egress and DNS enforced |
| `public` | Anyone who signed up | gVisor or a microVM, with egress and DNS enforced |

So a `public` tenant's runs never land on the `local-process` or plain `docker` executor;
they go to Cloud Run Jobs (a microVM per run) or `docker-gvisor`.

A **project** is a workspace inside a tenant and the unit of access control. Its id is a short
slug, unique in the tenant, because it appears in references such as
`dataset://growth/eu_media_daily`.

## Roles

There are four project roles. Each includes everything the one before it can do:

| Role | Can |
|---|---|
| `viewer` | Read everything in the project: datasets, specs, runs, logs, models, plots, usage. Validate specs. Mint tokens for themselves. |
| `editor` | Also create and change datasets, specs and saved plots. |
| `runner` | Also **spend compute**: submit, cancel, resume and retry runs; score models and endpoints; render plots. |
| `admin` | Also manage members, endpoints and their promotion, plugins, other people's tokens, and delete things. |

Running sits above editing on purpose: compute costs money, so a teammate can be trusted to
edit a spec without being able to start a 200-task sweep.

A **membership** grants one role, either on one project or tenant-wide. A tenant-wide grant
applies to the tenant itself (renaming it, its member list, its audit log and bill) and to
every project in it. In a project, a member's effective role is the higher of their tenant-wide
and project grants.

The complete permission matrix is `mapi_spec.tenancy.PERMISSIONS`. A test checks every role,
action and resource combination against an independently written table. In summary:

| Resource | `read` | `create` | `update` | `delete` | Other |
|---|---|---|---|---|---|
| tenant | viewer (tenant-wide) | operator | admin (tenant-wide) | operator | `manage` (tier, status, residency): operator |
| tenant members | viewer (tenant-wide) | admin (tenant-wide) | admin (tenant-wide) | admin (tenant-wide) | — |
| project | viewer | admin (tenant-wide) | admin | admin (tenant-wide) | — |
| project members | viewer | admin | admin | admin | — |
| token | viewer | viewer | — | viewer (own) | `manage` (others' tokens): admin |
| catalog | viewer | — | — | — | — |
| dataset, spec | viewer | editor | editor | admin | — |
| run | viewer | runner | — | — | `cancel`: runner |
| model | viewer | — | — | admin | `run` (score): runner |
| endpoint | viewer | admin | admin | admin | `run`: runner; `promote`: admin |
| plot | viewer | editor | — | — | `run` (render): runner |
| plugin | viewer | admin | — | admin | — |
| usage (project) | viewer | — | — | — | — |
| billing, audit (tenant) | admin (tenant-wide) | — | — | — | — |
| quotas, kill switches, executors | operator | — | operator | operator | — |

**Operators** run the platform. They manage tenants, quotas, kill switches and executors, and
can read tenant metadata, usage and audit logs. They cannot read tenant content (datasets,
specs, models, plots) through the API: no role or flag grants that.

```python
--8<-- "concepts_tenancy.py:can"
```

## Who can call

| Principal | How it authenticates | Identity |
|---|---|---|
| **User** | Signs in with OpenID Connect in the browser | `(issuer, subject)` from the identity provider, mapped to a platform id `usr_…` at first sign-in |
| **Token** | `Authorization: Bearer mapi_…` | The token id `tok_…`; it acts as its owner, a user or a service account `sa_…` (a *service token*) |
| **Internal service** | Platform identity (for example, Pub/Sub's signed OIDC token on `/internal/events`) | `svc_…`; only internal routes accept it |

## How a request is authorized

1. **Authenticate.** A session cookie, a bearer token or a service identity. Missing or invalid:
   `401 UNAUTHENTICATED`.
2. **Select the project.** From the path, the `X-Mapi-Project: <tenant>/<project>` header, or
   the token's binding (see below).
3. **Resolve roles.** The caller's tenant-wide and project roles, from memberships *now*. For
   a token, its owner's memberships: removing a person from a project immediately disarms
   their tokens there.
4. **Check the permission.** `AuthContext.allows(action, resource)` applies, in order: internal
   rules, operator rules, the tenant's status, the role at the rule's level and, for tokens,
   the scopes. A resource the caller cannot see answers `404`; a visible one they may not
   change answers `403 FORBIDDEN`.
5. **Check quotas** before anything that consumes compute or storage
   (see [Quotas and cost](quotas-and-cost.md)).

```python
--8<-- "concepts_tenancy.py:context"
```

### Selecting the project

Every resource of v0.2 (datasets, specs, runs, models, plots) now lives in a project. Send
`X-Mapi-Project: acme/growth`, or a bare `X-Mapi-Project: growth` when the tenant is
unambiguous. Tokens are bound to one project and may omit the header; naming another project
with a token is `403 FORBIDDEN`. A local single-tenant install falls back to `default/default`.

Because a token is bound to one project, it cannot administer its tenant: tenant members,
creating or deleting projects, the tenant audit log and tenant usage need a signed-in session
(`403 FORBIDDEN` for any token, whatever its scopes and its owner's role). A token may still
read its own tenant (`GET /v1/tenants/{tenant}`) and list its own project.

```python
--8<-- "concepts_tenancy.py:project"
```

## API tokens

Programs (the Python client, the CLI, Atlas, BI exports) use API tokens:

```text
mapi_7k2m9q4x_0123456789abcdefghjkmnpqrstvwxyz072gj7d
     prefix   secret (160 random bits)        checksum
```

- Lowercase Crockford base32 (no `i`, `l`, `o`, `u`), so tokens survive being read aloud or
  retyped.
- The **prefix** is public. Listings show the token as `mapi_7k2m9q4x_…`.
- The **secret** is shown once, in the response to `POST /v1/tokens`. The server stores only
  an HMAC-SHA256 of the whole token under a server-side pepper, and compares in constant
  time.
- The **checksum** (CRC-32 of the rest) lets clients and secret scanners reject typos and
  random strings without asking the server. Every single-character typo and every swap of two
  neighbouring characters is caught.

```python
--8<-- "concepts_tenancy.py:tokens"
```

### Scopes

A token's **scopes** narrow what it may do; they never add to its owner's role. An admin's
token with `models:read` can only read.

| Scope | Allows |
|---|---|
| `models:read` | Read datasets, specs, runs, models, endpoints, plots, usage and the catalog |
| `runs:create` | `models:read`, plus submit and cancel runs, score models and endpoints, render plots |
| `score:{endpoint}` | Read and score one endpoint alias, for example `score:geo-mmm-prod`; `score:*` covers every endpoint in the project |
| `datasets:write` | Create datasets and add versions |
| `plugins:publish` | Publish and yank plugins (from v0.5) |
| `admin` | Everything the owner's role allows, except minting tokens |

No scope lets a token mint tokens: a child token would escape its parent's expiry, budget,
rate limit and revocation. Mint tokens from a signed-in session (`mapi login` or the browser).

Each token also has an expiry (default 90 days, at most 365), and may have its own
requests-per-minute limit and a vCPU-second budget per day or month. These apply in addition
to the tenant's quotas.

## Signing in from the browser

The web app never holds an OAuth token. The API acts as a backend-for-frontend:

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant A as API (/auth)
    participant I as Identity provider
    B->>A: GET /auth/login?return_to=/runs
    A->>A: new state, nonce, PKCE verifier (kept server-side, 10 min)
    A-->>B: 302 to IdP (code_challenge S256, state, nonce) + __Host-mapi_login cookie
    B->>I: sign in
    I-->>B: 302 /auth/callback?code&state
    B->>A: GET /auth/callback (with the login cookie)
    A->>I: exchange code + PKCE verifier
    I-->>A: ID token
    A->>A: check issuer, audience, nonce, expiry; map (issuer, subject) to usr_…
    A-->>B: 302 /runs + __Host-mapi_session (HttpOnly, Secure, SameSite=Strict)
    B->>A: fetch /v1/... (cookie sent same-site only)
```

- The session cookie is `HttpOnly` (scripts cannot read it), `Secure`, `SameSite=Strict`
  (cross-site requests never carry it) and host-only (`__Host-` prefix: no `Domain`,
  `Path=/`).
- Sessions end after 2 hours idle or 12 hours in total. `POST /auth/logout` ends one at once.
- State-changing requests made with the session cookie also carry the session's CSRF token
  in `X-CSRF-Token` (from the readable `__Host-mapi_csrf` cookie or `GET /auth/session`);
  without it, or from another origin, they are refused.
- `return_to` only accepts a path on the same origin, so the login flow cannot be used as an
  open redirect.
- `GET /auth/session` tells the web app whether it is signed in (`authenticated: false` is a
  normal 200 answer).

## Audit

Security-relevant actions are written to an append-only audit log: sign-ins, token creation
and revocation, membership and project changes, quota and kill-switch changes, artifact link
minting, quota denials and rejected run events. Each entry records the actor's platform id,
the action, the resource, the outcome and a problem code. Client IP addresses and user agents
are stored only as keyed HMACs, so entries can be correlated without being reversible. Tenant
admins read their tenant's log at `GET /v1/tenants/{tenant_id}/audit`; operators read
everything at `GET /v1/admin/audit`.
