# GCS shim (local Cloud Storage)

`mapi-gcs-shim` is a local emulator of the parts of Google Cloud Storage that the platform
uses. The local stack, the test suites and the runner use it instead of real GCS. It
implements:

- object **generations** and **metagenerations**, with generation preconditions
- **CRC32C and MD5 that the server calculates**, in the GCS wire forms (base64)
- **V4 signed URLs**: GET with HTTP `Range`, and PUT
- **V4 POST policies** that limit uploads to a key prefix and a `content-length-range`
- **IAM-lite**: tokens and keys for each principal, with role bindings for each bucket
- a **retention lock** (write-once buckets) and **lifecycle** (delete after an age).

The control plane uses `ShimObjectStore` to communicate with the shim. `ShimObjectStore`
implements the [`ObjectStore`](run-protocol.md#storage-layout) protocol from
`mapi_spec.storage`. `GcsObjectStore` implements the same protocol on real GCS.

## Start the shim

```bash
mapi-gcs-shim                                   # serve .local/gcs on 127.0.0.1:4443
python -m mapi_gcs_shim --root .local/gcs --port 4443
mapi-gcs-shim init --root .local/gcs            # write config + key files, then exit
mapi-gcs-shim key exec --field token            # print the exec principal's bearer token
```

The defaults come from the local-dev variables:

- `MAPI_DATA_DIR`: the root is `$MAPI_DATA_DIR/gcs`.
- `MAPI_ENV`: the bucket names are `mapi-{env}-*`.
- `MAPI_GCS_ENDPOINT`: the host and the port.

`--port 0` selects a free port. The server stops correctly on SIGINT or SIGTERM. Only one
server can serve a root at a time. A lock file controls this.

### State on disk

| Path | Content |
|---|---|
| `_shim/config.json` | Buckets and principals, with their secrets. Mode `0600`. |
| `_shim/keys/{principal}.json` | One key file for each principal: email, bearer token, HMAC secret and endpoint. Mode `0600`. |
| `buckets/{bucket}/bucket.json` | The retention and lifecycle policies of the bucket. |
| `buckets/{bucket}/objects/{h[:2]}/{h}.json` | The metadata of the live generation, where `h = sha256(object name)`. |
| `buckets/{bucket}/objects/{h[:2]}/{h}.{generation}.bin` | The bytes of the object. |
| `tmp/` | The staging area for uploads. |

**An object name never becomes a filesystem path.** The shim hashes the names. Bucket names
must agree with the GCS bucket grammar. The shim rejects some names with `400`: names with
empty, `.` or `..` segments, a leading `/` or control characters. This rule applies to the
JSON API, the XML API and the `key` of a POST policy.

The shim writes data safely, also if it crashes:

1. It streams the bytes into `tmp/` and calculates the CRC32C and the MD5 during the stream.
2. It renames the data file into its final location.
3. It replaces the metadata sidecar atomically. This is the commit point.

When the shim starts, it deletes the staging files that stay from before. It also deletes all
data files that it did not commit. The shim keeps only the live generation, as in a bucket
without object versioning. Thus, if you read a superseded generation, the shim returns `404`.

## Buckets and principals

By default, `init` makes the five buckets from the storage layout of the plan:

| Bucket | Policy |
|---|---|
| `mapi-{env}-quarantine` | Lifecycle: the shim deletes objects after 7 days. |
| `mapi-{env}-bootstrap` | Lifecycle: the shim deletes objects after 1 day. |
| `mapi-{env}-runs` | No policy. |
| `mapi-{env}-bundles` | Locked retention of about 100 years. Nobody can overwrite or delete objects, also not an admin. `--bundle-retention-days N` (default `$MAPI_BUNDLE_RETENTION_DAYS`) locks it for `N` days instead, as the deployed bucket does (30). `0` sets no retention. Thus the tenant deletions of a test stack finish immediately. The shim uses this value when it makes the root. A root that exists keeps its policy. |
| `mapi-{env}-data` | No policy. |

It also makes two principals:

| Principal | Bindings |
|---|---|
| `control` (`control@mapi-{env}.iam.gserviceaccount.com`) | `roles/storage.admin` on all buckets. The control plane and the finalizer use it. |
| `exec` (`exec@…`) | `storage.objects.get` and `storage.objects.delete` on the bootstrap bucket **only**. It cannot list that bucket. It has no access to the bundles bucket. Its token is the `MAPI_EXEC_TOKEN` of the local executor. |

You can add principals and bindings to `config.json`. The predefined roles are:

- `roles/storage.admin`
- `roles/storage.objectAdmin`
- `roles/storage.objectUser`
- `roles/storage.objectViewer`
- `roles/storage.objectCreator`
- `roles/storage.legacyBucketReader`

A binding applies to one bucket, or to all buckets with `"*"`.

The shim does a check of the permissions as follows:

- **JSON-API calls:** the shim uses the bearer token of the caller. If the token is missing
  or unknown, the shim returns `401`. If a permission is missing, it returns `403`.
- **Signed URLs and POST policies:** these have the authority of the **signer**. The shim
  does the check when a client *uses* the URL. The shim refuses a URL that `exec` signed for
  the runs bucket.
- **Overwrite of an object that exists:** this also needs `storage.objects.delete`, as in GCS.

## HTTP surface

### JSON API

All JSON-API requests need `Authorization: Bearer <token>`.

| Method and path | Operation |
|---|---|
| `GET /storage/v1/b` | List buckets. Needs `storage.buckets.list`. |
| `POST /storage/v1/b` | Make a bucket. The body can set `retentionPolicy` and `lifecycle` (`Delete` with an `age` condition). |
| `GET` or `DELETE /storage/v1/b/{bucket}` | Get a bucket, or delete it if it is empty. |
| `GET /storage/v1/b/{bucket}/o` | List objects. Accepts `prefix`, `delimiter`, `pageToken`, `maxResults`, `startOffset` and `endOffset`. |
| `GET /storage/v1/b/{bucket}/o/{object}` | Get metadata. With `alt=media`, it returns the content. With `Range`, it returns `206`. |
| `PATCH /storage/v1/b/{bucket}/o/{object}` | Update `contentType` or `metadata`. This increases `metageneration`. |
| `DELETE /storage/v1/b/{bucket}/o/{object}` | Delete an object. Accepts `generation`. |
| `POST /storage/v1/b/{b}/o/{o}/rewriteTo/b/{b2}/o/{o2}` | Server-side copy. Also available as `copyTo`. Accepts `sourceGeneration`. |
| `POST /upload/storage/v1/b/{bucket}/o` | Upload with `uploadType=media` or `multipart`. Resumable uploads return `501`. |
| `GET /download/storage/v1/b/{bucket}/o/{object}` | Download the content. |

All object operations accept the preconditions `ifGenerationMatch`, `ifGenerationNotMatch`,
`ifMetagenerationMatch` and `ifMetagenerationNotMatch`. A rewrite also accepts the
`ifSource…` forms.

- On writes, a failed precondition returns `412`.
- On reads, a failed `…NotMatch` returns `304`.
- `ifGenerationMatch=0` means "the object must not exist yet".

Errors use the GCS JSON shape:
`{"error": {"code", "message", "errors": [{"reason"}]}}`.

### Signed URLs (XML style)

| Method and path | Operation |
|---|---|
| `GET /{bucket}/{object}?X-Goog-…` | Download. With `Range`, it returns `206` with `Content-Range`. If the shim cannot satisfy the range, it returns `416`. |
| `PUT /{bucket}/{object}?X-Goog-…` | Upload. |
| `POST /{bucket}` (`multipart/form-data`) | Upload through a V4 POST policy. The `file` field must be the last field. |

The signature uses **GOOG4-HMAC-SHA256** with an HMAC key for each principal:

- **Canonical request:** the method, the path, all query parameters except
  `X-Goog-Signature`, the signed headers (always with `host`) and `UNSIGNED-PAYLOAD`.
- **Signed values:** `X-Goog-Expires` (maximum 7 days) and `X-Goog-Date`. The expiry is
  strict. But `X-Goog-Date` can be a maximum of 5 minutes in the future, because clocks can
  have small differences.
- **Bound to the signature:** the method, the path, the host, the query (for example
  `generation`) and the values of the signed headers. If you change one of them, the shim
  returns `403 SignatureDoesNotMatch`.
- **Unsigned headers:** an `x-goog-*` header that is not signed returns `400`. The exception
  is `x-goog-hash`.
- **Expiry:** a URL that expired returns `400 ExpiredToken`.

Signed **GET** URLs from `sign_get` are pinned to a `generation`. If a client overwrites the
object, the URL returns `404`. The URL never serves the new bytes without an error.

Signed **PUT** URLs from `sign_put` sign these headers. The uploader must send them with no
change:

- `content-type`
- `x-goog-if-generation-match: 0`. Thus the object is write-once by default.
- `x-goog-content-length-range: 0,<max>`.

The uploader can also send `Content-MD5` or `x-goog-hash: crc32c=…,md5=…`. If a value does
not agree, the shim returns `400 BadDigest` and keeps nothing.

**POST policies** from `post_policy` are a base64 JSON document of conditions. They have an
HMAC signature on the document. The conditions are:

- `{"bucket": …}`
- `["starts-with", "$key", prefix]`
- `["starts-with", "$content-type", ""]`
- `["content-length-range", min, max]`
- the `x-goog-*` credential fields
- with `create_only=True`, `{"x-goog-if-generation-match": "0"}` (the client also sends it
  as a form field).

When a client uses the policy, the shim enforces these rules:

- The signature and the `expiration` must be valid.
- All conditions must be true.
- A condition must cover each form field. The exceptions are `x-goog-signature`, `policy`,
  `file` and `x-ignore-*`.
- The size must be in the range. The shim rejects a body that is too large during the
  stream.
- An `x-goog-if-generation-match` form field is a precondition. Thus a create-only policy
  returns `412` for a key that exists. This occurs also if the signer has permission to
  delete.

Real GCS does not accept a generation precondition in a POST form. Thus `GcsObjectStore`
signs create-only policies with `uploader_credentials` instead. This is a principal without
`storage.objects.delete`, which GCS needs for an overwrite.

`success_action_status` (`200`, `201` with an XML `PostResponse`, or `204` by default) and
`${filename}` in `key` operate as in GCS.

### Shim-only endpoints

- `GET /_shim/healthz`: no authentication.
- `POST /_shim/v1/lifecycle:sweep?now=<iso>`: needs `shim.admin`. It applies the lifecycle
  rules now. It never deletes objects that have retention.

The server also applies the lifecycle rules at each `--sweep-interval` seconds. The default is
300.

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

- `ShimObjectStore(endpoint, credentials, public_endpoint=None)`. `public_endpoint` is the
  host that the shim writes into signed URLs. Thus a sandbox can get access to the shim with
  a different name. The host is signed.
- `.from_root(root, principal)`.
- `.from_local_dev(LocalDev)`.

It has the protocol methods. It also has `create_bucket`, `get_bucket` and
`sweep_lifecycle`. Errors map to `NotFound`, `PreconditionFailed`, `AccessDenied`,
`InvalidRange` and `StorageError`:

- `get(max_bytes=…)` raises `PAYLOAD_TOO_LARGE` before it reads the body.
- A full read with a CRC32C that does not agree raises `INTEGRITY_FAILURE`.

`mapi_gcs_shim.transfer` has helpers for a client that *has* a URL:

- `download_signed`
- `upload_signed_put`
- `upload_post_policy`.

They use only GCS XML semantics. Thus they also operate with real GCS.

## Real Cloud Storage

`GcsObjectStore` (install `mapi-gcs-shim[gcp]`) implements the same protocol with
`google-cloud-storage`. The tests use only a fake client. Nobody tested it with live GCP.

- **Signatures.** If you give a `BlobSigner` (`blob_signer=`), `mapi_gcs_shim.gcs_signing`
  makes the signed GET/PUT URLs and the POST policies. It uses `GOOG4-RSA-SHA256`, and the
  result is byte-identical to the V4 signer of the library. Only the string to sign goes to
  the signer. `IamSignBlobSigner` signs with the IAM Credentials `signBlob` API. It uses a
  token provider that refreshes the token (`google_token_provider()`), and it retries on
  `429`/5xx. `GcsObjectStore.for_cloud_run(service_account_email=...,
  uploader_service_account_email=...)` connects the two accounts. The control account signs
  URLs. The uploader account (create, but no delete) signs create-only POST policies.
- **Integrity.** If the CRC32C, MD5 or size that the server calculates is different from the
  bytes that it got, the store deletes that write (that generation only). Then it raises
  `INTEGRITY_FAILURE`. A copy pins the source generation (`ifSourceGenerationMatch`). It
  compares the size and the CRC32C of the destination with the source.
- **Preconditions.** Signed GETs are pinned to a generation. Signed PUTs have a signed
  `x-goog-if-generation-match` (`0`, write-once, by default). `put`, `copy` and `delete` send
  `if_generation_match` to GCS.

## pytest fixtures

The package registers a `pytest11` plugin. Thus all workspace tests can use these fixtures:

```python
async def test_something(gcs_shim):            # a fresh shim per test, on a free port
    async with gcs_shim.store() as store:       # the control principal; store("exec") also works
        await store.put(gcs_shim.buckets.runs, "t/default/x", b"...")
    headers = gcs_shim.auth_headers("exec")     # for raw httpx calls
```

`gcs_shim_session` gives one shim for the full session. To use the shim outside pytest, use
`ShimServer(root)`. It is a context manager that runs the shim on a background thread.

## Differences from real GCS

- There is no object versioning. The shim keeps only the live generation.
- The shim does not implement resumable uploads (`uploadType=resumable`). Use `media` or
  `multipart` uploads, or a signed PUT.
- The signatures use HMAC keys (`GOOG4-HMAC-SHA256`), not the RSA keys of a service account
  (`GOOG4-RSA-SHA256`). `GcsObjectStore` signs RSA URLs itself through a `BlobSigner`
  (IAM `signBlob` on Cloud Run). If it has no `BlobSigner`, it uses the signatures of the
  library.
- There are no ACLs, CMEK, storage classes, notifications or bucket `PATCH`. You set bucket
  policies when you make the bucket, or in `config.json`.
- A signed URL is valid only for the method that it was signed for. A `HEAD` request needs
  its own signature, as in GCS.
