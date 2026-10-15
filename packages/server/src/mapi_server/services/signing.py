"""Bundle-manifest signing (plan §8.6): the :class:`ManifestSigner` protocol and the local
ECDSA P-256 signer that emulates Cloud KMS.

Every signer covers :func:`mapi_spec.run.bundle_manifest_signing_bytes` (domain-separated
canonical JSON of the manifest without ``signature``) and names its algorithm with the
contract's ``ManifestSignature.alg`` (``KMS-EC-P256-SHA256``, ``KMS-RSA-PSS-SHA256``), so the
local key and a real KMS key (:class:`mapi_server.services.kms.KmsSigner`) produce
interchangeable signatures: swapping one for the other changes neither the bytes nor the
verification code. URLs for a bundle are minted only after :meth:`ManifestSigner.verify`
succeeds (:mod:`mapi_server.services.bundles`).

:class:`LocalSigner` (alias :data:`BundleSigner`) keeps its private key in a ``0600`` PEM file
under the data directory.
"""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from mapi_spec.run import BundleManifest, ManifestSignature, bundle_manifest_signing_bytes

__all__ = [
    "RSA_PSS_ALG",
    "SIGNATURE_ALG",
    "BundleSigner",
    "LocalSigner",
    "ManifestSigner",
    "decode_signature",
    "encode_signature",
    "verify_signature_bytes",
]

SIGNATURE_ALG = "KMS-EC-P256-SHA256"
RSA_PSS_ALG = "KMS-RSA-PSS-SHA256"

PublicKey = ec.EllipticCurvePublicKey | rsa.RSAPublicKey


@runtime_checkable
class ManifestSigner(Protocol):
    """Signs bundle manifests and verifies them before any URL is minted."""

    @property
    def key_id(self) -> str:
        """The key (version) new signatures are made with."""
        ...

    async def sign_manifest(self, manifest: BundleManifest) -> BundleManifest:
        """``manifest`` with a fresh ``signature`` (any previous one is replaced)."""
        ...

    def verify(self, manifest: BundleManifest) -> bool:
        """``True`` only for a valid signature by a trusted key with the expected algorithm.

        Synchronous and local: remote signers verify against public keys they loaded before
        (the URL-minting path must not depend on a remote call)."""
        ...


def encode_signature(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_signature(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_signature_bytes(public: PublicKey, alg: str, signature: bytes, data: bytes) -> bool:
    """Check ``signature`` over ``data`` for a contract algorithm (never raises)."""
    try:
        if alg == SIGNATURE_ALG and isinstance(public, ec.EllipticCurvePublicKey):
            if not isinstance(public.curve, ec.SECP256R1):
                return False
            public.verify(signature, data, ec.ECDSA(hashes.SHA256()))
            return True
        if alg == RSA_PSS_ALG and isinstance(public, rsa.RSAPublicKey):
            public.verify(
                signature,
                data,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                hashes.SHA256(),
            )
            return True
    except (InvalidSignature, ValueError):
        return False
    return False


def _key_id(public: ec.EllipticCurvePublicKey) -> str:
    der = public.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "local-p256-" + hashlib.sha256(der).hexdigest()[:16]


class LocalSigner:
    """Signs and verifies bundle manifests with one active P-256 key (plus trusted old keys)."""

    def __init__(
        self,
        private_key: ec.EllipticCurvePrivateKey,
        *,
        trusted: dict[str, ec.EllipticCurvePublicKey] | None = None,
    ) -> None:
        if not isinstance(private_key.curve, ec.SECP256R1):
            raise ValueError("the bundle signing key must be an EC P-256 key")
        self._private = private_key
        self.key_id = _key_id(private_key.public_key())
        self._trusted = dict(trusted or {})
        self._trusted[self.key_id] = private_key.public_key()

    @classmethod
    def generate(cls) -> LocalSigner:
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_file(cls, path: Path, *, create: bool = True) -> LocalSigner:
        """Load the PEM key at ``path``; create it (mode 0600) if missing and ``create``."""
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey):
                raise ValueError(f"{path} does not hold an EC private key")
            return cls(key)
        if not create:
            raise FileNotFoundError(path)
        signer = cls.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        pem = signer._private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        # Publish atomically: write a private (0600) temp file, then hard-link it into place.
        # link() fails if another process (api and worker starting together) created the
        # key first; that key wins, so every process ends up trusting the same one.
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(pem)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                return cls.from_file(path, create=False)
            except OSError:  # no hard links on this filesystem: exclusive create instead
                try:
                    out = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    return cls.from_file(path, create=False)
                with os.fdopen(out, "wb") as fh:
                    fh.write(pem)
        finally:
            os.unlink(tmp)
        return signer

    def public_pem(self) -> str:
        return (
            self._private.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )

    def sign(self, manifest: BundleManifest) -> BundleManifest:
        unsigned = manifest.model_copy(update={"signature": None})
        der = self._private.sign(bundle_manifest_signing_bytes(unsigned), ec.ECDSA(hashes.SHA256()))
        sig = ManifestSignature(alg=SIGNATURE_ALG, key_id=self.key_id, value=encode_signature(der))
        return manifest.model_copy(update={"signature": sig})

    async def sign_manifest(self, manifest: BundleManifest) -> BundleManifest:
        return self.sign(manifest)

    def verify(self, manifest: BundleManifest) -> bool:
        """``True`` only for a valid signature by a trusted key with the expected algorithm."""
        sig = manifest.signature
        if sig is None or sig.alg != SIGNATURE_ALG:
            return False
        public = self._trusted.get(sig.key_id)
        if public is None:
            return False
        try:
            raw = decode_signature(sig.value)
        except ValueError:
            return False
        return verify_signature_bytes(public, sig.alg, raw, bundle_manifest_signing_bytes(manifest))


BundleSigner = LocalSigner
"""The M1 name of :class:`LocalSigner`."""
