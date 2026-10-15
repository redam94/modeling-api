"""The ManifestSigner protocol: LocalSigner and KmsSigner (against a KMS double backed by
local EC P-256 and RSA keys), integrity checks, rotation, and verification at URL minting."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
from mapi_server.app import load_signer, prepare_signer
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.bundles import BundleIntegrityError, verified_manifest
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.kms import KmsIntegrityError, KmsSigner
from mapi_server.services.runs import apply_transition, load_run
from mapi_server.services.signing import (
    RSA_PSS_ALG,
    SIGNATURE_ALG,
    LocalSigner,
    ManifestSigner,
)
from mapi_server.settings import Settings, SignerKind
from mapi_server_testkit.builders import queued_fit, simulate_outputs
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_spec.executor import RunKind
from mapi_spec.run import Actor, BundleManifest, RunState, RunTrigger
from mapi_spec.storage import crc32c

KEY = "projects/mapi-test/locations/europe-west1/keyRings/mapi/cryptoKeys/bundles"
V1, V2 = f"{KEY}/cryptoKeyVersions/1", f"{KEY}/cryptoKeyVersions/2"
RSA_V = f"{KEY}-rsa/cryptoKeyVersions/1"


@dataclass
class FakeKms:
    """``KeyManagementServiceAsyncClient`` double: real signatures with local keys."""

    keys: dict[str, tuple[str, Any]] = field(default_factory=dict)
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    corrupt: str | None = None
    """``digest`` | ``name`` | ``signature_crc`` | ``signature`` | ``pem``"""

    def add(self, version: str, algorithm: str = "EC_SIGN_P256_SHA256") -> None:
        if algorithm.startswith("EC_"):
            key: Any = ec.generate_private_key(ec.SECP256R1())
        else:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.keys[version] = (algorithm, key)

    async def get_public_key(self, *, request: dict[str, Any], timeout: float) -> Any:
        self.requests.append(("get_public_key", request))
        algorithm, key = self.keys[request["name"]]
        pem = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )
        crc = crc32c(pem.encode()) ^ (1 if self.corrupt == "pem" else 0)
        return SimpleNamespace(
            pem=pem, algorithm=SimpleNamespace(name=algorithm), pem_crc32c=crc, name=request["name"]
        )

    async def asymmetric_sign(self, *, request: dict[str, Any], timeout: float) -> Any:
        self.requests.append(("asymmetric_sign", request))
        algorithm, key = self.keys[request["name"]]
        digest = request["digest"]["sha256"]
        verified = crc32c(digest) == request["digest_crc32c"] and self.corrupt != "digest"
        if algorithm.startswith("EC_"):
            sig = key.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
        else:
            sig = key.sign(
                digest,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                Prehashed(hashes.SHA256()),
            )
        if self.corrupt == "signature":
            sig = bytes(reversed(sig))
        crc = crc32c(sig) ^ (1 if self.corrupt == "signature_crc" else 0)
        name = V2 if self.corrupt == "name" else request["name"]
        return SimpleNamespace(
            signature=sig, signature_crc32c=crc, verified_digest_crc32c=verified, name=name
        )


def _manifest(**kw: Any) -> BundleManifest:
    base: dict[str, Any] = {
        "tenant_id": "default",
        "project_id": "default",
        "model_id": "mdl_1",
        "model_version": 1,
        "run_id": "run_1",
        "attempt": 1,
        "kind": RunKind.FIT,
        "status": "complete",
        "spec_hash": "sha256:" + "ab" * 32,
        "catalog_digest": "sha256:" + "cd" * 32,
        "source_manifest_crc32c": "AAAAAA==",
        "signed_at": datetime(2026, 10, 15, tzinfo=UTC),
    }
    return BundleManifest(**{**base, **kw})


@pytest.fixture
def kms() -> FakeKms:
    fake = FakeKms()
    fake.add(V1)
    fake.add(V2)
    fake.add(RSA_V, "RSA_SIGN_PSS_2048_SHA256")
    return fake


@pytest.mark.parametrize(("version", "alg"), [(V1, SIGNATURE_ALG), (RSA_V, RSA_PSS_ALG)])
async def test_kms_sign_and_verify(kms: FakeKms, version: str, alg: str) -> None:
    signer = KmsSigner(kms, version)
    assert isinstance(signer, ManifestSigner) and signer.key_id == version
    signed = await signer.sign_manifest(_manifest())
    assert signed.signature is not None and signed.signature.alg == alg
    assert signed.signature.key_id == version
    assert signer.verify(signed)
    # only a digest went to KMS, with its CRC32C
    kind, request = kms.requests[-1]
    assert kind == "asymmetric_sign" and set(request) == {"name", "digest", "digest_crc32c"}
    assert len(request["digest"]["sha256"]) == 32
    assert not signer.verify(signed.model_copy(update={"tenant_id": "evil"}))
    bad_alg = signed.signature.model_copy(update={"alg": "HS256"})
    assert not signer.verify(signed.model_copy(update={"signature": bad_alg}))
    garbage = signed.signature.model_copy(update={"value": "%%%"})
    assert not signer.verify(signed.model_copy(update={"signature": garbage}))
    assert not signer.verify(_manifest())  # unsigned
    resigned = await signer.sign_manifest(signed)  # an old signature is replaced
    assert signer.verify(resigned)


async def test_kms_and_local_signatures_are_interchangeable_in_shape(kms: FakeKms) -> None:
    local = LocalSigner.generate()
    a = await local.sign_manifest(_manifest())
    b = await KmsSigner(kms, V1).sign_manifest(_manifest())
    assert a.signature is not None and b.signature is not None
    assert a.signature.alg == b.signature.alg == SIGNATURE_ALG
    assert local.verify(a) and not local.verify(b)


async def test_rotation_and_migration(kms: FakeKms) -> None:
    old = await KmsSigner(kms, V1).sign_manifest(_manifest())
    local = LocalSigner.generate()
    legacy = local.sign(_manifest())
    rotated = KmsSigner(kms, V2, trusted_versions=[V1], also_trust=[local])
    await rotated.prepare()
    assert rotated.trusted_versions == (V2, V1)
    assert rotated.verify(old) and rotated.verify(legacy)
    new = await rotated.sign_manifest(_manifest())
    assert new.signature is not None and new.signature.key_id == V2
    strict = KmsSigner(kms, V2)
    await strict.prepare()
    assert not strict.verify(old) and not strict.verify(legacy) and strict.verify(new)


async def test_verify_needs_prepared_keys(kms: FakeKms, caplog: pytest.LogCaptureFixture) -> None:
    signed = await KmsSigner(kms, V1).sign_manifest(_manifest())
    cold = KmsSigner(kms, V1)
    with caplog.at_level(logging.ERROR):
        assert not cold.verify(signed)
    assert "prepare()" in caplog.text
    await prepare_signer(cold)
    assert cold.verify(signed)
    fetches = sum(1 for k, _ in kms.requests if k == "get_public_key")
    await cold.prepare()
    assert sum(1 for k, _ in kms.requests if k == "get_public_key") == fetches  # cached


@pytest.mark.parametrize(
    ("corrupt", "fragment"),
    [
        ("digest", "digest CRC32C"),
        ("name", "another key version"),
        ("signature_crc", "signature_crc32c"),
        ("signature", "does not verify"),
        ("pem", "pem_crc32c"),
    ],
)
async def test_integrity_checks(kms: FakeKms, corrupt: str, fragment: str) -> None:
    kms.corrupt = corrupt
    with pytest.raises(KmsIntegrityError, match=fragment):
        await KmsSigner(kms, V1).sign_manifest(_manifest())


async def test_unsupported_keys_and_names(kms: FakeKms) -> None:
    with pytest.raises(ValueError, match="cryptoKeyVersions"):
        KmsSigner(kms, "projects/p/keys/k")
    kms.add(f"{KEY}-pkcs/cryptoKeyVersions/1", "RSA_SIGN_PKCS1_2048_SHA256")
    with pytest.raises(ValueError, match="cannot sign"):
        await KmsSigner(kms, f"{KEY}-pkcs/cryptoKeyVersions/1").prepare()
    kms.keys[f"{KEY}-mix/cryptoKeyVersions/1"] = ("EC_SIGN_P256_SHA256", kms.keys[RSA_V][1])
    with pytest.raises(ValueError, match="P-256"):
        await KmsSigner(kms, f"{KEY}-mix/cryptoKeyVersions/1").prepare()


async def test_requests_are_valid_kms_messages(kms: FakeKms) -> None:
    kms_types = pytest.importorskip("google.cloud.kms")
    await KmsSigner(kms, V1).sign_manifest(_manifest())
    for kind, request in kms.requests:
        cls = kms_types.AsymmetricSignRequest if kind == "asymmetric_sign" else None
        cls = cls or kms_types.GetPublicKeyRequest
        cls(request)  # unknown or mistyped fields would raise


def test_load_signer_selects_kms(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    common: dict[str, Any] = {"database_url": "postgresql://x/y", "data_dir": tmp_path}
    assert isinstance(load_signer(Settings(**common)), LocalSigner)
    with pytest.raises(ValueError, match="MAPI_KMS_KEY_VERSION"):
        Settings(**common, signer=SignerKind.KMS)
    created: dict[str, Any] = {}

    def fake_create(version: str, **kw: Any) -> KmsSigner:
        created.update(version=version, **kw)
        return KmsSigner(FakeKms(), version, **kw)

    monkeypatch.setattr(KmsSigner, "create", staticmethod(fake_create))
    signer = load_signer(
        Settings(**common, signer=SignerKind.KMS, kms_key_version=V2, kms_trusted_key_versions=V1)
    )
    assert isinstance(signer, KmsSigner)
    assert created == {"version": V2, "trusted_versions": [V1]}


# ----------------------------------------------------------- end to end: finalizer


async def _fit_with(ctx: ServerContext) -> m.ModelVersion:
    run_id = await queued_fit(ctx)
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        att = await s.get(m.RunAttempt, (run_id, 1))
        assert att is not None
        s.expunge_all()
    await simulate_outputs(ctx, run, att)
    assert await Finalizer(ctx).finalize(run_id, 1) is RunState.SUCCEEDED
    async with ctx.db.session() as s:
        done = await load_run(s, None, run_id)
        mv = await s.get(m.ModelVersion, done.output_model_version_id)
        assert mv is not None
        s.expunge_all()
    return mv


async def test_finalizer_signs_with_kms_and_urls_are_minted_after_verification(
    ctx: ServerContext, client: httpx.AsyncClient, kms: FakeKms
) -> None:
    signer = KmsSigner(kms, V1)
    await signer.prepare()
    ctx.signer = signer
    mv = await _fit_with(ctx)
    manifest = verified_manifest(signer, mv)
    assert manifest.signature is not None and manifest.signature.key_id == V1
    url = f"/v1/models/{mv.model_id}/versions/1/artifacts/bundle.zarr.zip"
    r = await client.get(url)
    assert r.status_code == 200, r.text
    # after a rotation that drops V1 from the trusted set, nothing is minted any more
    ctx.signer = KmsSigner(kms, V2)
    await prepare_signer(ctx.signer)
    r = await client.get(url)
    assert r.status_code == 500 and r.json()["code"] == "INTEGRITY_FAILURE"
    with pytest.raises(BundleIntegrityError):
        verified_manifest(ctx.signer, mv)
    # trusting the old version restores access
    ctx.signer = KmsSigner(kms, V2, trusted_versions=[V1])
    await prepare_signer(ctx.signer)
    assert (await client.get(url)).status_code == 200
