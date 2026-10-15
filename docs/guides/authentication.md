---
description: Signing in to a multi-tenant modeling-api - the browser, the development identity provider, Google and Identity Platform, the CLI, and API tokens for programs.
---

# Authentication <span class="mapi-badge">v0.3</span>

A v0.3 deployment answers `401 UNAUTHENTICATED` to anyone it does not know. There are three
ways in, one per kind of caller:

| Caller | Signs in with | Sends | Reference |
|---|---|---|---|
| A person in a **browser** | The organisation's identity provider (OpenID Connect) | An `HttpOnly` session cookie; the app never sees a token | [Browser sign-in](#browser-sign-in) |
| A person in a **terminal** | `mapi login`: the same provider, from the command line | The provider's ID token as `Authorization: Bearer` | [The command line](#the-command-line) |
| A **program** | Nothing: a person mints it an API token | `Authorization: Bearer mapi_…` | [API tokens](#api-tokens) |

Whichever way, the caller is a user (`usr_…`) with roles in projects, and every request is
checked against those roles at that moment ([Administration](administration.md#roles)). This
page is for the people who sign in and set up sign-in; the protocol details (cookies, CSRF,
PKCE, JWKS, the database roles) are in
[Authentication and tenant isolation](../operations/authentication.md).

## Browser sign-in

![The web app's sign-in page](../assets/screenshots/signin.png){ .mapi-shot }

Opening the web app without a session shows **Sign in to your workspace**. **Continue to sign
in** goes to the API's `/auth/login`, which sends the browser to the identity provider; after
the provider's own sign-in the browser comes back to the page it started on.

The API does all the OpenID Connect work (authorization code with PKCE, a nonce, ID-token
verification against the provider's keys) and keeps the tokens to itself: the browser holds
only two cookies, a `__Host-` session cookie that scripts cannot read and a CSRF token. A
session ends after 2 hours idle or 12 hours in all, or at **Sign out**.

![The project switcher: every tenant and project you belong to, with your role in each](../assets/screenshots/project-switcher.png){ .mapi-shot }

Once signed in, the **project switcher** at the top left lists every tenant and project you
have a role in, with that role and the tenant's trust tier. Everything the app shows and does
happens in the selected project (it is sent as `X-Mapi-Project` on every request). The avatar
menu shows who you are; a shield marks a platform operator.

The **first sign-in** of a person creates their user from the provider's `(issuer, subject)`
and, if the provider says the e-mail is **verified**, claims every invitation sent to that
address (a tenant admin's **Add member**, an operator's `--admin-email`). Someone with no
membership yet sees *You are not in a project yet*.

## The development IdP

`make dev` (and Docker Compose) run the servers in the `dev` profile, which mounts a small
OpenID provider inside the API at `/dev/idp`. Sign-in then works offline, through exactly the
same code path as a real provider: RS256-signed ID tokens, PKCE, single-use codes.

![The development IdP's account chooser](../assets/screenshots/dev-idp.png){ .mapi-shot }

There are no passwords: whoever can reach a `dev` API can sign in as any listed account. The
accounts come from `MAPI_DEVIDP_USERS`:

| Account | Gets at start-up |
|---|---|
| `admin@dev.local` | Admin of the `default` tenant and a **platform operator** |
| `analyst@dev.local` | `runner` in `default/default` |
| `viewer@dev.local` (and any further addresses) | `viewer` in `default/default` |

Scripts sign in without a browser with `mapi_client.oidc.dev_idp_login(api_url, email)`,
which runs the same flow and returns an ID token valid for an hour. `make seed`, the
end-to-end tests and the walkthroughs on this page use it; a server in any other profile
answers 404, so it cannot be pointed at production. The walkthrough below is
`docs/snippets/auth_tour.sh`, run by the test suite against `make dev`:

```bash
--8<-- "auth_tour.sh:signin"
```

```text
anonymous: 401
{"email":"analyst@dev.local","auth":"session","operator":false,"projects":["acme/growth admin","acme/retail admin","default/default runner"]}
```

`MAPI_STACK_PROFILE=single-tenant make dev` turns sign-in off altogether: the v0.2 behaviour,
where every request acts as the local administrator of `default`.

## Setting up a real identity provider

The API signs people in with any **OpenID Connect provider that offers the authorization-code
flow**: it needs an issuer whose `/.well-known/openid-configuration` lists an authorization
endpoint, a token endpoint and a JWKS.

### Google accounts

The default of the [Google Cloud deployment](../operations/gcp-deployment.md) and the
simplest choice for an organisation on Google Workspace:

1. In the Google Cloud console (**APIs & Services → Credentials**) create an **OAuth client
   ID** of type *Web application*, with the authorized redirect URI
   `https://<your app domain>/auth/callback`.
2. Give Terraform its id (`oidc_client_id` in `terraform.tfvars`) and its secret as the
   ephemeral variable `TF_VAR_oidc_client_secret` with `oidc_client_secret_version = 1`
   ([step 4 of the deployment](../operations/gcp-deployment.md#4-add-the-oauth-client-secret)).
   Outside Terraform, set `MAPI_OIDC_ISSUER=https://accounts.google.com`,
   `MAPI_OIDC_CLIENT_ID` and `MAPI_OIDC_CLIENT_SECRET` on the API.
3. List the operators' e-mails in `MAPI_OPERATOR_EMAILS`, and create each organisation's
   tenant with an `--admin-email` ([Administration](administration.md#tenants)).

`MAPI_OIDC_EXTRA_PARAMS='{"hd": "acme.com"}'` pre-selects a Workspace domain in Google's
account chooser. It is only a hint: who may do what is decided by memberships, never by the
e-mail domain.

### Identity Platform

Terraform also enables **Identity Platform** in the control project
(`infra/terraform/modules/control/identity.tf`): it registers the app's domain as an
authorized domain, turns off anonymous and (by default) e-mail/password sign-in, and is where
SAML and OIDC providers for enterprise single sign-on are added, in the console or with
`google_identity_platform_*_idp_config` resources kept outside the module (their secrets do not
belong in its state).

!!! warning "Known gap in v0.3"
    The API's sign-in cannot use Identity Platform's own tokens yet. Those (issuer
    `https://securetoken.google.com/<project>`) are obtained with Identity Platform's client
    SDK, and that issuer offers no authorization-code endpoint for the API to redirect to. Until
    that adapter exists, point `MAPI_OIDC_ISSUER` at Google (above) or directly at the
    organisation's own OpenID provider (below). The Identity Platform configuration that
    Terraform creates is harmless meanwhile.

### Other providers

Okta, Microsoft Entra ID, Keycloak, Auth0 and any other standard provider work the same way:
register a web client with the redirect URI `https://<your app domain>/auth/callback`, then set
`MAPI_OIDC_ISSUER` to its issuer (and `MAPI_OIDC_DISCOVERY_URL` if its discovery document is not
under the issuer), `MAPI_OIDC_CLIENT_ID` and `MAPI_OIDC_CLIENT_SECRET`. ID tokens must be signed
with an asymmetric algorithm (`MAPI_OIDC_ALGORITHMS`, default `RS256,ES256`).

## The command line

`mapi login` signs in with the same provider, from the terminal:

```bash
--8<-- "cli_tenancy.sh:login"
```

- With a browser, it opens the provider's page and listens once on `127.0.0.1` for the
  redirect (authorization code with PKCE).
- `--device` prints a short code to enter on another device (providers that support the device
  flow).
- `--with-token` stores an API token read from standard input instead (CI).

It keeps the sign-in in `credentials.json` next to the config file (mode `0600`) and sends the
provider's ID token as `Authorization: Bearer`. The API accepts such a token only when its
audience is one of `MAPI_OIDC_CLI_CLIENT_IDS` (default `mapi-cli`): register the CLI with your
provider as a public *installed app* client with a loopback redirect, PKCE and the `openid
email` scopes. Google's installed-app clients need a client secret, which the CLI reads from
`MAPI_OIDC_CLIENT_SECRET` and never prints.

Against the development IdP:

```bash
mapi login --issuer https://dev-idp.mapi.localhost \
           --discovery-url http://127.0.0.1:8000/dev/idp
```

Then choose where commands act with `mapi use <tenant>/<project>` (or `--project`, or
`MAPI_PROJECT`); `mapi whoami` shows who you are, how you are signed in and your projects. See
[The command line](cli.md#sign-in-and-choose-a-project).

## API tokens

Programs never sign in. A person mints them a token in one project, with only the scopes it
needs; the token then acts as that person, narrowed by its scopes, and only in that project.
In the web app it is **Tokens → New token** ([Administration](administration.md#api-tokens));
from the CLI, `mapi tokens create`; over HTTP:

```bash
--8<-- "auth_tour.sh:mint"
```

```text
{"id":"tok_01m50mxfqh4k95ed2dv2jc005v","display":"mapi_hhb9gjzf_…","scopes":["models:read"],"project":"default/default","expires_at":"2026-10-16T20:44:02.161164Z"}
```

The secret (`mapi_<prefix>_<secret><checksum>`) is in the response **once**. The server keeps
only an HMAC of it; listings show the prefix (`mapi_hhb9gjzf_…`). The built-in checksum lets
clients and secret scanners reject a mistyped or made-up token without asking the server.

A program sends it as a bearer. The token is bound to its project, so it needs no
`X-Mapi-Project` header:

```bash
--8<-- "auth_tour.sh:use"
```

The Python client takes it as `Client(token=…)` or `MAPI_TOKEN`, the CLI as `--token`,
`MAPI_TOKEN` or `mapi login --with-token` ([Python client](python-client.md#projects-tokens-and-usage)).
Both refuse to send a token over plain HTTP to anything but a loopback address.

### What a token cannot do

Scopes only narrow. A `models:read` token cannot start a run even when its owner is a runner;
it cannot act in another project; and no token carries operator powers:

```bash
--8<-- "auth_tour.sh:narrow"
```

```text
{"status":403,"code":"FORBIDDEN"}
{"status":403,"code":"FORBIDDEN"}
{"status":403,"code":"FORBIDDEN"}
```

Tokens expire (default 90 days, at most 365). Revoking one, or removing its owner from the
project, stops it at the next request:

```bash
--8<-- "auth_tour.sh:revoke"
```

```text
revoke: 204
{"status":401,"code":"UNAUTHENTICATED"}
```

Failed uses of a known token (a wrong secret, an expired or revoked token) are written to the
audit log as `token.auth_failed`; no error ever echoes the token back.

## Who can see what

| | Browser session | `mapi login` | API token |
|---|---|---|---|
| Identity | The person | The person | The person who minted it |
| Projects | Every project with a role (choose one per request) | Same | Only the one it was minted in |
| Permissions | The role | The role | The role **and** the scopes |
| Operator powers | Yes, for operators | Yes, for operators | Never |
| Needs CSRF token | Yes, for changes | No | No |
| Ends | 2 h idle, 12 h total, sign-out | `mapi logout`; the CLI renews the ID token with a refresh token when the provider issued one, otherwise `mapi login` again | Expiry or revocation |

## Where next

- [Administration](administration.md): tenants, members, roles, tokens, quotas and audit.
- [Authentication and tenant isolation](../operations/authentication.md): the operator's view,
  with cookies, CSRF, row-level security and Pub/Sub push authentication.
- [Tenancy and authentication](../concepts/tenancy-and-auth.md): the concepts and the
  request-authorization pipeline.
