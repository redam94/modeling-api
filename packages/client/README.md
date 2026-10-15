# mapi-client

Typed Python client for the modeling-api `/v1` HTTP API (sync and async, built on `httpx`).
Request and response bodies are the Pydantic DTOs from `mapi_spec.api`; errors are
`mapi_spec.errors.MapiError` subclasses rebuilt from RFC 7807 problem documents.

```python
from mapi_client import Client

with Client() as api:  # MAPI_URL / MAPI_TOKEN / ~/.config/mapi/config.toml / 127.0.0.1:8000
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

M1 has no authentication; a configured token is sent as `Authorization: Bearer …` (never
over plain http to a non-loopback host). OIDC arrives in M2.

License: Apache License 2.0.
