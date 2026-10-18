# mapi-client

Typed Python client for the modeling-api `/v1` HTTP API (sync and async, built on `httpx`).
Request and response bodies are the Pydantic DTOs from `mapi_spec.api`; errors are
`mapi_spec.errors.MapiError` subclasses rebuilt from RFC 7807 problem documents.

```python
from mapi_client import Client

with Client() as api:  # MAPI_URL / MAPI_TOKEN / MAPI_PROJECT / ~/.config/mapi/config.toml
    version = api.datasets.upload("eu_media.zarr.zip", dataset="eu_media_daily")
    rev = api.specs.upsert(open("spec.yaml").read())
    run = api.runs.fit(rev.spec_id, rev.revision)
    for item in api.runs.stream_events(run.id):  # SSE, resumes with Last-Event-ID
        print(item)
    run = api.runs.wait(run.id, raise_on_failure=True)
    result = api.models.score(
        model_id,
        1,
        {
            "model": "model://default/eu-geo-mmm@v1",
            "mode": "forecast",
            "horizon": {"dim": "time", "periods": 8},
        },
    )
    result.tree  # xr.DataTree with the `xarray` extra
```

- `AsyncClient` has the same resources with `await` / `async for`.
- List endpoints return lazy `Paginator`s (`for x in api.runs.list(state="running")`).
- `POST` endpoints that accept `Idempotency-Key` get a fresh UUID per call unless you pass
  one; idempotent calls are retried on 429/502/503/504 and connection errors.
- Signed upload/download URLs are fetched without the API's `Authorization` header, and
  every transfer is checked against the storage-computed CRC32C.
- `pip install 'mapi-client[xarray]'` enables `xr.Dataset` uploads and `xr.DataTree`
  score results (via `mapi-sdk`'s validated zarr-zip reader/writer).
- Tree datasets (v0.8): `datasets.upload` takes an `xr.DataTree` (`data.tree.zip`) or a raw
  HDF5 file with an `hdf5` mapping; `datasets.upload_files([...], concat="market",
  labels=[...])` / `merge=True` / `AssemblyFile(..., node="/media")` make one version from
  several files; `datasets.inspect_file(path, dataset=...)` lists an upload's groups and
  arrays before it is ingested (`docs/guides/tree-data.md`).

**Multi-tenant APIs (v0.3).**

- Credentials: an API token (`Client(token=…)`, `MAPI_TOKEN`, the profile's `token`) or the
  OpenID Connect sign-in stored by `mapi login` (`mapi_client.oidc`: loopback redirect with
  PKCE, or the device grant), renewed with its refresh token and only ever sent to the API
  it was made for. Tokens are never sent over plain http to a non-loopback host.
- Project: `Client(project="acme/growth")`, `MAPI_PROJECT` or the profile's `project` is sent
  as `X-Mapi-Project` on project-scoped requests (`api.use(...)` switches it).
- Resources `tenants`, `tokens`, `usage`, `admin` and `api.me()`.
- `429 RATE_LIMITED` is waited out (`Retry-After`, back-off) and resent; `429
  QUOTA_EXCEEDED` raises `QuotaExceeded` at once with `retry_after_of(exc)` (see
  `RetryPolicy`).
- `mapi login`, `mapi use` and `mapi config set` write `config.toml` and `credentials.json`
  atomically with mode `0600`.

**Guided mode and integrations (v0.4).**

- `api.guided`, `api.endpoints`, `api.webhooks`, saved plots, renders and embeds on
  `api.plots`, sign-up and the trust lifecycle (`api.signup`, `api.tenants.export`, ...).
- `Client().endpoint("geo-mmm-prod").score(ds)` sends an `xr.Dataset` inline (XrJSON) and
  returns the scored `xr.Dataset` (`xarray` extra).
- `mapi_client.webhooks.verify(headers, body, secret)` checks a delivery's `Mapi-Signature`.
- Budgets are optimised by the platform with mmm-framework
  (`POST /v1/models/{id}/versions/{v}:optimize`).

License: Apache License 2.0.
