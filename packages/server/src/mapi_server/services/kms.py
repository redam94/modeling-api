"""``KmsSigner``: bundle manifests signed by a Cloud KMS asymmetric key (plan §8.6, §9).

The private key never leaves KMS. Signing sends only the SHA-256 **digest** of the manifest's
signing bytes to ``asymmetricSign`` with a CRC32C of the digest, and checks the response the
way Google recommends for data integrity in transit:

* ``verified_digest_crc32c`` — KMS received the digest we sent;
* ``name`` — the key version that signed is the one we asked for;
* ``signature_crc32c`` — the signature was not corrupted on the way back;

and finally verifies the signature locally against the version's public key, so a signature
that does not verify is never stored.

Verification (:meth:`KmsSigner.verify`) is local and synchronous: the public keys of the
active version and of every *trusted* older version (rotation) are fetched once with
``getPublicKey`` (:meth:`KmsSigner.prepare`, called at startup), checked against their
``pem_crc32c`` and cached. Signatures by other signers (e.g. the local key, during a
migration) verify only if that signer is passed in ``also_trust``.

Supported algorithms: ``EC_SIGN_P256_SHA256`` (contract alg ``KMS-EC-P256-SHA256``) and
``RSA_SIGN_PSS_{2048,3072,4096}_SHA256`` (``KMS-RSA-PSS-SHA256``).

The client is a ``google.cloud.kms.KeyManagementServiceAsyncClient`` (``gcp`` extra,
import-guarded). It is exercised here only against a test double backed by local keys.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from mapi_spec.run import BundleManifest, ManifestSignature, bundle_manifest_signing_bytes
from mapi_spec.storage import crc32c

from mapi_server.services.signing import (
    RSA_PSS_ALG,
    SIGNATURE_ALG,
    ManifestSigner,
    PublicKey,
    decode_signature,
    encode_signature,
    verify_signature_bytes,
)

__all__ = ["KMS_ALGORITHMS", "KmsIntegrityError", "KmsPublicKey", "KmsSigner"]

log = logging.getLogger(__name__)

KMS_ALGORITHMS: Final[Mapping[str, str]] = {
    "EC_SIGN_P256_SHA256": SIGNATURE_ALG,
    "RSA_SIGN_PSS_2048_SHA256": RSA_PSS_ALG,
    "RSA_SIGN_PSS_3072_SHA256": RSA_PSS_ALG,
    "RSA_SIGN_PSS_4096_SHA256": RSA_PSS_ALG,
}
"""KMS ``CryptoKeyVersionAlgorithm`` → the contract's ``ManifestSignature.alg``."""

_VERSION = re.compile(
    r"^projects/[^/]+/locations/[^/]+/keyRings/[^/]+/cryptoKeys/[^/]+/cryptoKeyVersions/\d+$"
)


class KmsIntegrityError(RuntimeError):
    """A KMS request or response failed its integrity checks (retryable by the caller)."""


@dataclass(frozen=True)
class KmsPublicKey:
    name: str
    alg: str
    public_key: PublicKey


def _enum_name(value: Any) -> str:
    name = getattr(value, "name", None)
    return str(name) if name is not None else str(value)


def _int(value: Any) -> int | None:
    """A proto ``Int64Value`` read through proto-plus (an int) or a wrapper (``.value``)."""
    if value is None:
        return None
    inner = getattr(value, "value", value)
    return int(inner) if isinstance(inner, int) else None


def _import_kms() -> Any:
    try:
        from google.cloud import kms  # type: ignore[import-not-found,attr-defined,unused-ignore]
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "MAPI_SIGNER=kms needs google-cloud-kms: pip install 'mapi-server[gcp]'"
        ) from exc
    return kms


class KmsSigner:
    """:class:`~mapi_server.services.signing.ManifestSigner` backed by Cloud KMS."""

    def __init__(
        self,
        client: Any,
        key_version: str,
        *,
        trusted_versions: Sequence[str] = (),
        also_trust: Sequence[ManifestSigner] = (),
        call_timeout_s: float = 10.0,
    ) -> None:
        for v in (key_version, *trusted_versions):
            if not _VERSION.match(v):
                raise ValueError(f"{v!r} is not a KMS cryptoKeyVersions resource name")
        self._client = client
        self._version = key_version
        self._trusted = tuple(dict.fromkeys((key_version, *trusted_versions)))
        self._also = tuple(also_trust)
        self._timeout = call_timeout_s
        self._keys: dict[str, KmsPublicKey] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def create(
        cls,
        key_version: str,
        *,
        trusted_versions: Sequence[str] = (),
        also_trust: Sequence[ManifestSigner] = (),
        credentials: Any | None = None,
    ) -> KmsSigner:
        """A signer with a real ``KeyManagementServiceAsyncClient`` (``gcp`` extra)."""
        kms = _import_kms()
        client = kms.KeyManagementServiceAsyncClient(credentials=credentials)
        return cls(client, key_version, trusted_versions=trusted_versions, also_trust=also_trust)

    @property
    def key_id(self) -> str:
        return self._version

    @property
    def trusted_versions(self) -> tuple[str, ...]:
        return self._trusted

    async def prepare(self) -> None:
        """Fetch and cache the public key of every trusted version (startup)."""
        for version in self._trusted:
            await self._public_key(version)

    async def _public_key(self, version: str) -> KmsPublicKey:
        cached = self._keys.get(version)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._keys.get(version)
            if cached is not None:
                return cached
            resp = await self._client.get_public_key(
                request={"name": version}, timeout=self._timeout
            )
            pem = str(resp.pem)
            if getattr(resp, "name", version) != version:
                raise KmsIntegrityError("getPublicKey answered for another key version")
            want = _int(getattr(resp, "pem_crc32c", None))
            if want is not None and crc32c(pem.encode()) != want:
                raise KmsIntegrityError("the public key was corrupted in transit (pem_crc32c)")
            algorithm = _enum_name(resp.algorithm)
            alg = KMS_ALGORITHMS.get(algorithm)
            if alg is None:
                raise ValueError(f"KMS algorithm {algorithm} cannot sign bundle manifests")
            public = serialization.load_pem_public_key(pem.encode())
            if alg == SIGNATURE_ALG and not (
                isinstance(public, ec.EllipticCurvePublicKey)
                and isinstance(public.curve, ec.SECP256R1)
            ):
                raise ValueError(f"{version} is not an EC P-256 key")
            if alg == RSA_PSS_ALG and not isinstance(public, rsa.RSAPublicKey):
                raise ValueError(f"{version} is not an RSA key")
            assert isinstance(public, ec.EllipticCurvePublicKey | rsa.RSAPublicKey)
            key = KmsPublicKey(name=version, alg=alg, public_key=public)
            self._keys[version] = key
            return key

    async def sign_manifest(self, manifest: BundleManifest) -> BundleManifest:
        key = await self._public_key(self._version)
        unsigned = manifest.model_copy(update={"signature": None})
        data = bundle_manifest_signing_bytes(unsigned)
        digest = hashlib.sha256(data).digest()
        resp = await self._client.asymmetric_sign(
            request={
                "name": self._version,
                "digest": {"sha256": digest},
                "digest_crc32c": crc32c(digest),
            },
            timeout=self._timeout,
        )
        if not getattr(resp, "verified_digest_crc32c", False):
            raise KmsIntegrityError("KMS did not verify the digest CRC32C (corrupted request)")
        if getattr(resp, "name", None) != self._version:
            raise KmsIntegrityError("KMS signed with another key version than requested")
        signature = bytes(resp.signature)
        if crc32c(signature) != _int(getattr(resp, "signature_crc32c", None)):
            raise KmsIntegrityError("the signature was corrupted in transit (signature_crc32c)")
        if not verify_signature_bytes(key.public_key, key.alg, signature, data):
            raise KmsIntegrityError("KMS returned a signature that does not verify")
        sig = ManifestSignature(
            alg=key.alg,
            key_id=self._version,
            value=encode_signature(signature),
        )
        return manifest.model_copy(update={"signature": sig})

    def verify(self, manifest: BundleManifest) -> bool:
        sig = manifest.signature
        if sig is None:
            return False
        key = self._keys.get(sig.key_id)
        if key is None:
            if sig.key_id in self._trusted:
                log.error(
                    "bundle signed by trusted KMS key %s whose public key is not loaded "
                    "(call prepare() at startup)",
                    sig.key_id,
                )
                return False
            return any(other.verify(manifest) for other in self._also)
        if sig.alg != key.alg:
            return False
        try:
            raw = decode_signature(sig.value)
        except ValueError:
            return False
        return verify_signature_bytes(
            key.public_key, key.alg, raw, bundle_manifest_signing_bytes(manifest)
        )
