"""scripts/seed.py against an in-memory fake of the /v1 API (httpx.MockTransport).

The fake validates every request body with the contract DTOs and answers with DTO-valid
responses, so this pins the seed to mapi_spec.api (paths, methods, status codes, shapes).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import httpx
import pytest
from mapi_spec import api as dto

from .conftest import load_script

NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
SHA = "sha256:" + "ab" * 32


@pytest.fixture(scope="module")
def seed() -> ModuleType:
    return load_script("scripts/seed.py", "mapi_devtools_seed")


class FakeApi:
    """Just enough of the M1 API for the seed flow, with ingest finishing after one poll."""

    def __init__(self, *, fail_ingest: str | None = None) -> None:
        self.datasets: dict[str, dict[str, Any]] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.versions: dict[tuple[str, int], dict[str, Any]] = {}
        self.polls: dict[tuple[str, int], int] = {}
        self.specs: dict[str, dict[str, Any]] = {}
        self.revisions: dict[tuple[str, int], dict[str, Any]] = {}
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_ingest = fail_ingest
        self._n = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}_{self._n:026d}"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content and method == "POST" else None
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "version": "0.2.0"})
        if request.url.host == "gcs.test":
            assert method == "PUT" and request.headers["x-goog-if-generation-match"] == "0"
            self.objects[path] = request.content
            return httpx.Response(200, json={})
        parts = path.strip("/").split("/")
        if parts[:2] == ["v1", "datasets"]:
            return self._datasets(method, parts[2:], body)
        if parts[:2] == ["v1", "specs"]:
            return self._specs(method, parts[2:], body)
        return httpx.Response(404, json={"code": "NOT_FOUND", "title": path})

    # -- datasets
    def _dataset_json(self, ds: dict[str, Any]) -> dict[str, Any]:
        return dto.Dataset.model_validate(ds).model_dump(mode="json")

    def _datasets(self, method: str, rest: list[str], body: Any) -> httpx.Response:
        if not rest and method == "GET":
            return httpx.Response(200, json={"items": list(self.datasets.values())})
        if not rest and method == "POST":
            req = dto.DatasetCreate.model_validate(body)
            ds = {
                "id": self._id("ds"),
                "tenant_id": "default",
                "project_id": "default",
                "name": req.name,
                "ref": f"dataset://default/{req.name}",
                "description": req.description,
                "created_at": NOW,
                "updated_at": NOW,
            }
            self.datasets[ds["id"]] = self._dataset_json(ds)
            return httpx.Response(201, json=self.datasets[ds["id"]])
        ds = self.datasets[rest[0]]
        if rest[1:] == ["uploads"]:
            req = dto.UploadCreate.model_validate(body)
            upl = self._id("upl")
            session = dto.UploadSession(
                id=upl,
                tenant_id="default",
                project_id="default",
                dataset_id=ds["id"],
                filename=req.filename,
                content_type=req.content_type,
                size_bytes=req.size_bytes,
                status="pending",
                expires_at=NOW + timedelta(minutes=15),
                created_at=NOW,
                upload={
                    "method": "PUT",
                    "url": f"http://gcs.test/upload/{upl}",
                    "name": f"t/default/uploads/{upl}",
                    "expires_at": NOW + timedelta(minutes=15),
                    "headers": {
                        "content-type": req.content_type,
                        "x-goog-if-generation-match": "0",
                    },
                    "max_bytes": req.size_bytes,
                },
            )
            self.uploads[upl] = {"size": req.size_bytes, "path": f"/upload/{upl}"}
            return httpx.Response(201, json=session.model_dump(mode="json"))
        if rest[1:] == ["versions"] and method == "POST":
            req = dto.DatasetVersionCreate.model_validate(body)
            upload = self.uploads[req.upload_id]
            assert len(self.objects[upload["path"]]) == upload["size"]
            assert req.mapping.format == "zarr_zip"
            n = sum(1 for k in self.versions if k[0] == ds["id"]) + 1
            version = {
                "dataset_id": ds["id"],
                "tenant_id": "default",
                "project_id": "default",
                "version": n,
                "ref": f"dataset://default/{ds['name']}@v{n}",
                "status": "ingesting",
                "ingest_run_id": self._id("run"),
                "created_at": NOW,
            }
            self.versions[(ds["id"], n)] = version
            return httpx.Response(
                202, json=dto.DatasetVersion.model_validate(version).model_dump(mode="json")
            )
        if rest[1] == "versions" and method == "GET":
            key = (ds["id"], int(rest[2]))
            self.polls[key] = self.polls.get(key, 0) + 1
            version = dict(self.versions[key])
            if self.fail_ingest == ds["name"]:
                version["status"] = "failed"
                version["issues"] = [
                    {"code": "SCHEMA_INVALID", "severity": "error", "message": "bad data"}
                ]
            else:
                version.update(status="ready", sha256=SHA, ready_at=NOW)
            return httpx.Response(
                200, json=dto.DatasetVersion.model_validate(version).model_dump(mode="json")
            )
        raise AssertionError(f"unexpected {method} datasets/{rest}")

    # -- specs
    def _revision(self, spec_id: str, n: int, yaml_text: str, parent: int | None) -> dict[str, Any]:
        name = self.specs[spec_id]["name"]
        rev = dto.SpecRevision(
            spec_id=spec_id,
            tenant_id="default",
            project_id="default",
            revision=n,
            ref=f"spec://default/{name}@r{n}",
            kind="ModelSpec",
            hash=SHA,
            yaml=yaml_text,
            document={"kind": "ModelSpec"},
            valid=True,
            parent_revision=parent,
            created_at=NOW,
        ).model_dump(mode="json")
        self.revisions[(spec_id, n)] = rev
        self.specs[spec_id]["latest_revision"] = n
        return rev

    def _specs(self, method: str, rest: list[str], body: Any) -> httpx.Response:
        if not rest and method == "GET":
            return httpx.Response(200, json={"items": list(self.specs.values())})
        if not rest and method == "POST":
            req = dto.SpecCreate.model_validate(body)
            assert "dataset://default/eu_media_daily@v" in req.yaml
            sid = self._id("spec")
            self.specs[sid] = {
                "id": sid,
                "tenant_id": "default",
                "project_id": "default",
                "name": "eu-geo-mmm",
                "kind": "ModelSpec",
                "ref": "spec://default/eu-geo-mmm",
                "latest_revision": 1,
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
            }
            latest = self._revision(sid, 1, req.yaml, None)
            doc = dto.SpecDoc.model_validate({**self.specs[sid], "latest": latest})
            return httpx.Response(201, json=doc.model_dump(mode="json"))
        sid = rest[0]
        if rest[1:] == ["revisions"] and method == "POST":
            req = dto.SpecRevisionCreate.model_validate(body)
            assert req.parent_revision == self.specs[sid]["latest_revision"]
            n = req.parent_revision + 1
            return httpx.Response(201, json=self._revision(sid, n, req.yaml, req.parent_revision))
        if rest[1] == "revisions" and method == "GET":
            return httpx.Response(200, json=self.revisions[(sid, int(rest[2]))])
        raise AssertionError(f"unexpected {method} specs/{rest}")


def _client(fake: FakeApi) -> httpx.Client:
    return httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(fake))


TABLES = {"data": b"D" * 100, "fx": b"F" * 50, "holidays": b"H" * 10}


def test_paths_come_from_the_endpoint_table(seed: ModuleType) -> None:
    assert seed.path("create_upload", dataset_id="ds_1") == "/v1/datasets/ds_1/uploads"
    assert seed.path("get_dataset_version", dataset_id="d", version=3) == (
        "/v1/datasets/d/versions/3"
    )
    with pytest.raises(seed.SeedError, match="needs path parameter"):
        seed.path("get_dataset")


def test_spec_yaml_points_at_seeded_versions(seed: ModuleType) -> None:
    refs = {old: f"dataset://default/{name}@v7" for old, (name, _) in seed.DATASETS.items()}
    text = seed.spec_yaml(refs)
    assert "dataset://growth" not in text and "dataset://shared" not in text
    assert "dataset://default/eu_media_daily@v7" in text
    assert "project: default" in text
    dto.SpecCreate(yaml=text)  # within the API's size limits


def test_full_flow_and_idempotent_rerun(seed: ModuleType) -> None:
    fake = FakeApi()
    logs: list[str] = []
    with _client(fake) as http:
        rev = seed.seed(seed.Api(http), TABLES, timeout=5, log=logs.append, sleep=lambda s: None)
    assert rev.ref == "spec://default/eu-geo-mmm@r1" and rev.valid
    assert sorted(d["name"] for d in fake.datasets.values()) == [
        "eu_media_daily",
        "fx_daily",
        "holidays_eu",
    ]
    assert sorted(len(b) for b in fake.objects.values()) == [10, 50, 100]
    assert all(n == 1 for n in fake.polls.values())
    assert any("dataset://default/holidays_eu@v1 ready" in line for line in logs)

    with _client(fake) as http:  # re-run: same datasets, new versions, spec revised
        rev2 = seed.seed(seed.Api(http), TABLES, timeout=5, log=logs.append, sleep=lambda s: None)
    assert len(fake.datasets) == 3
    assert rev2.revision == 2 and "@v2" in rev2.yaml

    with _client(fake) as http:  # unchanged YAML: no new revision
        refs = {old: f"dataset://default/{name}@v2" for old, (name, _) in seed.DATASETS.items()}
        same = seed.seed_spec(seed.Api(http), refs)
    assert same.revision == 2


def test_failed_ingest_is_reported(seed: ModuleType) -> None:
    fake = FakeApi(fail_ingest="fx_daily")
    with _client(fake) as http, pytest.raises(seed.SeedError, match=r"ingest failed.*bad data"):
        seed.seed(seed.Api(http), TABLES, timeout=5, log=lambda s: None, sleep=lambda s: None)


def test_ingest_timeout(seed: ModuleType) -> None:
    fake = FakeApi()
    with _client(fake) as http:
        api = seed.Api(http)
        ds = seed.ensure_dataset(api, "eu_media_daily", "x")
        with pytest.raises(seed.SeedError, match="ingest still running"):
            seed.upload_version(
                api, ds, b"x", filename="x.zarr.zip", timeout=-1, sleep=lambda s: None
            )


def test_problem_details_surface(seed: ModuleType) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "version": "0"})
        return httpx.Response(
            422, json={"code": "SCHEMA_INVALID", "title": "Invalid", "detail": "name: bad"}
        )

    transport = httpx.MockTransport(handler)
    expected = "HTTP 422: SCHEMA_INVALID - Invalid - name: bad"
    with (
        httpx.Client(base_url="http://api.test", transport=transport) as http,
        pytest.raises(seed.SeedError, match=expected),
    ):
        seed.seed(seed.Api(http), TABLES, timeout=1, log=lambda s: None)


def test_unreachable_api_explains_how_to_start_it(seed: ModuleType) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    transport = httpx.MockTransport(handler)
    with (
        httpx.Client(base_url="http://api.test", transport=transport) as http,
        pytest.raises(seed.SeedError, match="make dev"),
    ):
        seed.Api(http).health()
