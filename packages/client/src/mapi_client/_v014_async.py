"""v0.14 resources of the asynchronous client (the twins of :mod:`mapi_client._v014`): run
media, comparison and keys (mixed into :class:`~mapi_client._async.AsyncRuns`),
:class:`AsyncExperiments` and :class:`AsyncRunViews`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from mapi_spec.api import (
    Experiment,
    ExperimentCreate,
    ExperimentRunsResult,
    MediaKeySummary,
    RunCompare,
    RunKeys,
    RunMedia,
    RunView,
    RunViewQuery,
)
from mapi_spec.errors import ErrorCode, MapiError

from mapi_client import _calls_v014 as vcalls
from mapi_client._calls_tracking import UNSET, Unset
from mapi_client._v014_common import (
    check_media_sha256,
    media_link,
    pick_experiment,
    runs_write,
    view_create,
    view_patch,
)
from mapi_client.pagination import AsyncPaginator

if TYPE_CHECKING:
    from mapi_client._async import AsyncClient

__all__ = ["AsyncExperiments", "AsyncRunViews", "AsyncRunsV014Mixin"]


class AsyncRunsV014Mixin:
    """Media, comparison and tracked keys of ``client.runs`` (v0.14)."""

    _c: AsyncClient

    # -------------------------------------------------------------------------- media
    def media(
        self,
        run_id: str,
        *,
        key: str | None = None,
        kind: str | None = None,
        attempt: int | None = None,
        links: bool = True,
        limit: int | None = None,
    ) -> AsyncPaginator[RunMedia]:
        """Images and tables the run logged (order: key, labels, step). With ``links``
        each item has a signed ``url``; ``caption`` is untrusted text."""
        return self._c.page_iter(
            lambda cur: vcalls.list_run_media(
                run_id, limit, cur, key=key, kind=kind, attempt=attempt, links=links
            )
        )

    def media_keys(
        self, run_id: str, *, attempt: int | None = None, limit: int | None = None
    ) -> AsyncPaginator[MediaKeySummary]:
        """Media series of the run (key + labels) with their counts and steps."""
        return self._c.page_iter(
            lambda cur: vcalls.list_run_media_keys(run_id, limit, cur, attempt=attempt)
        )

    async def get_media(self, run_id: str, media_id: str) -> RunMedia:
        """One media item with a fresh signed ``url``."""
        return await self._c.send(vcalls.get_run_media(run_id, media_id))

    async def download_media(self, run_id: str, media: str | RunMedia) -> bytes:
        """The bytes of a media item, checked for size, CRC32C and sha256.

        ``media`` is an id or a :class:`RunMedia` (one without a ``url``, or with an expired
        one, is fetched again first).
        """
        item = media if isinstance(media, RunMedia) else await self.get_media(run_id, media)
        if item.url is None:
            item = await self.get_media(run_id, item.id)
        data = await self._c.download(media_link(item))
        return check_media_sha256(item, data)

    async def log_image(
        self,
        run_id: str,
        key: str,
        image: Any,
        *,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
        caption: str | None = None,
    ) -> RunMedia:
        """Log a PNG/JPEG image (bytes or a file path; with ``mapi-sdk`` also an array, a
        Pillow image or a DataArray) to a run, also after it finished. At most 4 MiB."""
        body = vcalls.media_write(key, image, step=step, labels=labels, caption=caption)
        return await self._c.send(vcalls.log_run_media(run_id, body))

    async def log_table(
        self,
        run_id: str,
        key: str,
        table: Any,
        *,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
        caption: str | None = None,
    ) -> RunMedia:
        """Log a small table (a DataFrame, a mapping of columns or a list of records; JSON
        bytes of a table without ``mapi-sdk``) to a run."""
        body = vcalls.media_write(
            key, table, kind="table", step=step, labels=labels, caption=caption
        )
        return await self._c.send(vcalls.log_run_media(run_id, body))

    # --------------------------------------------------------------------- comparison
    async def keys(self, *, experiment_id: str | None = None, prefix: str | None = None) -> RunKeys:
        """Metric series, param keys and tag keys of the project's runs (or of one
        experiment's), most common first."""
        return await self._c.send(vcalls.list_run_keys(experiment_id=experiment_id, prefix=prefix))

    async def compare(
        self,
        run_ids: Sequence[str],
        *,
        keys: Sequence[str] = (),
        media_keys: Sequence[str] = (),
        max_points: int = 300,
        params: bool = True,
    ) -> RunCompare:
        """Compare 2 to 10 runs: metric summaries, the histories of ``keys`` (default: the
        keys most runs have), the params (``params=False`` leaves them out) and media."""
        call = vcalls.compare_runs(
            run_ids, keys=keys, media_keys=media_keys, max_points=max_points, params=params
        )
        return await self._c.send(call)


class _Resource:
    def __init__(self, client: AsyncClient) -> None:
        self._c = client


class AsyncExperiments(_Resource):
    """Named groups of runs (``client.experiments``; v0.14)."""

    def list(
        self,
        *,
        name: str | None = None,
        archived: bool | None = None,
        limit: int | None = None,
    ) -> AsyncPaginator[Experiment]:
        """Experiments, newest first (``archived=False`` hides archived ones)."""
        return self._c.page_iter(
            lambda cur: vcalls.list_experiments(limit, cur, name=name, archived=archived)
        )

    async def get(self, experiment_id: str) -> Experiment:
        return await self._c.send(vcalls.get_experiment(experiment_id))

    async def find(self, name_or_id: str) -> Experiment | None:
        """The experiment with this id, or with this name (case is ignored)."""
        if name_or_id.startswith("exp_"):
            try:
                return await self.get(name_or_id)
            except MapiError as exc:
                if exc.code is not ErrorCode.NOT_FOUND:
                    raise
        return pick_experiment(name_or_id, [e async for e in self.list(name=name_or_id, limit=200)])

    async def create(self, name: str, *, description: str | None = None) -> Experiment:
        body = ExperimentCreate(name=name, description=description)
        return await self._c.send(vcalls.create_experiment(body))

    async def update(
        self,
        experiment_id: str,
        *,
        name: str | None = None,
        description: str | Unset | None = UNSET,
        archived: bool | None = None,
    ) -> Experiment:
        """Rename, describe (``None`` clears the description), archive or unarchive."""
        body = vcalls.experiment_patch(name=name, description=description, archived=archived)
        return await self._c.send(vcalls.update_experiment(experiment_id, body))

    async def delete(self, experiment_id: str) -> None:
        """Delete the experiment; its runs stay (without an experiment), its views go."""
        await self._c.send(vcalls.delete_experiment(experiment_id))

    async def add_runs(self, experiment_id: str, run_ids: Sequence[str]) -> ExperimentRunsResult:
        """Move runs into the experiment (a run leaves its previous one)."""
        return await self._c.send(
            vcalls.update_experiment_runs(experiment_id, runs_write(add=run_ids))
        )

    async def remove_runs(self, experiment_id: str, run_ids: Sequence[str]) -> ExperimentRunsResult:
        body = runs_write(remove=run_ids)
        return await self._c.send(vcalls.update_experiment_runs(experiment_id, body))


class AsyncRunViews(_Resource):
    """Saved runs-table views (``client.run_views``; v0.14)."""

    def list(
        self, *, experiment_id: str | None = None, limit: int | None = None
    ) -> AsyncPaginator[RunView]:
        return self._c.page_iter(
            lambda cur: vcalls.list_run_views(limit, cur, experiment_id=experiment_id)
        )

    async def find(self, name_or_id: str, *, experiment_id: str | None = None) -> RunView | None:
        """The view with this id, or with this name (case is ignored)."""
        want = name_or_id.strip().lower()
        async for view in self.list(experiment_id=experiment_id):
            if view.id == name_or_id or view.name.lower() == want:
                return view
        return None

    async def create(
        self,
        name: str,
        query: RunViewQuery | Mapping[str, Any],
        *,
        experiment_id: str | None = None,
    ) -> RunView:
        """Save a ``runs.list`` query (filters, order, columns, ...) under a name."""
        return await self._c.send(vcalls.create_run_view(view_create(name, query, experiment_id)))

    async def update(
        self,
        view_id: str,
        *,
        name: str | None = None,
        query: RunViewQuery | Mapping[str, Any] | None = None,
    ) -> RunView:
        return await self._c.send(vcalls.update_run_view(view_id, view_patch(name, query)))

    async def delete(self, view_id: str) -> None:
        await self._c.send(vcalls.delete_run_view(view_id))
