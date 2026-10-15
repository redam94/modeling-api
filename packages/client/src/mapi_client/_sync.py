"""Synchronous client (``httpx.Client``)."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, overload

import httpx
from mapi_spec.api import (
    LAST_EVENT_ID_HEADER,
    MAX_PREFER_WAIT_SECONDS,
    SSE_RETRY_MS,
    ArtifactLink,
    CatalogResponse,
    Dataset,
    DatasetCreate,
    DatasetVersion,
    DatasetVersionCreate,
    Health,
    IngestMapping,
    LogLine,
    Model,
    ModelVersion,
    Page,
    PlotListItem,
    PlotSpec,
    PlotValidateResponse,
    Project,
    Run,
    RunCreate,
    RunEventView,
    RunRetry,
    ScoreCall,
    SpecCreate,
    SpecDoc,
    SpecRevision,
    SpecRevisionCreate,
    UploadCreate,
    UploadSession,
    ValidateRequest,
    ValidateResponse,
)
from mapi_spec.catalog import ModelCatalogEntry, PlotCatalogEntry, StepCatalogEntry
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.resources import ResourceClass
from mapi_spec.run import RunState
from mapi_spec.spec import ScoreRequest
from pydantic import JsonValue

from mapi_client import _calls as calls
from mapi_client._common import (
    USER_AGENT,
    ScoreResult,
    as_run_create,
    as_score_request,
    dataset_version_error,
    events_page_with_cursor,
    http_timeout,
    run_error,
    spec_name,
    sse_timeout,
    storage_timeout,
    tree_output,
    version_done,
)
from mapi_client._http import Call, RetryPolicy
from mapi_client.auth import BearerAuth
from mapi_client.config import ClientConfig, load_config
from mapi_client.errors import TransportError, raise_for_response, unexpected
from mapi_client.pagination import Paginator
from mapi_client.sse import RunEnd, RunEventFrame, RunStreamItem, SseDecoder, to_stream_item
from mapi_client.transfer import (
    Crc32cAccumulator,
    DownloadSink,
    UploadSource,
    check_put_response,
    check_upload_size,
    datatree_from_bytes,
    default_mapping,
    put_headers,
    upload_source,
    xarray_available,
)

if TYPE_CHECKING:
    import xarray as xr

__all__ = ["Client"]


class Client:
    """Synchronous client for the ``/v1`` API.

    >>> with Client() as api:                      # doctest: +SKIP
    ...     run = api.runs.create({"kind": "fit", "spec_id": sid, "revision": 1})
    ...     run = api.runs.wait(run.id)

    Settings come from :func:`~mapi_client.config.load_config` unless ``config`` is given
    (arguments, ``MAPI_URL``/``MAPI_TOKEN``, ``~/.config/mapi/config.toml``, defaults).
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        token: str | None = None,
        config: ClientConfig | None = None,
        auth: httpx.Auth | None = None,
        transport: httpx.BaseTransport | None = None,
        storage_transport: httpx.BaseTransport | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or load_config(url=base_url, token=token)
        if auth is None and self.config.token is not None:
            auth = BearerAuth(self.config.token)
        self._retry = retry or RetryPolicy(max_retries=self.config.max_retries)
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=self.config.base_url,
            auth=auth,
            timeout=http_timeout(self.config),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            transport=transport,
            verify=self.config.verify,
            follow_redirects=False,
        )
        # signed storage URLs: never send the API credentials there
        self._storage = httpx.Client(
            timeout=storage_timeout(self.config),
            headers={"User-Agent": USER_AGENT},
            transport=storage_transport,
            verify=self.config.verify,
            follow_redirects=False,
        )
        self.projects = Projects(self)
        self.catalog = Catalog(self)
        self.datasets = Datasets(self)
        self.specs = Specs(self)
        self.runs = Runs(self)
        self.models = Models(self)
        self.plots = Plots(self)

    # ---------------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self._http.close()
        self._storage.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Client(base_url={self.config.base_url!r})"

    # ------------------------------------------------------------------------ health
    def health(self) -> Health:
        """``GET /healthz`` (process liveness)."""
        return self.send(calls.health())

    def ready(self) -> Health:
        """``GET /readyz`` (``status`` is ``fail``/``degraded`` when a dependency is down)."""
        return self.send(calls.ready())

    # ------------------------------------------------------------------------- core
    def send[T](self, call: Call[T]) -> T:
        """Send one described request with retries for idempotent calls."""
        attempt = 0
        while True:
            try:
                response = self._http.request(
                    call.method,
                    call.path,
                    params=dict(call.params) or None,
                    json=call.json,
                    headers=dict(call.headers),
                )
            except httpx.TransportError as exc:
                if self._retry.should_retry(call, attempt, None):
                    self._sleep(self._retry.delay(attempt))
                    attempt += 1
                    continue
                raise TransportError(
                    f"{call.method} {call.path} failed: {type(exc).__name__}: {exc}"
                ) from exc
            if response.status_code in call.expect:
                return call.parse(response)
            if self._retry.should_retry(call, attempt, response):
                self._sleep(self._retry.delay(attempt, response))
                attempt += 1
                continue
            if response.status_code < 400:
                raise unexpected(
                    response,
                    f"{call.method} {call.path} returned unexpected status {response.status_code}",
                )
            raise_for_response(response)

    def page_iter[T](self, make: Callable[[str | None], Call[Page[T]]]) -> Paginator[T]:
        return Paginator(lambda cursor: self.send(make(cursor)))

    # -------------------------------------------------------------------- transfers
    def put_signed(self, session: UploadSession, src: UploadSource) -> None:
        """PUT ``src`` to an upload session's signed URL (CRC32C-checked)."""
        check_upload_size(src, session.upload)
        acc = Crc32cAccumulator()
        body = (acc.update(chunk) for chunk in src.chunks())
        try:
            response = self._storage.request(
                "PUT", session.upload.url, content=body, headers=put_headers(src, session.upload)
            )
        except httpx.TransportError as exc:
            raise TransportError(f"upload of {src.filename} failed: {exc}") from exc
        check_put_response(response, acc, src.filename)

    @overload
    def download(self, link: ArtifactLink, dest: None = None) -> bytes: ...
    @overload
    def download(self, link: ArtifactLink, dest: str | os.PathLike[str]) -> Path: ...
    def download(
        self, link: ArtifactLink, dest: str | os.PathLike[str] | None = None
    ) -> bytes | Path:
        """Fetch a signed artifact link; verify size and CRC32C.

        Returns the bytes, or the path when ``dest`` is given (written atomically).
        """
        sink = DownloadSink(link, Path(dest) if dest is not None else None)
        try:
            with self._storage.stream("GET", link.url) as response:
                if response.status_code != 200:
                    response.read()
                    raise_for_response(response)
                for chunk in response.iter_bytes():
                    sink.write(chunk)
            return sink.finish()
        except httpx.TransportError as exc:
            sink.abort()
            raise TransportError(f"download of {link.name} failed: {exc}") from exc
        except BaseException:
            sink.abort()
            raise


class _Resource:
    def __init__(self, client: Client) -> None:
        self._c = client


# =========================================================================== resources


class Projects(_Resource):
    def list(self, *, limit: int | None = None) -> Paginator[Project]:
        return self._c.page_iter(lambda cur: calls.list_projects(limit, cur))

    def get(self, project_id: str) -> Project:
        return self._c.send(calls.get_project(project_id))


class Catalog(_Resource):
    def get(self) -> CatalogResponse:
        """The full catalog and its digest."""
        return self._c.send(calls.get_catalog())

    def models(self, *, limit: int | None = None) -> Paginator[ModelCatalogEntry]:
        return self._c.page_iter(lambda cur: calls.list_catalog_models(limit, cur))

    def model(self, model_id: str, version: str) -> ModelCatalogEntry:
        return self._c.send(calls.get_catalog_model(model_id, version))

    def steps(self, *, limit: int | None = None) -> Paginator[StepCatalogEntry]:
        return self._c.page_iter(lambda cur: calls.list_catalog_steps(limit, cur))

    def plots(self, *, limit: int | None = None) -> Paginator[PlotCatalogEntry]:
        return self._c.page_iter(lambda cur: calls.list_catalog_plots(limit, cur))

    def schema(self, name: str) -> dict[str, Any]:
        """An exported JSON Schema, e.g. ``modelspec.json``."""
        return self._c.send(calls.get_schema(name))


class Datasets(_Resource):
    def create(
        self, name: str, *, description: str | None = None, labels: Mapping[str, str] | None = None
    ) -> Dataset:
        body = DatasetCreate(name=name, description=description, labels=dict(labels or {}))
        return self._c.send(calls.create_dataset(body))

    def list(self, *, limit: int | None = None) -> Paginator[Dataset]:
        return self._c.page_iter(lambda cur: calls.list_datasets(limit, cur))

    def get(self, dataset_id: str) -> Dataset:
        return self._c.send(calls.get_dataset(dataset_id))

    def find(self, name: str) -> Dataset | None:
        """The dataset called ``name`` in the project, if any."""
        return next((d for d in self.list(limit=200) if d.name == name), None)

    def ensure(self, name: str, *, description: str | None = None) -> Dataset:
        """Find the dataset by name or create it (safe against a concurrent create)."""
        found = self.find(name)
        if found is not None:
            return found
        try:
            return self.create(name, description=description)
        except MapiError as exc:
            if exc.code is not ErrorCode.CONFLICT:
                raise
            again = self.find(name)
            if again is None:
                raise
            return again

    def create_upload(
        self, dataset_id: str, *, filename: str, size_bytes: int, content_type: str
    ) -> UploadSession:
        body = UploadCreate(filename=filename, size_bytes=size_bytes, content_type=content_type)
        return self._c.send(calls.create_upload(dataset_id, body))

    def create_version(
        self,
        dataset_id: str,
        *,
        upload_id: str,
        mapping: IngestMapping,
        message: str | None = None,
        idempotency_key: str | None = None,
    ) -> DatasetVersion:
        body = DatasetVersionCreate(upload_id=upload_id, mapping=mapping, message=message)
        return self._c.send(calls.create_dataset_version(dataset_id, body, idempotency_key))

    def versions(self, dataset_id: str, *, limit: int | None = None) -> Paginator[DatasetVersion]:
        return self._c.page_iter(lambda cur: calls.list_dataset_versions(dataset_id, limit, cur))

    def version(self, dataset_id: str, version: int) -> DatasetVersion:
        return self._c.send(calls.get_dataset_version(dataset_id, version))

    def wait_version(
        self,
        dataset_id: str,
        version: int,
        *,
        timeout: float | None = None,
        poll_interval: float = 1.0,
        max_poll_interval: float = 10.0,
    ) -> DatasetVersion:
        """Poll until the ingest finishes; raises the ingest's error if it failed."""
        deadline = None if timeout is None else time.monotonic() + timeout
        delay = poll_interval
        while True:
            current = self.version(dataset_id, version)
            if version_done(current):
                if current.status.value == "failed":
                    run = self._c.runs.get(current.ingest_run_id) if current.ingest_run_id else None
                    raise dataset_version_error(current, run)
                return current
            if deadline is not None and time.monotonic() + delay > deadline:
                raise TimeoutError(f"{current.ref} is still ingesting after {timeout} s")
            self._c._sleep(delay)
            delay = min(delay * 1.5, max_poll_interval)

    def upload(
        self,
        source: str | os.PathLike[str] | xr.Dataset | bytes,
        *,
        dataset: str | None = None,
        dataset_id: str | None = None,
        mapping: IngestMapping | None = None,
        filename: str | None = None,
        message: str | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> DatasetVersion:
        """Upload a file (csv, parquet, ``.zarr.zip``, netCDF) or an ``xr.Dataset`` as a
        new dataset version: upload session → signed PUT → ingest run (→ wait).

        ``dataset`` is a dataset *name* (created when missing); or pass ``dataset_id``.
        csv/parquet need ``mapping``; self-describing formats default to an empty one.
        """
        if (dataset is None) == (dataset_id is None):
            raise ValueError("give exactly one of dataset (a name) or dataset_id")
        src = upload_source(source, filename=filename)
        ingest = mapping or default_mapping(src.format)
        if ingest.format != src.format:
            raise ValueError(f"mapping.format {ingest.format!r} does not match {src.format!r}")
        ds_id = dataset_id or self.ensure(str(dataset)).id
        session = self.create_upload(
            ds_id, filename=src.filename, size_bytes=src.size, content_type=src.content_type
        )
        self._c.put_signed(session, src)
        created = self.create_version(
            ds_id,
            upload_id=session.id,
            mapping=ingest,
            message=message,
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )
        if not wait:
            return created
        return self.wait_version(ds_id, created.version, timeout=timeout)

    def artifact(self, dataset_id: str, version: int, name: str) -> ArtifactLink:
        return self._c.send(calls.get_dataset_artifact(dataset_id, version, name))

    def download(
        self,
        dataset_id: str,
        version: int,
        name: str = "data.zarr.zip",
        dest: str | os.PathLike[str] | None = None,
    ) -> bytes | Path:
        """Download a version artifact (bytes, or the path when ``dest`` is given)."""
        return self._c.download(self.artifact(dataset_id, version, name), dest)


class Specs(_Resource):
    def validate(
        self,
        yaml: str | None = None,
        *,
        document: Mapping[str, JsonValue] | None = None,
        dataset: str | None = None,
    ) -> ValidateResponse:
        """Static validation; ``valid``/``issues`` hold the verdict (200 even when invalid)."""
        body = ValidateRequest(
            yaml=yaml, document=dict(document) if document is not None else None, dataset=dataset
        )
        return self._c.send(calls.validate_spec(body))

    def create(self, yaml: str, *, message: str | None = None) -> SpecDoc:
        return self._c.send(calls.create_spec(SpecCreate(yaml=yaml, message=message)))

    def list(self, *, limit: int | None = None) -> Paginator[SpecDoc]:
        return self._c.page_iter(lambda cur: calls.list_specs(limit, cur))

    def get(self, spec_id: str) -> SpecDoc:
        return self._c.send(calls.get_spec(spec_id))

    def find(self, name: str) -> SpecDoc | None:
        return next((s for s in self.list(limit=200) if s.name == name), None)

    def revise(
        self,
        spec_id: str,
        yaml: str,
        *,
        parent_revision: int | None = None,
        message: str | None = None,
    ) -> SpecRevision:
        """Add a revision. ``parent_revision`` defaults to the current latest (409 if a
        concurrent revision landed in between)."""
        parent = parent_revision or self.get(spec_id).latest_revision
        body = SpecRevisionCreate(yaml=yaml, parent_revision=parent, message=message)
        return self._c.send(calls.create_spec_revision(spec_id, body))

    def upsert(self, yaml: str, *, message: str | None = None) -> SpecRevision:
        """Create the spec named by ``metadata.name``, or add a revision to it."""
        existing = self.find(spec_name(yaml))
        if existing is None:
            doc = self.create(yaml, message=message)
            return doc.latest or self.revision(doc.id, doc.latest_revision)
        return self.revise(
            existing.id, yaml, parent_revision=existing.latest_revision, message=message
        )

    def revisions(self, spec_id: str, *, limit: int | None = None) -> Paginator[SpecRevision]:
        return self._c.page_iter(lambda cur: calls.list_spec_revisions(spec_id, limit, cur))

    def revision(self, spec_id: str, revision: int) -> SpecRevision:
        return self._c.send(calls.get_spec_revision(spec_id, revision))

    def dry_run(self, spec_id: str, revision: int, *, idempotency_key: str | None = None) -> Run:
        """Sandboxed dry-run (a ``validate`` run)."""
        return self._c.send(calls.dry_run_spec(spec_id, revision, idempotency_key))


class Runs(_Resource):
    def create(
        self, run: RunCreate | Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> Run:
        """Submit a run. A fresh ``Idempotency-Key`` is generated unless given; pass your
        own key to make retries across processes safe."""
        return self._c.send(calls.create_run(as_run_create(run), idempotency_key))

    def fit(
        self,
        spec_id: str,
        revision: int,
        *,
        resource_class: ResourceClass | str | None = None,
        labels: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> Run:
        body = RunCreate(
            kind=RunKind.FIT,
            spec_id=spec_id,
            revision=revision,
            resource_class=ResourceClass(resource_class) if resource_class else None,
            labels=dict(labels or {}),
        )
        return self.create(body, idempotency_key=idempotency_key)

    def get(self, run_id: str) -> Run:
        return self._c.send(calls.get_run(run_id))

    def list(
        self,
        *,
        kind: RunKind | str | None = None,
        state: RunState | str | None = None,
        spec_id: str | None = None,
        limit: int | None = None,
    ) -> Paginator[Run]:
        return self._c.page_iter(
            lambda cur: calls.list_runs(limit, cur, kind=kind, state=state, spec_id=spec_id)
        )

    def wait(
        self,
        run_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float = 1.0,
        max_poll_interval: float = 10.0,
        raise_on_failure: bool = False,
        on_update: Callable[[Run], None] | None = None,
    ) -> Run:
        """Poll until the run is terminal (``TimeoutError`` after ``timeout`` seconds).

        With ``raise_on_failure`` a failed/rejected/cancelled run raises its
        :class:`~mapi_spec.errors.MapiError` (``PARTIAL`` is returned: it is resumable).
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        delay = poll_interval
        while True:
            run = self.get(run_id)
            if on_update is not None:
                on_update(run)
            if run.state.terminal:
                err = run_error(run) if raise_on_failure else None
                if err is not None:
                    raise err
                return run
            if deadline is not None and time.monotonic() + delay > deadline:
                raise TimeoutError(f"run {run_id} is still {run.state.value} after {timeout} s")
            self._c._sleep(delay)
            delay = min(delay * 1.5, max_poll_interval)

    def stream_events(
        self,
        run_id: str,
        *,
        last_event_id: int | None = None,
        reconnect: bool = True,
        max_reconnects: int = 5,
    ) -> Iterator[RunStreamItem]:
        """Follow the run's SSE stream until ``end``.

        Yields :class:`RunSnapshot` (first frame of each connection), :class:`RunEventFrame`
        and finally :class:`RunEnd`. A dropped connection is resumed with ``Last-Event-ID``
        (after the server's ``retry`` delay); events are never yielded twice.
        """
        path = calls.run_events_path(run_id)
        last = last_event_id
        failures = 0
        retry_ms = SSE_RETRY_MS
        while True:
            headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
            if last is not None:
                headers[LAST_EVENT_ID_HEADER] = str(last)
            cause: BaseException | None = None
            try:
                with self._c._http.stream(
                    "GET", path, headers=headers, timeout=sse_timeout(self._c.config)
                ) as response:
                    if response.status_code != 200:
                        response.read()
                        if response.status_code < 400:
                            raise unexpected(response, f"unexpected status {response.status_code}")
                        if not (reconnect and response.status_code in (502, 503, 504)):
                            raise_for_response(response)
                    else:
                        ctype = response.headers.get("content-type", "")
                        if not ctype.startswith("text/event-stream"):
                            response.read()
                            raise unexpected(response, f"expected an event stream, got {ctype!r}")
                        decoder = SseDecoder()
                        for line in response.iter_lines():
                            frame = decoder.feed(line)
                            if decoder.retry is not None:
                                retry_ms = decoder.retry
                            item = None if frame is None else to_stream_item(frame)
                            if item is None:
                                continue
                            if isinstance(item, RunEventFrame):
                                if last is not None and item.id <= last:
                                    continue  # replayed twice by an overlapping reconnect
                                last = item.id
                                failures = 0
                            yield item
                            if isinstance(item, RunEnd):
                                return
            except httpx.TransportError as exc:
                cause = exc
            if not reconnect or failures >= max_reconnects:
                raise TransportError(
                    f"the event stream of run {run_id} ended before the run did"
                ) from cause
            failures += 1
            self._c._sleep(retry_ms / 1000.0)

    def events(
        self, run_id: str, *, after: int | None = None, limit: int | None = None
    ) -> Paginator[RunEventView]:
        """Persisted events as JSON pages (ascending ids, after ``after``)."""

        def fetch(cursor: str | None) -> Page[RunEventView]:
            start = int(cursor) if cursor is not None else after
            page = self._c.send(calls.list_run_events(run_id, start, limit))
            return events_page_with_cursor(page)

        return Paginator(fetch)

    def logs(
        self,
        run_id: str,
        *,
        attempt: int | None = None,
        stream: str | None = None,
        limit: int | None = None,
    ) -> Paginator[LogLine]:
        """Captured log lines. ``LogLine.line`` is untrusted text: display it as text."""
        return self._c.page_iter(
            lambda cur: calls.run_logs(run_id, limit, cur, attempt=attempt, stream=stream)
        )

    def cancel(self, run_id: str) -> Run:
        return self._c.send(calls.cancel_run(run_id))

    def resume(self, run_id: str) -> Run:
        return self._c.send(calls.resume_run(run_id))

    def retry(
        self,
        run_id: str,
        *,
        resource_class: ResourceClass | str | None = None,
        seed: int | None = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> Run:
        body = RunRetry(
            resource_class=ResourceClass(resource_class) if resource_class else None,
            seed=seed,
            reason=reason,
        )
        return self._c.send(calls.retry_run(run_id, body, idempotency_key))


class Models(_Resource):
    def list(self, *, limit: int | None = None) -> Paginator[Model]:
        return self._c.page_iter(lambda cur: calls.list_models(limit, cur))

    def get(self, model_id: str) -> Model:
        return self._c.send(calls.get_model(model_id))

    def versions(self, model_id: str, *, limit: int | None = None) -> Paginator[ModelVersion]:
        return self._c.page_iter(lambda cur: calls.list_model_versions(model_id, limit, cur))

    def version(self, model_id: str, version: int) -> ModelVersion:
        return self._c.send(calls.get_model_version(model_id, version))

    def artifact(self, model_id: str, version: int, name: str) -> ArtifactLink:
        return self._c.send(calls.get_model_artifact(model_id, version, name))

    def download(
        self,
        model_id: str,
        version: int,
        name: str,
        dest: str | os.PathLike[str] | None = None,
    ) -> bytes | Path:
        """Download a bundle object or ``ui/`` view (bytes, or the path with ``dest``)."""
        return self._c.download(self.artifact(model_id, version, name), dest)

    def score(
        self,
        model_id: str,
        version: int,
        request: ScoreRequest | Mapping[str, Any] | str,
        *,
        wait: int = MAX_PREFER_WAIT_SECONDS,
        timeout: float | None = None,
        resource_class: ResourceClass | str | None = None,
        labels: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        download: bool = True,
        raise_on_failure: bool = True,
        poll_interval: float = 1.0,
    ) -> ScoreResult:
        """Score with replayed preprocessing and wait for the result.

        Sends ``Prefer: wait=<wait>``; while the run is not done, the same request is
        replayed with the same ``Idempotency-Key`` (no second run is created) until it
        finishes or ``timeout`` passes (``TimeoutError``). With the ``xarray`` extra and
        ``download=True`` the scored ``xr.DataTree`` is fetched (CRC32C-verified) into
        :attr:`ScoreResult.tree`.
        """
        body = ScoreCall(
            request=as_score_request(request),
            resource_class=ResourceClass(resource_class) if resource_class else None,
            labels=dict(labels or {}),
        )
        key = idempotency_key or str(uuid.uuid4())
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            this_wait = wait if remaining is None else max(0, min(wait, int(remaining)))
            res = self._c.send(
                calls.score_model(model_id, version, body, wait=this_wait, idempotency_key=key)
            )
            if res.done or res.run.state.terminal:
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"score run {res.run.id} is still {res.run.state.value}")
            if this_wait < 1:
                self._c._sleep(poll_interval)
        if raise_on_failure:
            err = run_error(res.run)
            if err is not None:
                raise err
        tree = None
        link = tree_output(list(res.outputs))
        if download and link is not None and xarray_available():
            tree = datatree_from_bytes(self._c.download(link))
        return ScoreResult.from_call(res, tree)


class Plots(_Resource):
    def list(
        self, model_id: str, version: int, *, limit: int | None = None
    ) -> Paginator[PlotListItem]:
        return self._c.page_iter(lambda cur: calls.list_plots(model_id, version, limit, cur))

    def get(self, model_id: str, version: int, plot_id: str) -> PlotSpec:
        """A sanitized PlotSpec (render it only in the plot-sandbox origin)."""
        return self._c.send(calls.get_plot(model_id, version, plot_id))

    def validate(self, spec: PlotSpec | Mapping[str, Any]) -> PlotValidateResponse:
        plot = spec if isinstance(spec, PlotSpec) else PlotSpec.model_validate(dict(spec))
        return self._c.send(calls.validate_plot(plot))
