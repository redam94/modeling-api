"""Request builders for ``mapi_spec.api.V014_ENDPOINTS`` (v0.14): run media, run comparison,
tracked keys, experiments and saved run views.

Like the other builder modules they validate arguments before any request (media keys,
labels, the run-view query grammar), encode paths and return a
:class:`~mapi_client._http.Call`. :func:`media_write` builds the body of ``log_run_media``
from bytes, a file or (with the SDK installed) an array, a figure or a table.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from mapi_spec.api import (
    API_PREFIX,
    Experiment,
    ExperimentCreate,
    ExperimentPatch,
    ExperimentRunsResult,
    ExperimentRunsWrite,
    MediaKeySummary,
    MediaWrite,
    Page,
    RunCompare,
    RunKeys,
    RunMedia,
    RunView,
    RunViewCreate,
    RunViewPatch,
    RunViewQuery,
)
from mapi_spec.media import (
    MAX_API_MEDIA_BYTES,
    MediaContentType,
    MediaKind,
    check_media_key,
    sniff_image,
)
from mapi_spec.tracking import (
    MAX_COMPARE_KEYS,
    MAX_COMPARE_MEDIA_KEYS,
    MAX_COMPARE_RUNS,
    check_labels,
    check_metric_key,
)

from mapi_client._calls import _CREATED, _NO_CONTENT, page_params, parse_none
from mapi_client._calls_tracking import UNSET, Unset
from mapi_client._http import Call, dump, parse_model, segment

__all__ = [
    "COMPARE_POINTS",
    "compare_runs",
    "create_experiment",
    "create_run_view",
    "delete_experiment",
    "delete_run_view",
    "experiment_patch",
    "get_experiment",
    "get_run_media",
    "list_experiments",
    "list_run_keys",
    "list_run_media",
    "list_run_media_keys",
    "list_run_views",
    "log_run_media",
    "media_write",
    "update_experiment",
    "update_experiment_runs",
    "update_run_view",
    "view_list_params",
]

V1: Final = API_PREFIX
COMPARE_POINTS: Final = (10, 2000)
MEDIA_KINDS: Final = ("image", "table")
_IMAGE_TYPES: Final[dict[str, MediaContentType]] = {"png": "image/png", "jpeg": "image/jpeg"}


def _runs(run_id: str) -> str:
    return f"{V1}/runs/{segment(run_id)}"


def _exp(experiment_id: str) -> str:
    return f"{V1}/experiments/{segment(experiment_id)}"


def _attempt(value: int) -> int:
    if value < 1:
        raise ValueError("attempt must be >= 1")
    return value


def _bool(value: bool) -> str:
    return "true" if value else "false"


# ==================================================================================== media


def list_run_media(
    run_id: str,
    limit: int | None,
    cursor: str | None,
    *,
    key: str | None = None,
    kind: str | None = None,
    attempt: int | None = None,
    links: bool | None = None,
) -> Call[Page[RunMedia]]:
    # operation: list_run_media (links=True adds a signed url per item: the server default)
    params: dict[str, str | int | list[str]] = dict(page_params(limit, cursor))
    if key is not None:
        params["key"] = check_media_key(key)
    if kind is not None:
        if kind not in MEDIA_KINDS:
            raise ValueError(f"kind must be one of {', '.join(MEDIA_KINDS)}")
        params["kind"] = kind
    if attempt is not None:
        params["attempt"] = _attempt(attempt)
    if links is not None:
        params["links"] = _bool(links)
    return Call("GET", f"{_runs(run_id)}/media", parse_model(Page[RunMedia]), params=params)


def get_run_media(run_id: str, media_id: str) -> Call[RunMedia]:
    # operation: get_run_media (always with a signed url)
    return Call("GET", f"{_runs(run_id)}/media/{segment(media_id)}", parse_model(RunMedia))


def log_run_media(run_id: str, body: MediaWrite) -> Call[RunMedia]:
    # operation: log_run_media
    return Call("POST", f"{_runs(run_id)}/media", parse_model(RunMedia), _CREATED, json=dump(body))


def list_run_media_keys(
    run_id: str, limit: int | None, cursor: str | None, *, attempt: int | None = None
) -> Call[Page[MediaKeySummary]]:
    # operation: list_run_media_keys
    params: dict[str, str | int | list[str]] = dict(page_params(limit, cursor))
    if attempt is not None:
        params["attempt"] = _attempt(attempt)
    path = f"{_runs(run_id)}/media-keys"
    return Call("GET", path, parse_model(Page[MediaKeySummary]), params=params)


def media_write(
    key: str,
    source: Any,
    *,
    kind: MediaKind = "image",
    step: int | None = None,
    labels: Mapping[str, str] | None = None,
    caption: str | None = None,
) -> MediaWrite:
    """The ``MediaWrite`` body of one image or table.

    An image ``source`` is PNG/JPEG bytes, a path to such a file, or (when ``mapi-sdk`` is
    installed, as with the ``xarray`` extra) a numpy array, a Pillow image or a DataArray.
    A table ``source`` is a pandas DataFrame, a mapping of columns or a list of records (it
    needs ``mapi-sdk``), or the JSON bytes of a table. Raises :class:`ValueError` (also for
    more than ``MAX_API_MEDIA_BYTES`` bytes) before any request.
    """
    check_media_key(key)
    lab = check_labels(labels)
    if isinstance(source, str | Path):
        source = Path(source).read_bytes()
    if isinstance(source, bytearray | memoryview):
        source = bytes(source)
    content_type: MediaContentType
    if isinstance(source, bytes):
        data = source
        if kind == "table":
            content_type = "application/json"
        else:
            info = sniff_image(data)
            if info is None:
                raise ValueError(f"media {key!r}: the bytes are not a PNG or a JPEG image")
            content_type = _IMAGE_TYPES[info.format]
    else:
        item = _sdk_item(key, source, kind=kind, step=step, labels=lab, caption=caption)
        data, content_type = item.data, item.content_type
    if len(data) > MAX_API_MEDIA_BYTES:
        raise ValueError(
            f"media {key!r} has {len(data):,} bytes; the API accepts at most "
            f"{MAX_API_MEDIA_BYTES:,} (log larger images from a run)"
        )
    return MediaWrite(
        key=key,
        kind=kind,
        content_type=content_type,
        data=base64.b64encode(data).decode("ascii"),
        step=step,
        labels=lab,
        caption=caption,
    )


def _sdk_item(
    key: str,
    source: Any,
    *,
    kind: MediaKind,
    step: int | None,
    labels: dict[str, str],
    caption: str | None,
) -> Any:
    try:
        from mapi_sdk.media import image_item, table_item
    except ImportError:  # pragma: no cover - the SDK is an optional dependency
        raise ValueError(
            f"media {key!r}: arrays, images and tables need mapi-sdk "
            "(pip install 'mapi-client[xarray]'); bytes and files work without it"
        ) from None
    make = table_item if kind == "table" else image_item
    return make(key, source, step=step, labels=labels, caption=caption)


# =============================================================================== comparison


def compare_runs(
    run_ids: Sequence[str],
    *,
    keys: Sequence[str] = (),
    media_keys: Sequence[str] = (),
    max_points: int | None = None,
    params: bool | None = None,
) -> Call[RunCompare]:
    # operation: compare_runs (run, key and media_key are repeated query parameters)
    if isinstance(run_ids, str) or isinstance(keys, str) or isinstance(media_keys, str):
        raise ValueError("run_ids, keys and media_keys take a list of strings")
    runs = [str(r) for r in run_ids]
    if not 2 <= len(runs) <= MAX_COMPARE_RUNS:
        raise ValueError(f"compare 2 to {MAX_COMPARE_RUNS} runs, got {len(runs)}")
    if len(set(runs)) != len(runs):
        raise ValueError("a run is given more than once")
    if len(keys) > MAX_COMPARE_KEYS:
        raise ValueError(f"at most {MAX_COMPARE_KEYS} keys, got {len(keys)}")
    if len(media_keys) > MAX_COMPARE_MEDIA_KEYS:
        raise ValueError(f"at most {MAX_COMPARE_MEDIA_KEYS} media keys, got {len(media_keys)}")
    query: dict[str, str | int | list[str]] = {"run": runs}
    if keys:
        query["key"] = [check_metric_key(k) for k in keys]
    if media_keys:
        query["media_key"] = [check_media_key(k) for k in media_keys]
    if max_points is not None:
        lo, hi = COMPARE_POINTS
        if not lo <= max_points <= hi:
            raise ValueError(f"max_points must be between {lo} and {hi}")
        query["max_points"] = max_points
    if params is not None:
        query["params"] = _bool(params)
    return Call("GET", f"{V1}/run-compare", parse_model(RunCompare), params=query)


def list_run_keys(*, experiment_id: str | None = None, prefix: str | None = None) -> Call[RunKeys]:
    # operation: list_run_keys
    query: dict[str, str | int | list[str]] = {}
    if experiment_id is not None:
        query["experiment_id"] = experiment_id
    if prefix is not None:
        query["prefix"] = prefix
    return Call("GET", f"{V1}/run-keys", parse_model(RunKeys), params=query)


# ============================================================================= experiments


def list_experiments(
    limit: int | None,
    cursor: str | None,
    *,
    name: str | None = None,
    archived: bool | None = None,
) -> Call[Page[Experiment]]:
    # operation: list_experiments (newest first)
    query: dict[str, str | int | list[str]] = dict(page_params(limit, cursor, name=name))
    if archived is not None:
        query["archived"] = _bool(archived)
    return Call("GET", f"{V1}/experiments", parse_model(Page[Experiment]), params=query)


def create_experiment(body: ExperimentCreate) -> Call[Experiment]:
    # operation: create_experiment (a name taken in the project is a 409)
    return Call("POST", f"{V1}/experiments", parse_model(Experiment), _CREATED, json=dump(body))


def get_experiment(experiment_id: str) -> Call[Experiment]:
    return Call("GET", _exp(experiment_id), parse_model(Experiment))


def update_experiment(experiment_id: str, body: ExperimentPatch) -> Call[Experiment]:
    # operation: update_experiment (a merge patch: sending it twice has the same result)
    json = body.model_dump(mode="json", include=body.model_fields_set)
    return Call("PATCH", _exp(experiment_id), parse_model(Experiment), json=json, retry_safe=True)


def delete_experiment(experiment_id: str) -> Call[None]:
    # operation: delete_experiment (the runs stay; the experiment's views are deleted)
    return Call("DELETE", _exp(experiment_id), parse_none, _NO_CONTENT)


def update_experiment_runs(
    experiment_id: str, body: ExperimentRunsWrite
) -> Call[ExperimentRunsResult]:
    # operation: update_experiment_runs (moving the same runs again changes nothing)
    return Call(
        "POST",
        f"{_exp(experiment_id)}/runs",
        parse_model(ExperimentRunsResult),
        json=dump(body),
        retry_safe=True,
    )


def experiment_patch(
    *,
    name: str | None = None,
    description: str | Unset | None = UNSET,
    archived: bool | None = None,
) -> ExperimentPatch:
    """An :class:`ExperimentPatch` with the given fields (``description=None`` clears it;
    ``UNSET`` keeps it)."""
    fields: dict[str, Any] = {}
    if name is not None:
        fields["name"] = name
    if not isinstance(description, Unset):
        fields["description"] = description
    if archived is not None:
        fields["archived"] = archived
    if not fields:
        raise ValueError("set at least one of name, description or archived")
    return ExperimentPatch.model_validate(fields)


# ============================================================================ saved views


def list_run_views(
    limit: int | None, cursor: str | None, *, experiment_id: str | None = None
) -> Call[Page[RunView]]:
    # operation: list_run_views (order: name)
    query = page_params(limit, cursor, experiment_id=experiment_id)
    return Call("GET", f"{V1}/run-views", parse_model(Page[RunView]), params=query)


def create_run_view(body: RunViewCreate) -> Call[RunView]:
    # operation: create_run_view
    return Call("POST", f"{V1}/run-views", parse_model(RunView), _CREATED, json=dump(body))


def update_run_view(view_id: str, body: RunViewPatch) -> Call[RunView]:
    # operation: update_run_view
    json = body.model_dump(mode="json", include=body.model_fields_set, exclude_none=True)
    path = f"{V1}/run-views/{segment(view_id)}"
    return Call("PATCH", path, parse_model(RunView), json=json, retry_safe=True)


def delete_run_view(view_id: str) -> Call[None]:
    # operation: delete_run_view
    return Call("DELETE", f"{V1}/run-views/{segment(view_id)}", parse_none, _NO_CONTENT)


def view_list_params(query: RunViewQuery) -> dict[str, Any]:
    """The keyword arguments of ``runs.list`` that apply a saved view's query."""
    return {
        "kind": query.kind,
        "state": query.state,
        "name": query.name,
        "spec_id": query.spec_id,
        "experiment_id": query.experiment_id,
        "parent_run_id": query.parent_run_id,
        "tags": list(query.tags) or None,
        "filters": list(query.filters),
        "order_by": query.order_by,
        "columns": list(query.columns),
    }
