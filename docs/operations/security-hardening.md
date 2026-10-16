---
description: What each Google Cloud security control of modeling-api does, where it is defined in Terraform and how to check it - plus the M0 verification checklist for the cloud behaviours the design relies on.
---

# Security hardening on Google Cloud <span class="mapi-badge">v0.3</span>

Tenants can submit specs (and, from M4, code) that run on the platform's infrastructure, so the
cloud deployment assumes **every execution is hostile**. This page lists each control of the
[Google Cloud deployment](gcp-deployment.md), the threat it answers (plan §9), where it lives
in `infra/terraform`, and how to check it on a live environment. The application-level model
(supervisor and worker, signed events, verified outputs) is on
[Security model](../concepts/security.md).

The last section is the **M0 verification checklist**: the cloud behaviours the design depends
on that have not yet been confirmed against a real project. Work through it before production.

## Executions cannot reach the network

| Control | Threat | Where | Check |
|---|---|---|---|
| Direct VPC egress with `egress = ALL_TRAFFIC` on every Job | Bypassing the VPC's controls | `modules/exec/jobs.tf` | `gcloud run jobs describe` shows the network interface and `ALL_TRAFFIC` |
| No default route; one route to `199.36.153.4/30` | Any path to the internet | `modules/exec/network.tf` | `gcloud compute routes list --filter=network:mapi-<env>-exec-<pool>` lists only `restricted-vip` (and the subnet routes) |
| Firewall: deny all egress (IPv4 and IPv6) at priority 65534, logged | Data exfiltration over IP | `modules/exec/network.tf` | Red-team probe: `curl https://example.com` from a Job fails; the denial appears in the firewall logs |
| Firewall: allow only TCP 443 to the restricted VIP and port 53 to `35.199.192.0/19` | The two exceptions executions need | `modules/exec/network.tf` | `gcloud compute firewall-rules list` shows exactly these two allow rules |
| DNS response policy: `*.` answered locally (sinkhole), bypassed only for `googleapis.com`, `*.googleapis.com` and `metadata.google.internal` | **DNS exfiltration** (queries for `<secret>.attacker.example`) | `modules/exec/dns.tf` | Red-team gate below: a unique subdomain of a zone you control is never seen by its authoritative servers |
| Private zone `googleapis.com` → `restricted.googleapis.com` (199.36.153.4–7) | Reaching Google APIs through non-VPC-SC endpoints | `modules/exec/dns.tf` | From a Job, `storage.googleapis.com` resolves to 199.36.153.4–7 |
| VPC-SC perimeter around exec and data projects; every data-moving API restricted; VPC-accessible services = Cloud Storage only | Exfiltration through the VIP to an attacker-owned bucket, topic or dataset (with any credential) | `modules/perimeter` | Red-team probe: a signed PUT URL for a bucket *outside* the perimeter fails from a Job with a VPC-SC violation in the audit logs |

Cloud DNS response policies can answer with local data or bypass the policy; they cannot
return NXDOMAIN. The plan's "`*.` → NXDOMAIN" is therefore a local sinkhole answer
(192.0.2.1 / 100::1, both non-routable and denied by the firewall). What matters for
exfiltration is that the query is answered locally and never forwarded.

## Identities hold only what they use

| Identity | Can | Cannot |
|---|---|---|
| Execution SA (one per exec project) | `storage.objects.get` + `delete` on the bootstrap bucket (custom role, no list) | Anything else: no project roles, no secrets, no access to bundles or data |
| Orchestrator | Run, list, inspect and cancel executions of existing Jobs (custom role); write bootstrap objects; verify, copy and sign outputs (KMS signer) | Create or change a Job (image, class, identity); delete bundles |
| API | Read bucket metadata; mint signed URLs (`signBlob` on the URL signer only); read the signing key's public key | Sign manifests; run Jobs; read secrets other than its own |
| Events | Database (through `db-url-app`) | Everything else |
| URL signer | The bucket permissions signed URLs carry (custom reader/writer roles) | Nothing else; no key file exists, and nobody can impersonate it (only `signBlob`) |
| Pub/Sub push SA | Invoke the events service | Anything else |
| CI: ci-push / planner / deployer | Push images / read-only plan / apply; each bound to one GitHub environment of one repository | Use a long-lived key (none exist) |

Where: `modules/control/iam.tf`, `modules/exec/iam.tf`, `modules/data/iam.tf`,
`modules/build/wif.tf`. Check with
`gcloud projects get-iam-policy <project> --flatten=bindings --format='table(bindings.role,bindings.members)'`
and `gcloud storage buckets get-iam-policy gs://mapi-<env>-<bucket>`.

## Storage

| Control | Threat | Where |
|---|---|---|
| Public access prevention enforced and uniform bucket-level access on every bucket; no ACL resources anywhere | Public or ACL-granted objects | `modules/data/buckets.tf` |
| Bootstrap: 1-day lifecycle, **soft delete off** | The RunSpec (signed URLs, per-run HMAC key) surviving its read-then-delete | `modules/data/buckets.tf` |
| Bundles: retention policy (locked in prod), no delete role for anyone, never `force_destroy` | Artifact swap or replay after verification | `modules/data/` |
| Quarantine and runs expire (7 and 30 days), soft delete off | Tenant data outliving its purpose | `modules/data/buckets.tf` |
| Optional CMEK (`data.kms_key_name`) | Crypto-shredding for enterprise tenants | `modules/data/buckets.tf` |
| CORS only for the app origin and only the methods each bucket needs | Cross-origin abuse of signed URLs | `modules/data/buckets.tf` |

## Secrets and signing

- **No secret in Terraform state or plans.** Database passwords, the server's root secret (the
  API-token pepper and audit key derive from it) and the metrics scrape token come from
  ephemeral resources; the OAuth client secret is an ephemeral variable;
  all are written with write-only arguments (`password_wo`, `secret_data_wo`). Cloud Run
  revisions reference secrets by name, never by value.
- **Secrets replicated in one region** (the control region), for data residency.
- **Bundle manifests are signed by KMS** (EC P-256 / SHA-256, HSM in prod); only the
  orchestrator may sign, and the API verifies with the public key before minting any URL for a
  bundle (plan §8.6). Key versions are destroyed only after a 30-day schedule.
- **Database**: private IP only, TLS required, password policy enforced. Four roles: the
  migrations own the schema; `mapi_app` and `mapi_gateway` (API, events) are not the table
  owner, so `FORCE ROW LEVEL SECURITY` binds them (the gateway sees identity tables and a few
  metadata columns only); `mapi_orchestrator` alone gets `BYPASSRLS` from the migrations,
  and only the orchestrator can read its URL. The
  managed pooler runs in transaction mode, which is safe because the tenant is set with
  `SET LOCAL` per transaction.

## Event path and control-plane ingress

- The Logging sink of each exec project routes only `jsonPayload.mapi_event:*` lines of this
  environment's Jobs, and writes as its own identity, which may only publish to the events
  topic.
- User output is excluded at the exec project's `_Default` sink (plan §8.7,
  `modules/exec/logging.tf`): `log` and `progress` events of the runner Jobs still reach the
  events service through the routing sink, but are not ingested (and billed) in the exec
  project. State changes, warnings, errors, completions and Cloud Run's system logs stay.
- The events service is **internal-only** and IAM-protected: only the push identity may invoke
  it, and Cloud Run checks the push token's signature and audience before the request reaches
  the container. The server additionally checks the claims (`mapi_spec.auth.check_push_claims`)
  and the per-line HMAC.
- Undeliverable events are dead-lettered after 10 attempts instead of being dropped.
- Behind the load balancer the API accepts traffic only from it; TLS 1.2+ (MODERN profile);
  the plot sandbox has its own host (use a different registrable domain in prod), as plan
  §10.3 requires. Every backend carries the Cloud Armor policy below.

## Cloud Armor

`modules/control/armor.tf` creates a Cloud Armor policy with the load balancer
(`var.domains`) and attaches it to the `api`, `web` and `plots` backends (`lb.tf`). Supply your
own with `security_policy` instead, or turn it off with `cloud_armor.enabled = false`.

| Rule | Priority | Threat | Setting |
|---|---|---|---|
| Trusted ranges (CI, office) | 100 | Being banned by your own load tests | `cloud_armor.trusted_ip_ranges` (at most 10 CIDRs, never `*`) |
| OWASP CRS: `sqli`, `xss`, `lfi`, `rce`, `scannerdetection` (v3.3 stable) | 1000–1004 | Injection probes and scanners against the API and the SPA | `cloud_armor.waf_mode` (`preview` by default), `cloud_armor.waf_sensitivity` (paranoia level 1–4, default 1) |
| Per-IP rate ban on `/auth/signup` (and `/v1/signup`) | 2000 | Sign-up floods, Sybil farms | `rate_limits.signup_per_minute` (10) |
| Per-IP rate ban on `/auth/*` | 2001 | Credential stuffing on sign-in, callback and e-mail verification | `rate_limits.auth_per_minute` (120) |
| Per-IP rate ban on `/v1/embeds/*` | 2002 | Scraping of public embed links | `rate_limits.embed_per_minute` (600) |
| Per-IP rate ban on `/v1/endpoints/<alias>/score` | 2003 | Denial of wallet through scoring | `rate_limits.score_per_minute` (600) |
| Default | last | | allow |

- **Preview, then enforce.** The OWASP rules start in `preview`: matches are logged
  (`jsonPayload.previewSecurityPolicy` on the load balancer's request logs), nothing is
  blocked. Watch a week of real traffic, add exclusions or lower the sensitivity where a rule
  matches legitimate requests (spec YAML and plot JSON are the likely ones), then set
  `waf_mode = "enforce"` (in prod: `cloud_armor_waf_mode`). Enforced matches answer `403`.
- **Rate bans** are enforced from the start (`rate_limit_mode = "enforce"`): over the
  per-minute threshold a client gets `429`; three times the threshold within five minutes bans
  the address for `ban_duration_sec` (600 s). The API's own limits (sign-up per address,
  domain and platform; token and tenant request rates) apply behind them.
- **Adaptive Protection** (Layer 7 DDoS defense) is on: it learns each backend's normal
  traffic and alerts with a suggested rule; it never blocks by itself.
- **Request bodies.** JSON bodies are parsed (`json_parsing = STANDARD`), so the rules see
  field values instead of raw text. Cloud Armor inspects only the first
  `request_body_inspection_size` bytes of a body (8 KB by default, at most 64 KB): larger
  bodies (spec documents, inline XrJSON score payloads up to 5 MB) are inspected partially,
  not blocked. The API enforces its own body limits; signed-URL uploads go to Cloud Storage
  directly and never pass the load balancer.

Check on a live environment:

```bash
gcloud compute backend-services describe mapi-prod-api --global --format='value(securityPolicy)'
gcloud compute security-policies describe mapi-prod-edge --format='yaml(rules,adaptiveProtectionConfig)'
# a burst against sign-up is banned (expect 429s after the threshold)
for i in $(seq 1 40); do curl -s -o /dev/null -w '%{http_code}\n' -X POST https://app.example.com/auth/signup; done | sort | uniq -c
```

**Logs and tenant deletion.** The load balancer logs every request (`log_config.sample_rate =
1`) into the control project's `_Default` bucket, whose retention Terraform sets explicitly
(`log_retention_days`, 30 days, `modules/control/logging.tf`). Tenant deletion does not purge
logs; they hold ids, not content, and expire with that retention
([Deleting data](../guides/data-deletion.md#logs)).

## Supply chain

- Images are deployed by **digest only**; Terraform rejects tags. The registry's tags are
  immutable and nothing is garbage-collected automatically (digests referenced by live model
  versions must survive, plan §12).
- Builds attach SBOM and provenance attestations (`build-push.sh`, `--no-attestations` to skip).
- A Binary Authorization attestor, note and signing key exist (`modules/build/binauthz.tf`);
  enforcement on the Jobs (`binary_authorization = true`) is switched on in M5, once CI
  attests every base digest.
- CI authenticates with Workload Identity Federation; no service-account key is ever created.

## M0 verification checklist

Plan items marked **[verify M0]** and the assumptions this Terraform adds. Each needs a
throw-away environment (`dev`, perimeter enforced) and a probe Job built from a base image.
Record the date, the result and the evidence (log query, screenshot) for each.

### Network and DNS

- [ ] **The Direct VPC egress resolver honours response policies.** From a Job, resolve
  `<random>.probe.<zone you control>` (A, AAAA and TXT). Pass: the answer is the sinkhole (or
  NODATA) and the zone's authoritative query logs never see the name. Plan §9's red-team gate;
  if it fails, Cloud Run Jobs cannot host untrusted code (fallback: GKE Sandbox).
  **Until this passes, `verified` and `public` tenants stay off:** only then set
  `MAPI_CLOUDRUN_DNS_ENFORCED=true` on the orchestrator and record the security-suite and
  conformance evidence ([the shared-tier gate](cloud-run.md#the-shared-tier-gate)). New tenants
  default to `verified`, so the private beta needs this item.
- [ ] **A wildcard rule for the root (`*.`) is accepted** by Cloud DNS, and local data for
  A/AAAA does not leak TXT or other record types upstream (same probe, other types).
- [ ] **The bypass rules work**: `storage.googleapis.com` resolves to 199.36.153.4–7 and
  `metadata.google.internal` still serves the execution's token.
- [ ] **Egress is really denied**: `curl` to a public IP and to a public name, a raw TCP connect
  to 8.8.8.8:53 and an ICMP ping all fail; the firewall logs show the denials.
- [ ] **VPC-SC applies to Cloud Run Jobs**: a signed PUT URL for a bucket outside the
  perimeter fails from a Job, and a VPC-SC violation appears in the audit logs.

### Sandbox

- [ ] **The supervisor/worker uid split works on gen2**: the supervisor can start the worker
  under uid 10001 with `no_new_privs` and a seccomp filter; the worker cannot `ptrace` the
  supervisor or read `/proc/1/environ`. Note that Cloud Run runs the image's `USER`; the runner
  images must start the supervisor as root (or with the capabilities to switch users) for this
  to work.
- [ ] **Execution overrides are env-only**: `jobs.run` with overrides cannot change the image,
  resources, service account or network (try each; all must be refused or impossible).
- [ ] **Ephemeral disk**: whether Jobs offer any disk beyond the in-memory filesystem, and what
  happens at the memory limit (exit 137 → `RESOURCE_EXHAUSTED`, class+1 offered).
- [ ] **In-memory filesystem limits**: writing the class's scratch budget (3/6/12 GiB for
  S/M/L) succeeds; going beyond it gets the execution killed rather than the host degraded.
- [ ] **Bootstrap read-then-delete**: after the supervisor deletes the RunSpec, the object is
  gone (no soft-deleted copy can be restored) and the nonce in the audit log points at nothing.

### Quotas and rates (plan §8.8)

- [ ] **Jobs per region**: 1,000 (the inventory uses 9 per exec project).
- [ ] **Running executions**: 1,000 per project and region; an alert fires at 70 %.
- [ ] **`jobs.run` rate**: 180 per minute per region; the dispatcher's token bucket stays below
  it, and a 429 raises an alarm and backs off (`mapi_spec.quotas.backoff_seconds`).
- [ ] **Regional CPU and memory** for the expected fan-out (sweeps, SBC).
- [ ] **Cold start** of a ~2.5 GB base image: p50 measured; time-to-RUNNING p95 under 90 s.

### Control plane

- [ ] **Logging → Pub/Sub path**: an event line reaches the events service with the
  execution's labels (job, execution) in the push payload, within seconds.
- [ ] **Pub/Sub push to an internal-only service** works (same-project subscription).
- [ ] **Managed connection pooling**: the pooler port (6432 by default here) and transaction
  mode work with the server's driver; LISTEN goes over the direct URL.
- [ ] **`BYPASSRLS`** can be granted to `mapi_orchestrator` by the migrate role on Cloud SQL.
- [ ] **`run.jobs.runWithOverrides` without `iam.serviceAccounts.actAs`** on the execution
  service account is enough for the orchestrator (otherwise grant `actAs` on that account only).
- [ ] **Image indexes**: Cloud Run runs the images `build-push.sh` pushes with attestations
  (an OCI index); otherwise build with `--no-attestations`.

### Red-team probes (security suite, plan §13)

Run nightly once the platform is up, and keep them green for two weeks before cut line B:
metadata-token use beyond the bootstrap object, HTTP and DNS egress, writes outside signed
objects, reuse of another tenant's URL, reading `/proc/1`, overlay `.pth` injection, fork, memory
and disk bombs, log flood (`SANDBOX_VIOLATION`), forged events (bad HMAC, replayed sequence
numbers), a tampered manifest (`INTEGRITY_FAILURE`), ReDoS schemas, plot XSS and postMessage
abuse.
