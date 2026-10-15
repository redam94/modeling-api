---
description: Deploy modeling-api to Google Cloud step by step - projects, bootstrap, Terraform, images by digest, migrations, smoke tests, GitHub Actions with approvals, rollbacks and day-2 operations.
---

# Deploy to Google Cloud <span class="mapi-badge mapi-badge--soon">v0.3</span>

This page takes an environment (`dev`, `staging` or `prod`) from empty Google Cloud projects to
a running, multi-tenant platform: the API and web app on Cloud Run, runs executing as Cloud Run
Jobs in a zero-egress sandbox project, and data in private buckets behind a VPC Service Controls
perimeter. Everything is in `infra/terraform` and driven by four scripts in `deploy/gcp`.

!!! warning "Status: written, validated and tested offline; not yet applied"
    The Terraform passes `terraform validate` for every environment and a `terraform test` suite
    that applies every module against mocked providers (see
    [Testing the infrastructure](#testing-the-infrastructure)). It has **not** been applied to a
    real organisation yet, and several cloud behaviours it relies on are marked **[verify M0]**:
    check them with the [M0 verification checklist](security-hardening.md#m0-verification-checklist)
    before the first production deploy. The server's cloud adapters (Cloud Run Jobs executor,
    Cloud Storage, KMS signing, Pub/Sub push verification) ship in the same release; the
    environment variables they read are listed [below](#environment-of-the-cloud-run-workloads).

## Topology

One environment spans five kinds of project, one per trust zone (plan §8.1):

```mermaid
flowchart TB
    users(("browsers ·<br/>clients")) -->|HTTPS| lb["global HTTPS load balancer<br/>app.example.com · plots.example.net"]
    subgraph control["control project (trusted)"]
        lb --> api["api<br/>Cloud Run"]
        lb --> web["web · plots<br/>Cloud Run"]
        events["events<br/>Cloud Run, internal"]
        orch["orchestrator<br/>worker pool"]
        sql[("Cloud SQL<br/>PostgreSQL 16")]
        kms["KMS<br/>bundle signing"]
        topic["Pub/Sub<br/>run events"]
        api --> sql
        orch --> sql
        events --> sql
        topic -->|"push + OIDC"| events
    end
    subgraph perimeter["VPC-SC perimeter"]
        subgraph exec["exec-paid · exec-public (hostile)"]
            jobs["Cloud Run Jobs<br/>3 bases × S/M/L<br/>deny-all egress"]
            sink["Logging sink<br/>mapi_event lines"]
        end
        subgraph data["data project"]
            buckets[("quarantine · bootstrap · runs<br/>bundles · data")]
        end
    end
    subgraph build["build project"]
        ar["Artifact Registry<br/>images · curated PyPI"]
    end
    orch -->|"jobs.run (env: run id, nonce)"| jobs
    jobs -->|"signed URLs only<br/>(restricted VIP)"| buckets
    jobs --> sink -->|Publish| topic
    jobs -. pull .-> ar
```

| Module | Project | Creates |
|---|---|---|
| `control` | control | Cloud Run `api`, `events`, `web`, `plots`; orchestrator worker pool; `migrate` job; Cloud SQL (private IP, managed connection pooling); Secret Manager secrets; Pub/Sub topic, push subscription and dead letters; one Logging sink per exec project; KMS signing key; Identity Platform; optional load balancer |
| `exec` | each exec project | VPC without a default route, deny-all egress firewall, restricted-VIP route and DNS zone, DNS response policy, the Job inventory, the execution service account, the dispatcher role |
| `data` | data | The five buckets of plan §8.6 with lifecycle rules, retention, CORS and bucket-level IAM |
| `build` | build | Docker repository (immutable tags), curated PyPI remote, Binary Authorization attestor (stub), GitHub Workload Identity Federation |
| `perimeter` | organisation | The VPC-SC perimeter around the exec and data projects |
| `stack` | all | Wires the modules together and enables the APIs |

`envs/<env>/main.tf` holds each environment's **policy** (database size and availability,
deletion protection, retention lock, perimeter mode) where code review sees it;
`terraform.tfvars` holds only names and ids.

| | dev | staging | prod |
|---|---|---|---|
| Database | Enterprise, 1 vCPU, zonal, no pooler | Enterprise Plus, zonal, pooler | Enterprise Plus, **regional (HA)**, pooler |
| VPC-SC perimeter | dry-run (optional) | enforced, required | enforced, required |
| Load balancer + domains | optional | optional | **required** |
| Deletion protection | off | on | on |
| Bundle retention | 1 day, unlocked | 7 days, unlocked | 30 days, **locked** |
| Signing key | software | software | **HSM** |
| Minimum instances (api) | 0 | 1 | 2 |

## Prerequisites

- **An organisation** with an Access Context Manager policy (`gcloud access-context-manager
  policies list --organization ORG_ID`). Without one, only `dev` can be deployed, without the
  perimeter (every plan then warns).
- **Projects**, created by your organisation's project vending: `control`, `data`, `build`,
  and one exec project per pool (`exec-paid`, plus `exec-public` once public tenants exist),
  all linked to billing. Keep the exec projects free of anything else.
- **Tools**: Terraform ≥ 1.11 (tested with 1.14), `gcloud`, Docker with `buildx`, `python3`
  and `curl`.
- **Permissions** for the first run: project owner (or equivalent) on every project, and
  `roles/accesscontextmanager.policyEditor` on the organisation policy. Later runs use the
  CI identities Terraform creates.
- **An OAuth client** (web application) for the app origin, for browser sign-in.

## Step by step

### 1. Bootstrap the projects

```bash
deploy/gcp/bootstrap.sh --env staging --region europe-west4 \
  --control-project acme-mapi-stg-control --data-project acme-mapi-stg-data \
  --build-project acme-mapi-stg-build \
  --exec-project paid=acme-mapi-stg-exec-paid --exec-project public=acme-mapi-stg-exec-public \
  --state-project acme-mapi-stg-build --dry-run       # read what it will do, then drop --dry-run
```

It enables each project's APIs (the same lists as `modules/stack/apis.tf`; a test keeps them
identical), creates the state bucket (uniform access, public access prevention, object
versioning) and writes `infra/terraform/envs/staging/backend.hcl`.

### 2. Describe the environment

```bash
cp infra/terraform/envs/staging/terraform.tfvars.example infra/terraform/envs/staging/terraform.tfvars
$EDITOR infra/terraform/envs/staging/terraform.tfvars
```

Fill in the project ids, region, bucket location (follow your tenants' data residency), the
exec pools, the access policy id, the domains, the OAuth client id and the GitHub repository.
The file holds no secrets and is not committed.

!!! warning "Data residency and the exec region"
    A run is placed only on an executor whose region satisfies its tenant's data residency.
    The default residency (the platform tenant's, and that of every tenant created without
    one) is therefore what the dispatch pool's region satisfies: `eu` for `europe-west4`.
    Override it with the control module's `default_residency`. A tenant whose residency the
    exec region cannot satisfy has its runs refused at submission (`403`, "no executor of
    this platform can run this tenant's work"), and the worker logs a warning for it at
    start-up. Change it with `PATCH /v1/admin/tenants/{id}` (`data_residency`).

### 3. Apply the foundation

```bash
deploy/gcp/deploy.sh --env staging --foundation
```

A foundation apply creates everything except the Cloud Run workloads and Jobs, which need
images: the registry, networks, DNS policy, database, secrets, buckets, perimeter and CI
identities. `deploy.sh` always shows the plan and asks you to type the environment name
before applying it.

Database passwords, the token pepper and the session key are generated during this apply by
ephemeral resources and written with write-only arguments: they are in Secret Manager and
nowhere else (not in the state, not in the plan file).

### 4. Add the OAuth client secret

```bash
export TF_VAR_oidc_client_secret='...'      # from the OAuth client
# in terraform.tfvars: oidc_client_secret_version = 1
```

The secret is an *ephemeral* Terraform variable: it is written to Secret Manager as a new
version and never stored. Raise `oidc_client_secret_version` whenever you rotate it. Add the
identity providers you want to Identity Platform (console, or
`google_identity_platform_*_idp_config` resources outside this module).

### 5. Build and push the images

```bash
deploy/gcp/build-push.sh --env staging
```

It builds `server`, `web`, `base-bayes`, `base-ml` and `base-forecast` for `linux/amd64`,
pushes them as `<version>-<git sha>` (tags are immutable in the registry, so re-running for the
same commit reuses what is there) and writes their **digests** to
`deploy/gcp/.out/staging/images.tfvars.json`. Terraform refuses any image that is not pinned by
digest.

### 6. Deploy the release

```bash
deploy/gcp/deploy.sh --env staging
```

1. `terraform plan` with `terraform.tfvars` and the images file, saved to a plan file.
2. Confirmation (type the environment name; `--yes` skips it in automation).
3. **Migrations first**: the `mapi-staging-migrate` job is pointed at the new server image and
   executed (`deploy/gcp/migrate.sh`). Migrations are expand/contract, so the release still
   serving keeps working on the new schema. On the very first release the job does not exist
   yet, so they run right after the apply instead.
4. `terraform apply` of exactly the saved plan.
5. The images file is recorded as a release in
   `gs://<state bucket>/mapi/staging/releases/<UTC timestamp>-<release id>.json`. The release id
   is a hash of the images, so a retried deploy of the same images is the same release.
6. Smoke test: API `/healthz` and `/readyz`, the web app (with a Content-Security-Policy), the
   plot sandbox, and a check that the events service refuses requests from the internet.

### 7. Point DNS at the load balancer

```bash
terraform -chdir=infra/terraform/envs/staging output lb_ip
```

Create `A` records for both domains. The Google-managed certificate provisions once DNS
resolves (often 15–60 minutes); until then run `deploy.sh --skip-smoke`. Without `domains`,
the services answer on their `run.app` URLs (`terraform output urls`).

### 8. Verify the controls

Work through the [security hardening](security-hardening.md) page, and the
[M0 verification checklist](security-hardening.md#m0-verification-checklist) before the first
production deploy.

!!! warning "Verified tenants cannot run until DNS enforcement is verified"
    `verified` (the default tier of new tenants, the private beta) and `public` tenants need
    an executor with egress **and** DNS enforced. A fresh stack sets neither
    `MAPI_CLOUDRUN_DNS_ENFORCED` nor any suite evidence, so their runs are refused at
    submission (`INFRA_ERROR`, 503) and only `internal` and `self_hosted_trusted` tenants run.
    To open the shared tiers:

    1. pass the DNS red-team gate of the M0 checklist from a Job of this environment;
    2. set `MAPI_CLOUDRUN_DNS_ENFORCED = "true"` for the orchestrator through
       `control.extra_env` (`{ orchestrator = { MAPI_CLOUDRUN_DNS_ENFORCED = "true" } }`) and
       deploy;
    3. run the executor conformance suite and the security suite against the deployed
       executor and record both with `PUT /v1/admin/executors/cloudrun-jobs/evidence`
       ([the shared-tier gate](cloud-run.md#the-shared-tier-gate)). Evidence expires after
       `MAPI_EXECUTOR_EVIDENCE_MAX_AGE_S`, so record the nightly runs.

## Continuous deployment

`.github/workflows/deploy.yml` deploys on manual dispatch only, without service-account keys:

| Job | GitHub environment | Google identity | Does |
|---|---|---|---|
| build | `<env>-build` | `mapi-<env>-ci-push` | `build-push.sh`; uploads the images file |
| plan | `<env>-plan` | `mapi-<env>-planner` (read-only) | `deploy.sh --plan-only`; the plan goes to the job summary |
| apply | `<env>` | `mapi-<env>-deployer` | `deploy.sh --apply-plan`: migrations, apply of that plan, release record, smoke test |

Set up, per environment:

1. Create the three GitHub environments and put **required reviewers** on `<env>` (the apply).
   Restrict `<env>` to protected branches.
2. Copy the values of `terraform output github` into environment variables:
   `GCP_WIF_PROVIDER`, `GCP_CI_PUSH_SA`, `GCP_PLANNER_SA`, `GCP_DEPLOYER_SA`; also
   `GCP_IMAGE_REPOSITORY` (`terraform output image_repository`), `TF_STATE_BUCKET` and
   `TERRAFORM_TFVARS` (the contents of the environment's `terraform.tfvars`).
3. Optionally add `OIDC_CLIENT_SECRET` as a secret of `<env>-plan` and `<env>` (an ephemeral
   variable must be given to both the plan and the apply).

The workload identity provider accepts tokens from your repository only, and each service
account trusts exactly one GitHub environment, so a job can never assume more than its
environment allows. The very first apply of an environment (which creates these identities)
is run by a person.

The perimeter is an organisation-level resource, which project-level grants cannot cover:
give the deployer `roles/accesscontextmanager.policyEditor` and the planner
`roles/accesscontextmanager.policyReader` on the access policy yourself (or use a scoped
access policy for the platform's projects). Once the perimeter is enforced, people who run
Terraform or read the exec projects' logs from outside it need an access level
(`vpc_sc.access_levels`); the deployer has its own ingress rule.

## Limitations

- **Bucket names are global.** The storage contract names buckets `mapi-{env}-{purpose}`
  (`mapi_spec.storage.bucket_name`), so `mapi-prod-data` must be free in all of Cloud
  Storage. If a name is taken, choose a more specific environment name (any name matching
  `^[a-z][a-z0-9-]{0,15}$`, e.g. `acme-prod`, as a new `envs/` directory).
- **KMS key rings are permanent.** Recreating a destroyed environment needs
  `terraform import` of its key rings.
- **`--traffic-only` rollbacks** cover the HTTP services only; the orchestrator and the Jobs
  roll back with a release redeploy.

## Day-2 operations

**Rollback.** Two ways:

```bash
deploy/gcp/rollback.sh --env prod --list            # recorded releases, newest first
deploy/gcp/rollback.sh --env prod --previous        # redeploy the release before the deployed one
deploy/gcp/rollback.sh --env prod --traffic-only    # seconds: services back to their previous revision
```

A release rollback goes through `deploy.sh` (plan, confirmation, apply, smoke test) with
`--skip-migrate`: schema changes only move forward. It is recorded as
`<UTC timestamp>-<id of the release it redeployed>-rollback.json`, and `--previous` replays the
records: retried deploys count once, and after rolling back from a bad release a second
`--previous` goes one release further back instead of returning to the bad one. `--traffic-only` shifts traffic with
`gcloud` without touching Terraform, for incidents; it leaves the orchestrator and the Jobs as
they are, and the next deploy moves traffic to the newest revision again.

**Migrations by themselves.** `deploy/gcp/migrate.sh --env prod --image <server@sha256:...>`.

**Rotate secrets.** Raise `control.database.password_version` in the environment's `main.tf`
(all three database roles and their connection URLs change in one apply), or
`control.secret_versions.session_secret` (signs everyone out) or `token_pepper` (**invalidates
every API token**). New Cloud Run revisions pick up the latest secret versions.

**Add an exec pool.** Add an entry to `exec_pools`, run `bootstrap.sh` for its project, then
`deploy.sh`. The perimeter, the Logging sink, the image-pull grant and the bootstrap-bucket grant
follow automatically.

**Tear down dev.** KMS keys cannot be deleted (only their versions can be scheduled for
destruction), so the two signing keys carry `prevent_destroy`. Release them from the state
first, then destroy; dev has no deletion protection and lets Terraform empty its buckets,
while staging and prod refuse:

```bash
cd infra/terraform/envs/dev
terraform state list | grep 'google_kms_crypto_key\.' | xargs -n1 terraform state rm
terraform destroy
```

## Environment of the Cloud Run workloads

This is the deployment contract between Terraform (`modules/control/locals.tf`) and
`mapi-server`. Every name is a `mapi-server` setting (see the
[configuration reference](configuration.md)); `tests/infra` fails if Terraform sets one the
server does not read. Plain values:

| Variable | Workloads | Value |
|---|---|---|
| `MAPI_ENV` | all | Environment name; buckets are `mapi-{env}-*` |
| `MAPI_PROFILE` | all | `multi-tenant` (the local-process executor refuses it) |
| `MAPI_OBJECT_STORE` | all | `gcs`: real Cloud Storage, URLs signed keylessly |
| `MAPI_GCP_PROJECT` | all | Project of the Cloud Storage client |
| `MAPI_URL_SIGNER` | all | Service account whose `signBlob` signs URLs (no key file exists) |
| `MAPI_URL_UPLOADER` | all | Service account whose `signBlob` signs create-only upload policies (run outputs); it may create objects in `runs` and `quarantine` but never delete them |
| `MAPI_SIGNER`, `MAPI_KMS_KEY_VERSION` | all | `kms` and `…/cryptoKeyVersions/<control.kms_key_version>` of the EC P-256 key that signs bundle manifests |
| `MAPI_DB_APP_ROLE`, `MAPI_DB_SYSTEM_ROLE`, `MAPI_DB_GATEWAY_ROLE` | all | `mapi_app` (row-level security), `mapi_orchestrator` (the platform role, the only RLS bypass) and `mapi_gateway` (the API's lookups before a tenant is known, RLS applies) |
| `MAPI_DEFAULT_RESIDENCY` | all | Residency of the platform tenant and of tenants created without one: the control module's `default_residency`, else what the dispatch pool's region satisfies (`eu` for `europe-west4`) |
| `MAPI_API_URL`, `MAPI_WEB_ORIGIN`, `MAPI_PLOTS_ORIGIN` | all, web | Public origins (the API shares the app origin behind the load balancer) |
| `MAPI_DATA_DIR` | all | `/tmp/mapi` (in-memory scratch) |
| `MAPI_ALLOWED_HOSTS` | api, events | Host names the service answers |
| `MAPI_OIDC_ISSUER`, `MAPI_OIDC_CLIENT_ID` | api | Browser sign-in |
| `MAPI_PUBSUB_PUSH_AUDIENCE`, `MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT`, `MAPI_PUBSUB_PUSH_SUBSCRIPTIONS` | events | What a valid Pub/Sub push token must say, and the one subscription allowed to push |
| `MAPI_EXECUTOR` | orchestrator | `cloudrun-jobs` |
| `MAPI_CLOUDRUN_PROJECT`, `MAPI_CLOUDRUN_REGION` | orchestrator | The exec pool it dispatches to (`control.dispatch_pool`, default the first non-public pool); its Jobs follow the default `mapi-{env}-{base}-{class}` names |
| `MAPI_BOOTSTRAP_URL` | Jobs | `gs://mapi-{env}-bootstrap/{nonce}` (set on each Job, read by the runner) |

From Secret Manager (references, resolved when a revision starts):

| Variable | Workloads | Secret |
|---|---|---|
| `MAPI_DATABASE_URL` | api, events | `db-url-app-direct`: `mapi_app`, direct (the LISTEN that feeds SSE) |
| `MAPI_DATABASE_APP_URL` | api, events, orchestrator | `db-url-app`: `mapi_app` through the pooler (transaction mode); tenant sessions |
| `MAPI_DATABASE_GATEWAY_URL` | api, events | `db-url-gateway`: `mapi_gateway` through the pooler; sign-in lookups, operator routes, routing pushed events (no RLS bypass) |
| `MAPI_DATABASE_SYSTEM_URL` | orchestrator | `db-url-orchestrator`: `mapi_orchestrator`, direct; the orchestrator's loops. Only the orchestrator's service account can read this secret (plan §9) |
| `MAPI_DATABASE_URL` | orchestrator | `db-url-orchestrator` (procrastinate needs LISTEN) |
| `MAPI_DATABASE_URL` | migrate | `db-url-migrate`: role `mapi_migrate` (schema owner), direct |
| `MAPI_SERVER_SECRET` | all | The root of the API-token pepper and the audit fingerprint key; rotating it (`control.secret_versions.server_secret`) invalidates every API token |
| `MAPI_METRICS_TOKEN` | api, events, orchestrator | The bearer Prometheus presents on `GET /metrics` (the multi-tenant profile refuses scrapes without it) |
| `MAPI_OIDC_CLIENT_SECRET` | api | Only once a client secret version exists |

One orchestrator serves one exec pool. A second pool (for example a separate project for
public tenants) needs its own orchestrator with `control.dispatch_pool` and
`MAPI_CLOUDRUN_PROJECT` pointing at it; placing tenants across pools by tier is part of M5.

Each workload can get extra plain variables through `control.extra_env` in the environment's
`main.tf`; secrets never go there.

## Testing the infrastructure

```bash
uv run pytest tests/infra -q                     # everything below
TF_PLUGIN_CACHE_DIR=~/.terraform.d/plugin-cache uv run pytest tests/infra -q   # reuse providers
```

- **Static invariants** (always run): deny-all egress with exactly two exceptions, no public
  buckets or principals, `max_retries = 0` and gen2 on every Job, the VPC-SC perimeter and DNS
  policy, write-only secrets, digest-pinned images, and agreement with the `mapi_spec`
  contracts (bucket names, resource class sizes, runner environment, the push endpoint).
- **`terraform fmt`, `terraform validate`** for every environment (with `-backend=false`).
- **`terraform test`** for every module: each is applied against mocked Google providers, so
  every expression and wiring is evaluated, and the security properties are asserted on the
  resulting values (no credentials, no cloud calls).
- **Deploy scripts**: `--help`, argument checks, the confirmation guard and full dry runs, with
  stub `terraform`/`gcloud`/`docker`/`curl` binaries that prove nothing executes; under
  `/bin/bash` 3.2 and the `bash` on `PATH`. `shellcheck` runs when installed.

The Terraform tests are skipped when `terraform` is not installed; the first run downloads the
Google providers (about 250 MB, into a temporary cache unless `TF_PLUGIN_CACHE_DIR` is set).
