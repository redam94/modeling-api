"""Asynchronous client (``httpx.AsyncClient``); same surface as :class:`~mapi_client.Client`."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, overload

import httpx
from mapi_spec.api import (
    LAST_EVENT_ID_HEADER,
    MAX_PREFER_WAIT_SECONDS,
    PROJECT_HEADER,
    SSE_RETRY_MS,
    ApiToken,
    ArtifactLink,
    AuditEvent,
    CatalogResponse,
    Dataset,
    DatasetCreate,
    DatasetVersion,
    DatasetVersionCreate,
    ExecutorHealth,
    ExecutorUpdate,
    Health,
    IngestMapping,
    KillSwitches,
    KillSwitchUpdate,
    LogLine,
    Me,
    Membership,
    MembershipCreate,
    MembershipUpdate,
    Model,
    ModelVersion,
    Page,
    PlotListItem,
    PlotSpec,
    PlotValidateResponse,
    Project,
    ProjectCreate,
    ProjectUpdate,
    QuotaPolicy,
    QuotaPolicyUpsert,
    Run,
    RunCreate,
    RunEventView,
    RunRetry,
    ScoreCall,
    ScoreCallResult,
    SpecCreate,
    SpecDoc,
    SpecRevision,
    SpecRevisionCreate,
    Tenant,
    TenantAdminUpdate,
    TenantCreate,
    TenantUpdate,
    TokenCreate,
    TokenCreated,
    UploadCreate,
    UploadSession,
    UsageGroupBy,
    UsagePeriod,
    UsageReport,
    ValidateRequest,
    ValidateResponse,
)
from mapi_spec.audit import AuditAction, AuditOutcome
from mapi_spec.catalog import ModelCatalogEntry, PlotCatalogEntry, StepCatalogEntry
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.quotas import QuotaLimits, QuotaScope
from mapi_spec.resources import ResourceClass
from mapi_spec.run import RunState
from mapi_spec.spec import ScoreRequest
from mapi_spec.tenancy import ProjectRole, TenantStatus, TrustTier
from mapi_spec.tokens import DEFAULT_TOKEN_TTL_DAYS, BudgetPeriod, TokenStatus
from pydantic import JsonValue

from mapi_client import _calls as calls
from mapi_client._common import (
    USER_AGENT,
    ScoreResult,
    as_run_create,
    as_score_request,
    call_timeout,
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
from mapi_client._http import Call, RetryPolicy, RetryState
from mapi_client.auth import default_auth
from mapi_client.config import ClientConfig, check_project, load_config
from mapi_client.errors import TransportError, raise_for_response, unexpected
from mapi_client.pagination import AsyncPaginator
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

__all__ = ["AsyncClient"]


class AsyncClient:
    """Asynchronous client for the ``/v1`` API (use ``async with``).

    Configuration, credentials and project selection work as for
    :class:`~mapi_client.Client`. A stored ``mapi login`` sign-in is renewed synchronously
    inside the request (a brief blocking call to the identity provider).
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        token: str | None = None,
        project: str | None = None,
        config: ClientConfig | None = None,
        auth: httpx.Auth | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        storage_transport: httpx.AsyncBaseTransport | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config or load_config(url=base_url, token=token, project=project)
        if config is not None and project is not None:
            self.config = self.config.with_overrides(project=project)
        if auth is None:
            auth = default_auth(self.config)
        self._retry = retry or RetryPolicy(max_retries=self.config.max_retries)
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=self.config.base_url,
            auth=auth,
            timeout=http_timeout(self.config),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            transport=transport,
            verify=self.config.verify,
            follow_redirects=False,
        )
        self._storage = httpx.AsyncClient(
            timeout=storage_timeout(self.config),
            headers={"User-Agent": USER_AGENT},
            transport=storage_transport,
            verify=self.config.verify,
            follow_redirects=False,
        )
        self.projects = AsyncProjects(self)
        self.catalog = AsyncCatalog(self)
        self.datasets = AsyncDatasets(self)
        self.specs = AsyncSpecs(self)
        self.runs = AsyncRuns(self)
        self.models = AsyncModels(self)
        self.plots = AsyncPlots(self)
        self.tenants = AsyncTenants(self)
        self.tokens = AsyncTokens(self)
        self.usage = AsyncUsage(self)
        self.admin = AsyncAdmin(self)

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._storage.aclose()

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"AsyncClient(base_url={self.config.base_url!r}, project={self.project!r})"

    @property
    def project(self) -> str | None:
        """The ``X-Mapi-Project`` selector of project-scoped requests."""
        return self.config.project

    def use(self, project: str | None) -> None:
        """Select the project for subsequent requests (``<tenant>/<project>``)."""
        self.config = self.config.with_overrides(
            project=None if project is None else check_project(project)
        )

    def scope_headers(self) -> dict[str, str]:
        """``X-Mapi-Project`` for a project-scoped request (empty without a selection)."""
        return {PROJECT_HEADER: self.config.project} if self.config.project else {}

    async def me(self) -> Me:
        """``GET /v1/me``: who you are and where you may act (v0.3)."""
        return await self.send(calls.get_me())

    async def health(self) -> Health:
        return await self.send(calls.health())

    async def ready(self) -> Health:
        return await self.send(calls.ready())

    async def send[T](self, call: Call[T]) -> T:
        """Send one described request; see :class:`RetryPolicy` for what is resent."""
        state = RetryState()
        headers = {**self.scope_headers(), **call.headers} if call.scoped else dict(call.headers)
        while True:
            try:
                response = await self._http.request(
                    call.method,
                    call.path,
                    params=dict(call.params) or None,
                    json=call.json,
                    headers=headers,
                    timeout=call_timeout(self.config, call.server_wait),
                )
            except httpx.TransportError as exc:
                wait = self._retry.plan(call, state, None)
                if wait is not None:
                    await self._sleep(wait)
                    continue
                raise TransportError(
                    f"{call.method} {call.path} failed: {type(exc).__name__}: {exc}"
                ) from exc
            if response.status_code in call.expect:
                return call.parse(response)
            wait = self._retry.plan(call, state, response)
            if wait is not None:
                await self._sleep(wait)
                continue
            if response.status_code < 400:
                raise unexpected(
                    response,
                    f"{call.method} {call.path} returned unexpected status {response.status_code}",
                )
            raise_for_response(response)

    def page_iter[T](self, make: Callable[[str | None], Call[Page[T]]]) -> AsyncPaginator[T]:
        async def fetch(cursor: str | None) -> Page[T]:
            return await self.send(make(cursor))

        return AsyncPaginator(fetch)

    async def put_signed(self, session: UploadSession, src: UploadSource) -> None:
        check_upload_size(src, session.upload)
        acc = Crc32cAccumulator()

        async def body() -> AsyncIterator[bytes]:
            async for chunk in src.achunks():
                yield acc.update(chunk)

        try:
            response = await self._storage.request(
                "PUT", session.upload.url, content=body(), headers=put_headers(src, session.upload)
            )
        except httpx.TransportError as exc:
            raise TransportError(f"upload of {src.filename} failed: {exc}") from exc
        check_put_response(response, acc, src.filename)

    @overload
    async def download(self, link: ArtifactLink, dest: None = None) -> bytes: ...
    @overload
    async def download(self, link: ArtifactLink, dest: str | os.PathLike[str]) -> Path: ...
    async def download(
        self, link: ArtifactLink, dest: str | os.PathLike[str] | None = None
    ) -> bytes | Path:
        sink = DownloadSink(link, Path(dest) if dest is not None else None)
        try:
            async with self._storage.stream("GET", link.url) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise_for_response(response)
                async for chunk in response.aiter_bytes():
                    sink.write(chunk)
            return sink.finish()
        except httpx.TransportError as exc:
            sink.abort()
            raise TransportError(f"download of {link.name} failed: {exc}") from exc
        except BaseException:
            sink.abort()
            raise


class _Resource:
    def __init__(self, client: AsyncClient) -> None:
        self._c = client


class AsyncProjects(_Resource):
    def list(self, *, limit: int | None = None) -> AsyncPaginator[Project]:
        return self._c.page_iter(lambda cur: calls.list_projects(limit, cur))

    async def get(self, project_id: str) -> Project:
        return await self._c.send(calls.get_project(project_id))


class AsyncCatalog(_Resource):
    async def get(self) -> CatalogResponse:
        return await self._c.send(calls.get_catalog())

    def models(self, *, limit: int | None = None) -> AsyncPaginator[ModelCatalogEntry]:
        return self._c.page_iter(lambda cur: calls.list_catalog_models(limit, cur))

    async def model(self, model_id: str, version: str) -> ModelCatalogEntry:
        return await self._c.send(calls.get_catalog_model(model_id, version))

    def steps(self, *, limit: int | None = None) -> AsyncPaginator[StepCatalogEntry]:
        return self._c.page_iter(lambda cur: calls.list_catalog_steps(limit, cur))

    def plots(self, *, limit: int | None = None) -> AsyncPaginator[PlotCatalogEntry]:
        return self._c.page_iter(lambda cur: calls.list_catalog_plots(limit, cur))

    async def schema(self, name: str) -> dict[str, Any]:
        return await self._c.send(calls.get_schema(name))


class AsyncDatasets(_Resource):
    async def create(
        self, name: str, *, description: str | None = None, labels: Mapping[str, str] | None = None
    ) -> Dataset:
        body = DatasetCreate(name=name, description=description, labels=dict(labels or {}))
        return await self._c.send(calls.create_dataset(body))

    def list(self, *, limit: int | None = None) -> AsyncPaginator[Dataset]:
        return self._c.page_iter(lambda cur: calls.list_datasets(limit, cur))

    async def get(self, dataset_id: str) -> Dataset:
        return await self._c.send(calls.get_dataset(dataset_id))

    async def find(self, name: str) -> Dataset | None:
        async for d in self.list(limit=200):
            if d.name == name:
                return d
        return None

    async def ensure(self, name: str, *, description: str | None = None) -> Dataset:
        found = await self.find(name)
        if found is not None:
            return found
        try:
            return await self.create(name, description=description)
        except MapiError as exc:
            if exc.code is not ErrorCode.CONFLICT:
                raise
            again = await self.find(name)
            if again is None:
                raise
            return again

    async def create_upload(
        self, dataset_id: str, *, filename: str, size_bytes: int, content_type: str
    ) -> UploadSession:
        body = UploadCreate(filename=filename, size_bytes=size_bytes, content_type=content_type)
        return await self._c.send(calls.create_upload(dataset_id, body))

    async def create_version(
        self,
        dataset_id: str,
        *,
        upload_id: str,
        mapping: IngestMapping,
        message: str | None = None,
        idempotency_key: str | None = None,
    ) -> DatasetVersion:
        body = DatasetVersionCreate(upload_id=upload_id, mapping=mapping, message=message)
        return await self._c.send(calls.create_dataset_version(dataset_id, body, idempotency_key))

    def versions(
        self, dataset_id: str, *, limit: int | None = None
    ) -> AsyncPaginator[DatasetVersion]:
        return self._c.page_iter(lambda cur: calls.list_dataset_versions(dataset_id, limit, cur))

    async def version(self, dataset_id: str, version: int) -> DatasetVersion:
        return await self._c.send(calls.get_dataset_version(dataset_id, version))

    async def wait_version(
        self,
        dataset_id: str,
        version: int,
        *,
        timeout: float | None = None,
        poll_interval: float = 1.0,
        max_poll_interval: float = 10.0,
    ) -> DatasetVersion:
        deadline = None if timeout is None else time.monotonic() + timeout
        delay = poll_interval
        while True:
            current = await self.version(dataset_id, version)
            if version_done(current):
                if current.status.value == "failed":
                    run = (
                        await self._c.runs.get(current.ingest_run_id)
                        if current.ingest_run_id
                        else None
                    )
                    raise dataset_version_error(current, run)
                return current
            if deadline is not None and time.monotonic() + delay > deadline:
                raise TimeoutError(f"{current.ref} is still ingesting after {timeout} s")
            await self._c._sleep(delay)
            delay = min(delay * 1.5, max_poll_interval)

    async def upload(
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
        """See :meth:`mapi_client.Client.datasets.upload`."""
        if (dataset is None) == (dataset_id is None):
            raise ValueError("give exactly one of dataset (a name) or dataset_id")
        src = upload_source(source, filename=filename)
        ingest = mapping or default_mapping(src.format)
        if ingest.format != src.format:
            raise ValueError(f"mapping.format {ingest.format!r} does not match {src.format!r}")
        ds_id = dataset_id or (await self.ensure(str(dataset))).id
        session = await self.create_upload(
            ds_id, filename=src.filename, size_bytes=src.size, content_type=src.content_type
        )
        await self._c.put_signed(session, src)
        created = await self.create_version(
            ds_id,
            upload_id=session.id,
            mapping=ingest,
            message=message,
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )
        if not wait:
            return created
        return await self.wait_version(ds_id, created.version, timeout=timeout)

    async def artifact(self, dataset_id: str, version: int, name: str) -> ArtifactLink:
        return await self._c.send(calls.get_dataset_artifact(dataset_id, version, name))

    async def download(
        self,
        dataset_id: str,
        version: int,
        name: str = "data.zarr.zip",
        dest: str | os.PathLike[str] | None = None,
    ) -> bytes | Path:
        return await self._c.download(await self.artifact(dataset_id, version, name), dest)


class AsyncSpecs(_Resource):
    async def validate(
        self,
        yaml: str | None = None,
        *,
        document: Mapping[str, JsonValue] | None = None,
        dataset: str | None = None,
    ) -> ValidateResponse:
        body = ValidateRequest(
            yaml=yaml, document=dict(document) if document is not None else None, dataset=dataset
        )
        return await self._c.send(calls.validate_spec(body))

    async def create(self, yaml: str, *, message: str | None = None) -> SpecDoc:
        return await self._c.send(calls.create_spec(SpecCreate(yaml=yaml, message=message)))

    def list(self, *, limit: int | None = None) -> AsyncPaginator[SpecDoc]:
        return self._c.page_iter(lambda cur: calls.list_specs(limit, cur))

    async def get(self, spec_id: str) -> SpecDoc:
        return await self._c.send(calls.get_spec(spec_id))

    async def find(self, name: str) -> SpecDoc | None:
        async for s in self.list(limit=200):
            if s.name == name:
                return s
        return None

    async def revise(
        self,
        spec_id: str,
        yaml: str,
        *,
        parent_revision: int | None = None,
        message: str | None = None,
    ) -> SpecRevision:
        parent = parent_revision or (await self.get(spec_id)).latest_revision
        body = SpecRevisionCreate(yaml=yaml, parent_revision=parent, message=message)
        return await self._c.send(calls.create_spec_revision(spec_id, body))

    async def upsert(self, yaml: str, *, message: str | None = None) -> SpecRevision:
        existing = await self.find(spec_name(yaml))
        if existing is None:
            doc = await self.create(yaml, message=message)
            return doc.latest or await self.revision(doc.id, doc.latest_revision)
        return await self.revise(
            existing.id, yaml, parent_revision=existing.latest_revision, message=message
        )

    def revisions(self, spec_id: str, *, limit: int | None = None) -> AsyncPaginator[SpecRevision]:
        return self._c.page_iter(lambda cur: calls.list_spec_revisions(spec_id, limit, cur))

    async def revision(self, spec_id: str, revision: int) -> SpecRevision:
        return await self._c.send(calls.get_spec_revision(spec_id, revision))

    async def dry_run(
        self, spec_id: str, revision: int, *, idempotency_key: str | None = None
    ) -> Run:
        return await self._c.send(calls.dry_run_spec(spec_id, revision, idempotency_key))


class AsyncRuns(_Resource):
    async def create(
        self, run: RunCreate | Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> Run:
        return await self._c.send(calls.create_run(as_run_create(run), idempotency_key))

    async def fit(
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
        return await self.create(body, idempotency_key=idempotency_key)

    async def get(self, run_id: str) -> Run:
        return await self._c.send(calls.get_run(run_id))

    def list(
        self,
        *,
        kind: RunKind | str | None = None,
        state: RunState | str | None = None,
        spec_id: str | None = None,
        limit: int | None = None,
    ) -> AsyncPaginator[Run]:
        return self._c.page_iter(
            lambda cur: calls.list_runs(limit, cur, kind=kind, state=state, spec_id=spec_id)
        )

    async def wait(
        self,
        run_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float = 1.0,
        max_poll_interval: float = 10.0,
        raise_on_failure: bool = False,
        on_update: Callable[[Run], None] | None = None,
    ) -> Run:
        deadline = None if timeout is None else time.monotonic() + timeout
        delay = poll_interval
        while True:
            run = await self.get(run_id)
            if on_update is not None:
                on_update(run)
            if run.state.terminal:
                err = run_error(run) if raise_on_failure else None
                if err is not None:
                    raise err
                return run
            if deadline is not None and time.monotonic() + delay > deadline:
                raise TimeoutError(f"run {run_id} is still {run.state.value} after {timeout} s")
            await self._c._sleep(delay)
            delay = min(delay * 1.5, max_poll_interval)

    async def stream_events(
        self,
        run_id: str,
        *,
        last_event_id: int | None = None,
        reconnect: bool = True,
        max_reconnects: int = 5,
    ) -> AsyncIterator[RunStreamItem]:
        """See :meth:`mapi_client.Client.runs.stream_events`."""
        path = calls.run_events_path(run_id)
        last = last_event_id
        failures = 0
        retry_ms = SSE_RETRY_MS
        while True:
            headers = {
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
                **self._c.scope_headers(),
            }
            if last is not None:
                headers[LAST_EVENT_ID_HEADER] = str(last)
            cause: BaseException | None = None
            try:
                async with self._c._http.stream(
                    "GET", path, headers=headers, timeout=sse_timeout(self._c.config)
                ) as response:
                    if response.status_code != 200:
                        await response.aread()
                        if response.status_code < 400:
                            raise unexpected(response, f"unexpected status {response.status_code}")
                        if not (reconnect and response.status_code in (502, 503, 504)):
                            raise_for_response(response)
                    else:
                        ctype = response.headers.get("content-type", "")
                        if not ctype.startswith("text/event-stream"):
                            await response.aread()
                            raise unexpected(response, f"expected an event stream, got {ctype!r}")
                        decoder = SseDecoder()
                        async for line in response.aiter_lines():
                            frame = decoder.feed(line)
                            if decoder.retry is not None:
                                retry_ms = decoder.retry
                            item = None if frame is None else to_stream_item(frame)
                            if item is None:
                                continue
                            if isinstance(item, RunEventFrame):
                                if last is not None and item.id <= last:
                                    continue
                                last = item.id
                            # a healthy connection (it delivered a frame): a later cut is a
                            # normal proxy/request limit, not a failure towards max_reconnects
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
            await self._c._sleep(retry_ms / 1000.0)

    def events(
        self, run_id: str, *, after: int | None = None, limit: int | None = None
    ) -> AsyncPaginator[RunEventView]:
        async def fetch(cursor: str | None) -> Page[RunEventView]:
            start = int(cursor) if cursor is not None else after
            page = await self._c.send(calls.list_run_events(run_id, start, limit))
            return events_page_with_cursor(page)

        return AsyncPaginator(fetch)

    def logs(
        self,
        run_id: str,
        *,
        attempt: int | None = None,
        stream: str | None = None,
        limit: int | None = None,
    ) -> AsyncPaginator[LogLine]:
        return self._c.page_iter(
            lambda cur: calls.run_logs(run_id, limit, cur, attempt=attempt, stream=stream)
        )

    def plots(self, run_id: str, *, limit: int | None = None) -> AsyncPaginator[PlotSpec]:
        """Sanitized plots a succeeded run rendered (empty until it succeeds). Render them
        only in the plot-sandbox origin."""
        return self._c.page_iter(lambda cur: calls.list_run_plots(run_id, limit, cur))

    async def cancel(self, run_id: str) -> Run:
        return await self._c.send(calls.cancel_run(run_id))

    async def resume(self, run_id: str) -> Run:
        return await self._c.send(calls.resume_run(run_id))

    async def retry(
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
        return await self._c.send(calls.retry_run(run_id, body, idempotency_key))


class AsyncModels(_Resource):
    def list(self, *, limit: int | None = None) -> AsyncPaginator[Model]:
        return self._c.page_iter(lambda cur: calls.list_models(limit, cur))

    async def get(self, model_id: str) -> Model:
        return await self._c.send(calls.get_model(model_id))

    def versions(self, model_id: str, *, limit: int | None = None) -> AsyncPaginator[ModelVersion]:
        return self._c.page_iter(lambda cur: calls.list_model_versions(model_id, limit, cur))

    async def version(self, model_id: str, version: int) -> ModelVersion:
        return await self._c.send(calls.get_model_version(model_id, version))

    async def artifact(self, model_id: str, version: int, name: str) -> ArtifactLink:
        return await self._c.send(calls.get_model_artifact(model_id, version, name))

    async def download(
        self,
        model_id: str,
        version: int,
        name: str,
        dest: str | os.PathLike[str] | None = None,
    ) -> bytes | Path:
        return await self._c.download(await self.artifact(model_id, version, name), dest)

    async def score(
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
        """See :meth:`mapi_client.Client.models.score`."""
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
            res: ScoreCallResult | None
            try:
                res = await self._c.send(
                    calls.score_model(model_id, version, body, wait=this_wait, idempotency_key=key)
                )
            except TransportError as exc:
                # No answer within wait + timeout: the run is still going. Replaying the
                # same Idempotency-Key is safe (no second run); anything else is a failure.
                if not isinstance(exc.__cause__, httpx.ReadTimeout):
                    raise
                res = None
            if res is not None and (res.done or res.run.state.terminal):
                break
            if deadline is not None and time.monotonic() >= deadline:
                if res is None:
                    raise TimeoutError(f"score request {key} is still running after {timeout} s")
                raise TimeoutError(f"score run {res.run.id} is still {res.run.state.value}")
            if this_wait < 1:
                await self._c._sleep(poll_interval)
        if raise_on_failure:
            err = run_error(res.run)
            if err is not None:
                raise err
        tree = None
        link = tree_output(list(res.outputs))
        if download and link is not None and xarray_available():
            tree = datatree_from_bytes(await self._c.download(link))
        return ScoreResult.from_call(res, tree)


class AsyncPlots(_Resource):
    def list(
        self, model_id: str, version: int, *, limit: int | None = None
    ) -> AsyncPaginator[PlotListItem]:
        return self._c.page_iter(lambda cur: calls.list_plots(model_id, version, limit, cur))

    async def get(self, model_id: str, version: int, plot_id: str) -> PlotSpec:
        return await self._c.send(calls.get_plot(model_id, version, plot_id))

    async def validate(self, spec: PlotSpec | Mapping[str, Any]) -> PlotValidateResponse:
        plot = spec if isinstance(spec, PlotSpec) else PlotSpec.model_validate(dict(spec))
        return await self._c.send(calls.validate_plot(plot))


# ====================================================================== M2 resources (v0.3)


class AsyncTenants(_Resource):
    """Tenants the caller belongs to: projects, members, usage and audit (v0.3)."""

    def list(self, *, limit: int | None = None) -> AsyncPaginator[Tenant]:
        return self._c.page_iter(lambda cur: calls.list_tenants(limit, cur))

    async def get(self, tenant_id: str) -> Tenant:
        return await self._c.send(calls.get_tenant(tenant_id))

    async def rename(self, tenant_id: str, name: str) -> Tenant:
        """Change the display name (tenant admins)."""
        return await self._c.send(calls.update_tenant(tenant_id, TenantUpdate(name=name)))

    def projects(self, tenant_id: str, *, limit: int | None = None) -> AsyncPaginator[Project]:
        return self._c.page_iter(lambda cur: calls.list_tenant_projects(tenant_id, limit, cur))

    async def create_project(
        self, tenant_id: str, project_id: str, name: str, *, description: str | None = None
    ) -> Project:
        body = ProjectCreate(id=project_id, name=name, description=description)
        return await self._c.send(calls.create_project(tenant_id, body))

    async def update_project(
        self,
        tenant_id: str,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Project:
        fields = {k: v for k, v in (("name", name), ("description", description)) if v}
        body = ProjectUpdate.model_validate(fields)
        return await self._c.send(calls.update_project(tenant_id, project_id, body))

    async def delete_project(self, tenant_id: str, project_id: str) -> None:
        """Delete an empty project (``CONFLICT`` while it holds data)."""
        await self._c.send(calls.delete_project(tenant_id, project_id))

    def members(
        self, tenant_id: str, project_id: str | None = None, *, limit: int | None = None
    ) -> AsyncPaginator[Membership]:
        """Tenant-wide members, or the members of one project (incl. tenant-wide grants)."""
        return self._c.page_iter(lambda cur: calls.list_members(tenant_id, project_id, limit, cur))

    async def add_member(
        self,
        tenant_id: str,
        role: ProjectRole | str,
        *,
        project_id: str | None = None,
        principal_id: str | None = None,
        email: str | None = None,
    ) -> Membership:
        """Grant ``role`` to a known principal or invite an ``email``."""
        body = MembershipCreate(principal_id=principal_id, email=email, role=ProjectRole(role))
        return await self._c.send(calls.add_member(tenant_id, project_id, body))

    async def set_role(
        self,
        tenant_id: str,
        principal_id: str,
        role: ProjectRole | str,
        *,
        project_id: str | None = None,
    ) -> Membership:
        body = MembershipUpdate(role=ProjectRole(role))
        return await self._c.send(calls.update_member(tenant_id, project_id, principal_id, body))

    async def remove_member(
        self, tenant_id: str, principal_id: str, *, project_id: str | None = None
    ) -> None:
        await self._c.send(calls.remove_member(tenant_id, project_id, principal_id))

    async def usage(
        self,
        tenant_id: str,
        *,
        period: UsagePeriod | None = None,
        start: datetime | None = None,
        group_by: UsageGroupBy | None = None,
    ) -> UsageReport:
        """Cost and quotas of the whole tenant (tenant admins)."""
        return await self._c.send(
            calls.get_tenant_usage(tenant_id, period=period, start=start, group_by=group_by)
        )

    def audit(
        self,
        tenant_id: str,
        *,
        limit: int | None = None,
        project_id: str | None = None,
        actor_id: str | None = None,
        action: AuditAction | str | None = None,
        outcome: AuditOutcome | str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> AsyncPaginator[AuditEvent]:
        """The tenant's audit log (tenant admins)."""
        return self._c.page_iter(
            lambda cur: calls.list_tenant_audit(
                tenant_id,
                limit,
                cur,
                project_id=project_id,
                actor_id=actor_id,
                action=action,
                outcome=outcome,
                since=since,
                until=until,
            )
        )


class AsyncTokens(_Resource):
    """API tokens of the selected project. Secrets are returned once, by :meth:`create`."""

    def list(
        self,
        *,
        all_owners: bool = False,
        status: TokenStatus | str | None = None,
        limit: int | None = None,
    ) -> AsyncPaginator[ApiToken]:
        """Your tokens (``all_owners``: everyone's, project admins)."""
        return self._c.page_iter(
            lambda cur: calls.list_tokens(limit, cur, all_owners=all_owners, status=status)
        )

    async def create(
        self,
        name: str,
        scopes: Sequence[str],
        *,
        expires_in_days: int = DEFAULT_TOKEN_TTL_DAYS,
        rate_limit_per_minute: int | None = None,
        vcpu_seconds_budget: float | None = None,
        budget_period: BudgetPeriod | str = BudgetPeriod.MONTH,
    ) -> TokenCreated:
        """Mint a token. ``TokenCreated.secret`` is the only copy of the secret: store it.

        The request is never resent after a lost response (that could mint two tokens).
        """
        body = TokenCreate(
            name=name,
            scopes=list(scopes),
            expires_in_days=expires_in_days,
            rate_limit_per_minute=rate_limit_per_minute,
            vcpu_seconds_budget=vcpu_seconds_budget,
            budget_period=BudgetPeriod(budget_period),
        )
        return await self._c.send(calls.create_token(body))

    async def get(self, token_id: str) -> ApiToken:
        return await self._c.send(calls.get_token(token_id))

    async def revoke(self, token_id: str) -> None:
        """Revoke a token (idempotent)."""
        await self._c.send(calls.revoke_token(token_id))


class AsyncUsage(_Resource):
    """Metered cost and quota headroom (v0.3)."""

    async def get(
        self,
        *,
        period: UsagePeriod | None = None,
        start: datetime | None = None,
        group_by: UsageGroupBy | None = None,
    ) -> UsageReport:
        """The selected project's usage for a UTC day or month (default: this month)."""
        return await self._c.send(calls.get_usage(period=period, start=start, group_by=group_by))

    async def tenant(
        self,
        tenant_id: str,
        *,
        period: UsagePeriod | None = None,
        start: datetime | None = None,
        group_by: UsageGroupBy | None = None,
    ) -> UsageReport:
        """The whole tenant's usage (tenant admins)."""
        return await self._c.tenants.usage(tenant_id, period=period, start=start, group_by=group_by)


class AsyncAdmin(_Resource):
    """Platform operator endpoints (``/v1/admin``). Every change is audited."""

    def tenants(
        self,
        *,
        status: TenantStatus | str | None = None,
        trust_tier: TrustTier | str | None = None,
        limit: int | None = None,
    ) -> AsyncPaginator[Tenant]:
        return self._c.page_iter(
            lambda cur: calls.admin_list_tenants(limit, cur, status=status, trust_tier=trust_tier)
        )

    async def tenant(self, tenant_id: str) -> Tenant:
        return await self._c.send(calls.admin_get_tenant(tenant_id))

    async def create_tenant(self, body: TenantCreate | Mapping[str, Any]) -> Tenant:
        tc = body if isinstance(body, TenantCreate) else TenantCreate.model_validate(dict(body))
        return await self._c.send(calls.admin_create_tenant(tc))

    async def update_tenant(
        self, tenant_id: str, body: TenantAdminUpdate | Mapping[str, Any]
    ) -> Tenant:
        """Change a tenant's tier, status (``status_reason`` required) or residency."""
        upd = (
            body
            if isinstance(body, TenantAdminUpdate)
            else TenantAdminUpdate.model_validate(dict(body))
        )
        return await self._c.send(calls.admin_update_tenant(tenant_id, upd))

    def quotas(
        self,
        *,
        scope: QuotaScope | str | None = None,
        subject_id: str | None = None,
        limit: int | None = None,
    ) -> AsyncPaginator[QuotaPolicy]:
        return self._c.page_iter(
            lambda cur: calls.admin_list_quotas(limit, cur, scope=scope, subject_id=subject_id)
        )

    async def set_quota(
        self,
        scope: QuotaScope | str,
        subject_id: str,
        limits: QuotaLimits | Mapping[str, Any],
        *,
        note: str | None = None,
    ) -> QuotaPolicy:
        """Create or replace the policy of ``(scope, subject_id)`` (unset limits: none)."""
        body = QuotaPolicyUpsert(
            scope=QuotaScope(scope),
            subject_id=subject_id,
            limits=limits
            if isinstance(limits, QuotaLimits)
            else QuotaLimits.model_validate(dict(limits)),
            note=note,
        )
        return await self._c.send(calls.admin_put_quota(body))

    async def delete_quota(self, scope: QuotaScope | str, subject_id: str) -> None:
        """Remove a policy (the tier default applies again)."""
        await self._c.send(calls.admin_delete_quota(scope, subject_id))

    async def kill_switches(self) -> KillSwitches:
        return await self._c.send(calls.admin_get_kill_switches())

    async def set_kill_switches(self, update: KillSwitchUpdate | Mapping[str, Any]) -> KillSwitches:
        """Flip kill switches (``reason`` is required and audited)."""
        body = (
            update
            if isinstance(update, KillSwitchUpdate)
            else KillSwitchUpdate.model_validate(dict(update))
        )
        return await self._c.send(calls.admin_put_kill_switches(body))

    def executors(self, *, limit: int | None = None) -> AsyncPaginator[ExecutorHealth]:
        return self._c.page_iter(lambda cur: calls.admin_list_executors(limit, cur))

    async def executor(self, name: str) -> ExecutorHealth:
        return await self._c.send(calls.admin_get_executor(name))

    async def set_executor(self, name: str, *, enabled: bool, reason: str) -> ExecutorHealth:
        """Enable or disable an executor (``reason`` is required and audited). A disabled
        executor gets no new runs; runs already on it keep running."""
        body = ExecutorUpdate(enabled=enabled, reason=reason)
        return await self._c.send(calls.admin_put_executor(name, body))

    def audit(
        self,
        *,
        limit: int | None = None,
        tenant_id: str | None = None,
        project_id: str | None = None,
        actor_id: str | None = None,
        action: AuditAction | str | None = None,
        outcome: AuditOutcome | str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> AsyncPaginator[AuditEvent]:
        """The platform audit log."""
        return self._c.page_iter(
            lambda cur: calls.admin_list_audit(
                limit,
                cur,
                tenant_id=tenant_id,
                project_id=project_id,
                actor_id=actor_id,
                action=action,
                outcome=outcome,
                since=since,
                until=until,
            )
        )
