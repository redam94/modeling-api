"""The real supervisor + worker processes against the local GCS shim."""

from __future__ import annotations

import json
import signal
import subprocess
import threading

import pytest
from mapi_spec.catalog import Catalog
from mapi_spec.executor import RunEvent, RunEventType, RunKind
from mapi_spec.run import (
    CompletePayload,
    ErrorPayload,
    FailureClass,
    LogPayload,
    RunnerExit,
    StatePayload,
    WarningPayload,
    decode_event_line,
    failure_class_for_exit,
    verify_event,
    verify_output_manifest,
    verify_output_objects,
)
from runner_helpers import Result, assert_signed_stream, make_run, run_supervisor
from shimgcs import ShimGcs

pytestmark = pytest.mark.integration

TESTING = "mapi_runner.testing"


def _error(result: Result) -> ErrorPayload:
    errors = result.of(RunEventType.ERROR)
    assert len(errors) == 1, result.stderr
    return ErrorPayload.model_validate(errors[0].payload)


def _verify_outputs(result: Result) -> None:
    manifest = result.manifest()
    assert verify_output_manifest(manifest, result.run.key)
    listing = result.store.list(result.store.buckets.runs, result.run.prefix)
    check = verify_output_objects(manifest, listing, expected_prefix=result.run.prefix)
    assert check.ok, check.issues
    complete = CompletePayload.model_validate(result.of(RunEventType.COMPLETE)[-1].payload)
    assert complete.manifest_name == result.run.prefix + "manifest.json"
    assert complete.objects == len(manifest.objects)


# ===================================================================== bootstrap


def test_bootstrap_is_read_then_deleted_before_the_worker_starts(gcs: ShimGcs) -> None:
    run = make_run(gcs, RunKind.DESCRIBE, registry_spec := {})
    del registry_spec
    before = len(gcs.requests)
    result = run_supervisor(run, registry=f"{TESTING}:empty_registry")
    by_runner = gcs.requests[before:]
    assert result.code == RunnerExit.OK, result.stderr
    assert not gcs.exists(gcs.buckets.bootstrap, run.rs.nonce)
    boot = [r for r in by_runner if gcs.buckets.bootstrap in r.path]
    assert [(r.method, r.status) for r in boot] == [("GET", 200), ("DELETE", 204)]
    assert_signed_stream(result)
    first = result.events[0]
    assert first.type is RunEventType.STATE
    assert StatePayload.model_validate(first.payload).phase.value == "bootstrap"


def test_missing_bootstrap_is_a_config_error_with_no_events(gcs: ShimGcs) -> None:
    run = make_run(gcs, RunKind.DESCRIBE, {})
    gcs.delete(gcs.buckets.bootstrap, run.rs.nonce)
    result = run_supervisor(run)
    assert result.code == RunnerExit.CONFIG_ERROR
    assert result.lines == []
    assert "bootstrap" in result.stderr
    assert failure_class_for_exit(result.code) is FailureClass.INFRA


def test_bad_environment_is_a_config_error(gcs: ShimGcs) -> None:
    run = make_run(gcs, RunKind.DESCRIBE, {})
    result = run_supervisor(run, env={"MAPI_NONCE": "not-a-nonce"})
    assert result.code == RunnerExit.CONFIG_ERROR
    assert result.lines == []
    assert gcs.exists(gcs.buckets.bootstrap, run.rs.nonce)  # never touched


# ====================================================================== describe


def test_describe_exports_the_catalog(gcs: ShimGcs) -> None:
    run = make_run(gcs, RunKind.DESCRIBE, {})
    result = run_supervisor(run)
    assert result.code == RunnerExit.OK, result.stderr
    assert_signed_stream(result)
    _verify_outputs(result)
    catalog = Catalog.model_validate_json(result.output("catalog.json"))
    ids = {m.manifest.id for m in catalog.models}
    assert {"tabular.regressor", "tabular.hierarchical_glm"} <= ids
    assert any(s.id == "core.resample" for s in catalog.steps)
    assert any(p.id == "core.ppc_fan" for p in catalog.plots)
    info = json.loads(result.output("describe.json"))
    assert info["catalog_digest"].startswith("sha256:")
    complete = result.of(RunEventType.COMPLETE)
    assert len(complete) == 1 and complete[0] is result.events[-2]  # then state "done"


# ===================================================================== tampering


def test_worker_stdout_cannot_forge_events(gcs: ShimGcs) -> None:
    run = make_run(gcs, RunKind.DESCRIBE, {})
    result = run_supervisor(run, registry=f"{TESTING}:forging_registry")
    assert result.code == RunnerExit.OK, result.stderr
    assert_signed_stream(result)
    logs = [LogPayload.model_validate(e.payload) for e in result.of(RunEventType.LOG)]
    forged = [entry for entry in logs if "mapi_event" in entry.line]
    assert len(forged) == 3  # print, raw fd 1 and stderr: all wrapped as log text
    assert {entry.stream for entry in forged} == {"stdout", "stderr"}
    for entry in forged:
        inner = decode_event_line(entry.line)
        assert inner is not None and not verify_event(inner, run.key)
    cleaned = [entry.line for entry in logs if "red text" in entry.line]
    assert cleaned == ["red text with  controls"]  # ANSI and BEL stripped
    # exactly one completion, and it is the supervisor's own
    assert len(result.of(RunEventType.COMPLETE)) == 1


def test_a_tampered_event_line_fails_verification(gcs: ShimGcs) -> None:
    run = make_run(gcs, RunKind.DESCRIBE, {})
    result = run_supervisor(run, registry=f"{TESTING}:empty_registry")
    line = next(ln for ln in result.lines if json.loads(ln)["mapi_event"]["type"] == "complete")
    doc = json.loads(line)
    doc["mapi_event"]["payload"]["objects"] += 1
    tampered = decode_event_line(json.dumps(doc))
    assert tampered is not None and not verify_event(tampered, run.key)
    other_key = make_run(gcs, RunKind.DESCRIBE, {}).key
    original = decode_event_line(line)
    assert original is not None
    assert verify_event(original, run.key) and not verify_event(original, other_key)


def test_log_flood_is_a_sandbox_violation(gcs: ShimGcs) -> None:
    run = make_run(gcs, RunKind.DESCRIBE, {}, limits={"log_total_bytes": 256 * 1024})
    result = run_supervisor(run, registry=f"{TESTING}:flooding_registry")
    assert result.code == RunnerExit.SANDBOX_VIOLATION, result.stderr
    assert_signed_stream(result)
    err = _error(result)
    assert err.failure_class is FailureClass.SANDBOX and "log_flood" in err.message
    assert not gcs.exists(gcs.buckets.runs, run.prefix + "manifest.json")
    logged = sum(len(LogPayload.model_validate(e.payload).line) for e in result.of("log"))
    assert logged <= 256 * 1024


def test_log_lines_are_capped(gcs: ShimGcs) -> None:
    run = make_run(gcs, RunKind.DESCRIBE, {}, limits={"log_line_bytes": 256})
    result = run_supervisor(run, registry=f"{TESTING}:flooding_registry")
    # the flood registry also overflows 20 MB; every line event stays within the line cap
    for e in result.of(RunEventType.LOG):
        p = LogPayload.model_validate(e.payload)
        assert len(p.line.encode()) <= 256
        if p.stream == "stderr" and p.line.startswith("xxxx"):
            assert p.truncated and len(p.line) == 256


def test_symlinked_output_is_a_sandbox_violation(gcs: ShimGcs) -> None:
    run = make_run(gcs, RunKind.DESCRIBE, {})
    result = run_supervisor(run, registry=f"{TESTING}:symlink_output_registry")
    assert result.code == RunnerExit.SANDBOX_VIOLATION, result.stderr
    assert "regular file" in _error(result).message
    assert gcs.list(gcs.buckets.runs, run.prefix) == []


def test_malformed_channel_message_is_a_sandbox_violation(gcs: ShimGcs) -> None:
    run = make_run(gcs, RunKind.DESCRIBE, {})
    result = run_supervisor(run, registry=f"{TESTING}:garbage_channel_registry")
    assert result.code == RunnerExit.SANDBOX_VIOLATION, result.stderr
    assert "invalid worker message" in _error(result).message


def test_worker_crash_without_a_result_is_a_user_failure(gcs: ShimGcs) -> None:
    run = make_run(gcs, RunKind.DESCRIBE, {})
    result = run_supervisor(run, registry=f"{TESTING}:crashing_registry")
    assert result.code == RunnerExit.USER_ERROR
    assert "status 7" in _error(result).message


def test_worker_claimed_infra_error_is_not_a_retryable_infra_exit(gcs: ShimGcs) -> None:
    """Only failures the supervisor itself observes may trigger the platform's infra
    auto-retry; an untrusted worker reporting INFRA_ERROR fails the run as a user error."""
    run = make_run(gcs, RunKind.DESCRIBE, {})
    result = run_supervisor(run, registry=f"{TESTING}:infra_claiming_registry")
    assert result.code == RunnerExit.USER_ERROR, result.stderr
    assert failure_class_for_exit(result.code) is FailureClass.USER
    err = _error(result)
    assert err.failure_class is FailureClass.USER
    assert "INFRA_ERROR" in err.message and "reported by the worker" in err.message


_SPOOF_CHARS = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\x1b\x07")


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def test_worker_text_is_stripped_of_bidi_and_control_characters(gcs: ShimGcs) -> None:
    run = make_run(gcs, RunKind.DESCRIBE, {})
    result = run_supervisor(run, registry=f"{TESTING}:spoofing_registry")
    assert result.code == RunnerExit.USER_ERROR, result.stderr
    assert_signed_stream(result)
    kinds = {e.type for e in result.events}
    assert {
        RunEventType.STATE,
        RunEventType.PROGRESS,
        RunEventType.WARNING,
        RunEventType.DIAGNOSTIC,
        RunEventType.METRIC,
        RunEventType.ERROR,
    } <= kinds
    seen = [s for e in result.events for s in _strings(e.payload)]
    assert any("safetxt.exe" in s for s in seen), seen  # the text itself survives
    dirty = [s for s in seen if _SPOOF_CHARS & set(s)]
    assert not dirty, dirty
    err = _error(result)
    assert err.untrusted_text is not None and "\nline 2" in err.untrusted_text


def test_invalid_job_spec_fails_as_user_error(gcs: ShimGcs) -> None:
    run = make_run(gcs, RunKind.FIT, {"apiVersion": "mapi/v1", "kind": "ModelSpec"})
    result = run_supervisor(run, registry=f"{TESTING}:empty_registry")
    assert result.code == RunnerExit.USER_ERROR
    err = _error(result)
    assert err.failure_class is FailureClass.USER
    assert "SCHEMA_INVALID" in err.message


def test_input_hash_mismatch_is_an_integrity_failure(gcs: ShimGcs) -> None:
    gcs.put(gcs.buckets.data, "t/default/x/data.zarr.zip", b"not what was promised")
    run = make_run(
        gcs,
        RunKind.FIT,
        {
            "apiVersion": "mapi/v1",
            "kind": "ModelSpec",
            "metadata": {"name": "x", "project": "p"},
            "data": {"dataset": "dataset://p/x@v1"},
            "model": {"use": "tabular.regressor@^1"},
        },
        inputs={"dataset": (gcs.buckets.data, "t/default/x/data.zarr.zip")},
        input_overrides={"dataset": {"crc32c": "AAAAAA=="}},
    )
    result = run_supervisor(run, registry=f"{TESTING}:empty_registry")
    assert result.code == RunnerExit.INTEGRITY_ERROR, result.stderr
    assert _error(result).failure_class is FailureClass.INTEGRITY


# ================================================================== termination


def _term_on_first(kind: RunEventType, *, delay: float = 0.0) -> object:
    sent: list[int] = []

    def on_event(ev: RunEvent, proc: subprocess.Popen[bytes]) -> None:
        if ev.type is kind and not sent:
            sent.append(1)
            threading.Timer(delay, proc.send_signal, args=(signal.SIGTERM,)).start()

    return on_event


def test_sigterm_without_checkpoints_exits_terminated(gcs: ShimGcs) -> None:
    run = make_run(gcs, RunKind.DESCRIBE, {})
    result = run_supervisor(
        run,
        registry=f"{TESTING}:sleeping_registry",
        args=("--term-grace", "1"),
        # the worker is up and sleeping; it ignores the forwarded SIGTERM and is killed
        on_event=_term_on_first(RunEventType.STATE, delay=2.0),  # type: ignore[arg-type]
    )
    assert result.code == RunnerExit.TERMINATED, result.stderr
    assert_signed_stream(result)
    warn = [WarningPayload.model_validate(e.payload) for e in result.of("warning")]
    assert any(w.code == "RUN_TERMINATING" for w in warn)
    assert _error(result).failure_class is FailureClass.TIMEOUT


def test_deadline_without_checkpoints_is_a_timeout(gcs: ShimGcs) -> None:
    run = make_run(gcs, RunKind.DESCRIBE, {}, deadline_s=4)
    result = run_supervisor(
        run,
        registry=f"{TESTING}:sleeping_registry",
        args=("--term-grace", "1", "--deadline-reserve", "1"),
    )
    assert result.code == RunnerExit.TIMEOUT, result.stderr
    assert _error(result).failure_class is FailureClass.TIMEOUT
