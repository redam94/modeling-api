"""Pure helpers of the v0.14 calls shared by the sync and async clients: media downloads
(a signed link checked for size, CRC32C and sha256), experiment lookup by name and the
saved-view bodies. No I/O here."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from mapi_spec.api import (
    ArtifactLink,
    Experiment,
    ExperimentRunsWrite,
    RunMedia,
    RunViewCreate,
    RunViewPatch,
    RunViewQuery,
)
from mapi_spec.errors import IntegrityFailure
from mapi_spec.media import MEDIA_EXT

_UNSAFE = re.compile(r"[^A-Za-z0-9_.\-]")

__all__ = [
    "check_media_sha256",
    "media_file_name",
    "media_link",
    "pick_experiment",
    "runs_write",
    "view_create",
    "view_patch",
    "view_query",
]


def media_link(media: RunMedia) -> ArtifactLink:
    """The signed GET of a media item as an :class:`ArtifactLink` (size + CRC32C checked by
    the client's download)."""
    if media.url is None:
        raise ValueError(f"media {media.id} has no download link (get it with get_media)")
    return ArtifactLink(
        name=media_file_name(media),
        url=media.url,
        expires_at=media.expires_at or datetime.now(UTC) + timedelta(minutes=5),
        size=media.size,
        content_type=media.content_type,
        crc32c=media.crc32c,
        generation=1,
    )


def check_media_sha256(media: RunMedia, data: bytes) -> bytes:
    """``data`` when its sha256 is the one the server recorded (else IntegrityFailure)."""
    got = "sha256:" + hashlib.sha256(data).hexdigest()
    if got != media.sha256:
        raise IntegrityFailure(f"media {media.id} failed its sha256 check")
    return data


def media_file_name(media: RunMedia) -> str:
    """``<key>[-<label values>][-<step>].<ext>``: a readable, safe local file name."""
    parts = [media.key, *(v for _, v in sorted(media.labels.items()))]
    if media.step is not None:
        parts.append(str(media.step))
    stem = _UNSAFE.sub("_", "-".join(parts)).strip(".") or "media"
    return stem[:200] + MEDIA_EXT[media.content_type]


def pick_experiment(name_or_id: str, candidates: Iterable[Experiment]) -> Experiment | None:
    """The experiment whose id is ``name_or_id``, else whose name matches it (case is
    ignored, as the server compares names)."""
    want = name_or_id.strip().lower()
    by_name = None
    for exp in candidates:
        if exp.id == name_or_id:
            return exp
        if exp.name.strip().lower() == want and by_name is None:
            by_name = exp
    return by_name


def runs_write(add: Sequence[str] = (), remove: Sequence[str] = ()) -> ExperimentRunsWrite:
    if isinstance(add, str) or isinstance(remove, str):
        raise ValueError("add and remove take a list of run ids")
    return ExperimentRunsWrite(add=list(add), remove=list(remove))


def view_query(
    query: RunViewQuery | Mapping[str, Any] | None = None, **fields: Any
) -> RunViewQuery:
    """A :class:`RunViewQuery` from a query (or mapping) plus keyword fields (``None`` and
    empty values are left out)."""
    base: dict[str, Any] = {}
    if isinstance(query, RunViewQuery):
        base = query.model_dump(exclude_defaults=True)
    elif query is not None:
        base = dict(query)
    base.update({k: v for k, v in fields.items() if v not in (None, (), [])})
    return RunViewQuery.model_validate(base)


def view_create(
    name: str, query: RunViewQuery | Mapping[str, Any], experiment_id: str | None
) -> RunViewCreate:
    return RunViewCreate(name=name, experiment_id=experiment_id, query=view_query(query))


def view_patch(name: str | None, query: RunViewQuery | Mapping[str, Any] | None) -> RunViewPatch:
    return RunViewPatch(name=name, query=None if query is None else view_query(query))
