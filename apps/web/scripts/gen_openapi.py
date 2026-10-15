"""Generate ``apps/web/openapi.json`` from the M1 contracts in ``mapi_spec``.

The control plane (``mapi_server``) is being built in parallel, so the web app is typed
against an OpenAPI 3.1 document assembled from the same sources the server uses:

* the endpoint table :data:`mapi_spec.api.M1_ENDPOINTS` (paths, methods, status codes,
  query parameters, headers, endpoint-specific error codes);
* the Pydantic DTOs in :mod:`mapi_spec.api` (and the catalog / plot / problem models), whose
  JSON Schemas are emitted with pydantic's ``models_json_schema`` -- the same machinery
  FastAPI uses, so component names (``Run``, ``Page_Run_``, ``-Input``/``-Output``
  suffixes) match what the real server will publish.

Once the server exists the integrator replaces this file with the server's own
``/openapi.json`` (``pnpm --filter @mapi/web openapi:fetch``) and regenerates
``src/api/schema.d.ts``; the web code only refers to component names through
``src/api/types.ts`` so a rename touches one file.

Usage::

    uv run python apps/web/scripts/gen_openapi.py [--check]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic.json_schema import JsonSchemaMode, models_json_schema

from mapi_spec import api, run
from mapi_spec.capabilities import Diagnostic
from mapi_spec.catalog import ModelCatalogEntry, PlotCatalogEntry, StepCatalogEntry
from mapi_spec.errors import ERROR_INFO, ErrorCode, Problem
from mapi_spec.executor import RunKind
from mapi_spec.plots import PlotSpec
from mapi_spec.spec import CONTRACT_VERSION

OUT = Path(__file__).resolve().parent.parent / "openapi.json"

_NAMED: dict[str, type[BaseModel]] = {
    "ModelCatalogEntry": ModelCatalogEntry,
    "StepCatalogEntry": StepCatalogEntry,
    "PlotCatalogEntry": PlotCatalogEntry,
    "PlotSpec": PlotSpec,
    "Problem": Problem,
}

# Event payload models are not referenced by any path (they travel inside
# RunEventView.payload and SSE frames) but the UI decodes them, so publish them too.
_EXTRA_RESPONSE_MODELS: tuple[type[BaseModel], ...] = (
    run.StatePayload,
    run.ProgressPayload,
    run.LogPayload,
    run.WarningPayload,
    run.CheckpointPayload,
    run.MetricPayload,
    run.CompletePayload,
    run.ErrorPayload,
    Diagnostic,
)

_PATH_PARAM_RE = re.compile(r"\{([a-z_]+)\}")
_INT_PATH_PARAMS = {"version", "revision"}
_PAGE_RE = re.compile(r"^Page\[(\w+)\]$")


def _model(name: str) -> type[BaseModel]:
    page = _PAGE_RE.match(name)
    if page:
        return api.Page[_model(page.group(1))]  # type: ignore[index,no-any-return]
    if name in _NAMED:
        return _NAMED[name]
    model = getattr(api, name, None)
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise SystemExit(f"unknown DTO {name!r} in M1_ENDPOINTS")
    return model


def _query_param(name: str) -> dict[str, Any]:
    schemas: dict[str, dict[str, Any]] = {
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": api.PAGE_LIMIT_MAX,
            "default": api.PAGE_LIMIT_DEFAULT,
        },
        "cursor": {"type": "string", "maxLength": 2048},
        "kind": {"type": "string", "enum": [k.value for k in RunKind]},
        "state": {"type": "string", "enum": [s.value for s in run.RunState]},
        "spec_id": {"type": "string"},
        "after": {"type": "integer", "minimum": 0},
        "attempt": {"type": "integer", "minimum": 1},
        "stream": {"type": "string", "enum": ["stdout", "stderr", "system"]},
    }
    if name not in schemas:
        raise SystemExit(f"no schema for query parameter {name!r}")
    return {"name": name, "in": "query", "required": False, "schema": schemas[name]}


def _header_param(name: str) -> dict[str, Any]:
    schemas: dict[str, dict[str, Any]] = {
        api.IDEMPOTENCY_KEY_HEADER: {
            "type": "string",
            "pattern": api.IDEMPOTENCY_KEY_RE.pattern,
            "maxLength": 255,
        },
        api.LAST_EVENT_ID_HEADER: {"type": "string"},
        api.PREFER_HEADER: {"type": "string", "examples": ["wait=60"]},
    }
    return {"name": name, "in": "header", "required": False, "schema": schemas[name]}


def _path_params(path: str) -> list[dict[str, Any]]:
    params = []
    for name in _PATH_PARAM_RE.findall(path):
        schema: dict[str, Any] = (
            {"type": "integer", "minimum": 1} if name in _INT_PATH_PARAMS else {"type": "string"}
        )
        param: dict[str, Any] = {"name": name, "in": "path", "required": True, "schema": schema}
        if name == "name":
            param["description"] = "Object name; may contain '/' (sent percent-encoded)."
        params.append(param)
    return params


def build() -> dict[str, Any]:
    requests: list[type[BaseModel]] = []
    responses: list[type[BaseModel]] = [Problem, *_EXTRA_RESPONSE_MODELS]
    for decl in api.M1_ENDPOINTS:
        if decl.request:
            requests.append(_model(decl.request))
        if decl.response:
            responses.append(_model(decl.response))

    inputs: list[tuple[type[BaseModel], JsonSchemaMode]] = [(m, "validation") for m in requests]
    outputs: list[tuple[type[BaseModel], JsonSchemaMode]] = [
        (m, "serialization") for m in responses
    ]
    keymap, top = models_json_schema(
        [*dict.fromkeys(inputs), *dict.fromkeys(outputs)],
        ref_template="#/components/schemas/{model}",
        by_alias=True,
    )
    components: dict[str, Any] = top.get("$defs", {})

    def ref(name: str, mode: JsonSchemaMode) -> dict[str, Any]:
        return dict(keymap[(_model(name), mode)])

    problem_ref = dict(keymap[(Problem, "serialization")])
    paths: dict[str, dict[str, Any]] = {}
    for decl in api.M1_ENDPOINTS:
        op: dict[str, Any] = {
            "operationId": decl.operation_id,
            "summary": decl.summary,
            "tags": [_tag(decl.path)],
        }
        params = _path_params(decl.path)
        params += [_query_param(q) for q in decl.query]
        params += [_header_param(h) for h in decl.headers]
        if params:
            op["parameters"] = params
        if decl.request:
            op["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": ref(decl.request, "validation")}},
            }
        responses_obj: dict[str, Any] = {}
        if decl.response is None:
            body: dict[str, Any] = {"type": "object", "additionalProperties": True}
        else:
            body = ref(decl.response, "serialization")
        content: dict[str, Any] = {"application/json": {"schema": body}}
        if decl.streaming:
            content["text/event-stream"] = {
                "schema": {"type": "string"},
                "x-mapi-sse-events": [e.value for e in api.SseEventName],
            }
        responses_obj[str(decl.success)] = {"description": decl.summary, "content": content}
        if decl.operation_id == "score_model":
            responses_obj["202"] = {
                "description": "Not finished within the Prefer: wait budget",
                "content": {"application/json": {"schema": body}},
            }
        if decl.operation_id in ("create_run", "retry_run"):
            responses_obj["200"] = {
                "description": "Idempotent replay: the run created by the first request",
                "content": {"application/json": {"schema": body}},
            }
        if decl.operation_id == "ready":
            responses_obj["503"] = {
                "description": "A dependency is unreachable",
                "content": {"application/json": {"schema": body}},
            }
        by_status: dict[int, list[ErrorCode]] = {}
        for code in decl.errors:
            by_status.setdefault(ERROR_INFO[code].status, []).append(code)
        for status, codes in sorted(by_status.items()):
            responses_obj.setdefault(
                str(status),
                {
                    "description": ", ".join(c.value for c in codes),
                    "content": {"application/problem+json": {"schema": problem_ref}},
                },
            )
        responses_obj["default"] = {
            "description": "RATE_LIMITED, INTERNAL_ERROR, INFRA_ERROR or an endpoint error",
            "content": {"application/problem+json": {"schema": problem_ref}},
        }
        op["responses"] = responses_obj
        paths.setdefault(decl.path, {})[decl.method.lower()] = op

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "modeling-api",
            "version": "0.2.0-dev",
            "description": (
                "Generated from mapi_spec.api.M1_ENDPOINTS by apps/web/scripts/gen_openapi.py. "
                "Replace with the server's /openapi.json once it is available."
            ),
            "x-mapi-contract-version": CONTRACT_VERSION,
        },
        "servers": [{"url": "http://127.0.0.1:8000"}],
        "paths": paths,
        "components": {"schemas": dict(sorted(components.items()))},
    }


def _tag(path: str) -> str:
    parts = [p for p in path.split("/") if p and p != "v1"]
    head = parts[0] if parts else "health"
    return re.split(r"[:{]", head)[0] or "health"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if openapi.json is stale")
    args = parser.parse_args(argv)
    text = json.dumps(build(), indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT} is stale; run scripts/gen_openapi.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
