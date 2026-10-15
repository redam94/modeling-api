"""The run protocol (plan §8.1, §8.3–§8.7): state machine, bootstrap, events, manifests.

Everything a control plane, an executor and a runner must agree on lives here, as pure
Pydantic / stdlib code:

* :class:`RunState`, :class:`RunTrigger` and the transition table :data:`TRANSITIONS`, with
  the pure function :func:`transition` used by the API, dispatcher, events service,
  reconciler, finalizer and tests;
* :class:`RunSpec` — the bootstrap object written to ``bootstrap/<nonce>`` before an
  execution starts (signed URLs, a per-run HMAC key, limits, deadline);
* :class:`~mapi_spec.executor.RunEvent` payload models, canonical HMAC-SHA256 signing and
  verification, and the stdout JSONL wire format (``{"mapi_event": {...}}`` per line);
* :class:`OutputManifest` (the runner's *claim*), metadata-only verification against
  server-computed hashes, and the platform-signed :class:`BundleManifest`;
* the runner invocation contract: environment (:class:`RunnerEnv`) and exit codes
  (:class:`RunnerExit`).

Executor-facing shapes (:class:`Isolation`, :class:`ExecutorCaps`, :class:`Handle`,
:class:`ExecStatus`, :class:`RunKind`, :class:`RunEvent`, ...) are defined in
:mod:`mapi_spec.executor` and re-exported here unchanged, so both import paths name the
same classes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac as _hmac
import json
import re
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from mapi_spec._types import MetricName, StrictModel
from mapi_spec.capabilities import Diagnostic
from mapi_spec.catalog import Catalog
from mapi_spec.errors import ErrorCode, Issue, MapiError, SandboxViolation
from mapi_spec.executor import (
    ExecState,
    ExecStatus,
    Executor,
    ExecutorCaps,
    Handle,
    Isolation,
    RunEvent,
    RunEventType,
    RunKind,
    RunSubmit,
)
from mapi_spec.loader import canonical_value, spec_hash
from mapi_spec.resources import RESOURCE_CLASSES, ResourceClass
from mapi_spec.storage import (
    Crc32c,
    Md5,
    ObjectMeta,
    ObjectName,
    PostPolicy,
    SignedGet,
    SignedPut,
    check_object_name,
)

__all__ = [
    "BUNDLE_MANIFEST_SCHEMA",
    "ENV_BOOTSTRAP_URL",
    "ENV_EXEC_TOKEN",
    "ENV_NONCE",
    "ENV_REQUIRE_ISOLATION",
    "ENV_RUN_ID",
    "EVENT_PAYLOAD_MODELS",
    "INPUT_BUNDLE",
    "INPUT_DATASET",
    "LOGGING_FILTER",
    "MANIFEST_OBJECT_NAME",
    "MAX_EVENT_LINE_BYTES",
    "OUTPUT_ARTIFACTS",
    "OUTPUT_CHECKPOINTS",
    "OUTPUT_MANIFEST",
    "OUTPUT_MANIFEST_SCHEMA",
    "RUNNER_ENV_VARS",
    "RUNSPEC_SCHEMA",
    "STDOUT_EVENT_KEY",
    "TRANSITIONS",
    "WORKER_ENV_ALLOWLIST",
    "Actor",
    "BundleManifest",
    "BundleObject",
    "ChainProgress",
    "CheckpointPayload",
    "CompletePayload",
    "ErrorPayload",
    "ExecState",
    "ExecStatus",
    "Executor",
    "ExecutorCaps",
    "FailureClass",
    "Handle",
    "IllegalTransition",
    "InputDigest",
    "InvalidEvent",
    "Isolation",
    "LogPayload",
    "ManifestObject",
    "ManifestSignature",
    "MetricPayload",
    "OutputManifest",
    "OutputTarget",
    "ProgressPayload",
    "RunEvent",
    "RunEventType",
    "RunKind",
    "RunLimits",
    "RunSpec",
    "RunState",
    "RunSubmit",
    "RunTrigger",
    "RunnerEnv",
    "RunnerExit",
    "RunnerPhase",
    "StatePayload",
    "TransitionRule",
    "VerificationResult",
    "WarningPayload",
    "allowed_triggers",
    "bundle_manifest_signing_bytes",
    "can_transition",
    "canonical_bytes",
    "catalog_digest",
    "compute_event_hmac",
    "decode_event_line",
    "decode_hmac_key",
    "encode_event_line",
    "event_from_log_payload",
    "event_signing_bytes",
    "failure_class_for_exit",
    "make_event",
    "new_hmac_key",
    "new_nonce",
    "output_manifest_signing_bytes",
    "parse_payload",
    "sign_bundle_manifest",
    "sign_event",
    "sign_output_manifest",
    "transition",
    "verify_bundle_manifest",
    "verify_event",
    "verify_output_manifest",
    "verify_output_objects",
    "worker_environ",
]

_RUN_ID = r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$"
_NONCE = r"^[0-9a-f]{64}$"
_SHA256 = r"^sha256:[0-9a-f]{64}$"
_IO_NAME = r"^[a-z][a-z0-9_.\-]{0,63}$"
_TENANT = r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$"

IoName = Annotated[str, Field(pattern=_IO_NAME)]
TenantId = Annotated[str, Field(pattern=_TENANT, examples=["default"])]
Sha256Ref = Annotated[str, Field(pattern=_SHA256)]

# =============================================================================== state machine


class RunState(StrEnum):
    """Lifecycle of a run (plan §8.3). Terminal states never change again, except that a
    ``PARTIAL`` run can be resumed (``RESUME`` → ``QUEUED`` with a new attempt)."""

    CREATED = "created"
    REJECTED = "rejected"
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    STALLED = "stalled"
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        """No execution is active and no automatic transition will happen."""
        return self in _TERMINAL

    @property
    def resumable(self) -> bool:
        return self is RunState.PARTIAL

    @property
    def has_execution(self) -> bool:
        """An executor execution may exist (the reconciler must poll its status)."""
        return self in _WITH_EXECUTION


_TERMINAL = frozenset(
    {RunState.REJECTED, RunState.SUCCEEDED, RunState.PARTIAL, RunState.FAILED, RunState.CANCELLED}
)
_WITH_EXECUTION = frozenset(
    {
        RunState.DISPATCHING,
        RunState.PROVISIONING,
        RunState.RUNNING,
        RunState.STALLED,
        RunState.CANCELLING,
    }
)


class FailureClass(StrEnum):
    """Why a run ended ``FAILED``; each maps to one stable :class:`ErrorCode`."""

    USER = "user"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INFRA = "infra"
    INTEGRITY = "integrity"
    TIMEOUT = "timeout"
    SANDBOX = "sandbox"

    @property
    def error_code(self) -> ErrorCode:
        return _FAILURE_CODES[self]

    @classmethod
    def from_error_code(cls, code: ErrorCode | str) -> FailureClass:
        """Inverse of :attr:`error_code`; unknown codes are ``infra``."""
        for fc, c in _FAILURE_CODES.items():
            if c == code:
                return fc
        return cls.INFRA


_FAILURE_CODES: dict[FailureClass, ErrorCode] = {
    FailureClass.USER: ErrorCode.USER_CODE_ERROR,
    FailureClass.RESOURCE_EXHAUSTED: ErrorCode.RESOURCE_EXHAUSTED,
    FailureClass.INFRA: ErrorCode.INFRA_ERROR,
    FailureClass.INTEGRITY: ErrorCode.INTEGRITY_FAILURE,
    FailureClass.TIMEOUT: ErrorCode.TIMEOUT,
    FailureClass.SANDBOX: ErrorCode.SANDBOX_VIOLATION,
}


class RunTrigger(StrEnum):
    """What happened to a run. The *owner* of each transition is in :data:`TRANSITIONS`."""

    ACCEPT = "accept"  # static validation and quota ok
    REJECT = "reject"  # static validation failed
    LEASE = "lease"  # dispatcher leased the queued run
    SUBMITTED = "submitted"  # intent row (run, attempt, nonce) written + executor accepted
    EVENT = "event"  # a verified run event arrived (liveness hint only)
    SILENCE = "silence"  # no event for K x interval: warning, not a failure
    COMPLETE = "complete"  # executor reports exit 0 and the "complete" event was received
    VERIFIED = "verified"  # finalizer verified + copied a complete bundle
    VERIFIED_PARTIAL = "verified_partial"  # finalizer verified a partial (checkpointed) bundle
    INFRA_RETRY = "infra_retry"  # infra failure, old execution confirmed gone, retry allowed
    FAIL_USER = "fail_user"
    FAIL_RESOURCE_EXHAUSTED = "fail_resource_exhausted"
    FAIL_INFRA = "fail_infra"
    FAIL_INTEGRITY = "fail_integrity"
    FAIL_TIMEOUT = "fail_timeout"
    FAIL_SANDBOX = "fail_sandbox"
    CANCEL = "cancel"  # user asked to cancel
    CANCEL_CONFIRMED = "cancel_confirmed"  # executor confirms the execution is gone
    RESUME = "resume"  # user resumes a PARTIAL run from its checkpoints


class Actor(StrEnum):
    """Component allowed to apply a transition (plan §8.3 'who owns which transitions')."""

    API = "api"
    DISPATCHER = "dispatcher"
    EVENTS = "events"
    RECONCILER = "reconciler"
    FINALIZER = "finalizer"


@dataclass(frozen=True, slots=True)
class TransitionRule:
    """One allowed ``(source, trigger) → target`` edge."""

    source: RunState
    trigger: RunTrigger
    target: RunState
    actors: frozenset[Actor]
    failure_class: FailureClass | None = None
    new_attempt: bool = False
    """The run goes back to ``QUEUED`` with ``attempt + 1`` and a fresh nonce."""

    @property
    def noop(self) -> bool:
        """Accepted but changes nothing (idempotent re-delivery)."""
        return self.source is self.target


_FAIL_TRIGGERS: dict[RunTrigger, FailureClass] = {
    RunTrigger.FAIL_USER: FailureClass.USER,
    RunTrigger.FAIL_RESOURCE_EXHAUSTED: FailureClass.RESOURCE_EXHAUSTED,
    RunTrigger.FAIL_INFRA: FailureClass.INFRA,
    RunTrigger.FAIL_INTEGRITY: FailureClass.INTEGRITY,
    RunTrigger.FAIL_TIMEOUT: FailureClass.TIMEOUT,
    RunTrigger.FAIL_SANDBOX: FailureClass.SANDBOX,
}
_EXECUTION_FAILURES = (
    RunTrigger.FAIL_USER,
    RunTrigger.FAIL_RESOURCE_EXHAUSTED,
    RunTrigger.FAIL_INFRA,
    RunTrigger.FAIL_TIMEOUT,
    RunTrigger.FAIL_SANDBOX,
)


def _build_table() -> dict[tuple[RunState, RunTrigger], TransitionRule]:
    S, T, A = RunState, RunTrigger, Actor
    rules: list[TransitionRule] = []

    def add(
        src: RunState,
        trig: RunTrigger,
        dst: RunState,
        *actors: Actor,
        new_attempt: bool = False,
    ) -> None:
        rules.append(
            TransitionRule(src, trig, dst, frozenset(actors), _FAIL_TRIGGERS.get(trig), new_attempt)
        )

    add(S.CREATED, T.ACCEPT, S.QUEUED, A.API)
    add(S.CREATED, T.REJECT, S.REJECTED, A.API)
    add(S.QUEUED, T.LEASE, S.DISPATCHING, A.DISPATCHER)
    # The dispatcher normally confirms submission; after a dispatcher crash the reconciler
    # adopts the orphan execution by nonce and confirms it instead.
    add(S.DISPATCHING, T.SUBMITTED, S.PROVISIONING, A.DISPATCHER, A.RECONCILER)
    add(S.DISPATCHING, T.INFRA_RETRY, S.QUEUED, A.DISPATCHER, A.RECONCILER, new_attempt=True)
    add(S.DISPATCHING, T.FAIL_INFRA, S.FAILED, A.DISPATCHER, A.RECONCILER)

    for live in (S.PROVISIONING, S.RUNNING, S.STALLED):
        add(live, T.EVENT, S.RUNNING, A.EVENTS)
        add(live, T.COMPLETE, S.FINALIZING, A.RECONCILER)
        add(live, T.INFRA_RETRY, S.QUEUED, A.RECONCILER, new_attempt=True)
        for trig in _EXECUTION_FAILURES:
            add(live, trig, S.FAILED, A.RECONCILER)
    add(S.RUNNING, T.SILENCE, S.STALLED, A.RECONCILER)
    add(S.STALLED, T.SILENCE, S.STALLED, A.RECONCILER)

    add(S.FINALIZING, T.EVENT, S.FINALIZING, A.EVENTS)  # late, re-ordered event
    add(S.FINALIZING, T.VERIFIED, S.SUCCEEDED, A.FINALIZER)
    add(S.FINALIZING, T.VERIFIED_PARTIAL, S.PARTIAL, A.FINALIZER)
    add(S.FINALIZING, T.FAIL_INTEGRITY, S.FAILED, A.FINALIZER)
    add(S.FINALIZING, T.FAIL_INFRA, S.FAILED, A.FINALIZER)

    for src in RunState:
        if not src.terminal and src is not S.CANCELLING:
            add(src, T.CANCEL, S.CANCELLING, A.API)
    add(S.CANCELLING, T.CANCEL, S.CANCELLING, A.API)
    add(S.CANCELLING, T.EVENT, S.CANCELLING, A.EVENTS)
    add(S.CANCELLING, T.CANCEL_CONFIRMED, S.CANCELLED, A.RECONCILER)
    # On SIGTERM a checkpointing supervisor flushes and exits PARTIAL with a ``partial``
    # complete event (plan §8.7 step 3): that bundle is finalized (→ PARTIAL, resumable).
    add(S.CANCELLING, T.COMPLETE, S.FINALIZING, A.RECONCILER)

    add(S.PARTIAL, T.RESUME, S.QUEUED, A.API, new_attempt=True)
    return {(r.source, r.trigger): r for r in rules}


TRANSITIONS: Final[Mapping[tuple[RunState, RunTrigger], TransitionRule]] = _build_table()
"""Every allowed edge. Any ``(state, trigger)`` pair not in this table is illegal."""


class IllegalTransition(MapiError):
    """``trigger`` is not allowed in ``state`` (maps to 409 ``CONFLICT``)."""

    default_code = ErrorCode.CONFLICT

    def __init__(self, state: RunState, trigger: RunTrigger) -> None:
        super().__init__(f"cannot apply {trigger.value!r} to a run in state {state.value!r}")
        self.state = state
        self.trigger = trigger


def transition(
    state: RunState | str, event: RunTrigger | str, *, actor: Actor | str | None = None
) -> TransitionRule:
    """Apply ``event`` to ``state``; return the matching rule or raise :class:`IllegalTransition`.

    Pure and deterministic. Callers persist ``rule.target`` (and ``failure_class``; bump the
    attempt when ``new_attempt``) with a compare-and-set on the old state. If ``actor`` is
    given it must be one of the rule's owners.
    """
    st, trig = RunState(state), RunTrigger(event)
    rule = TRANSITIONS.get((st, trig))
    if rule is None:
        raise IllegalTransition(st, trig)
    if actor is not None and Actor(actor) not in rule.actors:
        raise IllegalTransition(st, trig)
    return rule


def can_transition(state: RunState | str, event: RunTrigger | str) -> bool:
    return (RunState(state), RunTrigger(event)) in TRANSITIONS


def allowed_triggers(state: RunState | str) -> frozenset[RunTrigger]:
    st = RunState(state)
    return frozenset(t for (s, t) in TRANSITIONS if s is st)


# ====================================================================== canonical JSON + HMAC

_EVENT_CONTEXT = b"mapi.run-event.v1\x00"
_OUTPUT_MANIFEST_CONTEXT = b"mapi.output-manifest.v1\x00"
_BUNDLE_MANIFEST_CONTEXT = b"mapi.bundle-manifest.v1\x00"
HMAC_KEY_BYTES: Final = 32


def canonical_bytes(data: Mapping[str, Any] | BaseModel, *, exclude: Iterable[str] = ()) -> bytes:
    """Canonical UTF-8 JSON used for every signature in the run protocol.

    Models are dumped in JSON mode **with defaults included** (so signer and verifier never
    disagree about defaulted fields), ``exclude`` top-level keys are dropped, keys are sorted,
    there is no whitespace, and integral floats become ints (as :func:`canonical_value`).
    """
    raw = data.model_dump(mode="json") if isinstance(data, BaseModel) else dict(data)
    for key in exclude:
        raw.pop(key, None)
    text = json.dumps(
        canonical_value(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return text.encode("utf-8")


def new_hmac_key() -> str:
    """A fresh per-run HMAC key: 32 random bytes, standard base64."""
    return base64.b64encode(secrets.token_bytes(HMAC_KEY_BYTES)).decode("ascii")


def decode_hmac_key(key_b64: str) -> bytes:
    """Decode a base64 HMAC key; it must hold at least 32 bytes."""
    try:
        key = base64.b64decode(key_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("hmac_key must be standard base64") from exc
    if len(key) < HMAC_KEY_BYTES:
        raise ValueError(f"hmac_key must hold at least {HMAC_KEY_BYTES} bytes")
    return key


def _key_bytes(key: bytes | str) -> bytes:
    return decode_hmac_key(key) if isinstance(key, str) else key


def _mac(signing_bytes: bytes, key: bytes | str) -> str:
    """Lower-case hex HMAC-SHA256."""
    return _hmac.new(_key_bytes(key), signing_bytes, hashlib.sha256).hexdigest()


def _eq(expected: str, given: str | None) -> bool:
    return given is not None and _hmac.compare_digest(expected, given.lower())


def event_signing_bytes(event: RunEvent) -> bytes:
    """Bytes covered by an event HMAC: context tag + canonical JSON without ``hmac``."""
    return _EVENT_CONTEXT + canonical_bytes(event, exclude=("hmac",))


def compute_event_hmac(event: RunEvent, key: bytes | str) -> str:
    """Lower-case hex HMAC-SHA256 of :func:`event_signing_bytes` under the run key."""
    return _mac(event_signing_bytes(event), key)


def sign_event(event: RunEvent, key: bytes | str) -> RunEvent:
    """Return a copy of ``event`` with ``hmac`` set (the supervisor does this per line)."""
    return event.model_copy(update={"hmac": compute_event_hmac(event, key)})


def verify_event(event: RunEvent, key: bytes | str) -> bool:
    """Constant-time check of ``event.hmac``; ``False`` if it is missing or wrong."""
    return _eq(compute_event_hmac(event, key), event.hmac)


# ================================================================================ event payloads


class RunnerPhase(StrEnum):
    """Coarse phase reported by the runner (drives progress text in the UI)."""

    BOOTSTRAP = "bootstrap"
    LOAD_INPUTS = "load_inputs"
    VALIDATE = "validate"
    PREPROCESS = "preprocess"
    FIT = "fit"
    SAMPLE = "sample"
    SCORE = "score"
    EVALUATE = "evaluate"
    DERIVE = "derive"
    PLOTS = "plots"
    UPLOAD = "upload"
    DONE = "done"


class _Payload(StrictModel):
    pass


class StatePayload(_Payload):
    """``state``: the runner entered a new phase."""

    phase: RunnerPhase
    message: str | None = Field(None, max_length=500)


class ChainProgress(StrictModel):
    """Per-chain sampler progress (Expert run view)."""

    chain: int = Field(..., ge=0)
    draws_done: int = Field(..., ge=0)
    draws_total: int = Field(..., ge=0)
    tuning: bool = False
    divergences: int = Field(0, ge=0)


class ProgressPayload(_Payload):
    """``progress``: coalesced to at most one event per ``limits.progress_interval_s``."""

    phase: RunnerPhase
    fraction: float | None = Field(None, ge=0.0, le=1.0)
    message: str | None = Field(None, max_length=500)
    eta_seconds: float | None = Field(None, ge=0)
    chains: list[ChainProgress] = Field(default_factory=list, max_length=64)


class LogPayload(_Payload):
    """``log``: one captured worker output line (ANSI/control sequences already stripped)."""

    stream: Literal["stdout", "stderr", "system"]
    line: str = Field(..., max_length=4096)
    truncated: bool = False


class WarningPayload(_Payload):
    """``warning``: a non-fatal, platform-classified problem."""

    code: str = Field(..., pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    message: str = Field(..., max_length=2000)
    untrusted_text: str | None = Field(None, max_length=8000)


class CheckpointPayload(_Payload):
    """``checkpoint``: a checkpoint object was uploaded under the checkpoint prefix."""

    name: ObjectName = Field(..., description="Object name relative to the checkpoint prefix")
    size: int = Field(..., ge=0)
    crc32c: Crc32c
    segment: int = Field(..., ge=0)
    draws_done: int | None = Field(None, ge=0)


class MetricPayload(_Payload):
    """``metric``: a scalar metric (holdout error, elpd, ...)."""

    name: MetricName
    value: float
    step: int | None = Field(None, ge=0)
    labels: dict[str, str] = Field(default_factory=dict, max_length=16)


class CompletePayload(_Payload):
    """``complete``: outputs and the manifest are uploaded; the supervisor exits next."""

    status: Literal["complete", "partial"]
    manifest_name: ObjectName = Field(..., description="Object name of manifest.json")
    manifest_crc32c: Crc32c
    objects: int = Field(..., ge=0)
    total_bytes: int = Field(..., ge=0)


class ErrorPayload(_Payload):
    """``error``: the run failed; ``message`` is platform-authored, plugin text is untrusted."""

    code: ErrorCode
    failure_class: FailureClass
    message: str = Field(..., max_length=2000)
    untrusted_text: str | None = Field(None, max_length=65536)

    @model_validator(mode="after")
    def _consistent(self) -> ErrorPayload:
        if self.failure_class.error_code is not self.code:
            raise ValueError(
                f"code {self.code.value} does not match failure_class {self.failure_class.value}"
            )
        return self


EVENT_PAYLOAD_MODELS: Final[Mapping[RunEventType, type[BaseModel]]] = {
    RunEventType.STATE: StatePayload,
    RunEventType.PROGRESS: ProgressPayload,
    RunEventType.LOG: LogPayload,
    RunEventType.DIAGNOSTIC: Diagnostic,
    RunEventType.WARNING: WarningPayload,
    RunEventType.CHECKPOINT: CheckpointPayload,
    RunEventType.METRIC: MetricPayload,
    RunEventType.COMPLETE: CompletePayload,
    RunEventType.ERROR: ErrorPayload,
}
"""The payload model of each event type."""


def parse_payload(event: RunEvent) -> BaseModel:
    """Validate ``event.payload`` against the model for ``event.type``."""
    return EVENT_PAYLOAD_MODELS[event.type].model_validate(event.payload)


def make_event(
    run_id: str,
    attempt: int,
    seq: int,
    payload: BaseModel,
    *,
    ts: datetime | None = None,
    key: bytes | str | None = None,
) -> RunEvent:
    """Build a :class:`RunEvent` from a typed payload (type inferred), signed if ``key``."""
    etype = next((t for t, m in EVENT_PAYLOAD_MODELS.items() if type(payload) is m), None)
    if etype is None:
        raise TypeError(f"{type(payload).__name__} is not a run-event payload model")
    event = RunEvent(
        run_id=run_id,
        attempt=attempt,
        seq=seq,
        type=etype,
        ts=ts or datetime.now(UTC),
        payload=payload.model_dump(mode="json"),
    )
    return sign_event(event, key) if key is not None else event


# ============================================================================ stdout wire format

STDOUT_EVENT_KEY: Final = "mapi_event"
"""Each supervisor stdout line is ``{"mapi_event": <RunEvent JSON>}``."""

LOGGING_FILTER: Final = "jsonPayload.mapi_event:*"
"""Cloud Logging sink filter selecting event lines (plan §8.7)."""

MAX_EVENT_LINE_BYTES: Final = 64 * 1024
"""Upper bound for one encoded line (well under Cloud Logging's 256 KB entry limit)."""


class InvalidEvent(SandboxViolation):
    """A line claimed to be an event but is malformed, oversized, or fails its HMAC."""


def encode_event_line(event: RunEvent) -> str:
    """One JSONL line (without the trailing newline) exactly as the supervisor prints it."""
    line = json.dumps(
        {STDOUT_EVENT_KEY: event.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    if len(line.encode("utf-8")) > MAX_EVENT_LINE_BYTES:
        raise ValueError(f"event line exceeds {MAX_EVENT_LINE_BYTES} bytes")
    return line


def event_from_log_payload(json_payload: Mapping[str, Any]) -> RunEvent:
    """Parse a Cloud Logging ``jsonPayload`` (or a decoded stdout line) into a RunEvent."""
    body = json_payload.get(STDOUT_EVENT_KEY)
    if not isinstance(body, Mapping):
        raise InvalidEvent(f"payload has no {STDOUT_EVENT_KEY!r} object")
    try:
        return RunEvent.model_validate(dict(body))
    except ValidationError as exc:
        raise InvalidEvent("malformed run event", untrusted_text=str(exc)[:8000]) from exc


def decode_event_line(line: str | bytes) -> RunEvent | None:
    """Parse one stdout line. Non-JSON lines and JSON without ``mapi_event`` return ``None``
    (they are not events); a present-but-invalid event raises :class:`InvalidEvent`."""
    raw = line.encode("utf-8") if isinstance(line, str) else line
    if len(raw) > MAX_EVENT_LINE_BYTES:
        raise InvalidEvent(f"event line exceeds {MAX_EVENT_LINE_BYTES} bytes")
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(obj, dict) or STDOUT_EVENT_KEY not in obj:
        return None
    return event_from_log_payload(obj)


# ======================================================================================= RunSpec

RUNSPEC_SCHEMA: Final = "mapi.runspec/v1"
OUTPUT_MANIFEST: Final = "manifest"
"""Output name of the runner's manifest (a :class:`SignedPut` for ``{prefix}manifest.json``)."""
OUTPUT_ARTIFACTS: Final = "artifacts"
"""Output name of the POST policy covering every other object under the attempt prefix."""
OUTPUT_CHECKPOINTS: Final = "checkpoints"
"""Output name of the POST policy for checkpoints (only for checkpointable runs)."""
INPUT_DATASET: Final = "dataset"
INPUT_BUNDLE: Final = "bundle"

OutputTarget = Annotated[SignedPut | PostPolicy, Field(discriminator="method")]


def new_nonce() -> str:
    """A fresh 256-bit execution nonce (64 lowercase hex characters)."""
    return secrets.token_hex(32)


class RunLimits(StrictModel):
    """Caps the supervisor enforces (exceeding them is a ``SANDBOX_VIOLATION``)."""

    log_line_bytes: int = Field(4096, ge=256, le=65536)
    log_total_bytes: int = Field(20 * 1024 * 1024, ge=0)
    max_event_bytes: int = Field(MAX_EVENT_LINE_BYTES, ge=1024, le=MAX_EVENT_LINE_BYTES)
    progress_interval_s: float = Field(2.0, ge=0.5, le=60)
    max_output_bytes: int = Field(..., ge=0, description="Sum of all uploaded output objects")
    max_output_objects: int = Field(10_000, ge=1)
    scratch_bytes: int | None = Field(None, ge=0, description="In-memory filesystem budget")

    @classmethod
    def for_class(cls, resource_class: ResourceClass, **overrides: Any) -> RunLimits:
        """Defaults derived from the resource class table (plan §8.5)."""
        rc = RESOURCE_CLASSES[resource_class]
        values: dict[str, Any] = {
            "max_output_bytes": rc.max_bundle_bytes,
            "scratch_bytes": rc.scratch_bytes,
        }
        values.update(overrides)
        return cls(**values)


class RunSpec(StrictModel):
    """The bootstrap object at ``bootstrap/<nonce>`` (plan §8.1).

    Written by the dispatcher right before the executor call and deleted by the supervisor
    immediately after reading it. It is the *only* way a run receives URLs and its HMAC key:
    the environment carries just :class:`RunnerEnv`.
    """

    schema_version: Literal["mapi.runspec/v1"] = RUNSPEC_SCHEMA
    run_id: str = Field(..., pattern=_RUN_ID)
    attempt: int = Field(..., ge=1)
    nonce: str = Field(..., pattern=_NONCE)
    kind: RunKind
    tenant_id: TenantId
    project_id: TenantId
    spec: dict[str, JsonValue] = Field(
        ..., description="Resolved spec JSON (ModelSpec/ScoreRequest/ingest mapping ...)"
    )
    spec_hash: Sha256Ref
    catalog_digest: Sha256Ref
    inputs: dict[IoName, SignedGet] = Field(default_factory=dict, max_length=256)
    outputs: dict[IoName, OutputTarget] = Field(..., max_length=16)
    resume: dict[IoName, SignedGet] = Field(
        default_factory=dict, max_length=1024, description="Checkpoints of the previous attempt"
    )
    checkpoint_prefix: ObjectName | None = None
    hmac_key: str = Field(..., repr=False, description="Base64 per-run HMAC-SHA256 key")
    limits: RunLimits
    resource_class: ResourceClass
    deadline: AwareDatetime = Field(..., description="Hard wall-clock deadline (UTC)")
    created_at: AwareDatetime
    labels: dict[str, str] = Field(default_factory=dict, max_length=64)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "schema_version": "mapi.runspec/v1",
                    "run_id": "run_01jabcdefghjkmnpqrstvwxyz0",
                    "attempt": 1,
                    "nonce": "9f" * 32,
                    "kind": "fit",
                    "tenant_id": "default",
                    "project_id": "default",
                    "spec": {"apiVersion": "mapi/v1", "kind": "ModelSpec"},
                    "spec_hash": "sha256:" + "ab" * 32,
                    "catalog_digest": "sha256:" + "cd" * 32,
                    "inputs": {
                        "dataset": {
                            "method": "GET",
                            "url": "http://127.0.0.1:4443/download/storage/v1/b/mapi-local-data"
                            "/o/t%2Fdefault%2Fdatasets%2Fab%2Fdata.zarr.zip?generation=3",
                            "expires_at": "2026-10-15T11:00:00Z",
                            "generation": 3,
                            "size": 1048576,
                            "crc32c": "4waSgw==",
                        }
                    },
                    "outputs": {
                        "manifest": {
                            "method": "PUT",
                            "url": "http://127.0.0.1:4443/upload/storage/v1/b/mapi-local-runs/o"
                            "?name=t%2Fdefault%2Fruns%2Frun_01jabcdefghjkmnpqrstvwxyz0%2Fa1%2F"
                            "manifest.json",
                            "name": "t/default/runs/run_01jabcdefghjkmnpqrstvwxyz0/a1/"
                            "manifest.json",
                            "expires_at": "2026-10-15T11:00:00Z",
                            "headers": {"x-goog-if-generation-match": "0"},
                            "max_bytes": 1048576,
                        },
                        "artifacts": {
                            "method": "POST",
                            "url": "http://127.0.0.1:4443/mapi-local-runs",
                            "prefix": "t/default/runs/run_01jabcdefghjkmnpqrstvwxyz0/a1/",
                            "fields": {"policy": "e30=", "x-goog-signature": "00"},
                            "content_length_range": [0, 1000000000],
                            "expires_at": "2026-10-15T11:00:00Z",
                        },
                    },
                    "hmac_key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
                    "limits": {"max_output_bytes": 1000000000, "scratch_bytes": 3221225472},
                    "resource_class": "S",
                    "deadline": "2026-10-15T10:45:00Z",
                    "created_at": "2026-10-15T10:00:00Z",
                }
            ]
        }
    )

    @field_validator("hmac_key")
    @classmethod
    def _key(cls, v: str) -> str:
        decode_hmac_key(v)
        return v

    @field_validator("checkpoint_prefix")
    @classmethod
    def _ckpt(cls, v: str | None) -> str | None:
        if v is not None:
            check_object_name(v)
            if not v.endswith("/"):
                raise ValueError("checkpoint_prefix must end with '/'")
        return v

    @model_validator(mode="after")
    def _check(self) -> RunSpec:
        manifest = self.outputs.get(OUTPUT_MANIFEST)
        if not isinstance(manifest, SignedPut):
            raise ValueError(f"outputs[{OUTPUT_MANIFEST!r}] must be a signed PUT")
        if self.deadline <= self.created_at:
            raise ValueError("deadline must be after created_at")
        if self.kind.is_parent:
            raise ValueError(f"{self.kind.value!r} runs are parents and never execute")
        ckpt = self.outputs.get(OUTPUT_CHECKPOINTS)
        if ckpt is not None and (
            not isinstance(ckpt, PostPolicy) or ckpt.prefix != self.checkpoint_prefix
        ):
            raise ValueError("outputs['checkpoints'] must be a POST policy on checkpoint_prefix")
        return self

    def hmac_key_bytes(self) -> bytes:
        return decode_hmac_key(self.hmac_key)

    def redacted(self) -> dict[str, Any]:
        """JSON for logs: the HMAC key and every signed URL are replaced by ``"<redacted>"``."""
        data = self.model_dump(mode="json")
        data["hmac_key"] = "<redacted>"
        for group in ("inputs", "outputs", "resume"):
            for entry in data[group].values():
                entry["url"] = "<redacted>"
                if "fields" in entry:
                    entry["fields"] = {k: "<redacted>" for k in entry["fields"]}
        return data


# =================================================================================== manifests

OUTPUT_MANIFEST_SCHEMA: Final = "mapi.output-manifest/v1"
BUNDLE_MANIFEST_SCHEMA: Final = "mapi.bundle-manifest/v1"
MANIFEST_OBJECT_NAME: Final = "manifest.json"


class ManifestObject(StrictModel):
    """One uploaded object as claimed by the runner (hashes computed while streaming)."""

    key: ObjectName = Field(..., description="Full object name in the runs bucket")
    size: int = Field(..., ge=0)
    crc32c: Crc32c
    md5: Md5
    content_type: str | None = Field(None, max_length=256)

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        return check_object_name(v)


class InputDigest(StrictModel):
    """What the runner actually read for one input (copied from the RunSpec, then checked)."""

    generation: int = Field(..., ge=1)
    size: int = Field(..., ge=0)
    crc32c: Crc32c


def _check_rel_name(name: str) -> str:
    check_object_name(name)
    if name.endswith("/"):
        raise ValueError("output names must not end with '/'")
    return name


class OutputManifest(StrictModel):
    """The runner's manifest at ``{prefix}manifest.json``: a *claim*, verified at FINALIZING.

    ``objects`` maps a relative output name (``bundle.zarr.zip``, ``ui/summaries.json``) to
    the uploaded object; every key must equal ``prefix + name``. The supervisor signs it with
    the run HMAC key (``hmac``), which only proves it came from this run's supervisor.
    """

    schema_version: Literal["mapi.output-manifest/v1"] = OUTPUT_MANIFEST_SCHEMA
    tenant_id: TenantId
    project_id: TenantId
    run_id: str = Field(..., pattern=_RUN_ID)
    attempt: int = Field(..., ge=1)
    kind: RunKind
    status: Literal["complete", "partial"]
    prefix: ObjectName
    spec_hash: Sha256Ref
    catalog_digest: Sha256Ref
    inputs: dict[IoName, InputDigest] = Field(default_factory=dict)
    objects: dict[str, ManifestObject] = Field(default_factory=dict, max_length=10_000)
    runner_version: str = Field(..., max_length=64)
    base_image: str | None = Field(None, max_length=256)
    overlay_hash: Sha256Ref | None = None
    lib_versions: dict[str, str] = Field(default_factory=dict, max_length=256)
    created_at: AwareDatetime
    hmac: str | None = Field(None, max_length=128)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "schema_version": "mapi.output-manifest/v1",
                    "tenant_id": "default",
                    "project_id": "default",
                    "run_id": "run_01jabcdefghjkmnpqrstvwxyz0",
                    "attempt": 1,
                    "kind": "fit",
                    "status": "complete",
                    "prefix": "t/default/runs/run_01jabcdefghjkmnpqrstvwxyz0/a1/",
                    "spec_hash": "sha256:" + "ab" * 32,
                    "catalog_digest": "sha256:" + "cd" * 32,
                    "inputs": {"dataset": {"generation": 3, "size": 1048576, "crc32c": "4waSgw=="}},
                    "objects": {
                        "bundle.zarr.zip": {
                            "key": "t/default/runs/run_01jabcdefghjkmnpqrstvwxyz0/a1/"
                            "bundle.zarr.zip",
                            "size": 2097152,
                            "crc32c": "AAAAAA==",
                            "md5": "1B2M2Y8AsgTpgAmY7PhCfg==",
                            "content_type": "application/zip",
                        }
                    },
                    "runner_version": "0.2.0",
                    "base_image": "base-bayes@2026.10",
                    "created_at": "2026-10-15T10:20:00Z",
                }
            ]
        }
    )

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, v: str) -> str:
        check_object_name(v)
        if not v.endswith("/"):
            raise ValueError("prefix must end with '/'")
        return v

    @model_validator(mode="after")
    def _keys(self) -> OutputManifest:
        for name, obj in self.objects.items():
            _check_rel_name(name)
            if name == MANIFEST_OBJECT_NAME:
                raise ValueError("the manifest must not list itself")
            if obj.key != self.prefix + name:
                raise ValueError(f"objects[{name!r}].key must be prefix + name")
        return self

    @property
    def manifest_key(self) -> str:
        return self.prefix + MANIFEST_OBJECT_NAME

    @property
    def total_bytes(self) -> int:
        return sum(o.size for o in self.objects.values())


def output_manifest_signing_bytes(manifest: OutputManifest) -> bytes:
    return _OUTPUT_MANIFEST_CONTEXT + canonical_bytes(manifest, exclude=("hmac",))


def sign_output_manifest(manifest: OutputManifest, key: bytes | str) -> OutputManifest:
    mac = _mac(output_manifest_signing_bytes(manifest), key)
    return manifest.model_copy(update={"hmac": mac})


def verify_output_manifest(manifest: OutputManifest, key: bytes | str) -> bool:
    return _eq(_mac(output_manifest_signing_bytes(manifest), key), manifest.hmac)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of :func:`verify_output_objects`."""

    issues: list[Issue]
    verified: dict[str, ObjectMeta]
    """Relative name → observed metadata (pin ``generation`` when copying)."""

    @property
    def ok(self) -> bool:
        return not self.issues


def verify_output_objects(
    manifest: OutputManifest,
    observed: Iterable[ObjectMeta],
    *,
    expected_prefix: str,
    allowed_extra_prefixes: Iterable[str] = ("checkpoints/",),
) -> VerificationResult:
    """Metadata-only FINALIZING check (plan §8.6), with no object content read.

    ``observed`` is the store listing of ``expected_prefix`` (server-computed size, CRC32C,
    MD5, generation). Every claimed object must exist with identical size and hashes, and
    no unexpected object may exist under the prefix except ``manifest.json`` and the
    ``allowed_extra_prefixes`` (relative). Each discrepancy is an ``INTEGRITY_FAILURE``.
    """
    issues: list[Issue] = []

    def bad(pointer: str, message: str) -> None:
        issues.append(Issue(code=ErrorCode.INTEGRITY_FAILURE, pointer=pointer, message=message))

    if manifest.prefix != expected_prefix:
        bad("/prefix", "manifest prefix does not match the attempt prefix")
        return VerificationResult(issues, {})
    by_key: dict[str, ObjectMeta] = {}
    for listed in observed:
        if not listed.name.startswith(expected_prefix):
            bad("", "listing returned an object outside the attempt prefix")
            continue
        by_key[listed.name] = listed
    verified: dict[str, ObjectMeta] = {}
    for name, claim in sorted(manifest.objects.items()):
        ptr = "/objects/" + name.replace("~", "~0").replace("/", "~1")
        meta = by_key.get(claim.key)
        if meta is None:
            bad(ptr, f"claimed object {name!r} does not exist")
        elif meta.size != claim.size:
            bad(ptr, f"size mismatch for {name!r}: claimed {claim.size}, stored {meta.size}")
        elif meta.crc32c != claim.crc32c:
            bad(ptr, f"crc32c mismatch for {name!r}")
        elif meta.md5_hash is None or meta.md5_hash != claim.md5:
            bad(ptr, f"md5 mismatch for {name!r}")
        else:
            verified[name] = meta
    extra_ok = tuple(expected_prefix + p for p in allowed_extra_prefixes)
    claimed = {o.key for o in manifest.objects.values()} | {manifest.manifest_key}
    for key in sorted(by_key):
        if key not in claimed and not key.startswith(extra_ok):
            bad("", f"unexpected object {key[len(expected_prefix) :]!r} under the run prefix")
    return VerificationResult(issues, verified)


class BundleObject(StrictModel):
    """A verified object after the server-side copy into the bundles bucket."""

    bucket: str = Field(..., max_length=63)
    key: ObjectName
    generation: int = Field(..., ge=1)
    size: int = Field(..., ge=0)
    crc32c: Crc32c
    md5: Md5
    content_type: str | None = Field(None, max_length=256)


class ManifestSignature(StrictModel):
    """Platform signature over :func:`bundle_manifest_signing_bytes`.

    ``HS256`` (HMAC-SHA256 with a server-held key) is the local/M1 signer; KMS asymmetric
    signers (M2) use their own ``alg`` and the same signing bytes.
    """

    alg: Literal["HS256", "KMS-EC-P256-SHA256", "KMS-RSA-PSS-SHA256"]
    key_id: str = Field(..., min_length=1, max_length=256)
    value: str = Field(..., min_length=1, max_length=2048)


class BundleManifest(StrictModel):
    """The platform-signed manifest of a bundle (``manifest.json`` + ``manifest.sig``).

    Binds tenant, run, base digest, overlay hash, spec hash, data hash and every
    *destination* generation. URLs to a bundle are minted only after this signature checks.
    """

    schema_version: Literal["mapi.bundle-manifest/v1"] = BUNDLE_MANIFEST_SCHEMA
    tenant_id: TenantId
    project_id: TenantId
    model_id: str = Field(..., pattern=_RUN_ID)
    model_version: int = Field(..., ge=1)
    run_id: str = Field(..., pattern=_RUN_ID)
    attempt: int = Field(..., ge=1)
    kind: RunKind
    status: Literal["complete", "partial"]
    spec_hash: Sha256Ref
    data_hash: Sha256Ref | None = None
    catalog_digest: Sha256Ref
    base_image: str | None = Field(None, max_length=256)
    overlay_hash: Sha256Ref | None = None
    objects: dict[str, BundleObject] = Field(default_factory=dict, max_length=10_000)
    source_manifest_crc32c: Crc32c
    signed_at: AwareDatetime
    signature: ManifestSignature | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "schema_version": "mapi.bundle-manifest/v1",
                    "tenant_id": "default",
                    "project_id": "default",
                    "model_id": "mdl_01jabcdefghjkmnpqrstvwxyz0",
                    "model_version": 1,
                    "run_id": "run_01jabcdefghjkmnpqrstvwxyz0",
                    "attempt": 1,
                    "kind": "fit",
                    "status": "complete",
                    "spec_hash": "sha256:" + "ab" * 32,
                    "data_hash": "sha256:" + "ef" * 32,
                    "catalog_digest": "sha256:" + "cd" * 32,
                    "base_image": "base-bayes@2026.10",
                    "objects": {
                        "bundle.zarr.zip": {
                            "bucket": "mapi-local-bundles",
                            "key": "t/default/models/mdl_01jabcdefghjkmnpqrstvwxyz0/1/"
                            "bundle.zarr.zip",
                            "generation": 1760520000000001,
                            "size": 2097152,
                            "crc32c": "AAAAAA==",
                            "md5": "1B2M2Y8AsgTpgAmY7PhCfg==",
                            "content_type": "application/zip",
                        }
                    },
                    "source_manifest_crc32c": "4waSgw==",
                    "signed_at": "2026-10-15T10:21:00Z",
                    "signature": {"alg": "HS256", "key_id": "local-1", "value": "00" * 32},
                }
            ]
        }
    )


def bundle_manifest_signing_bytes(manifest: BundleManifest) -> bytes:
    """Bytes covered by the platform signature (everything except ``signature``)."""
    return _BUNDLE_MANIFEST_CONTEXT + canonical_bytes(manifest, exclude=("signature",))


def sign_bundle_manifest(manifest: BundleManifest, key: bytes | str, key_id: str) -> BundleManifest:
    """HS256-sign a bundle manifest (the local/M1 signer)."""
    value = _mac(bundle_manifest_signing_bytes(manifest), key)
    sig = ManifestSignature(alg="HS256", key_id=key_id, value=value)
    return manifest.model_copy(update={"signature": sig})


def verify_bundle_manifest(manifest: BundleManifest, keys: Mapping[str, bytes | str]) -> bool:
    """Check an HS256 signature against ``keys`` (key id → key). KMS algs are verified by the
    KMS signer in the server and raise ``NotImplementedError`` here."""
    sig = manifest.signature
    if sig is None:
        return False
    if sig.alg != "HS256":
        raise NotImplementedError(f"{sig.alg} signatures are verified by the KMS signer (M2)")
    key = keys.get(sig.key_id)
    if key is None:
        return False
    return _eq(_mac(bundle_manifest_signing_bytes(manifest), key), sig.value)


def catalog_digest(catalog: Catalog) -> str:
    """``sha256:<hex>`` identity of a catalog snapshot (pinned in every RunSpec)."""
    return spec_hash(catalog)


# ========================================================================= invocation contract

ENV_RUN_ID: Final = "MAPI_RUN_ID"
ENV_NONCE: Final = "MAPI_NONCE"
ENV_BOOTSTRAP_URL: Final = "MAPI_BOOTSTRAP_URL"
ENV_EXEC_TOKEN: Final = "MAPI_EXEC_TOKEN"
RUNNER_ENV_VARS: Final = (ENV_RUN_ID, ENV_NONCE, ENV_BOOTSTRAP_URL, ENV_EXEC_TOKEN)
ENV_REQUIRE_ISOLATION: Final = "MAPI_REQUIRE_ISOLATION"
"""Set to ``1`` by executors that promise a supervisor/worker uid split (docker): the
supervisor then refuses to start the worker in dev mode (``SANDBOX_VIOLATION``) instead of
running untrusted code under its own uid. Not a secret, and never passed to the worker."""

WORKER_ENV_ALLOWLIST: Final = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "TMPDIR",
        "HOME",
        "PYTHONHASHSEED",
        "PYTHONUNBUFFERED",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMBA_CACHE_DIR",
        "PYTENSOR_FLAGS",
        "XLA_FLAGS",
    }
)
"""The only variables the supervisor passes to the worker (every ``MAPI_*`` is scrubbed)."""


def worker_environ(parent: Mapping[str, str]) -> dict[str, str]:
    """The scrubbed environment for the untrusted worker process."""
    return {k: v for k, v in parent.items() if k in WORKER_ENV_ALLOWLIST}


_BOOTSTRAP_SCHEMES = ("http://", "https://", "gs://")


class RunnerEnv(StrictModel):
    """The *entire* configuration an execution receives through its environment.

    * ``MAPI_RUN_ID`` / ``MAPI_NONCE`` — per execution (the only executor overrides);
    * ``MAPI_BOOTSTRAP_URL`` — static per executor/Job: a template containing ``{nonce}``,
      e.g. ``gs://mapi-prod-bootstrap/{nonce}`` (cloud: read with the execution service
      account) or ``http://127.0.0.1:4443/storage/v1/b/mapi-local-bootstrap/o/{nonce}``
      (local GCS shim);
    * ``MAPI_EXEC_TOKEN`` — **local only**: bearer token of the shim's exec principal
      (get+delete on the bootstrap bucket). Never set in the cloud.
    """

    run_id: str = Field(..., pattern=_RUN_ID)
    nonce: str = Field(..., pattern=_NONCE)
    bootstrap_url: str = Field(..., max_length=2048)
    exec_token: str | None = Field(None, max_length=4096, repr=False)

    @field_validator("bootstrap_url")
    @classmethod
    def _template(cls, v: str) -> str:
        if not v.startswith(_BOOTSTRAP_SCHEMES):
            raise ValueError("MAPI_BOOTSTRAP_URL must be http(s):// or gs://")
        if v.count("{nonce}") != 1:
            raise ValueError("MAPI_BOOTSTRAP_URL must contain '{nonce}' exactly once")
        if re.search(r"\{(?!nonce\})", v):
            raise ValueError("MAPI_BOOTSTRAP_URL may only use the '{nonce}' placeholder")
        return v

    @classmethod
    def from_environ(cls, env: Mapping[str, str]) -> RunnerEnv:
        """Parse the runner environment; raises ``ValueError`` naming the missing variable."""
        missing = [k for k in (ENV_RUN_ID, ENV_NONCE, ENV_BOOTSTRAP_URL) if not env.get(k)]
        if missing:
            raise ValueError(f"missing environment variable(s): {', '.join(missing)}")
        return cls(
            run_id=env[ENV_RUN_ID],
            nonce=env[ENV_NONCE],
            bootstrap_url=env[ENV_BOOTSTRAP_URL],
            exec_token=env.get(ENV_EXEC_TOKEN) or None,
        )

    def to_environ(self) -> dict[str, str]:
        out = {
            ENV_RUN_ID: self.run_id,
            ENV_NONCE: self.nonce,
            ENV_BOOTSTRAP_URL: self.bootstrap_url,
        }
        if self.exec_token:
            out[ENV_EXEC_TOKEN] = self.exec_token
        return out

    def resolved_bootstrap_url(self) -> str:
        """The bootstrap object location for this nonce."""
        return self.bootstrap_url.replace("{nonce}", self.nonce)


class RunnerExit(IntEnum):
    """Supervisor exit codes. ``0`` only after the manifest upload and the ``complete`` event.

    137 and 143 are not chosen by the supervisor: they are SIGKILL (OOM killer or hard
    timeout) and an unhandled SIGTERM as reported by the executor.
    """

    OK = 0
    CONFIG_ERROR = 2  # bad/missing env, bootstrap unreadable or invalid (infra)
    USER_ERROR = 10  # plugin/user code raised or produced invalid output
    RESOURCE_EXHAUSTED = 11  # MemoryError, scratch budget exceeded
    INFRA_ERROR = 12  # upload/download failures after retries
    INTEGRITY_ERROR = 13  # an input did not match its size/crc32c
    TIMEOUT = 14  # deadline reached without a checkpoint
    SANDBOX_VIOLATION = 15  # log flood, output caps, forged/oversized events
    PARTIAL = 16  # SIGTERM/deadline: checkpoint flushed and partial manifest uploaded
    KILLED = 137  # SIGKILL (OOM)
    TERMINATED = 143  # SIGTERM not handled in time


_EXIT_CLASSES: dict[int, FailureClass | None] = {
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


def failure_class_for_exit(exit_code: int) -> FailureClass | None:
    """Classify a supervisor exit code (``None`` = not a failure: OK or PARTIAL).

    Unknown non-zero codes (e.g. ``1`` from a crashed supervisor) are ``infra``. A run that
    the user cancelled is handled by the ``CANCELLING`` state, not by this function.
    """
    if exit_code in _EXIT_CLASSES:
        return _EXIT_CLASSES[exit_code]
    return FailureClass.INFRA
