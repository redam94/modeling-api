"""v0.16 resources of the asynchronous client (the twins of :mod:`mapi_client._v016`): client
dashboards (``client.dashboards``) and the share-link view (:class:`AsyncSharedDashboard`)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from mapi_spec.api import (
    ActualsTemplate,
    BatchTemplate,
    ClientDashboard,
    Dashboard,
    DashboardCreate,
    DashboardDraftUpdate,
    DashboardGrant,
    DashboardGrid,
    DashboardPresets,
    DashboardRefitRequest,
    DashboardRevision,
    DashboardShare,
    DashboardShareCreated,
    DashboardSummary,
    DashboardUpgrade,
    DashboardValidation,
    Readout,
    ReadoutHeader,
    ReadoutList,
    ShareSession,
    SourceCatalog,
    WhatIfResult,
)
from mapi_spec.dashboard_v2 import DashboardSpecV2
from mapi_spec.dashboards import DashboardSpec
from pydantic import SecretStr

from mapi_client import _calls_v016 as vcalls
from mapi_client import _calls_v017 as v17
from mapi_client._v016_common import (
    actuals_body,
    batch_body,
    dashboard_spec,
    dashboard_view,
    decision_body,
    next_before_seq,
    readout_done,
    readout_error,
    rows_body,
    scenario_body,
    session_body,
    session_due,
    share_body,
    whatif_body,
)
from mapi_client.pagination import AsyncPaginator

if TYPE_CHECKING:
    from datetime import datetime

    from mapi_client._async import AsyncClient

__all__ = ["AsyncDashboards", "AsyncSharedDashboard"]


class AsyncDashboards:
    """Client dashboards of the selected project (``client.dashboards``; v0.16, any model
    type from v0.17): presets, drafts and publishing, grants and share links, live inputs
    (what-ifs, scenario rows, batch files, actuals), readouts and refit requests."""

    def __init__(self, client: AsyncClient) -> None:
        self._c = client

    # ---------------------------------------------------------------------- dashboards
    def list(
        self, *, status: str | None = None, limit: int | None = None
    ) -> AsyncPaginator[DashboardSummary]:
        """Dashboards you may see (a client user: those granted to them)."""
        return self._c.page_iter(lambda cur: vcalls.list_dashboards(limit, cur, status=status))

    async def get(self, dashboard_id: str) -> Dashboard | ClientDashboard:
        """The analyst's :class:`Dashboard` (viewers and up), or the
        :class:`ClientDashboard` a client user sees."""
        return await self._c.send(dashboard_view(dashboard_id))

    async def create(
        self, name: str, spec: DashboardSpec | DashboardSpecV2 | Mapping[str, Any]
    ) -> Dashboard:
        """A new draft (``name`` is unique in the project)."""
        body = DashboardCreate(name=name, spec=dashboard_spec(spec))
        return await self._c.send(vcalls.create_dashboard(body))

    async def update_draft(
        self,
        dashboard_id: str,
        spec: DashboardSpec | DashboardSpecV2 | Mapping[str, Any],
        *,
        etag: str,
    ) -> Dashboard:
        """Replace the draft; ``etag`` is the ``draft_etag`` you edited (412 when another
        edit landed first)."""
        body = DashboardDraftUpdate(spec=dashboard_spec(spec))
        return await self._c.send(vcalls.update_dashboard_draft(dashboard_id, body, etag))

    async def validate(self, dashboard_id: str) -> DashboardValidation:
        """Whether the draft can be published, and what to fix."""
        return await self._c.send(vcalls.validate_dashboard(dashboard_id))

    async def publish(self, dashboard_id: str) -> Dashboard:
        return await self._c.send(vcalls.publish_dashboard(dashboard_id))

    async def archive(self, dashboard_id: str) -> Dashboard:
        return await self._c.send(vcalls.archive_dashboard(dashboard_id))

    def revisions(
        self, dashboard_id: str, *, limit: int | None = None
    ) -> AsyncPaginator[DashboardRevision]:
        return self._c.page_iter(
            lambda cur: vcalls.list_dashboard_revisions(dashboard_id, limit, cur)
        )

    # -------------------------------------------------------------------------- grants
    def grants(
        self, dashboard_id: str, *, limit: int | None = None
    ) -> AsyncPaginator[DashboardGrant]:
        """Client users who may see the dashboard."""
        return self._c.page_iter(lambda cur: vcalls.list_dashboard_grants(dashboard_id, limit, cur))

    async def grant(self, dashboard_id: str, user_id: str) -> DashboardGrant:
        """Let a client user see it (they must already be a member of the project)."""
        return await self._c.send(vcalls.put_dashboard_grant(dashboard_id, user_id))

    async def revoke_grant(self, dashboard_id: str, user_id: str) -> None:
        await self._c.send(vcalls.delete_dashboard_grant(dashboard_id, user_id))

    # -------------------------------------------------------------------------- shares
    def shares(
        self, dashboard_id: str, *, limit: int | None = None
    ) -> AsyncPaginator[DashboardShare]:
        """The dashboard's share links (never their tokens)."""
        return self._c.page_iter(lambda cur: vcalls.list_dashboard_shares(dashboard_id, limit, cur))

    async def create_share(
        self,
        dashboard_id: str,
        *,
        label: str | None = None,
        expires_in_days: int = 30,
        passcode: str | SecretStr | None = None,
        allowed_inputs: Collection[str] = (),
    ) -> DashboardShareCreated:
        """A share link. It is read-only unless ``allowed_inputs`` names ``whatif`` or
        ``actuals`` (which needs the runner role). ``token`` is returned only here: the
        request is never resent after a lost response (that would mint a second link)."""
        body = share_body(
            label=label,
            expires_in_days=expires_in_days,
            passcode=passcode,
            allowed_inputs=allowed_inputs,
        )
        return await self._c.send(vcalls.create_dashboard_share(dashboard_id, body))

    async def revoke_share(self, dashboard_id: str, share_id: str) -> DashboardShare:
        """End a share link (idempotent); its open sessions stop working."""
        return await self._c.send(vcalls.revoke_dashboard_share(dashboard_id, share_id))

    async def open_share(
        self, link: str, *, passcode: str | SecretStr | None = None
    ) -> AsyncSharedDashboard:
        """Open a share link (the token, the ``/s#mpd_...`` fragment or the whole URL) as an
        anonymous viewer: no credentials are sent with it or with the share routes."""
        session = await self._c.send(vcalls.create_share_session(session_body(link, passcode)))
        return AsyncSharedDashboard(self._c, session)

    # -------------------------------------------------------------------------- inputs
    async def grid(self, dashboard_id: str) -> DashboardGrid:
        """The response grid the what-if sliders are evaluated on."""
        return await self._c.send(vcalls.get_dashboard_grid(dashboard_id))

    async def whatif(self, dashboard_id: str, multipliers: Mapping[str, float]) -> WhatIfResult:
        """A spend scenario evaluated on the grid (no run): ``{"tv": 0.8}`` is TV at 80 %
        of the current plan; missing channels stay at 1."""
        return await self._c.send(
            vcalls.evaluate_dashboard_whatif(dashboard_id, whatif_body(multipliers))
        )

    async def save_scenario(
        self,
        dashboard_id: str,
        multipliers: Mapping[str, float],
        *,
        exact: bool = False,
        label: str | None = None,
        wait: int | None = None,
        idempotency_key: str | None = None,
    ) -> Readout:
        """Keep a scenario as a private readout (``exact``: as a score run when the
        dashboard allows it; ``wait`` seconds for it to finish)."""
        body = scenario_body(multipliers, exact=exact, label=label)
        call = vcalls.save_dashboard_scenario(
            dashboard_id, body, wait=wait, idempotency_key=idempotency_key
        )
        return await self._c.send(call)

    async def actuals_template(self, dashboard_id: str) -> ActualsTemplate:
        """The table an actuals upload fills in, and what happens to the numbers."""
        return await self._c.send(vcalls.get_dashboard_actuals_template(dashboard_id))

    async def submit_actuals(
        self,
        dashboard_id: str,
        inputs: object,
        *,
        label: str | None = None,
        request_refit: bool = False,
        idempotency_key: str | None = None,
    ) -> ReadoutHeader:
        """Upload actuals (an ``xr.Dataset`` or XrJSON with the template's columns); the
        readout they make waits for review. ``request_refit`` asks the analysts to refit
        with them (it starts nothing by itself). See :meth:`wait_readout`."""
        body = actuals_body(inputs, label=label, request_refit=request_refit)
        return await self._c.send(
            vcalls.submit_dashboard_actuals(dashboard_id, body, idempotency_key=idempotency_key)
        )

    async def refresh(
        self, dashboard_id: str, *, idempotency_key: str | None = None
    ) -> ReadoutHeader:
        """Make a new readout now (debounced by the server)."""
        return await self._c.send(
            vcalls.refresh_dashboard(dashboard_id, idempotency_key=idempotency_key)
        )

    # ------------------------------------------------------------------------ readouts
    async def readout_page(
        self,
        dashboard_id: str,
        *,
        kind: str | None = None,
        visibility: str | None = None,
        before_seq: int | None = None,
        limit: int | None = None,
    ) -> ReadoutList:
        """One page of readouts, newest first (``next_before_seq`` pages back)."""
        return await self._c.send(
            vcalls.list_dashboard_readouts(
                dashboard_id, kind=kind, before_seq=before_seq, limit=limit, visibility=visibility
            )
        )

    async def readouts(
        self,
        dashboard_id: str,
        *,
        kind: str | None = None,
        visibility: str | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[ReadoutHeader]:
        """Every readout, newest first, fetched a page (``limit``) at a time."""
        before: int | None = None
        while True:
            page = await self.readout_page(
                dashboard_id, kind=kind, visibility=visibility, before_seq=before, limit=limit
            )
            for item in page.items:
                yield item
            before = next_before_seq(page, before)
            if before is None:
                return

    async def readout(self, dashboard_id: str, seq: int) -> Readout:
        return await self._c.send(vcalls.get_dashboard_readout(dashboard_id, seq))

    async def wait_readout(
        self,
        dashboard_id: str,
        seq: int,
        *,
        timeout: float | None = None,
        poll_interval: float = 1.0,
        max_poll_interval: float = 10.0,
        raise_on_failure: bool = True,
    ) -> Readout:
        """Poll until the readout is ``ready`` or ``failed`` (``TimeoutError`` after
        ``timeout`` seconds); a failed one raises its error unless ``raise_on_failure`` is
        false."""
        return await _wait_readout(
            self._c,
            lambda: self.readout(dashboard_id, seq),
            timeout=timeout,
            poll_interval=poll_interval,
            max_poll_interval=max_poll_interval,
            raise_on_failure=raise_on_failure,
        )

    async def publish_readout(self, dashboard_id: str, seq: int) -> ReadoutHeader:
        """Show a readout to the dashboard's clients (an uploaded one waits for this)."""
        return await self._c.send(vcalls.publish_dashboard_readout(dashboard_id, seq))

    async def hide_readout(self, dashboard_id: str, seq: int) -> ReadoutHeader:
        return await self._c.send(vcalls.hide_dashboard_readout(dashboard_id, seq))

    # ------------------------------------------------------- v0.17: presets and layouts
    async def presets(
        self,
        *,
        model_id: str | None = None,
        version: int | None = None,
        alias: str | None = None,
    ) -> DashboardPresets:
        """The presets that fit a model version (``model_id``, optional ``version``) or the
        version an endpoint ``alias`` serves, best first with plain reasons, and the layout
        2 spec of the first as a starting draft (nothing is saved)."""
        return await self._c.send(
            v17.list_dashboard_presets(model_id=model_id, version=version, alias=alias)
        )

    async def sources(self, dashboard_id: str) -> SourceCatalog:
        """What the draft's model version offers to panels and inputs."""
        return await self._c.send(v17.get_dashboard_sources(dashboard_id))

    async def upgrade(self, dashboard_id: str, *, etag: str) -> DashboardUpgrade:
        """Convert a layout 1 draft to layout 2 and save it as the draft (``etag``: the
        ``draft_etag``). Publish it as usual. Dashboards are upgraded automatically when
        the server starts; use this for one that was flagged ``upgrade_needs_review``."""
        return await self._c.send(v17.upgrade_dashboard(dashboard_id, etag))

    # ---------------------------------------------- v0.17: try a case and score a file
    async def predict(
        self,
        dashboard_id: str,
        rows: Mapping[str, Any] | Iterable[Mapping[str, Any]],
        *,
        label: str | None = None,
        wait: int | None = None,
        idempotency_key: str | None = None,
    ) -> Readout:
        """Try a case: score one or more scenario rows (at most 20; inputs you leave out
        take the defaults) as your own private prediction readout. A value outside the
        range the analyst set is refused, never clipped. ``wait``: seconds the server may
        hold the call for the result (``Prefer: wait=N``). See :meth:`wait_readout`."""
        call = v17.submit_dashboard_predictions(
            dashboard_id, rows_body(rows, label=label), wait=wait,
            idempotency_key=idempotency_key,
        )  # fmt: skip
        return await self._c.send(call)

    async def keep_readout(self, dashboard_id: str, seq: int) -> ReadoutHeader:
        """Keep a tried case: it no longer expires and waits for an editor's review before
        it can enter the history."""
        return await self._c.send(v17.keep_dashboard_readout(dashboard_id, seq))

    async def batch_template(self, dashboard_id: str) -> BatchTemplate:
        """The columns of a batch file, an example CSV and the row cap."""
        return await self._c.send(v17.get_dashboard_batch_template(dashboard_id))

    async def score_batch(
        self,
        dashboard_id: str,
        inputs: object,
        *,
        label: str | None = None,
        wait: int | None = None,
        idempotency_key: str | None = None,
    ) -> Readout:
        """Score a file of new cases (an ``xr.Dataset`` or XrJSON with the template's
        columns, one row per id) as your own private batch readout (``wait``: seconds the
        server may hold the call for the result). When it is ready,
        :meth:`download_predictions` gives the predictions as CSV."""
        call = v17.submit_dashboard_batch(
            dashboard_id, batch_body(inputs, label=label), wait=wait,
            idempotency_key=idempotency_key,
        )  # fmt: skip
        return await self._c.send(call)

    async def download_predictions(self, dashboard_id: str, seq: int) -> bytes:
        """The predictions of a batch readout as CSV bytes (yours, or anybody's for an
        editor). ``DASHBOARD_DOWNLOAD_GONE`` once the readout's results expired."""
        return await self._c.send(v17.download_dashboard_predictions(dashboard_id, seq))

    async def asset(self, dashboard_id: str, asset_id: str) -> bytes:
        """A picture of the published dashboard (PNG bytes; ``dsa_...`` ids are in the
        readout's picture panels)."""
        return await self._c.send(v17.get_dashboard_asset(dashboard_id, asset_id))

    # ------------------------------------------------------------------ refit requests
    def refit_requests(
        self, dashboard_id: str, *, status: str | None = None, limit: int | None = None
    ) -> AsyncPaginator[DashboardRefitRequest]:
        """Requests, made with actuals uploads, to refit the model with those numbers."""
        return self._c.page_iter(
            lambda cur: vcalls.list_dashboard_refit_requests(
                dashboard_id, limit, cur, status=status
            )
        )

    async def approve_refit(
        self,
        dashboard_id: str,
        request_id: str,
        *,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> DashboardRefitRequest:
        """Approve a refit request (editors with the runner role): the readout is published,
        the actuals become a new dataset version and the refit workflow starts as you."""
        call = vcalls.approve_dashboard_refit_request(
            dashboard_id, request_id, decision_body(note), idempotency_key=idempotency_key
        )
        return await self._c.send(call)

    async def reject_refit(
        self, dashboard_id: str, request_id: str, *, note: str | None = None
    ) -> DashboardRefitRequest:
        return await self._c.send(
            vcalls.reject_dashboard_refit_request(dashboard_id, request_id, decision_body(note))
        )


class AsyncSharedDashboard:
    """A dashboard opened from a share link (:meth:`AsyncDashboards.open_share`): the requests
    carry the share session, never the client's credentials or project. The session is
    refreshed shortly before it expires."""

    def __init__(self, client: AsyncClient, session: ShareSession) -> None:
        self._c = client
        self._session = session
        self.dashboard: ClientDashboard = session.dashboard

    def __repr__(self) -> str:
        return f"AsyncSharedDashboard(dashboard={self.dashboard.id!r}, session=<redacted>)"

    @property
    def expires_at(self) -> datetime:
        return self._session.expires_at

    async def _token(self) -> str:
        if session_due(self._session):
            await self.refresh()
        return self._session.session

    async def refresh(self) -> ShareSession:
        """A fresh session while the link is valid (an ended link raises)."""
        self._session = await self._c.send(vcalls.refresh_share_session(self._session.session))
        self.dashboard = self._session.dashboard
        return self._session

    async def get(self) -> ClientDashboard:
        """The dashboard as shared now (also kept as :attr:`dashboard`)."""
        self.dashboard = await self._c.send(vcalls.get_shared_dashboard(await self._token()))
        return self.dashboard

    async def readout_page(
        self, *, kind: str | None = None, before_seq: int | None = None, limit: int | None = None
    ) -> ReadoutList:
        return await self._c.send(
            vcalls.list_shared_readouts(
                await self._token(), kind=kind, before_seq=before_seq, limit=limit
            )
        )

    async def readouts(
        self, *, kind: str | None = None, limit: int | None = None
    ) -> AsyncIterator[ReadoutHeader]:
        """Every published readout, newest first."""
        before: int | None = None
        while True:
            page = await self.readout_page(kind=kind, before_seq=before, limit=limit)
            for item in page.items:
                yield item
            before = next_before_seq(page, before)
            if before is None:
                return

    async def readout(self, seq: int) -> Readout:
        return await self._c.send(vcalls.get_shared_readout(await self._token(), seq))

    async def wait_readout(
        self,
        seq: int,
        *,
        timeout: float | None = None,
        poll_interval: float = 1.0,
        max_poll_interval: float = 10.0,
        raise_on_failure: bool = True,
    ) -> Readout:
        return await _wait_readout(
            self._c,
            lambda: self.readout(seq),
            timeout=timeout,
            poll_interval=poll_interval,
            max_poll_interval=max_poll_interval,
            raise_on_failure=raise_on_failure,
        )

    async def grid(self) -> DashboardGrid:
        return await self._c.send(vcalls.get_shared_grid(await self._token()))

    async def whatif(self, multipliers: Mapping[str, float]) -> WhatIfResult:
        """A scenario on the grid (only when the link allows ``whatif``)."""
        return await self._c.send(
            vcalls.evaluate_shared_whatif(await self._token(), whatif_body(multipliers))
        )

    async def save_scenario(
        self,
        multipliers: Mapping[str, float],
        *,
        exact: bool = False,
        label: str | None = None,
        wait: int | None = None,
        idempotency_key: str | None = None,
    ) -> Readout:
        body = scenario_body(multipliers, exact=exact, label=label)
        call = vcalls.save_shared_scenario(
            await self._token(), body, wait=wait, idempotency_key=idempotency_key
        )
        return await self._c.send(call)

    async def actuals_template(self) -> ActualsTemplate:
        return await self._c.send(vcalls.get_shared_actuals_template(await self._token()))

    async def submit_actuals(
        self,
        inputs: object,
        *,
        label: str | None = None,
        request_refit: bool = False,
        idempotency_key: str | None = None,
    ) -> ReadoutHeader:
        """Upload actuals (only when the link allows ``actuals``); the readout waits for the
        analysts' review before anyone else sees it."""
        body = actuals_body(inputs, label=label, request_refit=request_refit)
        return await self._c.send(
            vcalls.submit_shared_actuals(await self._token(), body, idempotency_key=idempotency_key)
        )

    async def predict(
        self,
        rows: Mapping[str, Any] | Iterable[Mapping[str, Any]],
        *,
        label: str | None = None,
        wait: int | None = None,
        idempotency_key: str | None = None,
    ) -> Readout:
        """Try a case (only when the link allows ``scenario_rows``): a private readout of
        this link (``wait``: seconds the server may hold the call for the result)."""
        call = v17.submit_shared_predictions(
            await self._token(), rows_body(rows, label=label), wait=wait,
            idempotency_key=idempotency_key,
        )  # fmt: skip
        return await self._c.send(call)

    async def batch_template(self) -> BatchTemplate:
        return await self._c.send(v17.get_shared_batch_template(await self._token()))

    async def score_batch(
        self,
        inputs: object,
        *,
        label: str | None = None,
        wait: int | None = None,
        idempotency_key: str | None = None,
    ) -> Readout:
        """Score a file of new cases (only when the link allows ``batch``): a private
        readout of this link (``wait``: seconds the server may hold the call)."""
        call = v17.submit_shared_batch(
            await self._token(), batch_body(inputs, label=label), wait=wait,
            idempotency_key=idempotency_key,
        )  # fmt: skip
        return await self._c.send(call)

    async def download_predictions(self, seq: int) -> bytes:
        """The predictions CSV of a batch readout made through this link."""
        return await self._c.send(v17.download_shared_predictions(await self._token(), seq))

    async def asset(self, asset_id: str) -> bytes:
        """A picture of the shared dashboard (PNG bytes)."""
        return await self._c.send(v17.get_shared_asset(await self._token(), asset_id))


async def _wait_readout(
    client: AsyncClient,
    fetch: Callable[[], Awaitable[Readout]],
    *,
    timeout: float | None,
    poll_interval: float,
    max_poll_interval: float,
    raise_on_failure: bool,
) -> Readout:
    deadline = None if timeout is None else time.monotonic() + timeout
    delay = poll_interval
    while True:
        current: Readout = await fetch()
        if readout_done(current):
            if raise_on_failure and current.status == "failed":
                raise readout_error(current)
            return current
        if deadline is not None and time.monotonic() + delay > deadline:
            raise TimeoutError(f"readout {current.seq} is still {current.status} after {timeout} s")
        await client._sleep(delay)
        delay = min(delay * 1.5, max_poll_interval)
