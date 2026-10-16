"""Logic shared by the sync and async clients that does no I/O."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import httpx
from mapi_spec.api import (
    SSE_HEARTBEAT_SECONDS,
    ArtifactLink,
    DatasetVersion,
    DatasetVersionStatus,
    Page,
    Run,
    RunCreate,
    RunEventView,
    ScoreCallResult,
    ScoreSummary,
)
from mapi_spec.errors import (
    ErrorCode,
    MapiError,
    ValidationFailed,
    error_for_code,
    error_from_problem,
)
from mapi_spec.loader import parse_text
from mapi_spec.run import FailureClass, RunState
from mapi_spec.spec import ScoreRequest

from mapi_client._version import __version__
from mapi_client.config import ClientConfig

if TYPE_CHECKING:
    import xarray as xr

__all__ = [
    "USER_AGENT",
    "ScoreResult",
    "as_run_create",
    "as_score_request",
    "call_timeout",
    "dataset_version_error",
    "events_page_with_cursor",
    "http_timeout",
    "run_error",
    "spec_name",
    "sse_timeout",
    "storage_timeout",
    "tree_output",
]

USER_AGENT: Final = f"mapi-client/{__version__}"


def http_timeout(cfg: ClientConfig) -> httpx.Timeout:
    return httpx.Timeout(cfg.timeout)


def call_timeout(cfg: ClientConfig, server_wait: float | None) -> httpx.Timeout:
    """Timeout of one API call: the client's, with the read timeout extended by the time
    the server may legitimately hold the response (``Prefer: wait``)."""
    if not server_wait:
        return http_timeout(cfg)
    return httpx.Timeout(cfg.timeout, read=cfg.timeout + server_wait)


def sse_timeout(cfg: ClientConfig) -> httpx.Timeout:
    # heartbeats arrive every 15 s: a silent read longer than 3 heartbeats is a dead stream
    return httpx.Timeout(cfg.timeout, read=max(cfg.timeout, 3.0 * SSE_HEARTBEAT_SECONDS))


def storage_timeout(cfg: ClientConfig) -> httpx.Timeout:
    return httpx.Timeout(cfg.timeout, read=max(cfg.timeout, 300.0), write=max(cfg.timeout, 300.0))


# ------------------------------------------------------------------------------------- runs


def run_error(run: Run) -> MapiError | None:
    """The error a finished run stands for (``None`` for succeeded/partial/non-terminal)."""
    if run.state in (RunState.FAILED, RunState.REJECTED):
        if run.problem is not None:
            return error_from_problem(run.problem)
        fc = run.failure_class or FailureClass.INFRA
        return error_for_code(fc.error_code, f"run {run.id} {run.state.value} ({fc.value})")
    if run.state is RunState.CANCELLED:
        return MapiError(f"run {run.id} was cancelled", code=ErrorCode.CANCELLED)
    return None


def as_run_create(run: RunCreate | Mapping[str, Any]) -> RunCreate:
    return run if isinstance(run, RunCreate) else RunCreate.model_validate(dict(run))


def events_page_with_cursor(page: Page[RunEventView]) -> Page[RunEventView]:
    """Rewrite a run-events page so its cursor is the ``after`` id of the next request."""
    if page.next_cursor is None or not page.items:
        return Page[RunEventView](items=page.items, next_cursor=None)
    return Page[RunEventView](items=page.items, next_cursor=str(page.items[-1].id))


# ------------------------------------------------------------------------------ datasets


def dataset_version_error(version: DatasetVersion, run: Run | None) -> MapiError:
    """Why an ingest failed: the run's problem, else the version's issues."""
    if run is not None:
        err = run_error(run)
        if err is not None:
            return err
    if version.issues:
        return ValidationFailed(version.issues)
    return MapiError(f"ingest of {version.ref} failed", code=ErrorCode.USER_CODE_ERROR)


def version_done(version: DatasetVersion) -> bool:
    return version.status is not DatasetVersionStatus.INGESTING


# ---------------------------------------------------------------------------------- specs


def spec_name(yaml_text: str) -> str:
    """``metadata.name`` of a spec document (safe YAML subset, size-limited)."""
    doc = parse_text(yaml_text)
    meta = doc.get("metadata") if isinstance(doc, dict) else None
    name = meta.get("name") if isinstance(meta, dict) else None
    if not isinstance(name, str) or not name:
        raise ValueError("the spec has no metadata.name")
    return name


# ---------------------------------------------------------------------------------- score


def as_score_request(request: ScoreRequest | Mapping[str, Any] | str) -> ScoreRequest:
    """A ScoreRequest from a model, a mapping or YAML/JSON text (validated locally)."""
    if isinstance(request, ScoreRequest):
        return request
    if isinstance(request, str):
        from mapi_spec.loader import load_score_request

        return load_score_request(request)
    data = dict(request)
    data.setdefault("apiVersion", "mapi/v1")
    data.setdefault("kind", "ScoreRequest")
    return ScoreRequest.model_validate(data)


def tree_output(outputs: list[ArtifactLink]) -> ArtifactLink | None:
    """The output holding the score DataTree (the first ``*.zarr.zip`` link)."""
    for link in outputs:
        if link.name.endswith(".zarr.zip"):
            return link
    return None


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """Outcome of :meth:`Models.score`.

    ``tree`` is the scored ``xr.DataTree`` when the ``xarray`` extra is installed and the
    output was downloaded (``download=True``); otherwise fetch ``outputs`` yourself.
    ``summary`` is the requested summary (mean and HDI by default) as plain tables, inlined
    by the server once the run succeeded (no extra needed).
    """

    run: Run
    outputs: list[ArtifactLink] = field(default_factory=list)
    tree: xr.DataTree | None = None
    summary: ScoreSummary | None = None

    @property
    def done(self) -> bool:
        return self.run.state.terminal

    @property
    def tree_link(self) -> ArtifactLink | None:
        """The output link holding the result DataTree (first ``*.zarr.zip``)."""
        return tree_output(self.outputs)

    @classmethod
    def from_call(cls, res: ScoreCallResult, tree: xr.DataTree | None = None) -> ScoreResult:
        return cls(run=res.run, outputs=list(res.outputs), tree=tree, summary=res.summary)
