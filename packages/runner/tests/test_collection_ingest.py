"""Collection ingest (v0.15) end to end in a real WorkerContext: a client manifest and two
image parts in the GCS shim (range-read, never downloaded whole) become ``manifest.parquet``,
``members.bin``, ``collection.json``, ``schema.json``, the manifest's summary/profile/preview
and the Data Explorer's ``gallery`` of a decoded sample. A second version reuses the first
one's images through its base inputs (its new part holds 2 images). Refusals: an entry whose
bytes do not match its sha256 name, an image in no part, too many packs, a reserved column.
Also the ``SignedPackSource`` digest check and ``Transfers.read_range``."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from collection_fixtures import Worker, client_manifest, images_and_labels, parts_of, put_inputs
from mapi_runner.jobs import IngestJob
from mapi_runner.transfer import Transfers, new_client
from mapi_runner.worker import collection_ingest
from mapi_runner.worker.blobs import SignedPackSource
from mapi_runner.worker.ingest import run_ingest
from mapi_sdk.collection import read_manifest
from mapi_spec.collection import (
    COLLECTION_INFO,
    COLLECTION_MANIFEST,
    COLLECTION_MEMBERS,
    MANIFEST_FIXED,
    CollectionInfo,
    CollectionIngest,
    CollectionOptions,
    pack_digest,
    pack_input,
    part_input,
)
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema
from mapi_spec.views import GalleryView
from shimgcs import ShimGcs

pytestmark = pytest.mark.unit

OPTS = CollectionOptions(label_column="label", split_column="split")


def _job(
    parts: int, *, base_version: int | None = None, base_packs: int = 0, **opts: Any
) -> IngestJob:
    options = OPTS.model_copy(update=opts) if opts else OPTS
    coll = CollectionIngest(
        options=options, parts=parts, base_version=base_version, base_packs=base_packs
    )
    return IngestJob(collection=coll, dataset="dataset://default/pets@2")  # type: ignore[call-arg]


def _ingest(
    gcs: ShimGcs, tmp: Path, name: str, objects: dict[str, bytes], job: IngestJob, **extra: Any
) -> tuple[Worker, DataSchema]:
    inputs = put_inputs(gcs, name, objects)
    inputs.update(extra.get("signed", {}))
    worker = Worker(tmp / name, inputs)
    try:
        schema = run_ingest(worker.ctx, job)
    finally:
        worker.finish()
    return worker, schema


def _first_version(gcs: ShimGcs, tmp: Path) -> tuple[Worker, list[bytes], list[str], list[bytes]]:
    images, labels = images_and_labels()
    parts = parts_of(images, part_bytes=2500)
    assert len(parts) >= 2
    objects = {"manifest": client_manifest(images, labels)}
    objects.update({part_input(i): p for i, p in enumerate(parts)})
    worker, _ = _ingest(gcs, tmp, "v1", objects, _job(len(parts)))
    return worker, images, labels, parts


def test_ingest_writes_a_collection_version(gcs: ShimGcs, tmp_path: Path) -> None:
    worker, images, labels, parts = _first_version(gcs, tmp_path)
    info = CollectionInfo.model_validate_json(worker.output(COLLECTION_INFO))
    assert info.n_images == len(images) == 12
    assert info.label and info.split and info.mode == "gray" and info.size == (8, 8)
    assert {c.label: c.count for c in info.classes} == {"circle": 4, "cross": 4, "square": 4}
    assert info.splits == {"test": 3, "train": 9}
    assert info.new_images == 12 and info.reused_images == 0 and info.base_version is None
    assert [p.input for p in info.packs] == [part_input(i) for i in range(len(parts))]
    q = gcs.buckets.quarantine
    for p, part in zip(info.packs, parts, strict=True):
        meta = gcs.stat(q, f"v1/{p.input}")
        assert p.digest == pack_digest(meta.size, meta.crc32c, meta.md5_hash)
        assert p.size == len(part) and p.source == "upload"
    members = worker.output(COLLECTION_MEMBERS)
    digests = [members[i : i + 32] for i in range(0, len(members), 32)]
    assert digests == sorted(digests) and len(digests) == 12
    assert {d.hex() for d in digests} == {hashlib.sha256(x).hexdigest() for x in images}
    raw = worker.output(COLLECTION_MANIFEST)
    assert hashlib.sha256(raw).hexdigest() == info.manifest_sha256
    df = read_manifest(raw)
    assert list(df.columns[: len(MANIFEST_FIXED)]) == [c for c, _ in MANIFEST_FIXED]
    assert list(df["image_id"]) == [f"{lab}/{i:03d}.png" for i, lab in enumerate(labels)]
    assert list(df["label"]) == labels and set(df["format"]) == {"png"}
    assert set(df["width"]) == {8}
    schema = DataSchema.model_validate_json(worker.output("schema.json"))
    assert schema.vars["pixels"].dims == ("image", "y", "x", "channel")
    assert schema.vars["label"].role.value == "target"
    assert worker.json("preview.json")["total_rows"] == 12
    assert worker.json("profile.json")["collection"] is True
    assert "image" in worker.json("summary.json")["coords"]
    index = worker.json("ui/explore/index.json")
    status = {s["section"]: s.get("status", "ok") for s in index["sections"]}
    assert status["gallery"] == "ok"
    gallery = GalleryView.model_validate(worker.json("ui/explore/gallery.json"))
    assert gallery.n_images == 12
    assert not (worker.ctx.outputs_dir / "data.zarr.zip").exists()
    # parts are range-read: no part was downloaded into inputs/
    assert sorted(p.name for p in worker.ctx.inputs_dir.iterdir()) == ["manifest"]


def test_rows_with_the_same_bytes_share_one_stored_image(gcs: ShimGcs, tmp_path: Path) -> None:
    images, labels = images_and_labels()
    images, labels = [*images, images[0]], [*labels, labels[0]]
    parts = parts_of(images, part_bytes=2500)
    objects = {"manifest": client_manifest(images, labels)}
    objects.update({part_input(i): p for i, p in enumerate(parts)})
    worker, _ = _ingest(gcs, tmp_path, "dup", objects, _job(len(parts)))
    info = CollectionInfo.model_validate_json(worker.output(COLLECTION_INFO))
    assert info.n_images == 13 and info.new_images == 12
    assert sum(p.images for p in info.packs) == 12
    assert len(worker.output(COLLECTION_MEMBERS)) == 12 * 32


def test_a_base_version_supplies_the_images_the_parts_lack(gcs: ShimGcs, tmp_path: Path) -> None:
    v1, images, labels, _ = _first_version(gcs, tmp_path)
    info1 = CollectionInfo.model_validate_json(v1.output(COLLECTION_INFO))
    new_images, new_labels = images_and_labels(n_per_class=1, seed=7)
    new_images, new_labels = new_images[:2], new_labels[:2]
    all_images, all_labels = images + new_images, labels + new_labels
    (new_part,) = parts_of(new_images, part_bytes=1 << 20)
    base = put_inputs(
        gcs,
        "base",
        {"base.manifest": v1.output(COLLECTION_MANIFEST), "base.info": v1.output(COLLECTION_INFO)},
    )
    q = gcs.buckets.quarantine
    for i, p in enumerate(info1.packs):
        base[pack_input(i, "base")] = gcs.sign_get(q, f"v1/{p.input}")
    objects = {"manifest": client_manifest(all_images, all_labels), part_input(0): new_part}
    job = _job(1, base_version=1, base_packs=len(info1.packs))
    v2, _ = _ingest(gcs, tmp_path, "v2", objects, job, signed=base)
    info = CollectionInfo.model_validate_json(v2.output(COLLECTION_INFO))
    assert info.n_images == 14 and info.new_images == 2 and info.reused_images == 12
    assert info.base_version == 1
    sources = [(p.source, p.input, p.images) for p in info.packs]
    assert sources[0] == ("upload", "part.0000", 2)
    assert {s for s, _, _ in sources[1:]} == {"base"} and all(i is None for _, i, _ in sources[1:])
    assert sum(n for _, _, n in sources) == 14
    df = read_manifest(v2.output(COLLECTION_MANIFEST)).set_index("sha256")
    old = read_manifest(v1.output(COLLECTION_MANIFEST)).set_index("sha256")
    for sha in old.index:  # reused images keep their pack and offset
        assert (df.loc[sha, "pack"], df.loc[sha, "offset"]) == (
            old.loc[sha, "pack"],
            old.loc[sha, "offset"],
        )


def _bad_part(images: list[bytes]) -> bytes:
    """A STORE zip whose first entry's bytes do not match its sha256 name."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for k, data in enumerate(images):
            sha = hashlib.sha256(images[(k + 1) % len(images)]).hexdigest()
            zf.writestr(zipfile.ZipInfo(f"{sha}.png", date_time=(1980, 1, 1, 0, 0, 0)), data)
    return buf.getvalue()


def test_refuses_an_entry_that_does_not_match_its_name(gcs: ShimGcs, tmp_path: Path) -> None:
    images, labels = images_and_labels(n_per_class=1)
    objects = {"manifest": client_manifest(images, labels), part_input(0): _bad_part(images)}
    with pytest.raises(MapiError) as err:
        _ingest(gcs, tmp_path, "bad", objects, _job(1))
    assert err.value.code is ErrorCode.INTEGRITY_FAILURE
    assert "does not match its content" in err.value.detail


def test_refuses_an_image_in_no_part(gcs: ShimGcs, tmp_path: Path) -> None:
    images, labels = images_and_labels(n_per_class=2)
    (part,) = parts_of(images[:-1], part_bytes=1 << 20)
    objects = {"manifest": client_manifest(images, labels), part_input(0): part}
    with pytest.raises(MapiError) as err:
        _ingest(gcs, tmp_path, "miss", objects, _job(1))
    issue = err.value.issues[0]
    assert "1 images of the manifest" in issue.message
    assert issue.untrusted_text == hashlib.sha256(images[-1]).hexdigest()
    assert "upload-images" in (issue.suggested_action or "")


def test_refuses_too_many_packs(
    gcs: ShimGcs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(collection_ingest, "MAX_VERSION_PACKS", 1)
    images, labels = images_and_labels()
    parts = parts_of(images, part_bytes=2500)
    objects = {"manifest": client_manifest(images, labels)}
    objects.update({part_input(i): p for i, p in enumerate(parts)})
    with pytest.raises(MapiError) as err:
        _ingest(gcs, tmp_path, "many", objects, _job(len(parts)))
    assert err.value.code is ErrorCode.PAYLOAD_TOO_LARGE
    assert "--no-base" in (err.value.issues[0].suggested_action or "")


def test_refuses_a_reserved_column(gcs: ShimGcs, tmp_path: Path) -> None:
    images, labels = images_and_labels(n_per_class=1)
    (part,) = parts_of(images, part_bytes=1 << 20)
    manifest = client_manifest(images, labels, extra={"offset": [1, 2, 3]})
    with pytest.raises(MapiError) as err:
        _ingest(gcs, tmp_path, "res", {"manifest": manifest, part_input(0): part}, _job(1))
    assert "reserved" in err.value.issues[0].message


def test_unused_part_is_a_note_and_ids_default_to_paths(gcs: ShimGcs, tmp_path: Path) -> None:
    images, labels = images_and_labels(n_per_class=1)
    extra, _ = images_and_labels(n_per_class=1, seed=3)
    (part,) = parts_of(images, part_bytes=1 << 20)
    (unused,) = parts_of(extra, part_bytes=1 << 20)
    objects = {
        "manifest": client_manifest(images, labels),
        part_input(0): part,
        part_input(1): unused,
    }
    worker, _ = _ingest(gcs, tmp_path, "note", objects, _job(2, label_column=None))
    info = CollectionInfo.model_validate_json(worker.output(COLLECTION_INFO))
    assert [p.input for p in info.packs] == ["part.0000"]
    assert not info.label and info.columns == ["label"]  # kept as a user column
    issues = worker.json("profile.json")["issues"]
    assert any("holds no image" in i["message"] for i in issues)
    df = pd.read_parquet(io.BytesIO(worker.output(COLLECTION_MANIFEST)))
    assert df["image_id"].iloc[0] == "circle/000.png"


def test_signed_pack_source_checks_the_digest(gcs: ShimGcs, tmp_path: Path) -> None:
    images, _ = images_and_labels(n_per_class=1)
    (part,) = parts_of(images, part_bytes=1 << 20)
    gets = put_inputs(gcs, "src", {"p": part})
    get = gets["p"]
    assert get.md5_hash is not None
    digest = pack_digest(get.size, get.crc32c, get.md5_hash)
    worker = Worker(tmp_path / "w", {})
    try:
        source = SignedPackSource(worker.ctx, {digest: get})
        assert source.read(digest, 10, 20) == part[10:30]
        assert source.read(digest, 0, 0) == b""
        with pytest.raises(MapiError) as err:
            SignedPackSource(worker.ctx, {"0" * 64: get})
        assert err.value.code is ErrorCode.INTEGRITY_FAILURE
        with pytest.raises(MapiError):
            SignedPackSource(worker.ctx, {digest: get.model_copy(update={"md5_hash": None})})
        with pytest.raises(MapiError):
            source.read("f" * 64, 0, 1)
        with pytest.raises(MapiError):
            source.read(digest, len(part) - 1, 2)
    finally:
        worker.finish()


def test_read_range_reads_exact_ranges(gcs: ShimGcs) -> None:
    data = bytes(range(256)) * 4
    get = put_inputs(gcs, "rr", {"x": data})["x"]
    transfers = Transfers(new_client(), sleep=lambda _s: None)
    assert transfers.read_range(get, 0, 1) == data[:1]
    assert transfers.read_range(get, 100, 1024) == data[100:]
    with pytest.raises(ValueError):
        transfers.read_range(get, 5, 5)
    with pytest.raises(ValueError):
        transfers.read_range(get, 0, 1025)
