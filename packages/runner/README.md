# mapi-runner

The sandboxed run harness of modeling-api: a **trusted supervisor** and an **untrusted
worker** for one execution (plan §8.1, §8.5, §8.7). The full reference is
[`docs/reference/runner.md`](../../docs/reference/runner.md); the protocol it implements is
[`docs/reference/run-protocol.md`](../../docs/reference/run-protocol.md).

```bash
MAPI_RUN_ID=… MAPI_NONCE=… MAPI_BOOTSTRAP_URL='http://127.0.0.1:4443/storage/v1/b/mapi-local-bootstrap/o/{nonce}' \
MAPI_EXEC_TOKEN=… mapi-runner
```

| Module | Side | What it does |
|---|---|---|
| `mapi_runner.supervisor` | trusted | bootstrap read-then-delete, worker spawn and isolation, event stream, caps, SIGTERM/deadline, uploads, output manifest; v0.11 tracking budgets (`TRACKING_BUDGET`), v0.14 media uploads through the `media/` POST policy (`MEDIA_BUDGET`), v0.15 the `GPU_UNAVAILABLE` warning |
| `mapi_runner.bootstrap` | trusted | JSON-API GET + DELETE of `bootstrap/{nonce}` (local shim token or GCE metadata token) |
| `mapi_runner.events` | trusted | HMAC-signed `{"mapi_event": …}` lines, log capping, progress coalescing |
| `mapi_runner.transfer` | both | ranged signed GETs, signed PUT, streaming POST-policy uploads with CRC32C/MD5 |
| `mapi_runner.outputs` | trusted | `O_NOFOLLOW` collection of the worker's output files |
| `mapi_runner.sandbox` | trusted | uid 10001 + no-new-privs when root; dev mode otherwise |
| `mapi_runner.channel` | both | the length-prefixed JSON message protocol over the socketpair |
| `mapi_runner.jobs` | both | `RunSpec.spec` per kind; input and output names |
| `mapi_runner.worker` | untrusted | `ingest`, `inspect`, `describe`, `validate`, `fit`, `score`, `evaluate`, `derive`, `render`, `optimize`, `translate_priors`, `explore`, `governance`, `workflow_node` (the sandboxed workflow nodes, for example `model.summary`, `evaluate.ppc`, `compare.stacking`, `model.score` and `plot.render`) |
| `mapi_runner.worker.tracking` | untrusted | v0.11: the thread-safe `WorkerTracker` behind `ctx.log_metric`/`log_param`/`set_tag`/`log_line`/`log_image` (batched flushes, the same budgets as the supervisor) |
| `mapi_runner.worker.image_ingest`, `.collection_ingest`, `.weights_ingest` | untrusted | v0.14 `image_folder` zips (Pillow), v0.15 image collections (ranged part reads, sha256 per image, explorer sample) and `safetensors` weights |
| `mapi_runner.worker.checkpoints` | untrusted | sampler checkpoints and, from v0.15, the file checkpoints of networks (`ckpt-<key>.weights.safetensors`, `.opt.safetensors`, `.meta.json`; `CHECKPOINT_SKIPPED` over 16 MiB of meta) |
| `mapi_runner.worker.hdf5_guard` | untrusted | v0.8: walks an HDF5 upload with h5py's low-level API (no link followed) and refuses external links, external storage, virtual datasets, filter plugins, large user blocks and unbounded shapes before any reader opens it |
| `mapi_runner.testing` | untrusted | misbehaving registries for the sandbox tests |

The supervisor never imports the SDK, plugins, numpy or xarray (an import-linter contract and
a subprocess test enforce it). Pillow (v0.14) is a dependency of the runner for the worker's
image ingest; the supervisor and the control plane only read image headers. The import-linter
contracts also keep `mapi_jax`, `jax`, `flax` and `optax` out of the supervisor (v0.15).

## Tests

```bash
uv run pytest packages/runner -q
```

The end-to-end tests run the real `mapi-runner` process against the real local GCS emulator
(`mapi_gcs_shim`, wrapped by `tests/shimgcs.py`, which also records every request so tests can
assert on ranged reads and the bootstrap GET-then-DELETE). The runner only sees what a real
execution sees: signed URLs, a POST policy and the bootstrap object read as the shim's `exec`
principal.

License: dual AGPL-3.0 / commercial (see ../../LICENSE.md).
