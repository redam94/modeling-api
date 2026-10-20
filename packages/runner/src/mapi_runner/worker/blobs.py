"""Image pack reads for collections (v0.15): a :class:`~mapi_sdk.collection.BlobSource` over
the run's signed, generation-pinned pack URLs.

The worker range-reads pack bytes itself (:meth:`~mapi_runner.transfer.Transfers.read_range`);
nothing is downloaded whole. Each pack's signed GET is checked once, when the source is made:
its content address (:func:`~mapi_spec.collection.pack_digest` over the store's own size,
CRC32C and MD5 of the pinned generation) must be the digest the collection names it by. A
range read cannot be checked against the object's hashes, so every image read checks its own
CRC-32 and sha256 instead (:func:`~mapi_sdk.collection.read_blob`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from mapi_spec.collection import pack_digest
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.storage import SignedGet

from mapi_runner.transfer import Transfers
from mapi_runner.worker.context import WorkerContext

__all__ = ["SignedPackSource", "check_pack"]

_MAX_READ: Final = 256 * 1024**2
"""Largest single range read (the loader reads spans of at most 64 MiB)."""


def check_pack(digest: str, get: SignedGet) -> None:
    """Refuse a signed pack whose stored hashes do not give ``digest`` (``INTEGRITY_FAILURE``;
    a pack without an MD5 cannot be addressed)."""
    if get.md5_hash is None:
        raise MapiError(
            "an image pack has no MD5, so its content address cannot be checked",
            code=ErrorCode.INTEGRITY_FAILURE,
            untrusted_text=digest[:64],
        )
    try:
        got = pack_digest(get.size, get.crc32c, get.md5_hash)
    except ValueError as exc:
        raise MapiError(
            "an image pack has hashes in an unknown form",
            code=ErrorCode.INTEGRITY_FAILURE,
            untrusted_text=str(exc)[:500],
        ) from None
    if got != digest:
        raise MapiError(
            "an image pack does not match its content address",
            code=ErrorCode.INTEGRITY_FAILURE,
            untrusted_text=digest[:64],
        )


class SignedPackSource:
    """Range reads of packs ``{digest: SignedGet}`` (thread-safe: one shared HTTP client)."""

    def __init__(self, ctx: WorkerContext, packs: Mapping[str, SignedGet]) -> None:
        for digest, get in packs.items():
            check_pack(digest, get)
        self._packs = dict(packs)
        self._transfers: Transfers = ctx.transfers
        self._cancel = ctx.cancel

    @property
    def digests(self) -> list[str]:
        return list(self._packs)

    def read(self, pack: str, offset: int, size: int) -> bytes:
        get = self._packs.get(pack)
        if get is None:
            raise MapiError(
                "the collection reads a pack that the run was not given",
                code=ErrorCode.INTEGRITY_FAILURE,
                untrusted_text=pack[:64],
            )
        if size == 0:
            return b""
        if offset < 0 or size < 0 or size > _MAX_READ or offset + size > get.size:
            raise MapiError(
                f"a read of {size} bytes at {offset} is outside the {get.size}-byte pack",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        if self._cancel is not None:
            self._cancel.check()
        return self._transfers.read_range(get, offset, offset + size)
