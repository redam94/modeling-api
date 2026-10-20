"""Run protocol: event HMACs, stdout wire format, RunSpec, manifests, env and exit codes."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_spec import Catalog
from mapi_spec.capabilities import Diagnostic
from mapi_spec.errors import ErrorCode
from mapi_spec.resources import ResourceClass
from mapi_spec.run import (
    ENV_BOOTSTRAP_URL,
    ENV_EXEC_TOKEN,
    ENV_NONCE,
    ENV_RUN_ID,
    EVENT_PAYLOAD_MODELS,
    LOGGING_FILTER,
    MAX_EVENT_LINE_BYTES,
    STDOUT_EVENT_KEY,
    BundleManifest,
    CheckpointPayload,
    CompletePayload,
    ErrorPayload,
    FailureClass,
    InvalidEvent,
    LogPayload,
    MetricBatchPayload,
    MetricPayload,
    MetricSeriesBatch,
    OutputManifest,
    ParamItem,
    ParamsPayload,
    ProgressPayload,
    RunEvent,
    RunEventType,
    RunKind,
    RunLimits,
    RunnerEnv,
    RunnerExit,
    RunnerPhase,
    RunSpec,
    StatePayload,
    TagItem,
    TagsPayload,
    WarningPayload,
    canonical_bytes,
    catalog_digest,
    compute_event_hmac,
    decode_event_line,
    decode_hmac_key,
    encode_event_line,
    event_from_log_payload,
    failure_class_for_exit,
    make_event,
    new_hmac_key,
    new_nonce,
    parse_payload,
    sign_bundle_manifest,
    sign_event,
    sign_output_manifest,
    verify_bundle_manifest,
    verify_event,
    verify_output_manifest,
    verify_output_objects,
    worker_environ,
)
from mapi_spec.storage import ObjectMeta, crc32c, crc32c_to_base64, md5_base64
from pydantic import ValidationError

KEY = new_hmac_key()
TS = datetime(2026, 10, 15, 10, 0, 0, 123456, tzinfo=UTC)


def _event(**kw: Any) -> RunEvent:
    base: dict[str, Any] = {
        "run_id": "run_01",
        "attempt": 1,
        "seq": 7,
        "type": RunEventType.PROGRESS,
        "ts": TS,
        "payload": {"phase": "sample", "fraction": 0.5, "chains": []},
    }
    base.update(kw)
    return RunEvent(**base)


# ------------------------------------------------------------------------------------ HMAC


def test_hmac_key_generation_and_decoding() -> None:
    raw = decode_hmac_key(KEY)
    assert len(raw) == 32 and new_hmac_key() != KEY
    with pytest.raises(ValueError, match="base64"):
        decode_hmac_key("not base64!")
    with pytest.raises(ValueError, match="32 bytes"):
        decode_hmac_key(base64.b64encode(b"short").decode())


def test_sign_and_verify_event() -> None:
    ev = sign_event(_event(), KEY)
    assert ev.hmac is not None and len(ev.hmac) == 64
    assert verify_event(ev, KEY)
    assert verify_event(ev, decode_hmac_key(KEY))  # bytes or base64 key
    assert verify_event(ev.model_copy(update={"hmac": ev.hmac.upper()}), KEY)
    assert not verify_event(_event(), KEY)  # unsigned
    assert not verify_event(ev, new_hmac_key())  # wrong key


@pytest.mark.parametrize(
    "update",
    [
        {"run_id": "run_02"},
        {"attempt": 2},
        {"seq": 8},
        {"type": RunEventType.LOG},
        {"ts": TS + timedelta(microseconds=1)},
        {"payload": {"phase": "sample", "fraction": 0.51, "chains": []}},
        {"payload": {"phase": "sample", "fraction": 0.5, "chains": [], "x": 1}},
        {"payload": {}},
    ],
)
def test_tampering_any_field_breaks_the_hmac(update: dict[str, Any]) -> None:
    ev = sign_event(_event(), KEY)
    assert not verify_event(ev.model_copy(update=update), KEY)


def test_canonical_bytes_are_order_and_float_insensitive() -> None:
    a = canonical_bytes({"b": 1.0, "a": [1, {"z": 2, "y": 3.5}]})
    b = canonical_bytes({"a": [1, {"y": 3.5, "z": 2}], "b": 1})
    assert a == b == b'{"a":[1,{"y":3.5,"z":2}],"b":1}'
    assert canonical_bytes({"a": 1, "hmac": "x"}, exclude=("hmac",)) == b'{"a":1}'
    # defaults are part of what is signed (no default-dropping ambiguity)
    assert b'"hmac"' not in canonical_bytes(_event(), exclude=("hmac",))
    assert b'"attempt":1' in canonical_bytes(_event())


def test_timestamps_are_normalised_to_utc_before_signing() -> None:
    plus2 = timezone(timedelta(hours=2))
    ev = _event(ts=TS.astimezone(plus2))
    assert ev.ts.tzinfo is UTC and ev.ts == TS
    assert compute_event_hmac(ev, KEY) == compute_event_hmac(_event(), KEY)
    with pytest.raises(ValidationError, match="timezone-aware"):
        _event(ts=datetime(2026, 1, 1))


# ------------------------------------------------------------------------------ wire format


def test_stdout_line_is_a_cloud_logging_json_payload() -> None:
    ev = sign_event(_event(), KEY)
    line = encode_event_line(ev)
    assert "\n" not in line
    payload = json.loads(line)
    assert list(payload) == [STDOUT_EVENT_KEY] and LOGGING_FILTER.startswith("jsonPayload.")
    assert payload["mapi_event"]["seq"] == 7
    back = decode_event_line(line)
    assert back == ev and verify_event(back, KEY)
    assert decode_event_line(line.encode()) == ev
    assert event_from_log_payload(payload) == ev


@settings(max_examples=60, deadline=None)
@given(
    payload=st.dictionaries(
        st.text(min_size=1, max_size=8),
        st.recursive(
            st.none()
            | st.booleans()
            | st.integers(-(2**53), 2**53)
            | st.floats(allow_nan=False, allow_infinity=False)
            | st.text(max_size=20),
            lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(max_size=5), c, max_size=3),
            max_leaves=10,
        ),
        max_size=5,
    ),
    seq=st.integers(0, 2**40),
    micro=st.integers(0, 999_999),
)
def test_signature_survives_the_stdout_round_trip(
    payload: dict[str, Any], seq: int, micro: int
) -> None:
    ev = sign_event(_event(payload=payload, seq=seq, ts=TS.replace(microsecond=micro)), KEY)
    back = decode_event_line(encode_event_line(ev))
    assert back is not None and verify_event(back, KEY)


def test_non_event_lines_are_ignored_and_bad_events_rejected() -> None:
    assert decode_event_line("plain text") is None
    assert decode_event_line('{"message": "hi"}') is None
    assert decode_event_line("[1, 2]") is None
    with pytest.raises(InvalidEvent) as info:
        decode_event_line('{"mapi_event": {"run_id": "r"}}')
    assert info.value.code is ErrorCode.SANDBOX_VIOLATION
    with pytest.raises(InvalidEvent):
        decode_event_line('{"mapi_event": 3}')
    with pytest.raises(InvalidEvent, match="exceeds"):
        decode_event_line("x" * (MAX_EVENT_LINE_BYTES + 1))
    with pytest.raises(ValueError, match="exceeds"):
        encode_event_line(_event(payload={"line": "x" * MAX_EVENT_LINE_BYTES}))


# --------------------------------------------------------------------------------- payloads

PAYLOADS = [
    StatePayload(phase=RunnerPhase.FIT),
    ProgressPayload(phase=RunnerPhase.SAMPLE, fraction=0.25, chains=[]),
    LogPayload(stream="stderr", line="warning: x"),
    Diagnostic(code="RHAT_HIGH", message_expert="r-hat 1.1", message_plain="Chains disagree"),
    WarningPayload(code="SLOW_SAMPLING", message="sampling is slow"),
    CheckpointPayload(name="seg-0.zarr.zip", size=10, crc32c="AAAAAA==", segment=0),
    MetricPayload(name="mape", value=0.12),
    CompletePayload(
        status="complete",
        manifest_name="t/default/runs/r/a1/manifest.json",
        manifest_crc32c="AAAAAA==",
        objects=3,
        total_bytes=100,
    ),
    ErrorPayload(
        code=ErrorCode.USER_CODE_ERROR, failure_class=FailureClass.USER, message="model failed"
    ),
    MetricBatchPayload(
        t0=datetime(2026, 10, 15, tzinfo=UTC),
        series=[MetricSeriesBatch(key="loss", steps=[1, 2], values=[0.5, 0.4], ts_ms=[0, 10])],
    ),
    ParamsPayload(params=[ParamItem(key="fit.draws", value=1000)]),
    TagsPayload(tags=[TagItem(key="stage", value="candidate")]),
]


def test_every_event_type_has_a_payload_model() -> None:
    assert set(EVENT_PAYLOAD_MODELS) == set(RunEventType)
    assert {type(p) for p in PAYLOADS} == set(EVENT_PAYLOAD_MODELS.values())


@pytest.mark.parametrize("payload", PAYLOADS, ids=lambda p: type(p).__name__)
def test_make_event_infers_type_and_parses_back(payload: Any) -> None:
    ev = make_event("run_01", 1, 0, payload, ts=TS, key=KEY)
    assert EVENT_PAYLOAD_MODELS[ev.type] is type(payload)
    assert verify_event(ev, KEY)
    assert parse_payload(ev) == payload


def test_make_event_rejects_foreign_payloads_and_error_codes_must_match() -> None:
    with pytest.raises(TypeError):
        make_event("r", 1, 0, RunLimits(max_output_bytes=1))
    assert make_event("r", 1, 0, StatePayload(phase=RunnerPhase.DONE)).hmac is None
    with pytest.raises(ValidationError, match="does not match"):
        ErrorPayload(code=ErrorCode.TIMEOUT, failure_class=FailureClass.USER, message="x")


# ---------------------------------------------------------------------------------- RunSpec


def _runspec(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = RunSpec.model_json_schema()["examples"][0]
    data = json.loads(json.dumps(data))
    data.update(kw)
    return data


def test_runspec_example_round_trips_and_hides_the_key() -> None:
    rs = RunSpec.model_validate(_runspec())
    assert RunSpec.model_validate_json(rs.model_dump_json()) == rs
    assert rs.hmac_key not in repr(rs)
    red = json.dumps(rs.redacted())
    assert rs.hmac_key not in red and "X-Goog" not in red and "e30=" not in red
    assert len(rs.hmac_key_bytes()) == 32


@pytest.mark.parametrize(
    ("update", "match"),
    [
        ({"nonce": "abc"}, "nonce"),
        ({"hmac_key": "AAAA"}, "32 bytes"),
        ({"deadline": "2026-10-15T09:00:00Z"}, "deadline"),
        ({"kind": "sweep"}, "parents"),
        ({"outputs": {}}, "manifest"),
        ({"checkpoint_prefix": "no/slash"}, "end with"),
        ({"spare": 1}, "Extra inputs"),
    ],
)
def test_runspec_rejects_invalid_bootstrap_objects(update: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        RunSpec.model_validate(_runspec(**update))


def test_runspec_checkpoint_output_must_match_prefix() -> None:
    data = _runspec(checkpoint_prefix="t/default/runs/r/a1/checkpoints/")
    artifacts = dict(data["outputs"]["artifacts"])
    data["outputs"]["checkpoints"] = {**artifacts, "prefix": "t/default/runs/r/a1/other/"}
    with pytest.raises(ValidationError, match="checkpoint_prefix"):
        RunSpec.model_validate(data)
    data["outputs"]["checkpoints"]["prefix"] = "t/default/runs/r/a1/checkpoints/"
    assert RunSpec.model_validate(data).checkpoint_prefix is not None


def test_limits_follow_the_resource_class_table() -> None:
    lim = RunLimits.for_class(ResourceClass.M)
    assert lim.scratch_bytes == 6 * 1024**3 and lim.max_output_bytes == 4 * 1000**3
    assert lim.log_line_bytes == 4096 and lim.log_total_bytes == 20 * 1024 * 1024
    assert RunLimits.for_class(ResourceClass.S, log_line_bytes=1024).log_line_bytes == 1024
    assert RunLimits.for_class(ResourceClass.XL).scratch_bytes is None


def test_nonces_are_256_bit_hex() -> None:
    n = new_nonce()
    assert len(n) == 64 and int(n, 16) >= 0 and n != new_nonce()


# -------------------------------------------------------------------------------- manifests

PREFIX = "t/default/runs/run_01/a1/"


def _blob(data: bytes) -> dict[str, Any]:
    return {"size": len(data), "crc32c": crc32c_to_base64(crc32c(data)), "md5": md5_base64(data)}


def _manifest(**objects: bytes) -> OutputManifest:
    return OutputManifest(
        tenant_id="default",
        project_id="default",
        run_id="run_01",
        attempt=1,
        kind=RunKind.FIT,
        status="complete",
        prefix=PREFIX,
        spec_hash="sha256:" + "a" * 64,
        catalog_digest="sha256:" + "b" * 64,
        objects={
            n.replace("__", "/"): {"key": PREFIX + n.replace("__", "/"), **_blob(d)}
            for n, d in objects.items()
        },
        runner_version="0.2.0",
        created_at=TS,
    )


def _meta(name: str, data: bytes, *, md5: bool = True, gen: int = 5) -> ObjectMeta:
    return ObjectMeta(
        bucket="mapi-local-runs",
        name=name,
        generation=gen,
        size=len(data),
        crc32c=crc32c_to_base64(crc32c(data)),
        md5_hash=md5_base64(data) if md5 else None,
        time_created=TS,
    )


def test_output_manifest_keys_must_match_the_prefix() -> None:
    m = _manifest(bundle=b"x")
    assert m.manifest_key == PREFIX + "manifest.json" and m.total_bytes == 1
    bad = m.model_dump()
    bad["objects"]["bundle"]["key"] = "t/default/runs/other/a1/bundle"
    with pytest.raises(ValidationError, match="prefix"):
        OutputManifest.model_validate(bad)
    selfref = m.model_dump()
    selfref["objects"] = {"manifest.json": {**bad["objects"]["bundle"], "key": m.manifest_key}}
    with pytest.raises(ValidationError, match="itself"):
        OutputManifest.model_validate(selfref)
    traversal = m.model_dump()
    traversal["objects"] = {"../x": {**bad["objects"]["bundle"], "key": PREFIX + "../x"}}
    with pytest.raises(ValidationError):
        OutputManifest.model_validate(traversal)


def test_output_manifest_hmac() -> None:
    m = sign_output_manifest(_manifest(bundle=b"x"), KEY)
    assert verify_output_manifest(m, KEY)
    assert not verify_output_manifest(m.model_copy(update={"status": "partial"}), KEY)
    assert not verify_output_manifest(m, new_hmac_key())
    assert not verify_output_manifest(_manifest(bundle=b"x"), KEY)
    # a manifest HMAC is not a valid event HMAC for the same key (domain separation)
    ev = _event()
    assert compute_event_hmac(ev, KEY) != m.hmac


def test_verify_output_objects_accepts_a_matching_listing() -> None:
    m = _manifest(bundle=b"abc", ui__summaries_json=b"{}")
    listing = [
        _meta(PREFIX + "bundle", b"abc"),
        _meta(PREFIX + "ui/summaries_json", b"{}"),
        _meta(PREFIX + "manifest.json", b"..."),
        _meta(PREFIX + "checkpoints/seg-0", b"ckpt"),
    ]
    res = verify_output_objects(m, listing, expected_prefix=PREFIX)
    assert res.ok, res.issues
    assert set(res.verified) == {"bundle", "ui/summaries_json"}
    assert res.verified["bundle"].generation == 5


@pytest.mark.parametrize(
    ("listing", "fragment"),
    [
        ([], "does not exist"),
        ([_meta(PREFIX + "bundle", b"abcd")], "size mismatch"),
        ([_meta(PREFIX + "bundle", b"abd")], "crc32c mismatch"),
        ([_meta(PREFIX + "bundle", b"abc", md5=False)], "md5 mismatch"),
        ([_meta(PREFIX + "bundle", b"abc"), _meta(PREFIX + "evil.py", b"x")], "unexpected"),
        ([_meta(PREFIX + "bundle", b"abc"), _meta("t/default/runs/x/a1/y", b"x")], "outside"),
    ],
)
def test_verify_output_objects_flags_integrity_failures(
    listing: list[ObjectMeta], fragment: str
) -> None:
    res = verify_output_objects(_manifest(bundle=b"abc"), listing, expected_prefix=PREFIX)
    assert not res.ok
    assert all(i.code is ErrorCode.INTEGRITY_FAILURE for i in res.issues)
    assert any(fragment in i.message for i in res.issues), res.issues


def test_verify_output_objects_rejects_a_foreign_prefix() -> None:
    res = verify_output_objects(_manifest(bundle=b"a"), [], expected_prefix="t/default/runs/x/a1/")
    assert [i.pointer for i in res.issues] == ["/prefix"] and not res.verified


def test_bundle_manifest_signature() -> None:
    example = BundleManifest.model_json_schema()["examples"][0]
    m = BundleManifest.model_validate({**example, "signature": None})
    assert not verify_bundle_manifest(m, {"local-1": KEY})
    signed = sign_bundle_manifest(m, KEY, "local-1")
    assert verify_bundle_manifest(signed, {"local-1": KEY})
    assert not verify_bundle_manifest(signed, {"other": KEY})
    assert not verify_bundle_manifest(signed, {"local-1": new_hmac_key()})
    obj = signed.objects["bundle.zarr.zip"].model_copy(update={"generation": 2})
    swapped = signed.model_copy(update={"objects": {"bundle.zarr.zip": obj}})
    assert not verify_bundle_manifest(swapped, {"local-1": KEY})
    kms = signed.model_copy(
        update={"signature": signed.signature.model_copy(update={"alg": "KMS-EC-P256-SHA256"})}  # type: ignore[union-attr]
    )
    with pytest.raises(NotImplementedError, match="KMS"):
        verify_bundle_manifest(kms, {"local-1": KEY})


def test_catalog_digest_is_stable(catalog: Catalog) -> None:
    d = catalog_digest(catalog)
    assert d.startswith("sha256:") and d == catalog_digest(
        Catalog.model_validate_json(catalog.model_dump_json())
    )
    assert catalog_digest(Catalog()) != d


# ------------------------------------------------------------------------ env and exit codes


def test_runner_env_round_trip() -> None:
    nonce = new_nonce()
    env = {
        ENV_RUN_ID: "run_01",
        ENV_NONCE: nonce,
        ENV_BOOTSTRAP_URL: "http://127.0.0.1:4443/storage/v1/b/mapi-local-bootstrap/o/{nonce}",
        ENV_EXEC_TOKEN: "local-exec",
        "PATH": "/usr/bin",
    }
    re = RunnerEnv.from_environ(env)
    assert re.resolved_bootstrap_url().endswith("/o/" + nonce)
    assert re.to_environ() == {k: v for k, v in env.items() if k.startswith("MAPI_")}
    assert "local-exec" not in repr(re)
    cloud = RunnerEnv(run_id="r", nonce=nonce, bootstrap_url="gs://mapi-prod-bootstrap/{nonce}")
    assert ENV_EXEC_TOKEN not in cloud.to_environ()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/{nonce}",
        "http://x/no-placeholder",
        "http://x/{nonce}/{nonce}",
        "gs://b/{x}{nonce}",
    ],
)
def test_runner_env_rejects_bad_templates(url: str) -> None:
    with pytest.raises(ValidationError):
        RunnerEnv(run_id="r", nonce=new_nonce(), bootstrap_url=url)


def test_runner_env_names_missing_variables() -> None:
    with pytest.raises(ValueError, match="MAPI_NONCE, MAPI_BOOTSTRAP_URL"):
        RunnerEnv.from_environ({ENV_RUN_ID: "r"})


def test_worker_environment_is_scrubbed() -> None:
    parent = {"PATH": "/bin", "MAPI_NONCE": "x", "MAPI_EXEC_TOKEN": "t", "AWS_SECRET": "s"}
    assert worker_environ(parent) == {"PATH": "/bin"}


def test_exit_codes_classify_failures() -> None:
    expected = {
        RunnerExit.OK: None,
        RunnerExit.PARTIAL: None,
        RunnerExit.CONFIG_ERROR: FailureClass.INFRA,
        RunnerExit.USER_ERROR: FailureClass.USER,
        RunnerExit.RESOURCE_EXHAUSTED: FailureClass.RESOURCE_EXHAUSTED,
        RunnerExit.INFRA_ERROR: FailureClass.INFRA,
        RunnerExit.INTEGRITY_ERROR: FailureClass.INTEGRITY,
        RunnerExit.TIMEOUT: FailureClass.TIMEOUT,
        RunnerExit.SANDBOX_VIOLATION: FailureClass.SANDBOX,
        RunnerExit.KILLED: FailureClass.RESOURCE_EXHAUSTED,
        RunnerExit.TERMINATED: FailureClass.TIMEOUT,
    }
    assert set(expected) == set(RunnerExit)
    for code, fc in expected.items():
        assert failure_class_for_exit(int(code)) is fc
    assert failure_class_for_exit(1) is FailureClass.INFRA
    assert failure_class_for_exit(-9) is FailureClass.INFRA


def test_run_kind_parents() -> None:
    assert {k for k in RunKind if k.is_parent} == {
        RunKind.SWEEP,
        RunKind.BACKTEST,
        RunKind.SBC,
        RunKind.CAMPAIGN,
        RunKind.WORKFLOW,  # M3.5: the orchestrator walks the DAG
    }
