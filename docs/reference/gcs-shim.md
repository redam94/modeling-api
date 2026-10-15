# GCS shim (local Cloud Storage)

`mapi-gcs-shim` is a local emulator of the parts of Google Cloud Storage the platform relies on.
The local stack, the test suites and the runner use it in place of real GCS. It implements:

- object **generations** and **metagenerations**, with generation preconditions;
- **server-computed CRC32C and MD5**, in the GCS wire forms (base64);
- **V4 signed URLs**: GET with HTTP `Range`, and PUT;
- **V4 POST policies** that limit uploads to a key prefix and a `content-length-range`;
- **IAM-lite**: per-principal tokens and keys, with per-bucket role bindings;
- a **retention lock** (write-once buckets) and **lifecycle** (age-based delete).

The control plane talks to it through `ShimObjectStore`, which implements the
[`ObjectStore`](run-protocol.md#storage-layout) protocol from `mapi_spec.storage`.
`GcsObjectStore` implements the same protocol on real GCS.

## Running it

```bash
mapi-gcs-shim                                   # serve .local/gcs on 127.0.0.1:4443
python -m mapi_gcs_shim --root .local/gcs --port 4443
mapi-gcs-shim init --root .local/gcs            # write config + key files, then exit
mapi-gcs-shim key exec --field token            # print the exec principal's bearer token
```

The defaults come from the local-dev variables:

- `MAPI_DATA_DIR`: the root is `$MAPI_DATA_DIR/gcs`;
- `MAPI_ENV`: bucket names are `mapi-{env}-*`;
- `MAPI_GCS_ENDPOINT`: the host and port.

`--port 0` picks a free port. The server stops gracefully on SIGINT or SIGTERM. Only one
server may serve a root at a time, which is enforced by a lock file.

### State on disk

| Path | Content |
|---|---|
| `_shim/config.json` | Buckets and principals, including secrets. Mode `0600`. |
| `_shim/keys/{principal}.json` | One key file per principal: email, bearer token, HMAC secret and endpoint. Mode `0600`. |
| `buckets/{bucket}/bucket.json` | The bucket's retention and lifecycle policies. |
| `buckets/{bucket}/objects/{h[:2]}/{h}.json` | The live generation's metadata, where `h = sha256(object name)`. |
| `buckets/{bucket}/objects/{h[:2]}/{h}.{generation}.bin` | The object's bytes. |
| `tmp/` | The staging area for uploads. |

**No object name ever becomes a filesystem path.** Names are hashed, and bucket names must
match the GCS bucket grammar. Names with empty, `.` or `..` segments, a leading `/` or control
characters are rejected with `400`, whether they arrive in the JSON API, the XML API or a
POST-policy `key`.

Writes are crash-safe:

1. bytes are streamed into `tmp/` while the CRC32C and MD5 are computed;
2. the data file is renamed into place;
3. the metadata sidecar is atomically replaced, which is the commit point.

On startup, the shim deletes leftover staging files and any data files that never committed.
Only the live generation is kept, as in a bucket without object versioning. Reading a
superseded generation therefore returns `404`.

## Buckets and principals

By default `init` creates the five buckets from the plan's storage layout:

| Bucket | Policy |
|---|---|
| `mapi-{env}-quarantine` | Lifecycle: objects are deleted after 7 days. |
| `mapi-{env}-bootstrap` | Lifecycle: objects are deleted after 1 day. |
| `mapi-{env}-runs` | No policy. |
| `mapi-{env}-bundles` | Locked retention of about 100 years. Objects cannot be overwritten or deleted, even by an admin. |
| `mapi-{env}-data` | No policy. |

It also creates two principals:

| Principal | Bindings |
|---|---|
| `control` (`control@mapi-{env}.iam.gserviceaccount.com`) | `roles/storage.admin` on every bucket. Used by the control plane and the finalizer. |
| `exec` (`exec@…`) | `storage.objects.get` and `storage.objects.delete` on the bootstrap bucket **only**. It cannot list that bucket and has no access to the bundles bucket. Its token is the local executor's `MAPI_EXEC_TOKEN`. |

You can add principals and bindings to `config.json`. The predefined roles are:

- `roles/storage.admin`
- `roles/storage.objectAdmin`
- `roles/storage.objectUser`
- `roles/storage.objectViewer`
- `roles/storage.objectCreator`
- `roles/storage.legacyBucketReader`

A binding targets one bucket, or `"*"` for every bucket.

Permissions are checked as follows:

- **JSON-API calls** are checked against the caller's bearer token. A missing or unknown token
  returns `401`, and a missing permission returns `403`.
- **Signed URLs and POST policies** carry the **signer's** authority, which is checked when the
  URL is *used*. A URL signed by `exec` for the runs bucket is refused.
- **Overwriting an existing object** also requires `storage.objects.delete`, as in GCS.

## HTTP surface

### JSON API

Every JSON-API request needs `Authorization: Bearer <token>`.

| Method and path | Operation |
|---|---|
| `GET /storage/v1/b` | List buckets. Needs `storage.buckets.list`. |
| `POST /storage/v1/b` | Create a bucket. The body may set `retentionPolicy` and `lifecycle` (`Delete` with an `age` condition). |
| `GET` or `DELETE /storage/v1/b/{bucket}` | Get a bucket, or delete it if it is empty. |
| `GET /storage/v1/b/{bucket}/o` | List objects. Accepts `prefix`, `delimiter`, `pageToken`, `maxResults`, `startOffset` and `endOffset`. |
| `GET /storage/v1/b/{bucket}/o/{object}` | Get metadata. With `alt=media` it returns the content, and `Range` gives `206`. |
| `PATCH /storage/v1/b/{bucket}/o/{object}` | Update `contentType` or `metadata`, which increments `metageneration`. |
| `DELETE /storage/v1/b/{bucket}/o/{object}` | Delete an object. Accepts `generation`. |
| `POST /storage/v1/b/{b}/o/{o}/rewriteTo/b/{b2}/o/{o2}` | Server-side copy. Also available as `copyTo`. Accepts `sourceGeneration`. |
| `POST /upload/storage/v1/b/{bucket}/o` | Upload with `uploadType=media` or `multipart`. Resumable uploads return `501`. |
| `GET /download/storage/v1/b/{bucket}/o/{object}` | Download the content. |

Every object operation accepts the `ifGenerationMatch`, `ifGenerationNotMatch`,
`ifMetagenerationMatch` and `ifMetagenerationNotMatch` preconditions. A rewrite also accepts
the `ifSource…` forms.

- On writes, a failed precondition returns `412`.
- On reads, a failed `…NotMatch` returns `304`.
- `ifGenerationMatch=0` means "the object must not exist yet".

Errors use the GCS JSON shape:
`{"error": {"code", "message", "errors": [{"reason"}]}}`.

### Signed URLs (XML style)

| Method and path | Operation |
|---|---|
| `GET /{bucket}/{object}?X-Goog-…` | Download. With `Range` it returns `206`, with `Content-Range`. An unsatisfiable range returns `416`. |
| `PUT /{bucket}/{object}?X-Goog-…` | Upload. |
| `POST /{bucket}` (`multipart/form-data`) | Upload through a V4 POST policy. The `file` field must be last. |

Signing follows **GOOG4-HMAC-SHA256** with per-principal HMAC keys:

- **Canonical request:** method, path, every query parameter except `X-Goog-Signature`, the
  signed headers (always including `host`) and `UNSIGNED-PAYLOAD`.
- **Signed values:** `X-Goog-Expires` (at most 7 days) and `X-Goog-Date` are signed, and the
  URL is valid within ±5 minutes of clock skew.
- **Bound to the signature:** the method, path, host, query (for example `generation`) and
  signed header values. Changing any of them returns `403 SignatureDoesNotMatch`.
- **Unsigned headers:** any `x-goog-*` header that was not signed returns `400`, except
  `x-goog-hash`.
- **Expiry:** an expired URL returns `400 ExpiredToken`.

Signed **GET** URLs from `sign_get` are pinned to a `generation`. If the object is overwritten,
the URL returns `404`; it never silently serves the new bytes.

Signed **PUT** URLs from `sign_put` sign these headers, which the uploader must send verbatim:

- `content-type`;
- `x-goog-if-generation-match: 0`, so the object is write-once by default;
- `x-goog-content-length-range: 0,<max>`.

The uploader may also send `Content-MD5` or `x-goog-hash: crc32c=…,md5=…`. A mismatch returns
`400 BadDigest`, and nothing is stored.

**POST policies** from `post_policy` are a base64 JSON document of conditions, with an HMAC
signature over the document. The conditions are:

- `{"bucket": …}`;
- `["starts-with", "$key", prefix]`;
- `["starts-with", "$content-type", ""]`;
- `["content-length-range", min, max]`;
- the `x-goog-*` credential fields.

When the policy is used, the shim enforces:

- the signature and the `expiration`;
- every condition;
- that every form field is covered by a condition, except `x-goog-signature`, `policy`,
  `file` and `x-ignore-*`;
- the size range. A body that is too large is rejected while it streams.

`success_action_status` (`200`, `201` with an XML `PostResponse`, or `204` by default) and
`${filename}` in `key` behave as in GCS.

### Shim-only endpoints

- `GET /_shim/healthz`: no authentication.
- `POST /_shim/v1/lifecycle:sweep?now=<iso>`: needs `shim.admin`. It runs the lifecycle rules
  now; objects under retention are never swept.

The server also sweeps every `--sweep-interval` seconds. The default is 300.

## Python API

```python
from datetime import timedelta
from mapi_gcs_shim import ShimObjectStore

async with ShimObjectStore.from_root(".local/gcs", "control") as store:
    meta = await store.put("mapi-local-runs", "t/default/runs/r1/a1/x.json", b"{}",
                           content_type="application/json", if_generation_match=0)
    url = await store.sign_get("mapi-local-runs", meta.name, expires_in=timedelta(minutes=15))
```

`ShimObjectStore` has three constructors:

- `ShimObjectStore(endpoint, credentials, public_endpoint=None)`. `public_endpoint` is the host
  written into signed URLs, so a sandbox can reach the shim under another name. The host is
  signed.
- `.from_root(root, principal)`.
- `.from_local_dev(LocalDev)`.

Besides the protocol methods, it offers `create_bucket`, `get_bucket` and `sweep_lifecycle`.
Errors map to `NotFound`, `PreconditionFailed`, `AccessDenied`, `InvalidRange` and
`StorageError`:

- `get(max_bytes=…)` raises `PAYLOAD_TOO_LARGE` before it reads the body;
- a full read whose CRC32C does not match raises `INTEGRITY_FAILURE`.

`mapi_gcs_shim.transfer` has helpers for whoever *holds* a URL:

- `download_signed`;
- `upload_signed_put`;
- `upload_post_policy`.

They speak plain GCS XML semantics, so they also work against real GCS.

`GcsObjectStore` (install `mapi-gcs-shim[gcp]`) implements the same protocol with
`google-cloud-storage`. It has been tested only with a fake client, never against live GCP.

## pytest fixtures

The package registers a `pytest11` plugin, so every workspace test can use these fixtures:

```python
async def test_something(gcs_shim):            # a fresh shim per test, on a free port
    async with gcs_shim.store() as store:       # the control principal; store("exec") also works
        await store.put(gcs_shim.buckets.runs, "t/default/x", b"...")
    headers = gcs_shim.auth_headers("exec")     # for raw httpx calls
```

`gcs_shim_session` gives one shim for the whole session. For use outside pytest,
`ShimServer(root)` is a context manager that runs the shim on a background thread.

## Differences from real GCS

- There is no object versioning: only the live generation is kept.
- Resumable uploads (`uploadType=resumable`) are not implemented. Use `media` or `multipart`
  uploads, or a signed PUT.
- Signing uses HMAC keys (`GOOG4-HMAC-SHA256`) instead of service-account RSA keys
  (`GOOG4-RSA-SHA256`). `GcsObjectStore` uses the library's RSA or IAM signing.
- There are no ACLs, CMEK, storage classes, notifications or bucket `PATCH`. Bucket policies
  are set when the bucket is created or in `config.json`.
- A signed URL is valid only for the method it was signed for. A `HEAD` request needs its own
  signature, as in GCS.
