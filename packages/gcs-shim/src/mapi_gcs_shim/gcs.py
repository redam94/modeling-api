"""``GcsObjectStore``: :class:`mapi_spec.storage.ObjectStore` on real Cloud Storage.

Built on ``google-cloud-storage`` (install ``mapi-gcs-shim[gcp]``); the import is deferred so
the shim and its client never need it. The synchronous library calls run in worker threads.

This driver is exercised only with mocks in this repository — it has **not** been run against
live GCP (see the package README).

Signing: with a :class:`~mapi_gcs_shim.gcs_signing.BlobSigner` (``blob_signer``), V4 signed
URLs and POST policies are built here and only the string to sign goes to the signer — on
Cloud Run an :class:`~mapi_gcs_shim.gcs_signing.IamSignBlobSigner` (IAM ``signBlob`` with a
refreshing token; :meth:`GcsObjectStore.for_cloud_run`). Without one, signed URLs fall back
to ``Blob.generate_signed_url`` (``signing_credentials``, or ``service_account_email`` +
a static ``access_token``). POST policies are always built here: the library's helper pins
an exact ``key``, while the platform needs a key *prefix*.

Integrity: every write is checked against the CRC32C/MD5 GCS computed (a mismatch deletes
that generation and raises ``INTEGRITY_FAILURE``); copies pin the source generation with
``ifSourceGenerationMatch`` and compare the destination's size and CRC32C with the source's;
signed GETs are generation-pinned and signed PUTs carry a signed
``x-goog-if-generation-match`` (write-once by default).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn, TypeVar

import anyio.to_thread
from mapi_spec.errors import ErrorCode
from mapi_spec.storage import (
    AccessDenied,
    NotFound,
    ObjectMeta,
    ObjectPage,
    PostPolicy,
    PreconditionFailed,
    SignedGet,
    SignedPut,
    StorageError,
    check_object_name,
    check_ttl,
    crc32c,
    crc32c_to_base64,
    md5_base64,
)

from mapi_gcs_shim.client import InvalidRange
from mapi_gcs_shim.gcs_signing import (
    RSA_ALGORITHM,
    BlobSigner,
    IamSignBlobSigner,
    google_token_provider,
    rsa_signed_url,
)
from mapi_gcs_shim.signing import build_post_policy

__all__ = ["GCS_API_ENDPOINT", "GcsObjectStore", "map_gcs_error"]

T = TypeVar("T")

GCS_API_ENDPOINT = "https://storage.googleapis.com"


def _import_storage() -> Any:
    try:
        from google.cloud import storage  # type: ignore[import-not-found,unused-ignore]
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "GcsObjectStore needs google-cloud-storage: pip install 'mapi-gcs-shim[gcp]'"
        ) from exc
    return storage


def _status(exc: BaseException) -> int | None:
    code = getattr(exc, "code", None)
    if isinstance(code, int) and 300 <= code <= 599:
        return code
    response = getattr(exc, "response", None)  # google.resumable_media.InvalidResponse
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def map_gcs_error(exc: BaseException, *, bucket: str | None, name: str | None) -> NoReturn:
    """Re-raise a google-cloud-storage error as the ``mapi_spec.storage`` hierarchy.

    Exceptions that carry no HTTP status (programming errors) propagate unchanged.
    """
    status = _status(exc)
    detail = f"GCS: {exc}"
    if status is None:
        raise exc
    if status == 404:
        raise NotFound(detail, bucket=bucket, name=name) from exc
    if status in (304, 412):
        raise PreconditionFailed(detail, bucket=bucket, name=name) from exc
    if status in (401, 403):
        raise AccessDenied(detail, bucket=bucket, name=name) from exc
    if status == 416:
        raise InvalidRange(detail, bucket=bucket, name=name) from exc
    if status == 409:
        raise StorageError(detail, bucket=bucket, name=name, code=ErrorCode.CONFLICT) from exc
    raise StorageError(detail, bucket=bucket, name=name) from exc


def _blob_meta(blob: Any) -> ObjectMeta:
    if blob.generation is None or blob.size is None or blob.crc32c is None:
        raise StorageError(
            "GCS returned an object resource without generation/size/crc32c",
            bucket=getattr(blob.bucket, "name", None),
            name=blob.name,
        )
    return ObjectMeta(
        bucket=blob.bucket.name,
        name=blob.name,
        generation=int(blob.generation),
        metageneration=int(blob.metageneration or 1),
        size=int(blob.size),
        crc32c=blob.crc32c,
        md5_hash=blob.md5_hash,
        content_type=blob.content_type,
        metadata=dict(blob.metadata or {}),
        time_created=blob.time_created,
    )


class GcsObjectStore:
    """Async :class:`~mapi_spec.storage.ObjectStore` on Google Cloud Storage.

    Args:
        client: a ``google.cloud.storage.Client`` (created with default credentials if omitted).
        signing_credentials: credentials with ``sign_bytes``/``signer_email`` used for signed
            URLs and POST policies (default: the client's credentials).
        service_account_email / access_token: IAM ``signBlob`` signing for signed URLs when
            the credentials hold no private key (e.g. Cloud Run metadata credentials).
        uploader_credentials: signing credentials of a principal with
            ``storage.objects.create`` but **not** ``storage.objects.delete`` on the runs
            bucket, used for ``create_only`` POST policies. GCS POST forms have no generation
            precondition; an upload by a principal that cannot delete cannot overwrite.
            Terraform provisions it as the ``url-uploader`` account (``MAPI_URL_UPLOADER``).
        blob_signer: signs V4 URLs and POST policies (preferred over the library path).
        uploader_signer: the ``create_only`` POST-policy signer (same role as
            ``uploader_credentials``; either may be given).
        api_endpoint: base URL for signed URLs and POST policies.
        clock: ``now()`` for signing timestamps (tests).
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        project: str | None = None,
        signing_credentials: Any | None = None,
        service_account_email: str | None = None,
        access_token: str | None = None,
        uploader_credentials: Any | None = None,
        blob_signer: BlobSigner | None = None,
        uploader_signer: BlobSigner | None = None,
        api_endpoint: str = GCS_API_ENDPOINT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if client is None:
            client = _import_storage().Client(project=project)
        self._client = client
        self._signing_credentials = signing_credentials
        self._sa_email = service_account_email
        self._access_token = access_token
        self._uploader_credentials = uploader_credentials
        self._blob_signer = blob_signer
        self._uploader_signer = uploader_signer
        self._api_endpoint = api_endpoint.rstrip("/")
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def for_cloud_run(
        cls,
        client: Any | None = None,
        *,
        service_account_email: str,
        uploader_service_account_email: str | None = None,
        token: Callable[[], str] | None = None,
        project: str | None = None,
    ) -> GcsObjectStore:
        """The production wiring: URLs signed with IAM ``signBlob`` as the control-plane
        service account; ``create_only`` POST policies as an uploader account that has
        ``storage.objects.create`` but no ``delete`` (so its uploads cannot overwrite)."""
        provider = token or google_token_provider()
        uploader = (
            IamSignBlobSigner(uploader_service_account_email, provider)
            if uploader_service_account_email
            else None
        )
        return cls(
            client,
            project=project,
            blob_signer=IamSignBlobSigner(service_account_email, provider),
            uploader_signer=uploader,
        )

    async def _call(self, fn: Callable[[], T], *, bucket: str | None, name: str | None) -> T:
        try:
            return await anyio.to_thread.run_sync(fn)
        except (StorageError, ValueError, TypeError):
            raise
        except Exception as exc:
            map_gcs_error(exc, bucket=bucket, name=name)

    def _blob(self, bucket: str, name: str, generation: int | None = None) -> Any:
        return self._client.bucket(bucket).blob(name, generation=generation)

    def _sign_kwargs(self) -> dict[str, Any]:
        out: dict[str, Any] = {"version": "v4"}
        if self._signing_credentials is not None:
            out["credentials"] = self._signing_credentials
        if self._sa_email is not None:
            out["service_account_email"] = self._sa_email
        if self._access_token is not None:
            out["access_token"] = self._access_token
        return out

    # ---------------------------------------------------------------- ObjectStore

    async def put(
        self,
        bucket: str,
        name: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
        if_generation_match: int | None = None,
    ) -> ObjectMeta:
        check_object_name(name)

        payload = bytes(data)
        want_crc = crc32c_to_base64(crc32c(payload))
        want_md5 = md5_base64(payload)

        def run() -> ObjectMeta:
            blob = self._blob(bucket, name)
            if metadata:
                blob.metadata = dict(metadata)
            blob.upload_from_string(
                payload,
                content_type=content_type or "application/octet-stream",
                if_generation_match=if_generation_match,
                checksum="crc32c",
            )
            meta = _blob_meta(blob)
            md5_bad = meta.md5_hash is not None and meta.md5_hash != want_md5
            if meta.crc32c != want_crc or meta.size != len(payload) or md5_bad:
                self._discard(bucket, name, meta.generation)
                raise StorageError(
                    "the stored object does not match the uploaded bytes (CRC32C/MD5/size)",
                    bucket=bucket,
                    name=name,
                    code=ErrorCode.INTEGRITY_FAILURE,
                )
            return meta

        return await self._call(run, bucket=bucket, name=name)

    def _discard(self, bucket: str, name: str, generation: int) -> None:
        """Best-effort delete of exactly ``generation`` (a corrupt write or copy)."""
        with contextlib.suppress(Exception):
            self._blob(bucket, name, generation).delete(if_generation_match=generation)

    async def get(
        self,
        bucket: str,
        name: str,
        *,
        generation: int | None = None,
        byte_range: tuple[int, int | None] | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        check_object_name(name)
        if byte_range is not None:
            start, end = byte_range
            if start < 0 or (end is not None and end < start):
                raise ValueError("byte_range must be (start >= 0, end >= start | None)")

        def run() -> bytes:
            gen = generation
            if max_bytes is not None:
                live = self._client.bucket(bucket).get_blob(name, generation=generation)
                if live is None:
                    raise NotFound("no such object", bucket=bucket, name=name)
                size = int(live.size)
                if byte_range is not None:
                    s, e = byte_range
                    size = max(0, min(size - 1, size - 1 if e is None else e) - s + 1)
                if size > max_bytes:
                    raise StorageError(
                        f"object is {size} bytes, above the {max_bytes}-byte cap",
                        bucket=bucket,
                        name=name,
                        code=ErrorCode.PAYLOAD_TOO_LARGE,
                    )
                gen = int(live.generation)  # pin: the object cannot grow under us
            blob = self._blob(bucket, name, gen)
            if byte_range is None:
                data: bytes = blob.download_as_bytes()
            else:
                data = blob.download_as_bytes(start=byte_range[0], end=byte_range[1], checksum=None)
            if max_bytes is not None and len(data) > max_bytes:  # pragma: no cover - defensive
                raise StorageError("object exceeds the byte cap", code=ErrorCode.PAYLOAD_TOO_LARGE)
            return data

        return await self._call(run, bucket=bucket, name=name)

    async def stat(self, bucket: str, name: str, *, generation: int | None = None) -> ObjectMeta:
        check_object_name(name)

        def run() -> ObjectMeta:
            blob = self._client.bucket(bucket).get_blob(name, generation=generation)
            if blob is None:
                raise NotFound("no such object", bucket=bucket, name=name)
            return _blob_meta(blob)

        return await self._call(run, bucket=bucket, name=name)

    async def list(
        self,
        bucket: str,
        *,
        prefix: str = "",
        delimiter: str | None = None,
        page_token: str | None = None,
        max_results: int = 1000,
    ) -> ObjectPage:
        def run() -> ObjectPage:
            iterator = self._client.list_blobs(
                bucket,
                prefix=prefix or None,
                delimiter=delimiter,
                page_token=page_token,
                page_size=max_results,
            )
            page = next(iter(iterator.pages), None)
            if page is None:
                return ObjectPage()
            items = [_blob_meta(b) for b in page]
            return ObjectPage(
                items=items,
                prefixes=sorted(page.prefixes or ()),
                next_page_token=iterator.next_page_token,
            )

        return await self._call(run, bucket=bucket, name=None)

    async def delete(
        self,
        bucket: str,
        name: str,
        *,
        generation: int | None = None,
        if_generation_match: int | None = None,
    ) -> None:
        check_object_name(name)

        def run() -> None:
            self._blob(bucket, name, generation).delete(if_generation_match=if_generation_match)

        await self._call(run, bucket=bucket, name=name)

    async def copy(
        self,
        src_bucket: str,
        src_name: str,
        dst_bucket: str,
        dst_name: str,
        *,
        source_generation: int | None = None,
        if_generation_match: int | None = None,
    ) -> ObjectMeta:
        check_object_name(src_name)
        check_object_name(dst_name)
        source = await self.stat(src_bucket, src_name, generation=source_generation)

        def run() -> ObjectMeta:
            src = self._blob(src_bucket, src_name, source.generation)
            dst = self._blob(dst_bucket, dst_name)
            pins = {
                "if_generation_match": if_generation_match,
                "if_source_generation_match": source.generation,
            }
            token, _, _ = dst.rewrite(src, **pins)
            while token is not None:
                token, _, _ = dst.rewrite(src, token=token, **pins)
            if dst.generation is None:
                dst.reload()
            meta = _blob_meta(dst)
            if meta.size != source.size or meta.crc32c != source.crc32c:
                self._discard(dst_bucket, dst_name, meta.generation)
                raise StorageError(
                    "the copy does not match its source generation (size/CRC32C)",
                    bucket=dst_bucket,
                    name=dst_name,
                    code=ErrorCode.INTEGRITY_FAILURE,
                )
            return meta

        return await self._call(run, bucket=dst_bucket, name=dst_name)

    async def sign_get(
        self, bucket: str, name: str, *, expires_in: timedelta, generation: int | None = None
    ) -> SignedGet:
        check_ttl(expires_in)
        meta = await self.stat(bucket, name, generation=generation)
        now = self._clock()

        def run() -> str:
            if self._blob_signer is not None:
                return rsa_signed_url(
                    self._blob_signer,
                    method="GET",
                    bucket=bucket,
                    name=name,
                    expires_in=expires_in,
                    endpoint=self._api_endpoint,
                    now=now,
                    query={"generation": str(meta.generation)},
                )
            blob = self._blob(bucket, name, meta.generation)
            url: str = blob.generate_signed_url(
                expiration=expires_in,
                method="GET",
                generation=meta.generation,
                **self._sign_kwargs(),
            )
            return url

        url = await self._call(run, bucket=bucket, name=name)
        return SignedGet(
            url=url,
            expires_at=now + expires_in,
            generation=meta.generation,
            size=meta.size,
            crc32c=meta.crc32c,
            md5_hash=meta.md5_hash,
            content_type=meta.content_type,
        )

    async def sign_put(
        self,
        bucket: str,
        name: str,
        *,
        expires_in: timedelta,
        content_type: str | None = None,
        max_bytes: int | None = None,
        if_generation_match: int | None = 0,
    ) -> SignedPut:
        check_ttl(expires_in)
        check_object_name(name)
        extra: dict[str, str] = {}
        if if_generation_match is not None:
            extra["x-goog-if-generation-match"] = str(if_generation_match)
        if max_bytes is not None:
            if max_bytes < 0:
                raise ValueError("max_bytes must be >= 0")
            extra["x-goog-content-length-range"] = f"0,{max_bytes}"
        now = self._clock()

        def run() -> str:
            if self._blob_signer is not None:
                signed_headers = dict(extra)
                if content_type is not None:
                    signed_headers["content-type"] = content_type
                return rsa_signed_url(
                    self._blob_signer,
                    method="PUT",
                    bucket=bucket,
                    name=name,
                    expires_in=expires_in,
                    endpoint=self._api_endpoint,
                    now=now,
                    headers=signed_headers,
                )
            url: str = self._blob(bucket, name).generate_signed_url(
                expiration=expires_in,
                method="PUT",
                content_type=content_type,
                headers=extra or None,
                **self._sign_kwargs(),
            )
            return url

        url = await self._call(run, bucket=bucket, name=name)
        headers = dict(extra)
        if content_type is not None:
            headers["content-type"] = content_type
        return SignedPut(
            url=url,
            name=name,
            expires_at=now + expires_in,
            headers=headers,
            max_bytes=max_bytes,
        )

    async def post_policy(
        self,
        bucket: str,
        prefix: str,
        *,
        expires_in: timedelta,
        max_bytes: int,
        min_bytes: int = 0,
        create_only: bool = False,
    ) -> PostPolicy:
        check_ttl(expires_in)
        check_object_name(prefix)
        if not prefix.endswith("/"):
            raise ValueError("a POST policy prefix must end with '/'")
        uploader = self._uploader_signer or self._uploader_credentials
        if create_only and uploader is None:
            raise NotImplementedError(
                "a create_only POST policy on GCS needs uploader_credentials: a signer without "
                "storage.objects.delete on the bucket (GCS POST forms take no generation "
                "precondition)"
            )
        creds = (
            uploader
            if create_only
            else self._blob_signer
            or self._signing_credentials
            or getattr(self._client, "_credentials", None)
        )
        sign_bytes = getattr(creds, "sign_bytes", None)
        signer_email = getattr(creds, "signer_email", None) or (
            None if create_only else self._sa_email
        )
        if sign_bytes is None or not signer_email:
            raise NotImplementedError(
                "GcsObjectStore.post_policy needs signing credentials with sign_bytes() and a "
                "signer email (a service-account key or google.auth impersonated credentials); "
                "IAM signBlob signing of POST policies is not implemented"
            )

        def sign(policy_b64: str, _date8: str) -> str:
            raw: bytes = sign_bytes(policy_b64.encode())
            return raw.hex()

        fields, expires_at = await self._call(
            lambda: build_post_policy(
                access_id=signer_email,
                bucket=bucket,
                prefix=prefix,
                min_bytes=min_bytes,
                max_bytes=max_bytes,
                expires_in=expires_in,
                sign=sign,
                algorithm=RSA_ALGORITHM,
                now=self._clock(),
            ),
            bucket=bucket,
            name=prefix,
        )
        return PostPolicy(
            url=f"{self._api_endpoint}/{bucket}/",
            prefix=prefix,
            fields=fields,
            content_length_range=(min_bytes, max_bytes),
            expires_at=expires_at,
            create_only=create_only,
        )
