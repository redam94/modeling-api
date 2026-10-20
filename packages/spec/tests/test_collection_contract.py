"""v0.15 image collection contracts: names and limits, the pack address, the client options,
the ingest job, collection.json, the HTTP DTOs and the safetensors ingest mapping."""

from __future__ import annotations

import hashlib
import re
from typing import Any

import pytest
from mapi_spec import api
from mapi_spec.api import (
    ROUTE_ACCESS,
    V015_ENDPOINTS,
    BlobMissing,
    BlobQuery,
    DatasetVersion,
    DatasetVersionCreate,
    IngestMapping,
    UploadSession,
)
from mapi_spec.collection import (
    COLLECTION_INFO,
    COLLECTION_MANIFEST,
    COLLECTION_MEMBERS,
    MANIFEST_FIXED,
    MANIFEST_RESERVED,
    MAX_BLOB_QUERY,
    MAX_COLLECTION_PARTS,
    MAX_PART_BYTES,
    MAX_VERSION_PACKS,
    PART_ENTRY_RE,
    CollectionInfo,
    CollectionIngest,
    CollectionOptions,
    CollectionRead,
    CollectionSource,
    info_input,
    pack_digest,
    pack_input,
    pack_object_name,
    part_input,
)
from mapi_spec.ingest import WeightsOptions
from mapi_spec.jobs import IngestJob
from mapi_spec.loader import canonical_json
from mapi_spec.run import IoName, RunSpec
from mapi_spec.tenancy import Action, ResourceKind
from mapi_spec.tree import DataLayout
from pydantic import TypeAdapter, ValidationError

pytestmark = pytest.mark.unit

CRC, MD5 = "4waSgw==", "XrY7u+Ae7tCTyyK7j1rNww=="
UPL = [f"upl_01jaf3d1e2f3g4h5j6k7m8n9p{i}" for i in range(4)]
INFO: dict[str, Any] = DatasetVersion.model_json_schema()["examples"][2]["collection"]


# =============================================================================== names


def test_input_names_are_io_names() -> None:
    io = TypeAdapter(IoName)
    names = [part_input(0), part_input(63), pack_input(0), pack_input(127, "base")]
    names += [info_input(), info_input("base"), "manifest", "base.manifest", "base.info"]
    for name in names:
        io.validate_python(name)
    assert (part_input(3), pack_input(2), pack_input(0, "base")) == (
        "part.0003",
        "dataset.pack.0002",
        "base.pack.0000",
    )
    assert info_input() == "dataset.info"
    with pytest.raises(ValueError):
        part_input(MAX_COLLECTION_PARTS)
    with pytest.raises(ValueError):
        pack_input(MAX_VERSION_PACKS)


def test_constants() -> None:
    assert (COLLECTION_MANIFEST, COLLECTION_INFO, COLLECTION_MEMBERS) == (
        "manifest.parquet",
        "collection.json",
        "members.bin",
    )
    assert [c for c, _ in MANIFEST_FIXED][:3] == ["image_id", "sha256", "pack"]
    assert {c for c, _ in MANIFEST_FIXED} == MANIFEST_RESERVED
    assert MAX_PART_BYTES < 2**31  # parts stay out of ZIP64
    assert re.match(PART_ENTRY_RE, "ab" * 32 + ".png")
    assert not re.match(PART_ENTRY_RE, "AB" * 32 + ".png")
    assert not re.match(PART_ENTRY_RE, "ab" * 32 + ".gif")


# ========================================================================= pack address


def test_pack_digest_is_the_dataset_sha_basis() -> None:
    expected = hashlib.sha256(
        canonical_json({"size": 1234, "crc32c": CRC, "md5": MD5}).encode()
    ).hexdigest()
    assert pack_digest(1234, CRC, MD5) == expected
    assert pack_digest(1235, CRC, MD5) != expected
    for bad in ((-1, CRC, MD5), (1, "nope", MD5), (1, CRC, "short=="), (True, CRC, MD5)):
        with pytest.raises(ValueError):
            pack_digest(*bad)  # type: ignore[arg-type]


def test_pack_object_name() -> None:
    digest = "5d" * 32
    assert pack_object_name("acme", digest) == f"t/acme/blobs/packs/{digest}.zip"
    with pytest.raises(ValueError):
        pack_object_name("acme", "5D" * 32)
    with pytest.raises(ValueError):
        pack_object_name("../x", digest)


# ======================================================================== client options


def test_collection_options() -> None:
    opts = CollectionOptions()
    assert (opts.sha256_column, opts.path_column, opts.var, opts.dim) == (
        "sha256",
        "path",
        "pixels",
        "image",
    )
    assert CollectionOptions(size=(32, 48)).size == (32, 48)
    for bad in (
        {"label_column": "sha256"},
        {"dim": "draw"},
        {"dim": "channel"},
        {"var": "image"},
        {"size": (0, 4)},
        {"columns": ["pack"]},
        {"max_images": 1_000_001},
    ):
        with pytest.raises(ValidationError):
            CollectionOptions.model_validate(bad)


def test_collection_source_uploads() -> None:
    src = CollectionSource(manifest_upload_id=UPL[0], part_upload_ids=UPL[1:3])
    assert src.upload_ids == UPL[:3] and src.base_version is None
    with pytest.raises(ValidationError, match="once"):
        CollectionSource(manifest_upload_id=UPL[0], part_upload_ids=[UPL[1], UPL[1]])
    with pytest.raises(ValidationError, match="manifest"):
        CollectionSource(manifest_upload_id=UPL[0], part_upload_ids=[UPL[0]])
    with pytest.raises(ValidationError):
        CollectionSource(manifest_upload_id=UPL[0], part_upload_ids=[UPL[1]] * 65)
    with pytest.raises(ValidationError):
        CollectionSource(manifest_upload_id=UPL[0], base_version=-1)


# =========================================================================== ingest job


def test_ingest_job_one_of_and_collection() -> None:
    job = IngestJob(
        collection=CollectionIngest(options=CollectionOptions(), parts=2, base_version=3)
    )
    assert job.collection is not None
    assert job.collection.input_names == ["manifest", "part.0000", "part.0001", "base.manifest",
                                          "base.info"]  # fmt: skip
    assert IngestJob.model_validate_json(job.model_dump_json()) == job
    mapping = IngestJob(mapping=IngestMapping(format="zarr_zip"))
    assert "collection" not in mapping.model_dump(mode="json")
    with pytest.raises(ValidationError, match="exactly one"):
        IngestJob(mapping=IngestMapping(format="zarr_zip"), collection=job.collection)
    with pytest.raises(ValidationError, match="exactly one"):
        IngestJob()
    with pytest.raises(ValidationError, match="base_version"):
        CollectionIngest(options=CollectionOptions(), base_packs=2)


# ======================================================================= collection.json


def test_collection_info_round_trip_and_rules() -> None:
    info = CollectionInfo.model_validate(INFO)
    assert info.format == "mapi.collection/v1" and info.size == (64, 64)
    assert CollectionInfo.model_validate_json(info.model_dump_json()) == info
    pack = INFO["packs"][0]
    for update in (
        {"packs": [pack, pack]},  # digests unique
        {"n_images": 1201, "new_images": 1201},  # packs hold fewer images
        {"new_images": 1201},  # more distinct images than images
        {"packs": [{**pack, "input": None}]},  # an upload pack names its input
        {"packs": [{**pack, "source": "base"}], "base_version": 2},  # base packs name none
        {"packs": [{**pack, "source": "base", "input": None}]},  # base packs need a base
        {"packs": []},
        {"size": [0, 64]},
        {"packs": [{**pack, "size": 10}]},
        {"classes": [{"label": "a", "count": 1}, {"label": "a", "count": 2}]},
    ):
        with pytest.raises(ValidationError):
            CollectionInfo.model_validate({**INFO, **update})
    base = CollectionInfo.model_validate(
        {**INFO, "base_version": 2, "packs": [{**pack, "source": "base", "input": None}]}
    )
    assert base.packs[0].source == "base"
    # two manifest rows with the same bytes share one stored image
    assert CollectionInfo.model_validate({**INFO, "n_images": 1201}).n_images == 1201


def test_collection_read_defaults() -> None:
    read = CollectionRead()
    assert (read.max_samples, read.size, read.stratify, read.seed) == (5000, None, "label", None)
    with pytest.raises(ValidationError):
        CollectionRead(max_samples=0)
    with pytest.raises(ValidationError):
        CollectionRead(seed=2**32)


# ========================================================================== HTTP contract


def test_dataset_version_create_collection_one_of() -> None:
    ex = DatasetVersionCreate.model_json_schema()["examples"][2]
    dto = DatasetVersionCreate.model_validate(ex)
    assert dto.collection is not None
    assert dto.upload_ids == [dto.collection.manifest_upload_id, *dto.collection.part_upload_ids]
    assert DatasetVersionCreate.model_validate_json(dto.model_dump_json()) == dto
    with pytest.raises(ValidationError, match="not several"):
        DatasetVersionCreate.model_validate({**ex, "upload_id": UPL[3]})
    with pytest.raises(ValidationError, match="not several"):
        DatasetVersionCreate.model_validate(
            {
                **ex,
                "assembly": {"sources": [{"upload_id": UPL[3], "mapping": {"format": "netcdf"}}]},
            }
        )
    one = DatasetVersionCreate(upload_id=UPL[0], mapping=IngestMapping(format="zarr_zip"))
    assert "collection" not in one.model_dump(mode="json")


def test_dataset_version_collection_layout() -> None:
    assert "collection" in DataLayout.__args__  # type: ignore[attr-defined]
    ex = DatasetVersion.model_json_schema()["examples"][2]
    dv = DatasetVersion.model_validate(ex)
    assert dv.layout == "collection" and dv.collection is not None
    assert dv.collection.packs[0].input == "part.0000"
    assert DatasetVersion.model_validate_json(dv.model_dump_json()) == dv
    plain = DatasetVersion.model_validate(DatasetVersion.model_json_schema()["examples"][0])
    assert "collection" not in plain.model_dump(mode="json")


def test_upload_session_received() -> None:
    ex = UploadSession.model_json_schema()["examples"][0]
    assert "received" not in UploadSession.model_validate(ex).model_dump(mode="json")
    assert UploadSession.model_validate({**ex, "received": True}).received is True


def test_blob_query_and_missing() -> None:
    q = BlobQuery(sha256=["ab" * 32, "cd" * 32])
    assert q.base_version is None
    with pytest.raises(ValidationError, match="unique"):
        BlobQuery(sha256=["ab" * 32, "ab" * 32])
    with pytest.raises(ValidationError):
        BlobQuery(sha256=[])
    with pytest.raises(ValidationError):
        BlobQuery(sha256=["AB" * 32])
    with pytest.raises(ValidationError):
        BlobQuery(sha256=[f"{i:064x}" for i in range(MAX_BLOB_QUERY + 1)])
    with pytest.raises(ValidationError):
        BlobQuery(sha256=["ab" * 32], base_version=-1)
    for ex in BlobMissing.model_json_schema()["examples"]:
        BlobMissing.model_validate(ex)
    assert BlobMissing(base_version=None, missing=[], present=0).present == 0


def test_v015_endpoints_and_access() -> None:
    assert api.ENDPOINTS[-len(V015_ENDPOINTS) :] == V015_ENDPOINTS
    by_op = {e.operation_id: e for e in V015_ENDPOINTS}
    assert set(by_op) == {"get_upload", "renew_upload", "find_missing_images"}
    for e in V015_ENDPOINTS:
        assert api.PROJECT_HEADER in e.headers, e
    assert by_op["find_missing_images"].request == "BlobQuery"
    assert by_op["find_missing_images"].response == "BlobMissing"
    assert by_op["renew_upload"].path.endswith("/renew")
    # the answer holds a signed PUT URL: a reader must not get one
    assert ROUTE_ACCESS["get_upload"] == ROUTE_ACCESS["create_upload"]
    assert ROUTE_ACCESS["find_missing_images"] == ROUTE_ACCESS["get_dataset"]
    assert ROUTE_ACCESS["renew_upload"] == ROUTE_ACCESS["create_upload"]
    assert ROUTE_ACCESS["get_upload"].resource is ResourceKind.DATASET
    assert ROUTE_ACCESS["renew_upload"].action is Action.UPDATE


def test_run_spec_input_layouts_take_collection() -> None:
    field = RunSpec.model_fields["input_layouts"]
    assert "collection" in (field.description or "")


# ============================================================================ safetensors


def test_safetensors_mapping() -> None:
    m = IngestMapping(format="safetensors")
    assert m.weights is None and "weights" not in m.model_dump(mode="json")
    w = IngestMapping.model_validate(
        {"format": "safetensors", "weights": {"arch": {"kind": "resnet"}}}
    )
    assert w.weights is not None and w.weights.max_parameters == 200_000_000
    with pytest.raises(ValidationError, match="only used with format 'safetensors'"):
        IngestMapping.model_validate({"format": "zarr_zip", "weights": {}})
    with pytest.raises(ValidationError, match="described by 'weights'"):
        IngestMapping.model_validate({"format": "safetensors", "vars": {"w": {"column": "w"}}})
    with pytest.raises(ValidationError, match="65536 bytes"):
        WeightsOptions(arch={"blob": "x" * 70_000})
    with pytest.raises(ValidationError):
        WeightsOptions(max_parameters=500_000_001)
    with pytest.raises(ValidationError, match="only source"):
        api.IngestAssembly.model_validate(
            {
                "sources": [
                    {"upload_id": UPL[0], "mapping": {"format": "safetensors"},
                     "combine": "merge"},
                    {"upload_id": UPL[1], "mapping": {"format": "netcdf"}, "combine": "merge"},
                ]
            }
        )  # fmt: skip
