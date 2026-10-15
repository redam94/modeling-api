# Runbook: rotating the HMAC / signing keys

Two long-lived secrets protect run integrity (plan §8.6, §8.7, §9). Rotate them on a schedule
and immediately on any suspected compromise (see [sandbox escape](sandbox-escape.md)).

| Key | Protects | Where it lives | Blast radius of rotation |
|---|---|---|---|
| Bundle-signing key (P-256) | The signed `manifest.json` bound to GCS generations; checked whenever URLs for a bundle are minted (score, resume, artifact links) | Local: `MAPI_SIGNING_KEY_PATH` (default `<data_dir>/keys/bundle-signing-p256.pem`), created by `mapi-server migrate`. GCP: the Cloud KMS key `…/cryptoKeys/bundle-signing`, version `MAPI_KMS_KEY_VERSION` | Bundles signed by a key the verifiers no longer trust fail `INTEGRITY_FAILURE` at every score or resume; keep the old key **trusted** (below) unless it is compromised |
| Per-run HMAC key | Each run's events and output manifest (`seq` + HMAC per line) | Minted per attempt by the dispatcher (`run_attempts.hmac_key`), delivered only in the bootstrap object | None for existing runs (each run already has its own); rotate the *exec token* that reads bootstrap instead |
| Exec principal token / service-account | Read+delete on the bootstrap bucket only | GCS shim `_shim/keys/exec.json` (local) / exec service-account key (cloud); `MAPI_EXEC_TOKEN` | New runs only; in-flight runs already hold their bootstrap data |
| Internal events token | `POST /internal/events` bearer, **local and self-hosted only** (on GCP the Pub/Sub push carries a Google-signed OIDC token instead) | `MAPI_INTERNAL_TOKEN` | New event posts only |

## Golden rule

**Every process that signs or verifies must share the same bundle-signing key at the same
time.** The worker signs manifests; the API verifies them and mints URLs. Roll them together.
`mapi-server migrate` writes the key once to shared storage; `api` and `worker` load it
(`MAPI_SIGNING_KEY_CREATE=false` in compose so only `migrate` creates it).

## Bundle-signing key

Every manifest records the `key_id` that signed it, and a verifier accepts a signature only
from a key it **trusts**. A bundle stays usable (score, resume, artifact links) only while its
signing key is trusted. Drop a key from the trusted set and every model version it signed
fails `INTEGRITY_FAILURE` at submit/dispatch. That is what you want after a compromise, and
an outage otherwise.

### Local and self-hosted (`MAPI_SIGNER=local`)

The active key is the one PEM at `MAPI_SIGNING_KEY_PATH`. Retired keys go in
`MAPI_SIGNING_TRUSTED_KEY_PATHS` (comma-separated PEM files, public or private), and they
only verify.

1. **Keep the old key's public half** next to the active key:
   `openssl ec -in bundle-signing-p256.pem -pubout -out bundle-signing-<date>.pub.pem`.
2. **Generate the new key** in place of the old one: move the old PEM away (keep an offline
   copy for forensics), then run `mapi-server migrate`, which creates a key when none exists.
   Or write one with `openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256`
   (mode `0600`).
3. **Trust the old key** in every process that verifies or signs (API, worker, events):
   `MAPI_SIGNING_TRUSTED_KEY_PATHS=/var/lib/mapi/keys/bundle-signing-<date>.pub.pem`.
4. **Roll API, worker and events together.** New manifests are signed with the new
   `key_id`, and old ones still verify.
5. **Retire the old key** only once no model version you still serve was signed with it
   (re-fit or re-finalize those first). Then remove it from
   `MAPI_SIGNING_TRUSTED_KEY_PATHS` and roll again.

### GCP (`MAPI_SIGNER=kms`)

The active key is a version of the Cloud KMS key `bundle-signing`, and Terraform sets
`MAPI_KMS_KEY_VERSION` from `control.kms_key_version`. Retired versions stay trusted through
`control.kms_trusted_key_versions`, which becomes `MAPI_KMS_TRUSTED_KEY_VERSIONS` on api,
events and the orchestrator. Their public keys are loaded at start-up, so verification stays
local.

1. **Create a version:**
   `gcloud kms keys versions create --key bundle-signing --keyring mapi-<env> --location <region> --project <control project>`
   (note its number, say `2`).
2. **Point the deployment at it and keep the old one trusted.** In `envs/<env>/main.tf`, under
   `control`: `kms_key_version = 2` and `kms_trusted_key_versions = [1]`.
3. **`deploy/gcp/deploy.sh --env <env>`.** All server workloads roll to the new version
   together. New manifests carry the new version as `key_id`, and old ones still verify.
4. **Retire the old version** once no served model version was signed with it: drop it from
   `kms_trusted_key_versions`, deploy, then disable the version
   (`gcloud kms keys versions disable 1 …`). Destroy it only after the retention period.

### On compromise

Skip the overlap. Generate or create the new key, and do **not** list the compromised key as
trusted. Roll everything. Every bundle it signed now fails verification, so no new URL is
minted for them. Re-verify those bundles against the GCS CRC32C/MD5/generation you recorded,
quarantine what does not match, and re-finalize what does (that signs them again with the
new key). Signed URLs that were already minted stay valid until their short TTL runs out.

## HMAC / exec tokens

- **Exec token / service account:** issue a new token/key; update `MAPI_EXEC_TOKEN` (or the
  exec service-account binding) and roll the orchestrator so new runs use it. Revoke the old
  token/key once no in-flight run still needs its bootstrap object (bootstrap objects are
  deleted by the supervisor at start and have a 1-day lifecycle, so the window is short).
- **Per-run HMAC:** nothing to rotate for existing runs — each attempt already has a unique
  key minted at lease time. Forged-event alerts are addressed by rotating the exec token and
  the internal events token, not the per-run keys.
- **Internal events token (local and self-hosted only):** set a new `MAPI_INTERNAL_TOKEN` and
  roll the events service together with whatever posts events with it. On GCP there is no
  shared token to rotate. Pub/Sub pushes with a Google-signed OIDC token for the push
  service account, and the events service checks its audience and identity
  (`MAPI_PUBSUB_PUSH_AUDIENCE`, `MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT`). If you suspect the push
  path, check the subscription's push identity and the `roles/run.invoker` grant on the
  events service. Forged events are already rejected by the per-run HMAC.

## Verification after any rotation

- A fresh fit end to end succeeds: sign → verify → mint URL → download (range read).
- An old signed URL behaves as intended (still valid until TTL for a scheduled rotation; dead
  for a compromise rotation).
- An existing model scores after the rotation. A scheduled rotation that leaves it failing
  `INTEGRITY_FAILURE` means the old key is not trusted somewhere: check
  `MAPI_SIGNING_TRUSTED_KEY_PATHS` / `kms_trusted_key_versions` on every server workload.
- No run fails with `INTEGRITY_FAILURE` solely because of the rotation (that would mean a
  signer/verifier mismatch — a process still on the old key).
- `mapi_integrity_failures_total` stays flat after the roll.

## Cadence

Scheduled rotation of the bundle-signing key quarterly; exec and internal tokens monthly;
always immediately on suspected compromise. Record each rotation (key_id, time, operator) in
the audit log.
