"""Dispatch of a run kind to its implementation."""

from __future__ import annotations

from mapi_spec.calibration import PriorTranslationJob
from mapi_spec.executor import RunKind
from mapi_spec.spec import ModelSpec

from mapi_runner.jobs import (
    DeriveJob,
    DescribeJob,
    EvaluateJob,
    IngestJob,
    RenderJob,
    ScoreJob,
    parse_job,
)
from mapi_runner.worker.context import WorkerContext

__all__ = ["run_kind"]


def run_kind(ctx: WorkerContext) -> None:
    """Run ``ctx.start.kind`` (raises on failure; the caller reports the error)."""
    kind = ctx.start.kind
    job = parse_job(kind, dict(ctx.start.spec))
    if kind is RunKind.INGEST:
        from mapi_runner.worker.ingest import run_ingest

        assert isinstance(job, IngestJob)
        run_ingest(ctx, job)
    elif kind is RunKind.DESCRIBE:
        from mapi_runner.worker.validate import run_describe

        assert isinstance(job, DescribeJob)
        run_describe(ctx)
    elif kind is RunKind.VALIDATE:
        from mapi_runner.worker.validate import run_validate

        assert isinstance(job, ModelSpec)
        run_validate(ctx, job)
    elif kind is RunKind.FIT:
        from mapi_runner.worker.fit import run_fit

        assert isinstance(job, ModelSpec)
        run_fit(ctx, job)
    elif kind is RunKind.SCORE:
        from mapi_runner.worker.score import run_score

        assert isinstance(job, ScoreJob)
        run_score(ctx, job)
    elif kind is RunKind.EVALUATE:
        from mapi_runner.worker.score import run_evaluate

        assert isinstance(job, EvaluateJob)
        run_evaluate(ctx, job)
    elif kind is RunKind.DERIVE:
        from mapi_runner.worker.score import run_derive

        assert isinstance(job, DeriveJob)
        run_derive(ctx, job)
    elif kind is RunKind.RENDER:
        from mapi_runner.worker.render import run_render

        assert isinstance(job, RenderJob)
        run_render(ctx, job)
    elif kind is RunKind.TRANSLATE_PRIORS:
        from mapi_runner.worker.priors import run_translate_priors

        assert isinstance(job, PriorTranslationJob)
        run_translate_priors(ctx, job)
    elif kind is RunKind.WORKFLOW_NODE:
        from mapi_spec.workflow import WorkflowNodeJob

        from mapi_runner.worker.workflow import run_workflow_node

        assert isinstance(job, WorkflowNodeJob)
        run_workflow_node(ctx, job)
    else:  # pragma: no cover - parse_job raises first
        raise NotImplementedError(f"the runner does not execute {kind.value!r} runs")
