---
description: What each Google Cloud security control of modeling-api does, where it is defined in Terraform and how to check it - plus the M0 verification checklist for the cloud behaviours the design relies on.
---

# Security hardening on Google Cloud <span class="mapi-badge">v0.3</span>

Tenants can submit specs (and, from M4, code) that run on the infrastructure of the
platform. Thus the cloud deployment assumes that **each execution is hostile**. This page
lists each control of the [Google Cloud deployment](gcp-deployment.md). For each control, it
gives the threat that it stops (plan §9) and its location in `infra/terraform`. It also tells
how to do a check of the control on a live environment. The model at application level (supervisor and worker,
signed events, verified outputs) is in [Security model](../concepts/security.md).

The last section is the **M0 verification checklist**. It lists the cloud behaviours that
the design depends on, which nobody has confirmed against a real project yet. Do this
checklist before production.

## Executions cannot reach the network

| Control | Threat | Where | Check |
|---|---|---|---|
| Direct VPC egress with `egress = ALL_TRAFFIC` on each Job | A path around the controls of the VPC | `modules/exec/jobs.tf` | `gcloud run jobs describe` shows the network interface and `ALL_TRAFFIC` |
| No default route. One route to `199.36.153.4/30` | A path to the internet | `modules/exec/network.tf` | `gcloud compute routes list --filter=network:mapi-<env>-exec-<pool>` lists only `restricted-vip` (and the subnet routes) |
| Firewall: deny all egress (IPv4 and IPv6) at priority 65534, with logs | Data exfiltration over IP | `modules/exec/network.tf` | Red-team probe: `curl https://example.com` from a Job fails. The denial shows in the firewall logs |
| Firewall: allow only TCP 443 to the restricted VIP and port 53 to `35.199.192.0/19` | The two exceptions that executions need | `modules/exec/network.tf` | `gcloud compute firewall-rules list` shows exactly these two allow rules |
| DNS response policy: `*.` answered locally (sinkhole). The bypass applies only to `googleapis.com`, `*.googleapis.com` and `metadata.google.internal` | **DNS exfiltration** (queries for `<secret>.attacker.example`) | `modules/exec/dns.tf` | Red-team gate below: the authoritative servers of a zone that you control never see a unique subdomain of that zone |
| Private zone `googleapis.com` → `restricted.googleapis.com` (199.36.153.4–7) | Access to Google APIs through endpoints without VPC-SC | `modules/exec/dns.tf` | From a Job, `storage.googleapis.com` resolves to 199.36.153.4–7 |
| VPC-SC perimeter around the exec and data projects. All APIs that move data are restricted. The only VPC-accessible service is Cloud Storage | Exfiltration through the VIP to a bucket, topic or dataset of an attacker (with all credentials) | `modules/perimeter` | Red-team probe: from a Job, a signed PUT URL for a bucket *outside* the perimeter fails, and a VPC-SC violation shows in the audit logs |

Cloud DNS response policies can answer with local data or bypass the policy. They cannot
return NXDOMAIN. Thus the "`*.` → NXDOMAIN" of the plan is a local sinkhole answer
(192.0.2.1 / 100::1). The two addresses are non-routable, and the firewall denies them. For
exfiltration, the important point is that the resolver answers the query locally and never
sends it on.

## Identities hold only what they use

| Identity | Can | Cannot |
|---|---|---|
| Execution SA (one for each exec project) | `storage.objects.get` + `delete` on the bootstrap bucket (custom role, no list) | All other actions: no project roles, no secrets, no access to bundles or data |
| Orchestrator | Run, list, examine and cancel executions of Jobs that are there (custom role). Write bootstrap objects. Verify, copy and sign outputs (KMS signer) | Make or change a Job (image, class, identity). Delete bundles |
| API | Read bucket metadata. Mint signed URLs (`signBlob` only on the URL signer). Read the public key of the signing key | Sign manifests. Run Jobs. Read secrets other than its own |
| Events | Database (through `db-url-app`) | All other actions |
| URL signer | The bucket permissions that signed URLs carry (custom reader/writer roles) | No other actions. There is no key file, and nobody can impersonate it (only `signBlob`) |
| Pub/Sub push SA | Invoke the events service | All other actions |
| CI: ci-push / planner / deployer | Push images / read-only plan / apply. Each is bound to one GitHub environment of one repository | Use a long-lived key (there are none) |

Where: `modules/control/iam.tf`, `modules/exec/iam.tf`, `modules/data/iam.tf`,
`modules/build/wif.tf`. To do a check, use
`gcloud projects get-iam-policy <project> --flatten=bindings --format='table(bindings.role,bindings.members)'`
and `gcloud storage buckets get-iam-policy gs://mapi-<env>-<bucket>`.

## Storage

| Control | Threat | Where |
|---|---|---|
| Public access prevention enforced and uniform bucket-level access on each bucket. No ACL resources | Public objects, or objects that an ACL makes available | `modules/data/buckets.tf` |
| Bootstrap: 1-day lifecycle, **soft delete off** | The RunSpec (signed URLs, HMAC key of the run) stays after its read-then-delete | `modules/data/buckets.tf` |
| Bundles: retention policy (locked in prod), no delete role for anybody, never `force_destroy` | Artifact swap or replay after verification | `modules/data/` |
| Quarantine and runs expire (7 and 30 days), soft delete off | Tenant data that stays after its purpose | `modules/data/buckets.tf` |
| Optional CMEK (`data.kms_key_name`) | Crypto-shredding for enterprise tenants | `modules/data/buckets.tf` |
| CORS only for the app origin, and only the methods that each bucket needs | Cross-origin abuse of signed URLs | `modules/data/buckets.tf` |

## Secrets and signing

- **No secret in Terraform state or plans.** Ephemeral resources make the database
  passwords, the root secret of the server and the metrics scrape token. The API-token pepper
  and the audit key come from the root secret. The OAuth client secret is an ephemeral
  variable. Terraform writes all of them with write-only arguments (`password_wo`,
  `secret_data_wo`). Cloud Run revisions refer to secrets by name, never by value.
- **Secrets are replicated in one region** (the control region), for data residency.
- **KMS signs the bundle manifests** (EC P-256 / SHA-256, HSM in prod). Only the orchestrator
  can sign. The API verifies with the public key before it mints a URL for a bundle (plan
  §8.6). Key versions are destroyed only after a 30-day schedule.
- **Database**: only private IP, TLS necessary, password policy enforced. There are four
  roles:
    - The migrations own the schema.
    - `mapi_app` and `mapi_gateway` (API, events) are not the table owner. Thus
      `FORCE ROW LEVEL SECURITY` applies to them. The gateway sees only identity tables and
      some metadata columns.
    - Only `mapi_orchestrator` gets `BYPASSRLS` from the migrations, and only the
      orchestrator can read its URL.

    The managed pooler runs in transaction mode. This is safe, because the tenant is set with
    `SET LOCAL` for each transaction.

## Event path and control-plane ingress

- The Logging sink of each exec project routes only the `jsonPayload.mapi_event:*` lines of
  the Jobs of this environment. It writes as its own identity, which can only publish to the
  events topic.
- The `_Default` sink of the exec project excludes user output (plan §8.7,
  `modules/exec/logging.tf`). The `log` and `progress` events of the runner Jobs still go to
  the events service through the routing sink. But the exec project does not ingest them (or
  bill them). State changes, warnings, errors, completions and the system logs of Cloud Run
  stay.
- The events service is **internal-only** and protected by IAM. Only the push identity can
  invoke it. Cloud Run does a check of the signature and audience of the push token before
  the request goes to the container. The server also does a check of the claims
  (`mapi_spec.auth.check_push_claims`) and of the HMAC of each line.
- After 10 attempts, events that cannot be delivered go to a dead-letter topic. They are not
  dropped.
- Behind the load balancer, the API accepts traffic only from the load balancer. TLS 1.2+
  (MODERN profile). The plot sandbox has its own host, as plan §10.3 needs. In prod, use a
  different registrable domain. Each backend has the Cloud Armor policy below.

## Cloud Armor

`modules/control/armor.tf` makes a Cloud Armor policy with the load balancer
(`var.domains`). It attaches the policy to the `api`, `web` and `plots` backends (`lb.tf`).
You can give your own policy with `security_policy`, or turn it off with
`cloud_armor.enabled = false`.

| Rule | Priority | Threat | Setting |
|---|---|---|---|
| Trusted ranges (CI, office) | 100 | Your own load tests cause a ban | `cloud_armor.trusted_ip_ranges` (a maximum of 10 CIDRs, never `*`) |
| OWASP CRS: `sqli`, `xss`, `lfi`, `rce`, `scannerdetection` (v3.3 stable) | 1000–1004 | Injection probes and scanners against the API and the SPA | `cloud_armor.waf_mode` (`preview` by default), `cloud_armor.waf_sensitivity` (paranoia level 1–4, default 1) |
| Rate ban for each IP on `/auth/signup` (and `/v1/signup`) | 2000 | Sign-up floods, Sybil farms | `rate_limits.signup_per_minute` (10) |
| Rate ban for each IP on `/auth/*` | 2001 | Credential stuffing on sign-in, callback and e-mail verification | `rate_limits.auth_per_minute` (120) |
| Rate ban for each IP on `/v1/embeds/*` | 2002 | Scraping of public embed links | `rate_limits.embed_per_minute` (600) |
| Rate ban for each IP on `/v1/endpoints/<alias>/score` | 2003 | Denial of wallet through scores | `rate_limits.score_per_minute` (600) |
| Default | last | | allow |

- **Preview, then enforce.** The OWASP rules start in `preview`. Cloud Armor logs the matches
  (`jsonPayload.previewSecurityPolicy` on the request logs of the load balancer), but it
  blocks nothing. Do these steps:
    1. Monitor one week of real traffic.
    2. Where a rule matches legitimate requests, add exclusions or decrease the sensitivity.
       Spec YAML and plot JSON are the most probable matches.
    3. Set `waf_mode = "enforce"` (in prod: `cloud_armor_waf_mode`). Enforced matches answer
       `403`.
- **Rate bans** are enforced from the start (`rate_limit_mode = "enforce"`). Above the
  threshold for each minute, a client gets `429`. At three times the threshold in five
  minutes, Cloud Armor bans the address for `ban_duration_sec` (600 s). The limits of the API
  apply behind these bans (sign-up for each address, domain and platform, and token and
  tenant request rates).
- **Adaptive Protection** (Layer 7 DDoS defense) is on. It learns the normal traffic of each
  backend and sends alerts with a suggested rule. It never blocks automatically.
- **Request bodies.** Cloud Armor parses JSON bodies (`json_parsing = STANDARD`). Thus the
  rules see field values, not raw text. Cloud Armor examines only the first
  `request_body_inspection_size` bytes of a body (8 KB by default, a maximum of 64 KB). It
  examines larger bodies only partially, and does not block them. Examples are spec
  documents and inline XrJSON score payloads of up to 5 MB. The API enforces its own body
  limits. Signed-URL uploads go directly to Cloud Storage and never go through the load
  balancer.

Do a check on a live environment:

```bash
gcloud compute backend-services describe mapi-prod-api --global --format='value(securityPolicy)'
gcloud compute security-policies describe mapi-prod-edge --format='yaml(rules,adaptiveProtectionConfig)'
# a burst against sign-up is banned (expect 429s after the threshold)
for i in $(seq 1 40); do curl -s -o /dev/null -w '%{http_code}\n' -X POST https://app.example.com/auth/signup; done | sort | uniq -c
```

**Logs and tenant deletion.** The load balancer logs each request (`log_config.sample_rate =
1`) into the `_Default` bucket of the control project. Terraform sets the retention of this
bucket explicitly (`log_retention_days`, 30 days, `modules/control/logging.tf`). Tenant
deletion does not purge logs. The logs hold ids, not content, and they expire with that
retention ([Deleting data](../guides/data-deletion.md#logs)).

## Supply chain

- You deploy images **only by digest**. Terraform refuses tags. The tags of the registry are
  immutable, and nothing is deleted automatically. The digests that live model versions use
  must stay (plan §12).
- Builds attach SBOM and provenance attestations (`build-push.sh`, `--no-attestations` to
  skip).
- A Binary Authorization attestor, note and signing key are available
  (`modules/build/binauthz.tf`). Enforcement on the Jobs (`binary_authorization = true`)
  starts in M5, when CI attests each base digest.
- CI authenticates with Workload Identity Federation. Nobody ever makes a service-account
  key.

## M0 verification checklist

This checklist has the plan items with the mark **[verify M0]**, and the assumptions that
this Terraform adds. Each item needs a temporary environment (`dev`, perimeter enforced) and
a probe Job built from a base image. For each item, record the date, the result and the
evidence (log query, screenshot).

### Network and DNS

- [ ] **The Direct VPC egress resolver obeys response policies.** From a Job, resolve
  `<random>.probe.<zone you control>` (A, AAAA and TXT). Pass: the answer is the sinkhole (or
  NODATA), and the authoritative query logs of the zone never see the name. This is the
  red-team gate of plan §9. If it fails, Cloud Run Jobs cannot host untrusted code (fallback:
  GKE Sandbox).
  **Until this item passes, `verified` and `public` tenants stay off.** After it passes, set
  `MAPI_CLOUDRUN_DNS_ENFORCED=true` on the orchestrator. Then record the security-suite and
  conformance evidence ([the shared-tier gate](cloud-run.md#the-shared-tier-gate)). New
  tenants are `verified` by default, so the private beta needs this item.
- [ ] **Cloud DNS accepts a wildcard rule for the root (`*.`).** Local data for A/AAAA does
  not send TXT or other record types upstream (the same probe, other types).
- [ ] **The bypass rules work**: `storage.googleapis.com` resolves to 199.36.153.4–7, and
  `metadata.google.internal` still serves the token of the execution.
- [ ] **Egress is really denied**: `curl` to a public IP and to a public name, a raw TCP
  connect to 8.8.8.8:53 and an ICMP ping all fail. The firewall logs show the denials.
- [ ] **VPC-SC applies to Cloud Run Jobs**: from a Job, a signed PUT URL for a bucket outside
  the perimeter fails, and a VPC-SC violation shows in the audit logs.

### Sandbox

- [ ] **The supervisor/worker uid split works on gen2**: the supervisor can start the worker
  under uid 10001 with `no_new_privs` and a seccomp filter. The worker cannot `ptrace` the
  supervisor or read `/proc/1/environ`. Cloud Run runs the `USER` of the image. Thus the
  runner images must start the supervisor as root (or with the capabilities to change users)
  for this item.
- [ ] **Execution overrides are env-only**: `jobs.run` with overrides cannot change the
  image, resources, service account or network. Try each one. All must be refused or
  impossible.
- [ ] **Ephemeral disk**: find out if Jobs have a disk in addition to the in-memory
  filesystem. Find out what occurs at the memory limit (exit 137 → `RESOURCE_EXHAUSTED`,
  class+1 offered).
- [ ] **In-memory filesystem limits**: a write of the scratch budget of the class (3/6/12 GiB
  for S/M/L) succeeds. A larger write stops the execution, and the host continues to operate
  correctly.
- [ ] **Bootstrap read-then-delete**: after the supervisor deletes the RunSpec, the object is
  gone (you cannot restore a soft-deleted copy). The nonce in the audit log points at
  nothing.

### Quotas and rates (plan §8.8)

- [ ] **Jobs for each region**: 1,000 (the inventory uses 9 for each exec project).
- [ ] **Running executions**: 1,000 for each project and region. An alert fires at 70 %.
- [ ] **`jobs.run` rate**: 180 each minute for each region. The token bucket of the
  dispatcher stays below it. A 429 causes an alarm and a back-off
  (`mapi_spec.quotas.backoff_seconds`).
- [ ] **Regional CPU and memory** for the expected fan-out (sweeps, SBC).
- [ ] **Cold start** of a base image of about 2.5 GB: measure the p50. The p95 of
  time-to-RUNNING must be below 90 s.

### Control plane

- [ ] **Logging → Pub/Sub path**: an event line goes to the events service with the labels
  of the execution (job, execution) in the push payload, in some seconds.
- [ ] **Pub/Sub push to an internal-only service** works (a subscription in the same
  project).
- [ ] **Managed connection pooling**: the pooler port (6432 by default here) and transaction
  mode work with the driver of the server. LISTEN uses the direct URL.
- [ ] **`BYPASSRLS`**: the migrate role can grant it to `mapi_orchestrator` on Cloud SQL.
- [ ] **`run.jobs.runWithOverrides` without `iam.serviceAccounts.actAs`** on the execution
  service account is sufficient for the orchestrator. If not, grant `actAs` only on that
  account.
- [ ] **Image indexes**: Cloud Run runs the images that `build-push.sh` pushes with
  attestations (an OCI index). If not, build with `--no-attestations`.

### Red-team probes (security suite, plan §13)

When the platform operates, run these probes each night. Keep them green for two weeks
before cut line B:

- Use of the metadata token for more than the bootstrap object.
- HTTP and DNS egress.
- Writes outside signed objects.
- Use of the URL of a different tenant.
- Reads of `/proc/1`.
- Overlay `.pth` injection.
- Fork, memory and disk bombs.
- Log flood (`SANDBOX_VIOLATION`).
- Forged events (bad HMAC, replayed sequence numbers).
- A tampered manifest (`INTEGRITY_FAILURE`).
- ReDoS schemas.
- Plot XSS and postMessage abuse.
