# mapi-gcs-shim

A local Google Cloud Storage emulator for modeling-api development and tests, plus two drivers
for the `mapi_spec.storage.ObjectStore` protocol:

- **the server** covers the GCS JSON-API subset the platform uses: buckets, and objects
  insert, get, list, patch, delete and rewrite, all with generation preconditions. It also
  serves V4 signed GET (with `Range`) and PUT URLs and V4 POST policies. Around these it adds
  IAM-lite principals, a retention lock, lifecycle sweeps, and server-computed CRC32C and MD5
  on a crash-safe filesystem backend;
- **`ShimObjectStore`** is an async driver over the shim's HTTP API, using httpx;
- **`GcsObjectStore`** is an async driver over `google-cloud-storage`. It comes with the `[gcp]`
  extra and has been tested only with mocks;
- **the `gcs_shim` pytest fixture** is registered through a `pytest11` entry point.

```bash
mapi-gcs-shim --root .local/gcs --port 4443      # or: python -m mapi_gcs_shim ...
mapi-gcs-shim key exec --field token             # MAPI_EXEC_TOKEN for the local executor
```

See `docs/reference/gcs-shim.md` for the full reference: layout, principals, endpoints,
signing and the differences from GCS.

License: Apache License 2.0.
