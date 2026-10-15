---
description: The modeling-api /v1 OpenAPI 3.1 document, browsable with every endpoint, schema and example.
hide:
  - toc
---

# OpenAPI explorer <span class="mapi-badge">v0.2</span>

The `/v1` API's OpenAPI 3.1 document, rendered with [Redoc](https://github.com/Redocly/redoc).
It is the committed `apps/web/openapi.json`, generated from the server's own FastAPI app and
the `mapi_spec.api` models (`uv run python apps/web/scripts/gen_openapi.py`); CI fails when
it drifts from the server. A running API serves the same document at `/openapi.json`.

[Open full screen](openapi-viewer.html){ .md-button }
[Download openapi.json](generated/openapi.json){ .md-button }

<iframe class="mapi-openapi" src="../openapi-viewer.html" title="OpenAPI explorer"
        loading="lazy" referrerpolicy="no-referrer"></iframe>

The explorer loads nothing from outside this site: the Redoc bundle (version 2.5.4) is
vendored by `scripts/vendor_redoc.py`, which checks the npm tarball's integrity and pins the
bundle's SHA-256, and a Content-Security-Policy on the page allows scripts, styles, images and
requests from this site only.

For prose (conventions, idempotency, server-sent events, which errors each endpoint returns)
read the [HTTP API reference](http-api.md); for a worked example, the
[HTTP API guide](../guides/http-api.md).
