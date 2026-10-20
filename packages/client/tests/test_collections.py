"""Image collections on the client (v0.15): folder scans, deterministic parts, the chunked
``blobs/missing`` query and ``upload_collection`` (manifest first, parts, version create)
against a stateful respx server - including a resume that skips parts that arrived and
renews an expired link. Sync and async."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from mapi_client import AsyncClient, Client, ClientConfig
from mapi_client import _v015_common as common
from mapi_client.collections import (
    UploadState,
    client_manifest_csv,
    parse_part_size,
    plan_parts,
    scan_images,
    write_parts,
)
from mapi_spec.api import BlobMissing, Dataset, DatasetVersion, UploadSession
from mapi_spec.collection import CollectionOptions
from mapi_spec.errors import ErrorCode, MapiError, Problem

from .conftest import STORAGE
from .conftest import _example as ex

pytestmark = pytest.mark.unit

BASE = "http://api.test"
V1 = f"{BASE}/v1"
DS = ex(Dataset)["id"]


def _image(i: int, size: int = 40) -> bytes:
    """Fake image bytes (the client never decodes them): a PNG signature + filler."""
    return b"\x89PNG\r\n\x1a\n" + bytes([i % 251]) * size + i.to_bytes(4, "big")


def _folder(root: Path, per_class: int = 3, classes: tuple[str, ...] = ("cat", "dog")) -> Path:
    for c, name in enumerate(classes):
        (root / name).mkdir(parents=True)
        for i in range(per_class):
            (root / name / f"{i}.png").write_bytes(_image(c * 100 + i))
    (root / "notes.txt").write_text("not an image")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "x.png").write_bytes(_image(999))
    return root


# ============================================================================ pure helpers


def test_scan_reads_the_folder_in_path_order(tmp_path: Path) -> None:
    plan = scan_images(_folder(tmp_path / "imgs"), label_from_folder=True)
    assert [im.rel for im in plan.images] == [
        f"{c}/{i}.png" for c in ("cat", "dog") for i in range(3)
    ]
    assert plan.columns == ["path", "sha256", "label"]
    assert [r["label"] for r in plan.rows] == ["cat"] * 3 + ["dog"] * 3
    assert plan.rows[0]["sha256"] == hashlib.sha256(_image(0)).hexdigest()
    text = client_manifest_csv(plan).decode()
    assert text.splitlines()[0] == "path,sha256,label"
    assert client_manifest_csv(plan) == client_manifest_csv(
        scan_images(tmp_path / "imgs", label_from_folder=True)
    )


def test_scan_with_a_manifest_keeps_its_columns_and_checks_paths(tmp_path: Path) -> None:
    root = _folder(tmp_path / "imgs")
    manifest = tmp_path / "m.csv"
    manifest.write_text("file,class,fold\ndog/1.png,d,train\ncat/0.png,c,test\n")
    plan = scan_images(root, manifest, path_column="file")
    assert plan.columns == ["file", "sha256", "class", "fold"]
    assert [r["class"] for r in plan.rows] == ["d", "c"]
    manifest.write_text("file\n../outside.png\n")
    with pytest.raises(ValueError, match="inside the folder"):
        scan_images(root, manifest, path_column="file")
    manifest.write_text("file,sha256\ncat/0.png," + "0" * 64 + "\n")
    with pytest.raises(ValueError, match="does not match"):
        scan_images(root, manifest, path_column="file")
    manifest.write_text("file,offset\ncat/0.png,1\n")
    with pytest.raises(ValueError, match="reserved"):
        scan_images(root, manifest, path_column="file")


def test_parts_are_deterministic_store_zips(tmp_path: Path) -> None:
    plan = scan_images(_folder(tmp_path / "imgs"))
    first = write_parts(plan, plan.shas, tmp_path / "a", part_bytes=1024)
    again = write_parts(plan, plan.shas, tmp_path / "b", part_bytes=1024)
    assert len(first) > 1
    assert [p.path.read_bytes() for p in first] == [p.path.read_bytes() for p in again]
    entries: list[str] = []
    for part in first:
        assert part.path.stat().st_size <= 1024
        with zipfile.ZipFile(part.path) as zf:
            for info in zf.infolist():
                assert info.compress_type == zipfile.ZIP_STORED
                assert info.date_time == (1980, 1, 1, 0, 0, 0)
                assert info.extra == b"" and info.create_system == 0
                sha = info.filename.split(".")[0]
                assert hashlib.sha256(zf.read(info)).hexdigest() == sha
                entries.append(info.filename)
    assert entries == sorted(entries) and len(entries) == len(plan.shas)


def test_parts_match_the_sdk_helper(tmp_path: Path) -> None:
    images = pytest.importorskip("mapi_sdk.testing.images")
    plan = scan_images(_folder(tmp_path / "imgs"))
    ours = write_parts(plan, plan.shas, tmp_path / "p")
    theirs = images.build_parts([im.path.read_bytes() for im in plan.by_sha.values()])
    assert [p.path.read_bytes() for p in ours] == list(theirs)


def test_plan_parts_only_packs_the_missing_images(tmp_path: Path) -> None:
    plan = scan_images(_folder(tmp_path / "imgs"))
    missing = plan.shas[:2]
    assert plan_parts(plan, missing) == [sorted(missing)]
    with pytest.raises(ValueError, match="not in the plan"):
        plan_parts(plan, ["f" * 64])
    with pytest.raises(ValueError, match="part size"):
        plan_parts(plan, missing, 10)


def test_part_size_and_blob_query_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse_part_size("256MB") == 256_000_000
    assert parse_part_size("1GiB") == 1024**3 and parse_part_size("64m") == 64 * 1024**2
    with pytest.raises(ValueError):
        parse_part_size("lots")
    monkeypatch.setattr(common, "MAX_BLOB_QUERY", 2)
    shas = [f"{i:064x}" for i in range(5)]
    queries = common.blob_queries(shas + shas[:1], None)
    assert [len(q.sha256) for q in queries] == [2, 2, 1]


def test_upload_action_on_resume() -> None:
    now = datetime.now(UTC)
    base = UploadSession.model_validate(
        ex(UploadSession, expires_at=(now + timedelta(hours=1)).isoformat())
    )
    assert common.upload_action(base.model_copy(update={"received": True})) == "done"
    assert common.upload_action(base.model_copy(update={"status": "consumed"})) == "new"
    assert common.upload_action(base.model_copy(update={"expires_at": now})) == "renew"
    assert common.upload_action(base) == "put"


# ===================================================================== a stateful server


class FakeServer:
    """Uploads (``received`` once their PUT arrived), renew, blobs/missing against a set of
    present sha256, and version create."""

    def __init__(self, router: respx.MockRouter, present: set[str] | None = None) -> None:
        self.present = present or set()
        self.uploads: dict[str, dict[str, Any]] = {}
        self.blobs: list[dict[str, Any]] = []
        self.versions: list[dict[str, Any]] = []
        self.puts: list[str] = []
        self.fail_puts: set[str] = set()
        self.base_version: int | None = 3
        uploads = f"{V1}/datasets/{DS}/uploads"
        router.post(uploads).mock(side_effect=self._create)
        router.get(url__regex=rf"{uploads}/(?P<upl>[^/]+)$").mock(side_effect=self._get)
        router.post(url__regex=rf"{uploads}/(?P<upl>[^/]+)/renew$").mock(side_effect=self._renew)
        router.put(url__startswith=f"{STORAGE}/put/").mock(side_effect=self._put)
        router.post(f"{V1}/datasets/{DS}/blobs/missing").mock(side_effect=self._missing)
        router.post(f"{V1}/datasets/{DS}/versions").mock(side_effect=self._version)
        router.get(f"{V1}/datasets").mock(
            return_value=httpx.Response(
                200, json={"items": [ex(Dataset, name="pets")], "next_cursor": None}
            )
        )

    def _session(self, upl: str) -> dict[str, Any]:
        u = self.uploads[upl]
        data = ex(
            UploadSession,
            id=upl,
            filename=u["filename"],
            size_bytes=u["size"],
            content_type=u["ctype"],
            status="uploaded" if u["received"] else "pending",
            expires_at=u["expires"].isoformat(),
            received=u["received"],
        )
        data["upload"] = data["upload"] | {
            "url": f"{STORAGE}/put/{upl}?gen={u['gen']}",
            "max_bytes": 10_000_000,
            "headers": {"content-type": u["ctype"]},
            "expires_at": u["expires"].isoformat(),
        }
        return data

    def _create(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        upl = f"upl_{len(self.uploads):04d}"
        self.uploads[upl] = {
            "filename": body["filename"],
            "size": body["size_bytes"],
            "ctype": body["content_type"],
            "received": False,
            "expires": datetime.now(UTC) + timedelta(hours=1),
            "gen": 0,
            "data": None,
        }
        return httpx.Response(201, json=self._session(upl))

    def _get(self, request: httpx.Request, upl: str) -> httpx.Response:
        if upl not in self.uploads:
            p = Problem.from_code(ErrorCode.NOT_FOUND, "no such upload")
            return httpx.Response(404, json=p.model_dump(mode="json"))
        return httpx.Response(200, json=self._session(upl))

    def _renew(self, request: httpx.Request, upl: str) -> httpx.Response:
        u = self.uploads[upl]
        u["expires"] = datetime.now(UTC) + timedelta(hours=1)
        u["gen"] += 1
        return httpx.Response(200, json=self._session(upl))

    def _put(self, request: httpx.Request) -> httpx.Response:
        upl = request.url.path.rsplit("/", 1)[-1]
        u = self.uploads[upl]
        if upl in self.fail_puts or u["expires"] < datetime.now(UTC):
            return httpx.Response(403, text="<Error>expired</Error>")
        self.puts.append(upl)
        u["received"] = True
        u["data"] = request.content
        return httpx.Response(200, json={})

    def _missing(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.blobs.append(body)
        shas = body["sha256"]
        base = body.get("base_version")
        base = self.base_version if base is None else (None if base == 0 else base)
        missing = [s for s in shas if base is None or s not in self.present]
        return httpx.Response(
            200,
            json={"base_version": base, "missing": missing, "present": len(shas) - len(missing)},
        )

    def _version(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.versions.append({"body": body, "headers": dict(request.headers)})
        return httpx.Response(202, json=ex(DatasetVersion, status="ingesting"))

    def data(self, upl: str) -> bytes:
        return bytes(self.uploads[upl]["data"])


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def client(api: respx.MockRouter) -> Iterator[Client]:
    with Client(config=ClientConfig(base_url=BASE, max_retries=0), sleep=lambda _s: None) as c:
        yield c


def test_missing_images_chunks_and_pins_the_base(
    client: Client, api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    shas = [f"{i:064x}" for i in range(5)]
    server = FakeServer(api, present={shas[1], shas[4]})
    monkeypatch.setattr(common, "MAX_BLOB_QUERY", 2)
    found = client.datasets.missing_images(DS, list(reversed(shas)))
    assert isinstance(found, BlobMissing)
    assert found.missing == [shas[3], shas[2], shas[0]]  # in the order asked
    assert found.present == 2 and found.base_version == 3
    assert [b.get("base_version") for b in server.blobs] == [None, 3, 3]


def test_upload_collection_uploads_the_manifest_then_only_missing_parts(
    client: Client, api: respx.MockRouter, tmp_path: Path
) -> None:
    root = _folder(tmp_path / "imgs")
    plan = scan_images(root, label_from_folder=True)
    server = FakeServer(api, present=set(plan.shas[:2]))
    events: list[tuple[str, int, int]] = []
    client.datasets.upload_collection(
        DS,
        root,
        label_from_folder=True,
        part_bytes=1024,
        options=CollectionOptions(size=(12, 12)),
        progress=lambda s, d, t: events.append((s, d, t)),
    )
    (version,) = server.versions
    coll = version["body"]["collection"]
    assert coll["base_version"] == 3
    assert coll["options"]["label_column"] == "label" and coll["options"]["size"] == [12, 12]
    manifest = server.data(coll["manifest_upload_id"])
    assert manifest == client_manifest_csv(plan)
    packed: list[str] = []
    for upl in coll["part_upload_ids"]:
        with zipfile.ZipFile(io.BytesIO(server.data(upl))) as zf:
            packed += [n.split(".")[0] for n in zf.namelist()]
    assert sorted(packed) == plan.shas[2:]  # the base has the first two
    assert version["headers"]["idempotency-key"] == common.collection_key(
        DS, coll["manifest_upload_id"], coll["part_upload_ids"]
    )
    assert ("create", 1, 1) in events and events[0][0] == "hash"


def test_no_base_uploads_everything(client: Client, api: respx.MockRouter, tmp_path: Path) -> None:
    root = _folder(tmp_path / "imgs")
    server = FakeServer(api, present=set(scan_images(root).shas))
    client.datasets.upload_collection(DS, root, base_version=0)
    (version,) = server.versions
    assert version["body"]["collection"]["base_version"] == 0
    assert server.blobs[0]["base_version"] == 0
    assert len(version["body"]["collection"]["part_upload_ids"]) == 1


def test_resume_skips_received_parts_and_renews_expired_links(
    client: Client, api: respx.MockRouter, tmp_path: Path
) -> None:
    root = _folder(tmp_path / "imgs", per_class=4)
    state_file = tmp_path / "state.json"
    server = FakeServer(api)
    server.base_version = None
    # the second part's PUT fails (and so does the retry on a renewed link)
    server.fail_puts = {"upl_0002"}
    with pytest.raises(MapiError):  # the storage refused the PUT (and its retry)
        client.datasets.upload_collection(DS, root, part_bytes=1024, state_file=state_file)
    assert not server.versions
    saved = json.loads(state_file.read_text())
    assert saved["manifest_upload_id"] == "upl_0000"
    assert [(p["upload_id"], p["received"]) for p in saved["parts"]] == [
        ("upl_0001", True),
        ("upl_0002", False),
    ]
    server.fail_puts = set()
    server.uploads["upl_0002"]["expires"] = datetime.now(UTC) - timedelta(minutes=1)
    puts_before = list(server.puts)
    client.datasets.upload_collection(DS, root, part_bytes=1024, state_file=state_file)
    (version,) = server.versions
    coll = version["body"]["collection"]
    assert coll["manifest_upload_id"] == "upl_0000"
    assert coll["part_upload_ids"][:2] == ["upl_0001", "upl_0002"]
    new_puts = server.puts[len(puts_before) :]
    assert "upl_0000" not in new_puts and "upl_0001" not in new_puts  # arrived before
    assert new_puts[0] == "upl_0002" and server.uploads["upl_0002"]["gen"] >= 1  # renewed
    assert not state_file.exists()  # the version exists: nothing to resume


def test_state_file_of_another_manifest_starts_over(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    UploadState(DS, "a" * 64, "upl_x", [{"index": 0, "sha256": "b" * 64}]).save(path)
    same = UploadState.load(path, dataset_id=DS, manifest_sha256="a" * 64)
    assert same.manifest_upload_id == "upl_x" and same.part(0, "b" * 64) is not None
    other = UploadState.load(path, dataset_id=DS, manifest_sha256="c" * 64)
    assert other.manifest_upload_id is None and other.parts == []


async def test_async_upload_collection(api: respx.MockRouter, tmp_path: Path) -> None:
    root = _folder(tmp_path / "imgs")
    server = FakeServer(api)

    async def nosleep(_: float) -> None:
        return None

    async with AsyncClient(config=ClientConfig(base_url=BASE, max_retries=0), sleep=nosleep) as c:
        await c.datasets.upload_collection(DS, root, label_from_folder=True)
        found = await c.datasets.missing_images(DS, scan_images(root).shas, base_version=0)
    (version,) = server.versions
    assert len(version["body"]["collection"]["part_upload_ids"]) == 1
    assert len(found.missing) == 6


def test_upload_weights_uses_the_safetensors_mapping(
    client: Client, api: respx.MockRouter, tmp_path: Path
) -> None:
    server = FakeServer(api)
    path = tmp_path / "w.safetensors"
    path.write_bytes(b"\x08\x00\x00\x00\x00\x00\x00\x00{}      ")
    arch = tmp_path / "arch.json"
    arch.write_text(json.dumps({"kind": "resnet"}))
    client.datasets.upload_weights(path, dataset_id=DS, arch=arch, wait=False)
    (version,) = server.versions
    mapping = version["body"]["mapping"]
    assert mapping["format"] == "safetensors"
    assert mapping["weights"]["arch"] == {"kind": "resnet"}
    with pytest.raises(ValueError, match="safetensors"):
        client.datasets.upload_weights(arch, dataset_id=DS)
