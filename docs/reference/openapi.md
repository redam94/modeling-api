---
description: The modeling-api /v1 OpenAPI 3.1 document, browsable with every endpoint, schema and example.
hide:
  - toc
---

# OpenAPI explorer <span class="mapi-badge">v0.2</span>

This page shows the OpenAPI 3.1 document of the `/v1` API. [Redoc](https://github.com/Redocly/redoc)
renders the document. The document is the committed file `apps/web/openapi.json`. A script makes
it from the FastAPI app of the server and the `mapi_spec.api` models
(`uv run python apps/web/scripts/gen_openapi.py`). CI fails when the file is different from the
server. A running API serves the same document at `/openapi.json`.

[Open full screen](openapi-viewer.html){ .md-button }
[Download openapi.json](generated/openapi.json){ .md-button }

<iframe class="mapi-openapi" src="../openapi-viewer.html" title="OpenAPI explorer"
        loading="lazy" referrerpolicy="no-referrer"></iframe>

The explorer loads nothing from outside this site. The script `scripts/vendor_redoc.py` copies
the Redoc bundle (version 2.5.4) into the repository. The script does a check of the integrity
of the npm tarball and pins the SHA-256 of the bundle. A Content-Security-Policy on the page
permits scripts, styles, images and requests from this site only.

For descriptions of the conventions, idempotency, server-sent events and the errors of each
endpoint, read the [HTTP API reference](http-api.md). For a full example, read the
[HTTP API guide](../guides/http-api.md).
