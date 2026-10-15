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
| `mapi_runner.supervisor` | trusted | bootstrap read-then-delete, worker spawn and isolation, event stream, caps, SIGTERM/deadline, uploads, output manifest |
| `mapi_runner.bootstrap` | trusted | JSON-API GET + DELETE of `bootstrap/{nonce}` (local shim token or GCE metadata token) |
| `mapi_runner.events` | trusted | HMAC-signed `{"mapi_event": …}` lines, log capping, progress coalescing |
| `mapi_runner.transfer` | both | ranged signed GETs, signed PUT, streaming POST-policy uploads with CRC32C/MD5 |
| `mapi_runner.outputs` | trusted | `O_NOFOLLOW` collection of the worker's output files |
| `mapi_runner.sandbox` | trusted | uid 10001 + no-new-privs when root; dev mode otherwise |
| `mapi_runner.channel` | both | the length-prefixed JSON message protocol over the socketpair |
| `mapi_runner.jobs` | both | `RunSpec.spec` per kind; input and output names |
| `mapi_runner.worker` | untrusted | `ingest`, `describe`, `validate`, `fit`, `score`, `evaluate`, `derive`, `render` |
| `mapi_runner.testing` | untrusted | misbehaving registries for the sandbox tests |

The supervisor never imports the SDK, plugins, numpy or xarray (an import-linter contract and
a subprocess test enforce it).

## Tests

```bash
uv run pytest packages/runner -q
```

The end-to-end tests run the real `mapi-runner` process against `tests/fakegcs.py`, a small
signed-URL GCS fake (generations, server-side CRC32C/MD5, signed GET with ranges, write-once
signed PUT, POST policies, exec-token JSON API on the bootstrap bucket). It stands in for
`mapi_gcs_shim` until the shim lands; the tests only use the contract shapes in
`mapi_spec.storage`, so switching to the shim fixture is a fixture swap.

License: dual AGPL-3.0 / commercial (see ../../LICENSE.md).
