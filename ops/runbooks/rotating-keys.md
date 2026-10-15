# Runbook: rotating the HMAC / signing keys

Two long-lived secrets protect run integrity (plan §8.6, §8.7, §9). Rotate them on a schedule
and immediately on any suspected compromise (see [sandbox escape](sandbox-escape.md)).

| Key | Protects | Where it lives | Blast radius of rotation |
|---|---|---|---|
| Bundle-signing key (P-256) | The signed `manifest.json` bound to GCS generations; checked when score URLs are minted | `MAPI_SIGNING_KEY_PATH` (default `<data_dir>/keys/bundle-signing-p256.pem`); created by `mapi-server migrate` | **Invalidates every outstanding signed URL** and requires re-verifying bundles before re-minting |
| Per-run HMAC key | Each run's events and output manifest (`seq` + HMAC per line) | Minted per attempt by the dispatcher (`run_attempts.hmac_key`), delivered only in the bootstrap object | None for existing runs (each run already has its own); rotate the *exec token* that reads bootstrap instead |
| Exec principal token / service-account | Read+delete on the bootstrap bucket only | GCS shim `_shim/keys/exec.json` (local) / exec service-account key (cloud); `MAPI_EXEC_TOKEN` | New runs only; in-flight runs already hold their bootstrap data |
| Internal events token | `POST /internal/events` bearer (local) | `MAPI_INTERNAL_TOKEN` | New event posts only |

## Golden rule

**Every process that signs or verifies must share the same bundle-signing key at the same
time.** The worker signs manifests; the API verifies them and mints URLs. Roll them together.
`mapi-server migrate` writes the key once to shared storage; `api` and `worker` load it
(`MAPI_SIGNING_KEY_CREATE=false` in compose so only `migrate` creates it).

## Bundle-signing key

Zero-downtime rotation needs the verifier to accept **both** the old and new key during the
overlap (keys carry a `key_id`; manifests record which signed them).

1. **Generate** the new key alongside the current one on the shared key volume (new file, new
   `key_id`). Do not remove the old file yet.
2. **Roll verifiers first** (API) so they trust old+new, then **roll signers** (worker) to
   sign with the new key. Now new bundles are signed new; old bundles still verify against the
   old key.
3. **Re-mint / re-verify.** Any score URL minted under the old key keeps working only until it
   expires; existing bundles remain valid because their stored manifest still verifies.
4. **Retire** the old key after the longest signed-URL TTL has passed and no live manifest is
   signed with it. Remove the old file; keep an offline copy for forensics for the retention
   period.

On compromise, skip the overlap: generate new, roll API+worker, and treat all outstanding
signed URLs as dead (they will fail verification at mint time). Re-verify bundles against GCS
CRC32C/MD5/generation before re-signing and re-minting.

## HMAC / exec tokens

- **Exec token / service account:** issue a new token/key; update `MAPI_EXEC_TOKEN` (or the
  exec service-account binding) and roll the orchestrator so new runs use it. Revoke the old
  token/key once no in-flight run still needs its bootstrap object (bootstrap objects are
  deleted by the supervisor at start and have a 1-day lifecycle, so the window is short).
- **Per-run HMAC:** nothing to rotate for existing runs — each attempt already has a unique
  key minted at lease time. Forged-event alerts are addressed by rotating the exec token and
  the internal events token, not the per-run keys.
- **Internal events token:** set a new `MAPI_INTERNAL_TOKEN`, roll the events service and the
  Pub/Sub push auth together.

## Verification after any rotation

- A fresh fit end to end succeeds: sign → verify → mint URL → download (range read).
- An old signed URL behaves as intended (still valid until TTL for a scheduled rotation; dead
  for a compromise rotation).
- No run fails with `INTEGRITY_FAILURE` solely because of the rotation (that would mean a
  signer/verifier mismatch — a process still on the old key).
- `mapi_integrity_failures_total` stays flat after the roll.

## Cadence

Scheduled rotation of the bundle-signing key quarterly; exec and internal tokens monthly;
always immediately on suspected compromise. Record each rotation (key_id, time, operator) in
the audit log.
