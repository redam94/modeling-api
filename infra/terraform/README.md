# infra/terraform

Google Cloud infrastructure of modeling-api (plan §8.1 topology). Deploy it with the scripts in
[`deploy/gcp`](../../deploy/gcp); the step-by-step guide is
[docs/operations/gcp-deployment.md](../../docs/operations/gcp-deployment.md) and the security
controls are explained in
[docs/operations/security-hardening.md](../../docs/operations/security-hardening.md).

```
modules/
  control/    Cloud Run api, events, web, plots; orchestrator worker pool; migrate job;
              Cloud SQL (Postgres 16, private IP, managed pooling); secrets; Pub/Sub push;
              Logging sinks; KMS signing key; Identity Platform; optional HTTPS LB
  exec/       one exec project: zero-egress VPC, restricted VIP, DNS response policy,
              3 bases x S/M/L Cloud Run Jobs (gen2, max-retries 0), execution identity
  data/       quarantine, bootstrap, runs, bundles, data buckets and their IAM
  build/      Artifact Registry, curated PyPI remote, Binary Authorization attestor, GitHub WIF
  perimeter/  VPC Service Controls perimeter around the exec and data projects
  stack/      wires one environment together and enables the APIs
envs/{dev,staging,prod}/   environment policy (main.tf) + your terraform.tfvars and backend.hcl
testing/mocks/             mock provider data for `terraform test`
```

Rules:

- Never `terraform apply` by hand in staging or prod; use `deploy/gcp/deploy.sh` (it migrates
  first and applies exactly the plan you reviewed) or the Deploy workflow.
- Nothing secret goes into `.tfvars` or the state: generated secrets are ephemeral and written
  with write-only arguments; the OAuth client secret is `TF_VAR_oidc_client_secret`.
- Images are referenced by digest only.

Checks (no credentials needed, nothing is applied):

```bash
terraform fmt -check -recursive infra/terraform
uv run pytest tests/infra -q    # static invariants, validate per env, terraform test per module
```
