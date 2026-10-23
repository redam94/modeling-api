"""``plugin_inspect`` (v0.19): check an uploaded wheel without importing or extracting it.

The run downloads the wheel (input ``plugin.whl``), checks it with
:func:`mapi_runner.plugin_inspect.inspect_wheel` against the distributions installed in this
image, and writes ``plugin/inspection.json`` (:class:`~mapi_spec.plugins.PluginInspection`).
A wheel with problems is still a successful run: the control plane reads the problems and
moves the version to ``inspect_failed``. The run fails only when it cannot do the check
(the input is missing, the output cannot be written).
"""

from __future__ import annotations

from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.jobs import PLUGIN_INPUT, PLUGIN_INSPECTION_OUTPUT, PluginInspectJob
from mapi_spec.plugins import INSPECTION_MAX_BYTES
from mapi_spec.run import RunnerPhase

from mapi_runner.plugin_inspect import inspect_wheel, installed_index
from mapi_runner.worker.context import WorkerContext

__all__ = ["run_plugin_inspect"]


def run_plugin_inspect(ctx: WorkerContext, job: PluginInspectJob) -> None:
    """Inspect the uploaded wheel and write :data:`PLUGIN_INSPECTION_OUTPUT`."""
    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the wheel")
    path = ctx.download(PLUGIN_INPUT)
    assert path is not None
    ctx.state(RunnerPhase.VALIDATE, "checking the files and metadata of the wheel")
    inspection = inspect_wheel(path, job=job, index=installed_index())
    data = inspection.model_dump_json().encode("utf-8")
    if len(data) > INSPECTION_MAX_BYTES:  # pragma: no cover - inspect_wheel trims first
        raise MapiError(
            "The inspection report is larger than the control plane accepts.",
            code=ErrorCode.INTERNAL_ERROR,
        )
    ctx.write_output(PLUGIN_INSPECTION_OUTPUT, data)
    problems = len(inspection.problems)
    ctx.progress(
        1.0,
        "wheel checked: no problems" if inspection.ok else f"wheel checked: {problems} problem(s)",
        force=True,
    )
