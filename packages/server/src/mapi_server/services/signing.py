"""Bundle-manifest signing: a local ECDSA P-256 key emulating Cloud KMS (plan §8.6).

The signature covers :func:`mapi_spec.run.bundle_manifest_signing_bytes` (domain-separated
canonical JSON of the manifest without ``signature``) and uses the contract algorithm
``KMS-EC-P256-SHA256``, so swapping in a real KMS key in M2 changes neither the bytes nor the
verification code. The private key lives in a ``0600`` PEM file under the data directory.
URLs for a bundle are minted only after :meth:`BundleSigner.verify` succeeds.
"""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from mapi_spec.run import BundleManifest, ManifestSignature, bundle_manifest_signing_bytes

__all__ = ["SIGNATURE_ALG", "BundleSigner"]

SIGNATURE_ALG = "KMS-EC-P256-SHA256"


def _key_id(public: ec.EllipticCurvePublicKey) -> str:
    der = public.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "local-p256-" + hashlib.sha256(der).hexdigest()[:16]


class BundleSigner:
    """Signs and verifies bundle manifests with one active key (plus trusted old keys)."""

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
    def generate(cls) -> BundleSigner:
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_file(cls, path: Path, *, create: bool = True) -> BundleSigner:
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
        sig = ManifestSignature(
            alg=SIGNATURE_ALG,
            key_id=self.key_id,
            value=base64.urlsafe_b64encode(der).decode().rstrip("="),
        )
        return manifest.model_copy(update={"signature": sig})

    def verify(self, manifest: BundleManifest) -> bool:
        """``True`` only for a valid signature by a trusted key with the expected algorithm."""
        sig = manifest.signature
        if sig is None or sig.alg != SIGNATURE_ALG:
            return False
        public = self._trusted.get(sig.key_id)
        if public is None:
            return False
        try:
            der = base64.urlsafe_b64decode(sig.value + "=" * (-len(sig.value) % 4))
            public.verify(der, bundle_manifest_signing_bytes(manifest), ec.ECDSA(hashes.SHA256()))
        except (InvalidSignature, ValueError):
            return False
        return True
