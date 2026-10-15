"""Local (in-process) execution for ``mapi check/fit/score`` without a server.

Uses the SDK registry (entry points of the installed plugins) and the
:class:`~mapi_sdk.executors.InprocExecutor`. Isolation is NONE: only run code you trust.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from mapi_spec.api import new_id
from mapi_spec.errors import ErrorCode, MapiError, error_from_problem
from mapi_spec.executor import ExecState, ExecStatus, RunEvent, RunKind, RunSubmit
from mapi_spec.loader import parse_text
from mapi_spec.schema import DataSchema

if TYPE_CHECKING:
    import xarray as xr
    from mapi_sdk.registry import Registry

__all__ = [
    "LocalRun",
    "load_registry",
    "parse_assignments",
    "raw_schema",
    "read_dataset",
    "read_schema_file",
    "run_inproc",
]

SCHEMA_ATTR: Final = "mapi_schema"
"""Dataset attribute holding the raw DataSchema JSON (``mapi_sdk.harness.SCHEMA_ATTR``)."""
_POLL_SECONDS: Final = 0.2


def load_registry() -> tuple[Registry, list[str]]:
    """The process registry from installed entry points, plus plugin load errors."""
    from mapi_sdk.registry import default_registry

    reg = default_registry()
    return reg, list(reg.load_errors)


def read_dataset(path: Path) -> xr.Dataset:
    """Read a local ``*.zarr.zip`` with the SDK's validating reader."""
    if not path.is_file():
        raise FileNotFoundError(f"{path} is not a file")
    if not path.name.lower().endswith((".zarr.zip", ".zip")):
        raise MapiError(
            f"{path.name}: local runs read .zarr.zip datasets; csv/parquet need an ingest "
            "mapping (upload them with 'mapi datasets upload --mapping')",
            code=ErrorCode.SCHEMA_INVALID,
        )
    from mapi_sdk.wire import read_zarr_zip

    return read_zarr_zip(path)


def read_schema_file(path: Path) -> DataSchema:
    """A DataSchema from a JSON or YAML file (safe YAML subset)."""
    return DataSchema.model_validate(parse_text(path.read_text(encoding="utf-8")))


def raw_schema(ds: xr.Dataset, schema_file: Path | None = None) -> DataSchema:
    """The dataset's raw schema: an explicit file, the stored attribute, else inferred."""
    if schema_file is not None:
        return read_schema_file(schema_file)
    stored = ds.attrs.get(SCHEMA_ATTR)
    if isinstance(stored, str):
        return DataSchema.model_validate_json(stored)
    if isinstance(stored, Mapping):
        return DataSchema.model_validate(dict(stored))
    from mapi_sdk.schema import infer_schema

    return infer_schema(ds)


def parse_assignments(values: Sequence[str], what: str = "--side-table") -> dict[str, Path]:
    """``name=path`` pairs (repeatable option) as a mapping."""
    out: dict[str, Path] = {}
    for item in values:
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            raise MapiError(
                f"{what} expects NAME=PATH, got {item!r}", code=ErrorCode.SCHEMA_INVALID
            )
        if name in out:
            raise MapiError(f"{what} {name!r} given twice", code=ErrorCode.SCHEMA_INVALID)
        out[name] = Path(path).expanduser()
    return out


def file_ref(path: Path) -> str:
    return "file://" + str(path.expanduser().resolve())


@dataclass(slots=True)
class LocalRun:
    run_id: str
    status: ExecStatus
    result: Any
    events: list[RunEvent]

    @property
    def ok(self) -> bool:
        return self.status.state is ExecState.SUCCEEDED

    def error(self) -> MapiError:
        if self.status.problem is not None:
            return error_from_problem(self.status.problem)
        return MapiError(
            f"local run {self.run_id} ended {self.status.state.value}",
            code=ErrorCode.CANCELLED
            if self.status.state is ExecState.CANCELLED
            else ErrorCode.INTERNAL_ERROR,
        )


def run_inproc(
    kind: RunKind,
    payload: Mapping[str, Any],
    *,
    registry: Registry | None = None,
    on_event: Callable[[RunEvent], None] | None = None,
    timeout: float | None = None,
) -> LocalRun:
    """Run one fit/score in this process; events are delivered to ``on_event`` as they come."""
    from mapi_sdk.executors import InprocExecutor

    json.dumps(payload)  # payloads are JSON only (same contract as remote executors)
    executor = InprocExecutor(registry=registry)
    submit = RunSubmit(run_id=new_id("run"), kind=kind, payload=dict(payload))

    async def main() -> LocalRun:
        handle = await executor.submit(submit, nonce=secrets.token_hex(16))
        seen = 0

        def drain() -> None:
            nonlocal seen
            events = executor.events(handle)
            for ev in events[seen:]:
                if on_event is not None:
                    on_event(ev)
            seen = len(events)

        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        try:
            while not (await executor.status(handle)).state.terminal:
                drain()
                if deadline is not None and loop.time() > deadline:
                    await executor.cancel(handle)
                    raise TimeoutError(f"local run {submit.run_id} exceeded {timeout} s")
                await asyncio.sleep(_POLL_SECONDS)
        except BaseException:
            await executor.cancel(handle)
            raise
        status = await executor.wait(handle)
        drain()
        result = executor.result(handle) if status.state is ExecState.SUCCEEDED else None
        return LocalRun(submit.run_id, status, result, executor.events(handle))

    return asyncio.run(main())
