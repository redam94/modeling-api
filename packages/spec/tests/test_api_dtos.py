"""REST DTOs: examples validate and round-trip, request rules, SSE/Prefer helpers, the
M1 endpoint table, and import purity of the new contract modules."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mapi_spec import api, export_schemas, run, storage
from mapi_spec.api import (
    M1_ENDPOINTS,
    RUN_CREATE_KINDS,
    SSE_HEARTBEAT,
    DatasetCreate,
    IngestMapping,
    Page,
    Run,
    RunCreate,
    RunEventView,
    SseEventName,
    ValidateRequest,
    encode_sse,
    new_id,
    parse_last_event_id,
    parse_prefer_wait,
)
from mapi_spec.executor import RunEventType, RunKind
from mapi_spec.jsonschema_export import API_SCHEMA_MODELS
from pydantic import BaseModel, ValidationError

DTOS: list[type[BaseModel]] = [
    obj
    for name in api.__all__
    if isinstance(obj := getattr(api, name), type) and issubclass(obj, BaseModel)
]


def _examples(model: type[BaseModel]) -> list[dict[str, Any]]:
    schema = model.model_json_schema()
    examples: list[dict[str, Any]] = schema.get("examples", [])
    return examples


def test_every_dto_is_listed() -> None:
    assert len(DTOS) >= 40
    assert Page in DTOS and Run in DTOS


@pytest.mark.parametrize("model", DTOS, ids=lambda m: m.__name__)
def test_every_dto_has_valid_round_tripping_examples(model: type[BaseModel]) -> None:
    examples = _examples(model)
    assert examples, f"{model.__name__} needs json_schema_extra examples"
    for ex in examples:
        obj = model.model_validate(ex)
        again = model.model_validate_json(obj.model_dump_json(by_alias=True))
        assert again == obj
        json.loads(obj.model_dump_json())  # plain JSON


def test_generic_pages_validate_concrete_items() -> None:
    run_example = _examples(Run)[0]
    page = Page[Run].model_validate({"items": [run_example], "next_cursor": None})
    assert isinstance(page.items[0], Run)
    assert Page[Run].model_validate_json(page.model_dump_json()) == page
    with pytest.raises(ValidationError):
        Page[Run].model_validate({"items": [{"id": "x"}]})
    assert Page[Run].model_json_schema()["properties"]["items"]["items"]["$ref"].endswith("/Run")


def test_requests_reject_unknown_fields_and_responses_ignore_them() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        DatasetCreate.model_validate({"name": "x", "nope": 1})
    run_example = {**_examples(Run)[0], "added_in_a_later_server": True}
    assert Run.model_validate(run_example).id.startswith("run_")


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ({"kind": "fit"}, "spec_id and revision"),
        ({"kind": "fit", "spec_id": "s", "revision": 1, "model_version_id": "m"}, "not a model"),
        ({"kind": "score", "model_version_id": "m"}, "'request' is required"),
        ({"kind": "evaluate"}, "model_version_id"),
        ({"kind": "evaluate", "spec_id": "s", "model_version_id": "m"}, "not a spec"),
        ({"kind": "ingest"}, "cannot be created"),
        ({"kind": "sweep"}, "cannot be created"),
        (
            {
                "kind": "derive",
                "model_version_id": "m",
                "request": _examples(RunCreate)[1]["request"],
            },
            "only allowed",
        ),
    ],
)
def test_run_create_shapes(body: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        RunCreate.model_validate(body)


def test_run_create_valid_shapes() -> None:
    assert RunCreate(kind=RunKind.VALIDATE, spec_id="s", revision=1).kind is RunKind.VALIDATE
    rc = RunCreate.model_validate({"kind": "render", "model_version_id": "m", "timeout": "10m"})
    assert str(rc.timeout) == "10m"
    assert all(not k.is_parent for k in RUN_CREATE_KINDS)
    assert RunKind.INGEST not in RUN_CREATE_KINDS


def test_validate_request_needs_exactly_one_source() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        ValidateRequest()
    with pytest.raises(ValidationError, match="exactly one"):
        ValidateRequest(yaml="a: 1", document={"a": 1})
    assert ValidateRequest(document={"kind": "ModelSpec"}).yaml is None


def test_tabular_ingest_needs_a_mapping() -> None:
    with pytest.raises(ValidationError, match="dim and one var"):
        IngestMapping(format="csv")
    assert IngestMapping(format="zarr_zip").dims == {}


# ---------------------------------------------------------------------- headers, SSE and ids


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("wait=10", 10),
        ("respond-async, wait=5", 5),
        ("wait=600", 60),
        ("WAIT = 7", 7),
        ("handling=lenient", None),
        ("wait=abc", None),
        ("nowait=5", None),
    ],
)
def test_prefer_wait(header: str | None, expected: int | None) -> None:
    assert parse_prefer_wait(header) == expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [(None, None), ("", None), ("17", 17), (" 3 ", 3), ("0", None), ("x", None), ("-1", None)],
)
def test_last_event_id(header: str | None, expected: int | None) -> None:
    assert parse_last_event_id(header) == expected


def test_sse_frames() -> None:
    view = RunEventView.model_validate(_examples(RunEventView)[0])
    frame = encode_sse(SseEventName.STATE, view, id=view.id)
    lines = frame.split("\n")
    assert lines[0] == "id: 17" and lines[1] == "event: state" and frame.endswith("\n\n")
    assert json.loads(lines[2].removeprefix("data: "))["seq"] == 3
    snap = encode_sse("snapshot", {"state": "running"}, retry_ms=3000)
    assert snap.startswith("retry: 3000\nevent: snapshot\ndata: {") and "id:" not in snap
    assert SSE_HEARTBEAT.startswith(":") and SSE_HEARTBEAT.endswith("\n\n")
    with pytest.raises(ValueError):
        encode_sse("state", {}, id="a\nb")
    with pytest.raises(ValueError):
        encode_sse("bogus", {})
    # every run-event type has an SSE event name
    assert {t.value for t in RunEventType} <= {e.value for e in SseEventName}


def test_idempotency_keys_and_error_pointers() -> None:
    assert api.check_idempotency_key("b6f1c2d4-1d2e-4f00-9a1b-2c3d4e5f6a7b")
    for bad in ("", "has space", "x" * 256, "tab\t", "é"):
        with pytest.raises(ValueError):
            api.check_idempotency_key(bad)
    assert api.loc_to_pointer(("body", "request", "horizon", 0)) == "/body/request/horizon/0"
    assert api.loc_to_pointer(["query", "a/b~c"]) == "/query/a~1b~0c"
    assert api.loc_to_pointer(()) == ""
    assert api.ARTIFACT_LINK_TTL.total_seconds() == 900
    storage.check_ttl(api.ARTIFACT_LINK_TTL)


def test_ids_are_prefixed_sortable_and_storage_safe() -> None:
    a = new_id("run")
    time.sleep(0.002)
    b = new_id("run")
    assert re.match(r"^run_[0-9a-hjkmnp-tv-z]{26}$", a)
    assert a < b and storage.check_segment(a) == a
    for bad in ("", "R", "toolongprefix", "a1"):
        with pytest.raises(ValueError):
            new_id(bad)


# --------------------------------------------------------------------------- endpoint table

_EXTERNAL = {"PlotSpec", "ModelCatalogEntry", "StepCatalogEntry", "PlotCatalogEntry"}


def _resolve(name: str) -> type[BaseModel]:
    m = re.fullmatch(r"Page\[(\w+)\]", name)
    inner = m.group(1) if m else name
    if inner in _EXTERNAL:
        import mapi_spec

        return getattr(mapi_spec, inner)  # type: ignore[no-any-return]
    obj = getattr(api, inner)
    assert isinstance(obj, type) and issubclass(obj, BaseModel), name
    return obj


def test_endpoint_table_is_consistent() -> None:
    keys = [(e.method, e.path) for e in M1_ENDPOINTS]
    assert len(keys) == len(set(keys))
    ops = [e.operation_id for e in M1_ENDPOINTS]
    assert len(ops) == len(set(ops))
    for e in M1_ENDPOINTS:
        assert e.path.startswith("/v1/") or e.path in ("/healthz", "/readyz"), e.path
        assert e.success in (200, 201, 202), e
        for param in re.findall(r"\{([^}]+)\}", e.path):
            assert re.fullmatch(r"[a-z_]+", param), e.path
        if e.request is not None:
            assert e.method in ("POST", "PUT"), e
            _resolve(e.request)
        if e.response is not None:
            _resolve(e.response)
        assert e.paginated == (e.response or "").startswith("Page["), e
        if e.paginated:
            assert "limit" in e.query, e
        if e.method == "POST" and e.path == "/v1/runs":
            assert "Idempotency-Key" in e.headers
    assert [e.path for e in M1_ENDPOINTS if e.streaming] == ["/v1/runs/{run_id}/events"]


def test_openapi_components_carry_examples() -> None:
    schemas = export_schemas()
    for name in API_SCHEMA_MODELS:
        assert schemas[name]["examples"], name
    for name in ("runspec.json", "outputmanifest.json", "bundlemanifest.json", "objectmeta.json"):
        assert schemas[name]["examples"], name


def _docs_dir() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "reference" / "http-api.md"
        if candidate.exists():
            return candidate.parent
    return None


def test_reference_docs_list_every_endpoint_and_dto() -> None:
    docs = _docs_dir()
    if docs is None:  # pragma: no cover - sdist without the docs tree
        pytest.skip("docs/ not available")
    text = (docs / "http-api.md").read_text("utf-8")
    for e in M1_ENDPOINTS:
        assert f"`{e.method} {e.path}`" in text, f"http-api.md is missing {e.method} {e.path}"
        for dto in filter(None, (e.request, e.response)):
            assert dto in text, dto
    protocol = (docs / "run-protocol.md").read_text("utf-8")
    for needle in (
        run.ENV_RUN_ID,
        run.ENV_NONCE,
        run.ENV_BOOTSTRAP_URL,
        run.ENV_EXEC_TOKEN,
        run.STDOUT_EVENT_KEY,
        run.LOGGING_FILTER,
        "DATABASE_URL",
        "127.0.0.1:4443",
        "127.0.0.1:8000",
        "5173",
        "5174",
    ):
        assert needle in protocol, needle
    for code in run.RunnerExit:
        assert f"| {int(code)} " in protocol, code
    for state in run.RunState:
        assert f"`{state.value.upper()}`" in protocol, state


# ----------------------------------------------------------------------------- import purity


def test_new_contract_modules_do_not_import_array_libraries() -> None:
    code = (
        "import sys\n"
        "import mapi_spec.run, mapi_spec.storage, mapi_spec.api, mapi_spec.jsonschema_export\n"
        "bad = sorted(m for m in ('numpy', 'xarray', 'pandas', 'pyarrow', 'fastapi',"
        " 'sqlalchemy', 'mapi_sdk', 'mapi_server', 'mapi_runner') if m in sys.modules)\n"
        "assert not bad, bad\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert proc.returncode == 0, proc.stderr
