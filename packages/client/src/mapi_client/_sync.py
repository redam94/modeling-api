"""Synchronous client (``httpx.Client``)."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, overload

import httpx
from mapi_spec.api import (
    LAST_EVENT_ID_HEADER,
    MAX_PREFER_WAIT_SECONDS,
    PROJECT_HEADER,
    SSE_RETRY_MS,
    AdminUsageReport,
    ApiToken,
    ArtifactLink,
    AuditEvent,
    CatalogResponse,
    Dataset,
    DatasetCreate,
    DatasetVersion,
    DatasetVersionCreate,
    ExecutorEvidence,
    ExecutorEvidenceUpdate,
    ExecutorHealth,
    ExecutorUpdate,
    Health,
    IngestAssembly,
    IngestMapping,
    KillSwitches,
    KillSwitchUpdate,
    LogLine,
    Me,
    Membership,
    MembershipCreate,
    MembershipUpdate,
    Model,
    ModelInputSchema,
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
    SignupAccepted,
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
    UploadInspection,
    UploadSession,
    UsageGroupBy,
    UsagePeriod,
    UsageReport,
    ValidateRequest,
    ValidateResponse,
    VerificationResult,
)
from mapi_spec.audit import AuditAction, AuditOutcome
from mapi_spec.catalog import ModelCatalogEntry, PlotCatalogEntry, StepCatalogEntry
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.ingest import CoordValue
from mapi_spec.quotas import QuotaLimits, QuotaScope
from mapi_spec.resources import ResourceClass
from mapi_spec.run import RunState
from mapi_spec.spec import ScoreRequest
from mapi_spec.tenancy import Capability, ProjectRole, TenantStatus, TrustTier
from mapi_spec.tokens import DEFAULT_TOKEN_TTL_DAYS, BudgetPeriod, TokenStatus
from mapi_spec.tree import TreeJoin
from pydantic import JsonValue

from mapi_client import _calls as calls
from mapi_client import _calls_m4 as m4calls
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
from mapi_client._m3 import (
    AdminTrustMixin,
    Endpoints,
    Guided,
    PlotSharingMixin,
    RemoteEndpoint,
    TenantTrustMixin,
    Webhooks,
)
from mapi_client._m3 import signup as _signup
from mapi_client._m3 import verify_email as _verify_email
from mapi_client._m4_common import (
    AssemblyFile,
    InspectedUpload,
    assembly_for,
    is_not_ready,
    plan_assembly,
)
from mapi_client._m35 import (
    CatalogAboutMixin,
    DatasetAboutMixin,
    Explorations,
    Governance,
    ModelInsightMixin,
    Validations,
    Workflows,
)
from mapi_client.auth import default_auth
from mapi_client.config import ClientConfig, check_project, load_config
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
    mapping_reads,
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
    (arguments, ``MAPI_URL``/``MAPI_TOKEN``/``MAPI_PROJECT``, ``~/.config/mapi/config.toml``,
    defaults). Credentials: the configured token, else the sign-in ``mapi login`` stored for
    the profile (see :func:`~mapi_client.auth.default_auth`). ``project``
    (``<tenant>/<project>``) is sent as ``X-Mapi-Project`` on project-scoped requests.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        token: str | None = None,
        project: str | None = None,
        config: ClientConfig | None = None,
        auth: httpx.Auth | None = None,
        transport: httpx.BaseTransport | None = None,
        storage_transport: httpx.BaseTransport | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or load_config(url=base_url, token=token, project=project)
        if config is not None and project is not None:
            self.config = self.config.with_overrides(project=project)
        if auth is None:
            auth = default_auth(self.config)
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
        self.tenants = Tenants(self)
        self.tokens = Tokens(self)
        self.usage = Usage(self)
        self.admin = Admin(self)
        self.guided = Guided(self)
        self.endpoints = Endpoints(self)
        self.webhooks = Webhooks(self)
        self.workflows = Workflows(self)
        self.governance = Governance(self)
        self.validations = Validations(self)
        self.explorations = Explorations(self)

    # ---------------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self._http.close()
        self._storage.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Client(base_url={self.config.base_url!r}, project={self.project!r})"

    # ----------------------------------------------------------------------- project
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

    def me(self) -> Me:
        """``GET /v1/me``: who you are and where you may act (v0.3)."""
        return self.send(calls.get_me())

    def endpoint(self, alias: str) -> RemoteEndpoint:
        """An endpoint of the selected project as an object (``.schema()``,
        ``.validate(ds)``, ``.score(ds) -> xr.Dataset``)."""
        return RemoteEndpoint(self, alias)

    def signup(
        self,
        email: str,
        name: str,
        *,
        accepted_terms: bool,
        organisation: str | None = None,
        terms_version: str | None = None,
        challenge: str | None = None,
    ) -> SignupAccepted:
        """``POST /auth/signup`` (public; the answer is the same for known addresses). The
        e-mail carries a link with the token for :meth:`verify_email`."""
        return _signup(
            self,
            email,
            name,
            accepted_terms=accepted_terms,
            organisation=organisation,
            terms_version=terms_version,
            challenge=challenge,
        )

    def verify_email(self, token: str) -> VerificationResult:
        """``POST /auth/verify-email``: creates a public-tier tenant and its project."""
        return _verify_email(self, token)

    # ------------------------------------------------------------------------ health
    def health(self) -> Health:
        """``GET /healthz`` (process liveness)."""
        return self.send(calls.health())

    def ready(self) -> Health:
        """``GET /readyz`` (``status`` is ``fail``/``degraded`` when a dependency is down)."""
        return self.send(calls.ready())

    # ------------------------------------------------------------------------- core
    def send[T](self, call: Call[T]) -> T:
        """Send one described request; see :class:`RetryPolicy` for what is resent."""
        state = RetryState()
        headers = {**self.scope_headers(), **call.headers} if call.scoped else dict(call.headers)
        while True:
            try:
                response = self._http.request(
                    call.method,
                    call.path,
                    params=dict(call.params) or None,
                    json=call.json,
                    headers=headers,
                    timeout=call_timeout(self.config, call.server_wait),
                    auth=None if call.public else httpx.USE_CLIENT_DEFAULT,
                )
            except httpx.TransportError as exc:
                wait = self._retry.plan(call, state, None)
                if wait is not None:
                    self._sleep(wait)
                    continue
                raise TransportError(
                    f"{call.method} {call.path} failed: {type(exc).__name__}: {exc}"
                ) from exc
            if response.status_code in call.expect:
                return call.parse(response)
            wait = self._retry.plan(call, state, response)
            if wait is not None:
                self._sleep(wait)
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


class Catalog(_Resource, CatalogAboutMixin):
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

    def plot_example(self, plot_id: str, version: str) -> PlotSpec:
        """A plot recipe rendered on synthetic example data."""
        return self._c.send(calls.get_catalog_plot_example(plot_id, version))

    def schema(self, name: str) -> dict[str, Any]:
        """An exported JSON Schema, e.g. ``modelspec.json``."""
        return self._c.send(calls.get_schema(name))


class Datasets(_Resource, DatasetAboutMixin):
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
        upload_id: str | None = None,
        mapping: IngestMapping | None = None,
        assembly: IngestAssembly | None = None,
        message: str | None = None,
        idempotency_key: str | None = None,
    ) -> DatasetVersion:
        """Ingest one upload (``upload_id`` + ``mapping``) or, from M4, several combined into
        one version (``assembly``) - exactly one of the two."""
        body = DatasetVersionCreate(
            upload_id=upload_id, mapping=mapping, assembly=assembly, message=message
        )
        return self._c.send(calls.create_dataset_version(dataset_id, body, idempotency_key))

    def inspect_upload(
        self, dataset_id: str, upload_id: str, *, idempotency_key: str | None = None
    ) -> Run:
        """Start (or find) the ``inspect`` run listing the groups and arrays of an upload
        (M4). Read the result with :meth:`upload_inspection` once the run has finished."""
        return self._c.send(m4calls.inspect_upload(dataset_id, upload_id, idempotency_key))

    def upload_inspection(self, dataset_id: str, upload_id: str) -> UploadInspection:
        """What the inspect run found (M4); ``NOT_READY`` (409) while it is running."""
        return self._c.send(m4calls.get_upload_inspection(dataset_id, upload_id))

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
        source: str | os.PathLike[str] | xr.Dataset | xr.DataTree | bytes,
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
        """Upload a file (csv, parquet, ``.zarr.zip``, netCDF, HDF5), an ``xr.Dataset`` or
        an ``xr.DataTree`` as a new dataset version: upload session → signed PUT → ingest
        run (→ wait).

        ``dataset`` is a dataset *name* (created when missing); or pass ``dataset_id``.
        csv/parquet and raw HDF5 (``.h5``/``.hdf5``/``.he5``) need ``mapping``;
        self-describing formats default to an empty one. Several files make one version
        with :meth:`upload_files`.
        """
        if (dataset is None) == (dataset_id is None):
            raise ValueError("give exactly one of dataset (a name) or dataset_id")
        src = upload_source(source, filename=filename)
        ingest = mapping or default_mapping(src.format)
        if not mapping_reads(ingest.format, src.format):
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

    def ensure_id(self, dataset: str | None, dataset_id: str | None) -> str:
        """The id of ``dataset_id``, or of the dataset named ``dataset`` (created when
        missing) - exactly one of the two."""
        if (dataset is None) == (dataset_id is None):
            raise ValueError("give exactly one of dataset (a name) or dataset_id")
        return dataset_id or (self.ensure(str(dataset))).id

    def stage_upload(
        self,
        source: str | os.PathLike[str] | xr.Dataset | xr.DataTree | bytes,
        *,
        dataset_id: str,
        filename: str | None = None,
    ) -> UploadSession:
        """Upload a file to the dataset's quarantine *without* ingesting it (M4): to
        :meth:`inspect` it, or to ingest it later with :meth:`create_version`."""
        return self._stage(dataset_id, upload_source(source, filename=filename))

    def _stage(self, dataset_id: str, src: UploadSource) -> UploadSession:
        session = self.create_upload(
            dataset_id, filename=src.filename, size_bytes=src.size, content_type=src.content_type
        )
        self._c.put_signed(session, src)
        return session

    def upload_files(
        self,
        files: Sequence[AssemblyFile | str | os.PathLike[str] | xr.Dataset | xr.DataTree | bytes],
        *,
        dataset: str | None = None,
        dataset_id: str | None = None,
        mapping: IngestMapping | None = None,
        node: str = "/",
        concat: str | None = None,
        labels: Sequence[CoordValue] | None = None,
        merge: bool = False,
        join: TreeJoin = "exact",
        message: str | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> DatasetVersion:
        """Upload several files as ONE dataset version (M4, an ``IngestAssembly``).

        Plain items (paths, ``xr.Dataset``/``xr.DataTree``) all go to ``node`` and are
        combined there: ``concat="year", labels=[2024, 2025]`` stacks them along a dim (one
        label per file), ``merge=True`` merges their variables on shared coords (``join``
        ``exact``/``inner``/``outer``). Items given as :class:`~mapi_client.AssemblyFile`
        carry their own node, mapping and combine rule, e.g. a PSF file at ``/instrument``.
        ``mapping`` applies to every file whose format it reads (self-describing formats
        default to an empty one). The assembly is checked before anything is uploaded.
        """
        plan = plan_assembly(
            files, mapping=mapping, node=node, concat=concat, labels=labels, merge=merge,
            join=join,
        )  # fmt: skip
        ds_id = self.ensure_id(dataset, dataset_id)
        upload_ids = [(self._stage(ds_id, p.upload)).id for p in plan]
        created = self.create_version(
            ds_id,
            assembly=assembly_for(plan, upload_ids),
            message=message,
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )
        if not wait:
            return created
        return self.wait_version(ds_id, created.version, timeout=timeout)

    def inspect(
        self,
        dataset_id: str,
        upload_id: str,
        *,
        timeout: float | None = 300.0,
        poll_interval: float = 1.0,
        max_poll_interval: float = 10.0,
    ) -> UploadInspection:
        """Inspect an upload (M4): start (or find) its ``inspect`` run, then poll the
        inspection until it is ready (``NOT_READY`` means "still running"). Raises the run's
        error when the inspection failed and ``TimeoutError`` after ``timeout`` seconds."""
        run = self.inspect_upload(dataset_id, upload_id)
        deadline = None if timeout is None else time.monotonic() + timeout
        delay = poll_interval
        while True:
            err = run_error(run)
            if err is not None:
                raise err
            try:
                return self.upload_inspection(dataset_id, upload_id)
            except MapiError as exc:
                if exc.code is ErrorCode.NOT_FOUND:
                    # the run may have failed since it was polled: its error says why
                    err = run_error(self._c.runs.get(run.id))
                    if err is not None:
                        raise err from None
                if not is_not_ready(exc):
                    raise
            if deadline is not None and time.monotonic() + delay > deadline:
                raise TimeoutError(
                    f"inspection of upload {upload_id} is not ready after {timeout} s"
                )
            self._c._sleep(delay)
            delay = min(delay * 1.5, max_poll_interval)
            run = self._c.runs.get(run.id)

    def inspect_file(
        self,
        source: str | os.PathLike[str] | xr.Dataset | xr.DataTree | bytes,
        *,
        dataset: str | None = None,
        dataset_id: str | None = None,
        filename: str | None = None,
        timeout: float | None = 300.0,
    ) -> InspectedUpload:
        """Stage a file (:meth:`stage_upload`) and :meth:`inspect` it: the groups and arrays
        (path, shape, dtype, dims hint) needed to write a raw-HDF5 mapping. Ingest it later
        with ``create_version(dataset_id, upload_id=found.upload_id, mapping=...)``."""
        ds_id = self.ensure_id(dataset, dataset_id)
        session = self.stage_upload(source, dataset_id=ds_id, filename=filename)
        found = self.inspect(ds_id, session.id, timeout=timeout)
        return InspectedUpload(session, found)

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
        ``max_reconnects`` bounds *consecutive* connections that delivered no frame, so a
        long run behind a proxy that cuts every connection is followed to the end.
        """
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

    def plots(self, run_id: str, *, limit: int | None = None) -> Paginator[PlotSpec]:
        """Sanitized plots a succeeded run rendered (empty until it succeeds). Render them
        only in the plot-sandbox origin."""
        return self._c.page_iter(lambda cur: calls.list_run_plots(run_id, limit, cur))

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


class Models(_Resource, ModelInsightMixin):
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

    def inputs(
        self, model_id: str, version: int, *, periods: int | None = None
    ) -> ModelInputSchema:
        """What a score call of the version can send (``GET …/inputs``): the input contract
        and the input table with ``periods`` periods after the fit."""
        return self._c.send(calls.get_model_inputs(model_id, version, periods))

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
        finishes or ``timeout`` passes (``TimeoutError``). The read timeout of each call is
        the client's ``timeout`` plus ``wait``, and a read timeout counts as "still running".
        With the ``xarray`` extra and
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
            res: ScoreCallResult | None
            try:
                res = self._c.send(
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


class Plots(_Resource, PlotSharingMixin):
    """A model version's plots, and (v0.4) saved plots, static renders and embed links."""

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


# ====================================================================== M2 resources (v0.3)


class Tenants(_Resource, TenantTrustMixin):
    """Tenants the caller belongs to: projects, members, usage and audit (v0.3);
    verification, deletion and GDPR export (v0.4)."""

    def list(self, *, limit: int | None = None) -> Paginator[Tenant]:
        return self._c.page_iter(lambda cur: calls.list_tenants(limit, cur))

    def get(self, tenant_id: str) -> Tenant:
        return self._c.send(calls.get_tenant(tenant_id))

    def rename(self, tenant_id: str, name: str) -> Tenant:
        """Change the display name (tenant admins)."""
        return self._c.send(calls.update_tenant(tenant_id, TenantUpdate(name=name)))

    def projects(self, tenant_id: str, *, limit: int | None = None) -> Paginator[Project]:
        return self._c.page_iter(lambda cur: calls.list_tenant_projects(tenant_id, limit, cur))

    def create_project(
        self, tenant_id: str, project_id: str, name: str, *, description: str | None = None
    ) -> Project:
        body = ProjectCreate(id=project_id, name=name, description=description)
        return self._c.send(calls.create_project(tenant_id, body))

    def update_project(
        self,
        tenant_id: str,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Project:
        fields = {k: v for k, v in (("name", name), ("description", description)) if v}
        body = ProjectUpdate.model_validate(fields)
        return self._c.send(calls.update_project(tenant_id, project_id, body))

    def delete_project(self, tenant_id: str, project_id: str) -> None:
        """Delete an empty project (``CONFLICT`` while it holds data)."""
        self._c.send(calls.delete_project(tenant_id, project_id))

    def members(
        self, tenant_id: str, project_id: str | None = None, *, limit: int | None = None
    ) -> Paginator[Membership]:
        """Tenant-wide members, or the members of one project (incl. tenant-wide grants)."""
        return self._c.page_iter(lambda cur: calls.list_members(tenant_id, project_id, limit, cur))

    def add_member(
        self,
        tenant_id: str,
        role: ProjectRole | str,
        *,
        project_id: str | None = None,
        principal_id: str | None = None,
        email: str | None = None,
        capabilities: Sequence[Capability | str] = (),
    ) -> Membership:
        """Grant ``role`` to a known principal or invite an ``email``, with optional
        capabilities on top of the role (``reviewer`` approves model validations)."""
        body = MembershipCreate(
            principal_id=principal_id,
            email=email,
            role=ProjectRole(role),
            capabilities=[Capability(c) for c in capabilities],
        )
        return self._c.send(calls.add_member(tenant_id, project_id, body))

    def set_role(
        self,
        tenant_id: str,
        principal_id: str,
        role: ProjectRole | str,
        *,
        project_id: str | None = None,
    ) -> Membership:
        body = MembershipUpdate(role=ProjectRole(role))
        return self._c.send(calls.update_member(tenant_id, project_id, principal_id, body))

    def set_capabilities(
        self,
        tenant_id: str,
        principal_id: str,
        capabilities: Sequence[Capability | str],
        *,
        project_id: str | None = None,
    ) -> Membership:
        """Replace a member's capabilities (``[]`` removes them all); the role is kept."""
        body = MembershipUpdate(capabilities=[Capability(c) for c in capabilities])
        return self._c.send(calls.update_member(tenant_id, project_id, principal_id, body))

    def remove_member(
        self, tenant_id: str, principal_id: str, *, project_id: str | None = None
    ) -> None:
        self._c.send(calls.remove_member(tenant_id, project_id, principal_id))

    def usage(
        self,
        tenant_id: str,
        *,
        period: UsagePeriod | None = None,
        start: datetime | None = None,
        group_by: UsageGroupBy | None = None,
    ) -> UsageReport:
        """Cost and quotas of the whole tenant (tenant admins)."""
        return self._c.send(
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
    ) -> Paginator[AuditEvent]:
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


class Tokens(_Resource):
    """API tokens of the selected project. Secrets are returned once, by :meth:`create`."""

    def list(
        self,
        *,
        all_owners: bool = False,
        status: TokenStatus | str | None = None,
        limit: int | None = None,
    ) -> Paginator[ApiToken]:
        """Your tokens (``all_owners``: everyone's, project admins)."""
        return self._c.page_iter(
            lambda cur: calls.list_tokens(limit, cur, all_owners=all_owners, status=status)
        )

    def create(
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
        return self._c.send(calls.create_token(body))

    def get(self, token_id: str) -> ApiToken:
        return self._c.send(calls.get_token(token_id))

    def revoke(self, token_id: str) -> None:
        """Revoke a token (idempotent)."""
        self._c.send(calls.revoke_token(token_id))


class Usage(_Resource):
    """Metered cost and quota headroom (v0.3)."""

    def get(
        self,
        *,
        period: UsagePeriod | None = None,
        start: datetime | None = None,
        group_by: UsageGroupBy | None = None,
    ) -> UsageReport:
        """The selected project's usage for a UTC day or month (default: this month)."""
        return self._c.send(calls.get_usage(period=period, start=start, group_by=group_by))

    def tenant(
        self,
        tenant_id: str,
        *,
        period: UsagePeriod | None = None,
        start: datetime | None = None,
        group_by: UsageGroupBy | None = None,
    ) -> UsageReport:
        """The whole tenant's usage (tenant admins)."""
        return self._c.tenants.usage(tenant_id, period=period, start=start, group_by=group_by)


class Admin(_Resource, AdminTrustMixin):
    """Platform operator endpoints (``/v1/admin``). Every change is audited."""

    def tenants(
        self,
        *,
        status: TenantStatus | str | None = None,
        trust_tier: TrustTier | str | None = None,
        limit: int | None = None,
    ) -> Paginator[Tenant]:
        return self._c.page_iter(
            lambda cur: calls.admin_list_tenants(limit, cur, status=status, trust_tier=trust_tier)
        )

    def tenant(self, tenant_id: str) -> Tenant:
        return self._c.send(calls.admin_get_tenant(tenant_id))

    def create_tenant(self, body: TenantCreate | Mapping[str, Any]) -> Tenant:
        tc = body if isinstance(body, TenantCreate) else TenantCreate.model_validate(dict(body))
        return self._c.send(calls.admin_create_tenant(tc))

    def update_tenant(self, tenant_id: str, body: TenantAdminUpdate | Mapping[str, Any]) -> Tenant:
        """Change a tenant's tier, status (``status_reason`` required) or residency."""
        upd = (
            body
            if isinstance(body, TenantAdminUpdate)
            else TenantAdminUpdate.model_validate(dict(body))
        )
        return self._c.send(calls.admin_update_tenant(tenant_id, upd))

    def quotas(
        self,
        *,
        scope: QuotaScope | str | None = None,
        subject_id: str | None = None,
        limit: int | None = None,
    ) -> Paginator[QuotaPolicy]:
        return self._c.page_iter(
            lambda cur: calls.admin_list_quotas(limit, cur, scope=scope, subject_id=subject_id)
        )

    def set_quota(
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
        return self._c.send(calls.admin_put_quota(body))

    def delete_quota(self, scope: QuotaScope | str, subject_id: str) -> None:
        """Remove a policy (the tier default applies again)."""
        self._c.send(calls.admin_delete_quota(scope, subject_id))

    def kill_switches(self) -> KillSwitches:
        return self._c.send(calls.admin_get_kill_switches())

    def set_kill_switches(self, update: KillSwitchUpdate | Mapping[str, Any]) -> KillSwitches:
        """Flip kill switches (``reason`` is required and audited)."""
        body = (
            update
            if isinstance(update, KillSwitchUpdate)
            else KillSwitchUpdate.model_validate(dict(update))
        )
        return self._c.send(calls.admin_put_kill_switches(body))

    def executors(self, *, limit: int | None = None) -> Paginator[ExecutorHealth]:
        return self._c.page_iter(lambda cur: calls.admin_list_executors(limit, cur))

    def executor(self, name: str) -> ExecutorHealth:
        return self._c.send(calls.admin_get_executor(name))

    def executor_evidence(self, name: str) -> ExecutorEvidence:
        """The conformance and security-suite reports recorded for an executor."""
        return self._c.send(calls.admin_get_executor_evidence(name))

    def record_executor_evidence(
        self, name: str, update: ExecutorEvidenceUpdate | Mapping[str, Any]
    ) -> ExecutorEvidence:
        """Record suite results; shared tiers need both, passing, for the current caps."""
        body = (
            update
            if isinstance(update, ExecutorEvidenceUpdate)
            else ExecutorEvidenceUpdate.model_validate(dict(update))
        )
        return self._c.send(calls.admin_put_executor_evidence(name, body))

    def usage(
        self, *, period: str | None = None, start: datetime | str | None = None
    ) -> AdminUsageReport:
        """Cost by tenant for a UTC ``day`` or ``month`` and spend against the global cap."""
        return self._c.send(calls.admin_get_usage(period=period, start=start))

    def set_executor(self, name: str, *, enabled: bool, reason: str) -> ExecutorHealth:
        """Enable or disable an executor (``reason`` is required and audited). A disabled
        executor gets no new runs; runs already on it keep running."""
        body = ExecutorUpdate(enabled=enabled, reason=reason)
        return self._c.send(calls.admin_put_executor(name, body))

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
    ) -> Paginator[AuditEvent]:
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
