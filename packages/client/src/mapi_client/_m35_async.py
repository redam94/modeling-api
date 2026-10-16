"""Asynchronous M3.5 resources (workflows, governance, explorers, optimisation, About cards;
v0.5).

The same resources as :mod:`mapi_client._m35`, awaited: ``AsyncClient.workflows``,
``.governance``, ``.validations``, ``.explorations`` and the M3.5 methods of ``.catalog``,
``.datasets`` and ``.models``.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, overload

import httpx
from mapi_spec.about import About, AboutKind
from mapi_spec.api import (
    LAST_EVENT_ID_HEADER,
    MAX_PREFER_WAIT_SECONDS,
    SSE_RETRY_MS,
    ApprovalCreate,
    ArtifactLink,
    CatalogAbout,
    CheckResult,
    ChooserQuestionnaire,
    ChooserResult,
    DataExploreRequest,
    DatasetAbout,
    DatasetAboutUpdate,
    EvidencePack,
    EvidencePackRequest,
    Exploration,
    GovernanceCheckDef,
    GovernancePolicy,
    GovernancePolicyCreate,
    ModelCompareRequest,
    ModelExploreRequest,
    ModelValidation,
    NodeImplementation,
    NodeRun,
    Optimization,
    OptimizeRequest,
    Page,
    ScheduleCreate,
    TriggerCreate,
    ValidationNote,
    ValidationRequest,
    Workflow,
    WorkflowCreate,
    WorkflowRevision,
    WorkflowRevisionCreate,
    WorkflowRun,
    WorkflowRunCreate,
    WorkflowSchedule,
    WorkflowTemplate,
    WorkflowTrigger,
    WorkflowUpdate,
    WorkflowValidateRequest,
    WorkflowValidation,
)
from mapi_spec.errors import MapiError
from mapi_spec.governance import PolicyCheck, PolicyMatch, PolicyScope, ValidationStatus
from mapi_spec.loader import parse_text
from mapi_spec.resources import ResourceClass
from mapi_spec.workflow import NodeRunStatus, WorkflowEvent, WorkflowRunStatus, workflow_hash
from mapi_spec.workflow_nodes import NodeKind
from pydantic import JsonValue

from mapi_client import _calls_m35 as m35
from mapi_client._common import sse_timeout
from mapi_client._m3_async import _keyed, _poll
from mapi_client._m35_common import (
    WorkflowSource,
    data_explore_body,
    exploration_done,
    exploration_error,
    model_compare_body,
    model_explore_body,
    optimization_done,
    optimization_error,
    optimize_body,
    policy_body,
    policy_update_body,
    schedule_update_body,
    section_link,
    validation_done,
    workflow_run_error,
    workflow_source,
)
from mapi_client.errors import TransportError, raise_for_response, unexpected
from mapi_client.pagination import AsyncPaginator
from mapi_client.sse import (
    SseDecoder,
    WorkflowEnd,
    WorkflowEventFrame,
    WorkflowStreamItem,
    to_workflow_stream_item,
)

if TYPE_CHECKING:
    from mapi_client._async import AsyncClient

__all__ = [
    "AsyncCatalogAboutMixin",
    "AsyncDatasetAboutMixin",
    "AsyncExplorations",
    "AsyncGovernance",
    "AsyncModelInsightMixin",
    "AsyncValidations",
    "AsyncWorkflows",
]

Answer = str | Sequence[str]


class _Resource:
    def __init__(self, client: AsyncClient) -> None:
        self._c = client


# ========================================================== catalog: About cards, chooser


class AsyncCatalogAboutMixin(_Resource):
    """About cards, "Help me choose", workflow nodes and governance checks on
    ``Client.catalog`` (v0.5)."""

    def abouts(
        self, *, kind: AboutKind | str | None = None, limit: int | None = None
    ) -> AsyncPaginator[CatalogAbout]:
        """The About card of every item a user can pick (``kind``: ``model``, ``step``,
        ``split``, ``template``, ``workflow_template``, ``node_kind``, ``node``, ``check``)."""
        return self._c.page_iter(lambda cur: m35.list_catalog_abouts(limit, cur, kind=kind))

    async def about(self, item_id: str, *, kind: AboutKind | str | None = None) -> About:
        """One item's About card (``ValueError`` when no installed item has that id)."""
        async for card in self.abouts(kind=kind, limit=200):
            if card.id == item_id:
                return card.about
        where = f" of kind {AboutKind(kind).value}" if kind is not None else ""
        raise ValueError(f"no catalog item {item_id!r}{where} has an About card")

    async def chooser(self) -> ChooserQuestionnaire:
        """The "Help me choose" questions, in the order to ask them."""
        return await self._c.send(m35.get_chooser())

    async def recommend(
        self,
        answers: Mapping[str, Answer] | None = None,
        /,
        *,
        kind: AboutKind | str | None = None,
        limit: int | None = None,
        **more: Answer,
    ) -> ChooserResult:
        """Catalog items ranked for chooser answers, best first, with plain reasons.

        ``api.catalog.recommend(goal="forecast", data_shape="one_series",
        experience="beginner")``; a question with several answers takes a list. Without
        ``kind`` the candidates are models, guided templates and workflow templates.
        Unknown questions or answers raise ``ValueError`` before anything is sent.
        """
        merged: dict[str, Answer] = {**dict(answers or {}), **more}
        return await self._c.send(m35.recommend_catalog_items(merged, kind=kind, limit=limit))

    def workflow_nodes(
        self, *, kind: NodeKind | str | None = None, limit: int | None = None
    ) -> AsyncPaginator[NodeImplementation]:
        """Workflow node implementations (ports, parameters, result fields, About)."""
        return self._c.page_iter(lambda cur: m35.list_workflow_nodes(limit, cur, kind=kind))

    async def workflow_node(self, node_id: str) -> NodeImplementation:
        return await self._c.send(m35.get_workflow_node(node_id))

    def governance_checks(self, *, limit: int | None = None) -> AsyncPaginator[GovernanceCheckDef]:
        """The governance check catalog (metric, thresholds, parameters, About)."""
        return self._c.page_iter(lambda cur: m35.list_governance_checks(limit, cur))


# ======================================================= datasets: About and Data Explorer


class _AsyncExploreWaiting(_Resource):
    async def _finish(
        self,
        started: Exploration,
        *,
        wait: bool,
        timeout: float | None,
        poll_interval: float,
        raise_on_failure: bool,
    ) -> Exploration:
        if not wait or exploration_done(started):
            got = started
        else:
            got = await _poll(
                self._c,
                lambda: self._c.send(m35.get_exploration(started.run.id)),
                exploration_done,
                what=f"exploration {started.run.id}",
                timeout=timeout,
                poll_interval=poll_interval,
            )
        err = exploration_error(got) if raise_on_failure else None
        if err is not None:
            raise err
        return got


class AsyncDatasetAboutMixin(_AsyncExploreWaiting):
    """Dataset About and the Data Explorer on ``Client.datasets`` (v0.5)."""

    async def about(self, dataset_id: str) -> DatasetAbout:
        """The owner's description and the summary generated from the latest version."""
        return await self._c.send(m35.get_dataset_about(dataset_id))

    async def describe(self, dataset_id: str, description: str | None) -> DatasetAbout:
        """Set (or, with ``None``, clear) the owner's description of a dataset."""
        body = DatasetAboutUpdate(description_user=description)
        return await self._c.send(m35.update_dataset_about(dataset_id, body))

    async def explore(
        self,
        dataset_id: str,
        version: int,
        request: DataExploreRequest | Mapping[str, Any] | None = None,
        *,
        wait: bool = True,
        timeout: float | None = 900.0,
        poll_interval: float = 1.0,
        raise_on_failure: bool = True,
        idempotency_key: str | None = None,
        **fields: Any,
    ) -> Exploration:
        """Compute Data Explorer sections in the sandbox (``sections=[...]``, ``kpi=``,
        ``facet_by=[...]``, ``max_lag=``...). Asking the same again is instant (cached).
        With ``wait`` the run is followed until its views are ready; read them with
        :meth:`AsyncExplorations.view`."""
        body = data_explore_body(request, fields)
        started = await self._c.send(
            m35.explore_dataset_version(dataset_id, version, body, idempotency_key)
        )
        return await self._finish(
            started,
            wait=wait,
            timeout=timeout,
            poll_interval=poll_interval,
            raise_on_failure=raise_on_failure,
        )


# ============================================== models: explorer, comparison, optimisation


class AsyncModelInsightMixin(_AsyncExploreWaiting):
    """The Model Explorer, comparisons, budget optimisation and validations on
    ``Client.models`` (v0.5)."""

    async def explore(
        self,
        model_id: str,
        version: int,
        request: ModelExploreRequest | Mapping[str, Any] | None = None,
        *,
        wait: bool = True,
        timeout: float | None = 900.0,
        poll_interval: float = 1.0,
        raise_on_failure: bool = True,
        idempotency_key: str | None = None,
        **fields: Any,
    ) -> Exploration:
        """Compute Model Explorer sections (``sections=[...]``, ``horizon=``,
        ``compare_with=[model://...]``, ``backtest=<split plan>``)."""
        body = model_explore_body(request, fields)
        started = await self._c.send(
            m35.explore_model_version(model_id, version, body, idempotency_key)
        )
        return await self._finish(
            started,
            wait=wait,
            timeout=timeout,
            poll_interval=poll_interval,
            raise_on_failure=raise_on_failure,
        )

    async def compare(
        self,
        models: Sequence[str] | None = None,
        request: ModelCompareRequest | Mapping[str, Any] | None = None,
        *,
        wait: bool = True,
        timeout: float | None = 900.0,
        poll_interval: float = 1.0,
        raise_on_failure: bool = True,
        idempotency_key: str | None = None,
        **fields: Any,
    ) -> Exploration:
        """Compare 2-11 model versions (``model://project/name@vN``) side by side: a
        metrics table, a ranking and pairwise Diebold-Mariano tests on the shared holdout
        (the ``comparison`` view)."""
        body = model_compare_body(models, request, fields)
        started = await self._c.send(m35.compare_model_versions(body, idempotency_key))
        return await self._finish(
            started,
            wait=wait,
            timeout=timeout,
            poll_interval=poll_interval,
            raise_on_failure=raise_on_failure,
        )

    async def optimize(
        self,
        model_id: str,
        version: int,
        request: OptimizeRequest | Mapping[str, Any] | None = None,
        *,
        wait: int = MAX_PREFER_WAIT_SECONDS,
        timeout: float | None = 900.0,
        poll_interval: float = 1.0,
        raise_on_failure: bool = True,
        idempotency_key: str | None = None,
        **fields: Any,
    ) -> Optimization:
        """Split a budget across channels with mmm-framework's planner and wait for the
        plan (``total_budget=``, ``channels={"tv": {"max_multiplier": 1.5}}``,
        ``objective="p10"``, ``frontier={"n_points": 12}``...). ``Prefer: wait`` is sent
        and the request repeated with the same ``Idempotency-Key`` until it is done."""
        body = optimize_body(request, fields)
        key = idempotency_key or str(uuid.uuid4())
        opt: Optimization = await _keyed(
            self._c,
            lambda w: self._c.send(
                m35.optimize_model_version(model_id, version, body, wait=w, idempotency_key=key)
            ),
            optimization_done,
            wait=wait,
            timeout=timeout,
            poll_interval=poll_interval,
            what=f"optimisation {key} of {model_id}@v{version}",
        )
        err = optimization_error(opt) if raise_on_failure else None
        if err is not None:
            raise err
        return opt

    async def optimization(self, run_id: str) -> Optimization:
        """An optimize run and, once done, its plan."""
        return await self._c.send(m35.get_optimization(run_id))

    async def validate(
        self,
        model_id: str,
        version: int,
        *,
        policy_ids: Sequence[str] = (),
        note: str | None = None,
        dataset: str | None = None,
        wait: bool = False,
        timeout: float | None = 1800.0,
        poll_interval: float = 2.0,
        idempotency_key: str | None = None,
    ) -> ModelValidation:
        """Submit a model version for validation (see :meth:`AsyncValidations.request`)."""
        return await self._c.validations.request(
            model_id,
            version,
            policy_ids=policy_ids,
            note=note,
            dataset=dataset,
            wait=wait,
            timeout=timeout,
            poll_interval=poll_interval,
            idempotency_key=idempotency_key,
        )

    def validations(
        self, model_id: str, version: int, *, limit: int | None = None
    ) -> AsyncPaginator[ModelValidation]:
        """AsyncValidations of one model version, newest first."""
        return self._c.page_iter(
            lambda cur: m35.list_model_validations(model_id, version, limit, cur)
        )


# =========================================================================== explorations


class AsyncExplorations(_Resource):
    """Explore runs (``Client.explorations``, v0.5): status, waiting and the views."""

    async def get(self, run_id: str) -> Exploration:
        return await self._c.send(m35.get_exploration(run_id))

    async def wait(
        self,
        run_id: str,
        *,
        timeout: float | None = 900.0,
        poll_interval: float = 1.0,
        raise_on_failure: bool = True,
    ) -> Exploration:
        got: Exploration = await _poll(
            self._c,
            lambda: self.get(run_id),
            exploration_done,
            what=f"exploration {run_id}",
            timeout=timeout,
            poll_interval=poll_interval,
        )
        err = exploration_error(got) if raise_on_failure else None
        if err is not None:
            raise err
        return got

    def link(self, exploration: Exploration, section: str) -> ArtifactLink:
        """The signed link of one section's view (``ValueError`` says why one is missing)."""
        return section_link(exploration, section)

    async def view(self, exploration: Exploration, section: str) -> dict[str, Any]:
        """One section's view as JSON (CRC32C-verified download): e.g. ``stationarity`` -
        ADF/KPSS per series - or ``comparison`` - the metrics table and Diebold-Mariano
        tests. Plot the views with the platform's Vega-Lite recipes
        (``mapi_plots.explorer``)."""
        data = json.loads(await self._c.download(section_link(exploration, section)))
        if not isinstance(data, dict):
            raise ValueError(f"the {section!r} view is not a JSON object")
        return data


# ============================================================================== workflows


class AsyncWorkflows(_Resource):
    """AsyncWorkflows (``Client.workflows``, v0.5): documents and revisions, runs with live
    node status, node outputs, schedules, dataset triggers and the template gallery."""

    # ------------------------------------------------------------------- documents
    def list(
        self, *, archived: bool | None = None, limit: int | None = None
    ) -> AsyncPaginator[Workflow]:
        return self._c.page_iter(lambda cur: m35.list_workflows(limit, cur, archived=archived))

    async def find(self, name: str) -> Workflow | None:
        """The workflow called ``name`` in the project (archived ones included)."""
        for archived in (False, True):
            async for wf in self.list(archived=archived, limit=200):
                if wf.name == name:
                    return wf
        return None

    async def create(
        self,
        source: WorkflowSource | None = None,
        *,
        template: str | None = None,
        message: str | None = None,
    ) -> Workflow:
        """Create a workflow from a document (a ``WorkflowSpec``, a mapping, YAML text or a
        file path) or from a gallery ``template`` (copied into the project as revision 1).
        A document with errors is stored but cannot run until a revision fixes it."""
        if (source is None) == (template is None):
            raise ValueError("give a workflow document or a template, not both")
        fields: dict[str, Any] = {"message": message}
        fields.update(workflow_source(source) if source is not None else {"template": template})
        return await self._c.send(m35.create_workflow(WorkflowCreate.model_validate(fields)))

    async def validate(self, source: WorkflowSource) -> WorkflowValidation:
        """Static checks on the server (stores nothing): ``valid``, ``issues`` with JSON
        pointers, the execution ``order`` and the resolved node implementations."""
        body = WorkflowValidateRequest.model_validate(workflow_source(source))
        return await self._c.send(m35.validate_workflow(body))

    async def get(self, workflow_id: str) -> Workflow:
        return await self._c.send(m35.get_workflow(workflow_id))

    async def update(
        self, workflow_id: str, *, description: str | None = None, archived: bool | None = None
    ) -> Workflow:
        """Change the description, or archive/unarchive (documents change by revisions)."""
        body = WorkflowUpdate(description=description, archived=archived)
        return await self._c.send(m35.update_workflow(workflow_id, body))

    async def delete(self, workflow_id: str) -> None:
        """Delete the workflow with its runs, schedules and triggers."""
        await self._c.send(m35.delete_workflow(workflow_id))

    def revisions(
        self, workflow_id: str, *, limit: int | None = None
    ) -> AsyncPaginator[WorkflowRevision]:
        return self._c.page_iter(lambda cur: m35.list_workflow_revisions(workflow_id, limit, cur))

    async def revision(self, workflow_id: str, revision: int) -> WorkflowRevision:
        return await self._c.send(m35.get_workflow_revision(workflow_id, revision))

    async def revise(
        self,
        workflow_id: str,
        source: WorkflowSource,
        *,
        message: str | None = None,
        expected_revision: int | None = None,
    ) -> WorkflowRevision:
        """Save a new revision (validated; ``expected_revision`` refuses a concurrent edit
        with ``409``). Check ``revision.validation.valid`` before running it."""
        fields: dict[str, Any] = {
            **workflow_source(source),
            "message": message,
            "expected_revision": expected_revision,
        }
        body = WorkflowRevisionCreate.model_validate(fields)
        return await self._c.send(m35.create_workflow_revision(workflow_id, body))

    async def upsert(
        self, source: WorkflowSource, *, message: str | None = None
    ) -> tuple[Workflow, WorkflowRevision]:
        """Create the workflow, or add a revision when its document changed (matched by
        ``metadata.name``). Returns the workflow and its latest revision."""
        doc = _document_of(workflow_source(source))
        name = _document_name(doc)
        existing = await self.find(name) if name else None
        if existing is None:
            wf = await self.create(source, message=message)
            return wf, await self.revision(wf.id, wf.latest_revision)
        latest = await self.revision(existing.id, existing.latest_revision)
        if doc is not None and workflow_hash(doc) == latest.spec_hash:
            return existing, latest  # unchanged: no new revision
        rev = await self.revise(
            existing.id, source, message=message, expected_revision=existing.latest_revision
        )
        return await self.get(existing.id), rev

    # ------------------------------------------------------------------------ runs
    async def run(
        self,
        workflow_id: str,
        *,
        inputs: Mapping[str, str] | None = None,
        params: Mapping[str, JsonValue] | None = None,
        revision: int | None = None,
        use_cache: bool = True,
        resource_class: ResourceClass | str | None = None,
        idempotency_key: str | None = None,
        wait: bool = False,
        timeout: float | None = None,
        poll_interval: float = 2.0,
        raise_on_failure: bool = True,
    ) -> WorkflowRun:
        """Start a run (inputs are references such as ``dataset://growth/sales@latest``,
        pinned to an exact version at start). With ``wait`` the run is followed until it
        is terminal and a failed or cancelled run raises its error."""
        body = WorkflowRunCreate(
            revision=revision,
            params=dict(params or {}),
            inputs=dict(inputs or {}),
            use_cache=use_cache,
            resource_class=ResourceClass(resource_class) if resource_class else None,
        )
        key = idempotency_key or str(uuid.uuid4())
        run = await self._c.send(m35.start_workflow_run(workflow_id, body, key))
        if not wait:
            return run
        return await self.wait(
            run.run_id,
            timeout=timeout,
            poll_interval=poll_interval,
            raise_on_failure=raise_on_failure,
        )

    def runs(
        self,
        workflow_id: str,
        *,
        status: WorkflowRunStatus | str | None = None,
        limit: int | None = None,
    ) -> AsyncPaginator[WorkflowRun]:
        return self._c.page_iter(
            lambda cur: m35.list_workflow_runs(workflow_id, limit, cur, status=status)
        )

    async def get_run(self, run_id: str) -> WorkflowRun:
        """A run with every node (status, result, outputs), decisions, outputs and cost."""
        return await self._c.send(m35.get_workflow_run(run_id))

    async def wait(
        self,
        run_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float = 2.0,
        raise_on_failure: bool = True,
    ) -> WorkflowRun:
        """Poll until the run is terminal (``TimeoutError`` after ``timeout`` seconds)."""
        run: WorkflowRun = await _poll(
            self._c,
            lambda: self.get_run(run_id),
            lambda r: r.status.terminal,
            what=f"workflow run {run_id}",
            timeout=timeout,
            poll_interval=poll_interval,
        )
        err = workflow_run_error(run) if raise_on_failure else None
        if err is not None:
            raise err
        return run

    async def cancel(self, run_id: str) -> WorkflowRun:
        """Cancel the run and its running children (``cancelling`` until they stopped)."""
        return await self._c.send(m35.cancel_workflow_run(run_id))

    async def stream_events(
        self,
        run_id: str,
        *,
        last_event_id: int | None = None,
        reconnect: bool = True,
        max_reconnects: int = 5,
    ) -> AsyncIterator[WorkflowStreamItem]:
        """Follow the run's server-sent events until it ends.

        Yields :class:`~mapi_client.sse.WorkflowSnapshot` (first frame of each connection:
        the run with every node), :class:`~mapi_client.sse.WorkflowEventFrame` (node
        queued/started/succeeded/cached/skipped/failed, branch decided, ...) and finally
        :class:`~mapi_client.sse.WorkflowEnd`. A dropped connection is resumed with
        ``Last-Event-ID``; events are never yielded twice.
        """
        path = m35.workflow_events_path(run_id)
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
                            item = None if frame is None else to_workflow_stream_item(frame)
                            if item is None:
                                continue
                            if isinstance(item, WorkflowEventFrame):
                                if last is not None and item.id <= last:
                                    continue  # replayed by an overlapping reconnect
                                last = item.id
                            failures = 0
                            yield item
                            if isinstance(item, WorkflowEnd):
                                return
            except httpx.TransportError as exc:
                cause = exc
            if not reconnect or failures >= max_reconnects:
                raise TransportError(
                    f"the event stream of workflow run {run_id} ended before the run did"
                ) from cause
            failures += 1
            await self._c._sleep(retry_ms / 1000.0)

    def events(
        self, run_id: str, *, after: int | None = None, limit: int | None = None
    ) -> AsyncPaginator[WorkflowEvent]:
        """The persisted events as JSON pages (ascending ``seq``, after ``after``)."""

        async def fetch(cursor: str | None) -> Page[WorkflowEvent]:
            start = int(cursor) if cursor is not None else after
            page = await self._c.send(m35.workflow_run_events(run_id, start, limit))
            return _events_page(page)

        return AsyncPaginator(fetch)

    def nodes(
        self,
        run_id: str,
        *,
        status: NodeRunStatus | str | None = None,
        limit: int | None = None,
    ) -> AsyncPaginator[NodeRun]:
        return self._c.page_iter(
            lambda cur: m35.list_workflow_node_runs(run_id, limit, cur, status=status)
        )

    async def node(self, run_id: str, node_id: str) -> NodeRun:
        """One node's status, resolved parameters, result, outputs and child runs."""
        return await self._c.send(m35.get_workflow_node_run(run_id, node_id))

    async def output_link(self, run_id: str, node_id: str, port: str) -> ArtifactLink:
        """A signed link to a node output's file (a derived dataset, a fitted bundle).
        JSON outputs (test results, tables, metrics) are inline in :meth:`node`."""
        return await self._c.send(m35.get_workflow_node_output(run_id, node_id, port))

    @overload
    async def download_output(
        self, run_id: str, node_id: str, port: str, dest: None = None
    ) -> bytes: ...
    @overload
    async def download_output(
        self, run_id: str, node_id: str, port: str, dest: str | os.PathLike[str]
    ) -> Path: ...
    async def download_output(
        self, run_id: str, node_id: str, port: str, dest: str | os.PathLike[str] | None = None
    ) -> bytes | Path:
        """Download a node output's file (CRC32C-verified): bytes, or the path."""
        link = await self.output_link(run_id, node_id, port)
        if dest is None:
            return await self._c.download(link)
        return await self._c.download(link, dest)

    # ------------------------------------------------------------------ automation
    def schedules(
        self, workflow_id: str, *, limit: int | None = None
    ) -> AsyncPaginator[WorkflowSchedule]:
        return self._c.page_iter(lambda cur: m35.list_workflow_schedules(workflow_id, limit, cur))

    async def schedule(
        self,
        workflow_id: str,
        cron: str,
        *,
        timezone: str = "UTC",
        inputs: Mapping[str, str] | None = None,
        params: Mapping[str, JsonValue] | None = None,
        revision: int | None = None,
        active: bool = True,
        description: str | None = None,
    ) -> WorkflowSchedule:
        """Run the workflow on a cron schedule (``"0 6 * * 1"``: Mondays at 06:00) in
        ``timezone``; at most every 15 minutes."""
        body = ScheduleCreate(
            cron=cron,
            timezone=timezone,
            revision=revision,
            params=dict(params or {}),
            inputs=dict(inputs or {}),
            active=active,
            description=description,
        )
        return await self._c.send(m35.create_workflow_schedule(workflow_id, body))

    async def update_schedule(
        self,
        workflow_id: str,
        schedule_id: str,
        *,
        cron: str | None = None,
        timezone: str | None = None,
        revision: int | None = None,
        params: Mapping[str, JsonValue] | None = None,
        inputs: Mapping[str, str] | None = None,
        active: bool | None = None,
        description: str | None = None,
    ) -> WorkflowSchedule:
        """Change a schedule; ``active=False`` pauses it."""
        body = schedule_update_body(
            cron=cron,
            timezone=timezone,
            revision=revision,
            params=params,
            inputs=inputs,
            active=active,
            description=description,
        )
        return await self._c.send(m35.update_workflow_schedule(workflow_id, schedule_id, body))

    async def delete_schedule(self, workflow_id: str, schedule_id: str) -> None:
        await self._c.send(m35.delete_workflow_schedule(workflow_id, schedule_id))

    def triggers(
        self, workflow_id: str, *, limit: int | None = None
    ) -> AsyncPaginator[WorkflowTrigger]:
        return self._c.page_iter(lambda cur: m35.list_workflow_triggers(workflow_id, limit, cur))

    async def trigger(
        self,
        workflow_id: str,
        dataset: str,
        input: str,
        *,
        revision: int | None = None,
        params: Mapping[str, JsonValue] | None = None,
        active: bool = True,
        debounce_seconds: int | None = None,
    ) -> WorkflowTrigger:
        """Run the workflow whenever ``dataset`` (``dataset://project/name``) gets a new
        version, bound to the workflow input ``input``."""
        fields: dict[str, Any] = {
            "dataset": dataset,
            "input": input,
            "revision": revision,
            "params": dict(params or {}),
            "active": active,
        }
        if debounce_seconds is not None:
            fields["debounce_seconds"] = debounce_seconds
        body = TriggerCreate.model_validate(fields)
        return await self._c.send(m35.create_workflow_trigger(workflow_id, body))

    async def delete_trigger(self, workflow_id: str, trigger_id: str) -> None:
        await self._c.send(m35.delete_workflow_trigger(workflow_id, trigger_id))

    # --------------------------------------------------------------------- gallery
    def templates(self, *, limit: int | None = None) -> AsyncPaginator[WorkflowTemplate]:
        """The gallery: each template with its About card and document."""
        return self._c.page_iter(lambda cur: m35.list_workflow_templates(limit, cur))

    async def template(self, template_id: str) -> WorkflowTemplate:
        return await self._c.send(m35.get_workflow_template(template_id))


def _document_of(body: Mapping[str, Any]) -> dict[str, Any] | None:
    """The document of a create/validate body (``text`` parsed; ``None`` if it does not)."""
    doc = body.get("document")
    if doc is None:
        try:
            doc = parse_text(str(body.get("text", "")))
        except (ValueError, MapiError):
            return None
    return dict(doc) if isinstance(doc, Mapping) else None


def _document_name(doc: Mapping[str, Any] | None) -> str | None:
    meta = doc.get("metadata") if doc is not None else None
    name = meta.get("name") if isinstance(meta, Mapping) else None
    return name if isinstance(name, str) else None


def _events_page(page: Page[WorkflowEvent]) -> Page[WorkflowEvent]:
    """A workflow-events page whose cursor is the ``after`` of the next request."""
    if page.next_cursor is None or not page.items:
        return Page[WorkflowEvent](items=page.items, next_cursor=None)
    return Page[WorkflowEvent](items=page.items, next_cursor=str(page.items[-1].seq))


# ============================================================================= governance


class AsyncGovernance(_Resource):
    """AsyncGovernance policies (``Client.governance``, v0.5): the check catalog and versioned
    project and tenant-wide policies. ``tenant_id=`` addresses the tenant's policies."""

    def checks(self, *, limit: int | None = None) -> AsyncPaginator[GovernanceCheckDef]:
        return self._c.catalog.governance_checks(limit=limit)

    def policies(
        self,
        *,
        scope: PolicyScope | None = None,
        tenant_id: str | None = None,
        limit: int | None = None,
    ) -> AsyncPaginator[GovernancePolicy]:
        """Policies in force in the project (tenant-wide first), or a tenant's own."""
        if tenant_id is not None:
            if scope is not None:
                raise ValueError("scope filters a project's policies; omit it with tenant_id")
            return self._c.page_iter(lambda cur: m35.list_tenant_policies(tenant_id, limit, cur))
        return self._c.page_iter(lambda cur: m35.list_policies(limit, cur, scope=scope))

    async def policy(self, policy_id: str, *, tenant_id: str | None = None) -> GovernancePolicy:
        """The latest version of a policy."""
        if tenant_id is not None:
            return await self._c.send(m35.get_tenant_policy(tenant_id, policy_id))
        return await self._c.send(m35.get_policy(policy_id))

    async def create_policy(
        self,
        policy: GovernancePolicyCreate | Mapping[str, Any] | None = None,
        *,
        tenant_id: str | None = None,
        name: str | None = None,
        checks: Sequence[PolicyCheck | Mapping[str, Any] | str] | None = None,
        applies_to: PolicyMatch | Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> GovernancePolicy:
        """Add a policy (version 1). ``checks`` may be check ids with the catalog
        defaults, or :class:`~mapi_spec.governance.PolicyCheck` mappings with
        ``severity``, ``thresholds`` and ``params``; other fields: ``description``,
        ``require_approval``, ``approvals_required``,
        ``require_validated_for_promotion``, ``active``."""
        body = policy_body(policy, name=name, checks=checks, applies_to=applies_to, **fields)
        if tenant_id is not None:
            return await self._c.send(m35.create_tenant_policy(tenant_id, body))
        return await self._c.send(m35.create_policy(body))

    async def update_policy(
        self,
        policy_id: str,
        policy: GovernancePolicyCreate | Mapping[str, Any] | None = None,
        *,
        tenant_id: str | None = None,
        expected_version: int | None = None,
        **fields: Any,
    ) -> GovernancePolicy:
        """Save the next version (the whole policy; ``expected_version`` refuses a
        concurrent edit with ``409 CONFLICT``)."""
        body = policy_update_body(policy, expected_version=expected_version, **fields)
        if tenant_id is not None:
            return await self._c.send(m35.update_tenant_policy(tenant_id, policy_id, body))
        return await self._c.send(m35.update_policy(policy_id, body))

    async def retire_policy(self, policy_id: str, *, tenant_id: str | None = None) -> None:
        """Retire a policy (its versions stay for audit)."""
        if tenant_id is not None:
            await self._c.send(m35.delete_tenant_policy(tenant_id, policy_id))
        else:
            await self._c.send(m35.delete_policy(policy_id))

    def policy_versions(
        self, policy_id: str, *, limit: int | None = None
    ) -> AsyncPaginator[GovernancePolicy]:
        """Every version of a project policy, newest first."""
        return self._c.page_iter(lambda cur: m35.list_policy_versions(policy_id, limit, cur))


class AsyncValidations(_Resource):
    """Model validations (``Client.validations``, v0.5): requests, the reviewers' queue,
    evidence, second-person approvals and the evidence pack."""

    async def request(
        self,
        model_id: str,
        version: int,
        *,
        policy_ids: Sequence[str] = (),
        note: str | None = None,
        dataset: str | None = None,
        wait: bool = False,
        timeout: float | None = 1800.0,
        poll_interval: float = 2.0,
        idempotency_key: str | None = None,
    ) -> ModelValidation:
        """Submit a model version: the policies in force (or ``policy_ids``) decide the
        checks, which run in the sandbox. With ``wait`` it returns once the checks
        finished (``validated``, ``awaiting_approval``, ``checks_failed``, ...)."""
        body = ValidationRequest(policy_ids=list(policy_ids), note=note, dataset=dataset)
        got = await self._c.send(
            m35.request_model_validation(model_id, version, body, idempotency_key)
        )
        if not wait or validation_done(got):
            return got
        return await self.wait(got.id, timeout=timeout, poll_interval=poll_interval)

    def list(
        self, *, status: ValidationStatus | str | None = None, limit: int | None = None
    ) -> AsyncPaginator[ModelValidation]:
        """Validations of the project; ``status="awaiting_approval"`` is the review queue."""
        return self._c.page_iter(lambda cur: m35.list_validations(limit, cur, status=status))

    def for_model(
        self, model_id: str, version: int, *, limit: int | None = None
    ) -> AsyncPaginator[ModelValidation]:
        return self._c.page_iter(
            lambda cur: m35.list_model_validations(model_id, version, limit, cur)
        )

    async def get(self, validation_id: str) -> ModelValidation:
        return await self._c.send(m35.get_validation(validation_id))

    async def wait(
        self, validation_id: str, *, timeout: float | None = 1800.0, poll_interval: float = 2.0
    ) -> ModelValidation:
        """Poll until the checks finished (the status no longer changes by itself)."""
        return await _poll(
            self._c,
            lambda: self.get(validation_id),
            validation_done,
            what=f"validation {validation_id}",
            timeout=timeout,
            poll_interval=poll_interval,
        )

    def evidence(
        self, validation_id: str, *, limit: int | None = None
    ) -> AsyncPaginator[CheckResult]:
        """Every check result with its evidence."""
        return self._c.page_iter(
            lambda cur: m35.list_validation_evidence(validation_id, limit, cur)
        )

    async def cancel(self, validation_id: str, *, note: str | None = None) -> ModelValidation:
        """Stop running checks (back to ``draft``)."""
        return await self._c.send(m35.cancel_validation(validation_id, ValidationNote(note=note)))

    async def resubmit(
        self, validation_id: str, *, note: str | None = None, idempotency_key: str | None = None
    ) -> ModelValidation:
        """Run the checks again after a failure or a rejection."""
        body = ValidationNote(note=note)
        return await self._c.send(m35.resubmit_validation(validation_id, body, idempotency_key))

    async def decide(
        self,
        validation_id: str,
        decision: Literal["approve", "reject"],
        *,
        comment: str | None = None,
    ) -> ModelValidation:
        """A reviewer's decision (signed-in reviewers only: never the submitter, never an
        API token). A rejection needs a ``comment``."""
        body = ApprovalCreate(decision=decision, comment=comment)
        return await self._c.send(m35.decide_validation(validation_id, body))

    async def approve(self, validation_id: str, *, comment: str | None = None) -> ModelValidation:
        return await self.decide(validation_id, "approve", comment=comment)

    async def reject(self, validation_id: str, comment: str) -> ModelValidation:
        return await self.decide(validation_id, "reject", comment=comment)

    async def deprecate(self, validation_id: str, reason: str) -> ModelValidation:
        """Retire a validated model version (audited with the reason)."""
        return await self._c.send(
            m35.deprecate_validation(validation_id, ValidationNote(note=reason))
        )

    async def evidence_pack(
        self,
        validation_id: str,
        *,
        format: Literal["html", "json"] = "json",
        include_plots: bool = True,
    ) -> EvidencePack:
        """The audit export: the validation, every result with evidence, approvals,
        history, policies, lineage and a digest; ``format="html"`` adds a signed link to a
        static HTML rendering (``download``)."""
        body = EvidencePackRequest(format=format, include_plots=include_plots)
        return await self._c.send(m35.export_evidence_pack(validation_id, body))

    async def download_evidence_pack(
        self, validation_id: str, dest: str | os.PathLike[str], *, include_plots: bool = True
    ) -> Path:
        """Write the evidence pack to ``dest``: the HTML rendering for a ``.html`` path,
        else the JSON document."""
        path = Path(dest)
        if path.suffix.lower() in (".html", ".htm"):
            pack = await self.evidence_pack(
                validation_id, format="html", include_plots=include_plots
            )
            if pack.download is None:
                raise ValueError(f"the evidence pack of {validation_id} has no HTML rendering")
            return await self._c.download(ArtifactLink.model_validate(pack.download), path)
        pack = await self.evidence_pack(validation_id, format="json", include_plots=include_plots)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pack.model_dump_json(indent=2), "utf-8")
        return path
