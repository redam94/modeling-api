#!/usr/bin/env python3
"""Load the E33 demo (hierarchical geo MMM) into a running stack: ``make seed``.

Uploads the conformance corpus' E33 data — daily spend/revenue per market and channel, an FX
side table and a holidays table — as three datasets, waits for their sandboxed ingest runs,
then creates (or revises) the ``eu-geo-mmm`` spec pointing at the new dataset versions and
prints its static validation. Afterwards, fit it from the UI or ``POST /v1/runs``.

Everything goes through the public HTTP API (``/v1``, ``mapi_spec.api``): request bodies are
built from, and responses validated against, the contract DTOs; paths come from
``mapi_spec.api.M1_ENDPOINTS`` by operation id. Uploads go to the signed PUT URL the API mints.
Re-running is safe: datasets are reused by name, and a new spec revision is only added when
the YAML changed.

    uv run python scripts/seed.py                    # API at $MAPI_API_URL (127.0.0.1:8000)
    uv run python scripts/seed.py --size full --timeout 1800
    uv run python scripts/seed.py --export demo/     # only write the files (no API needed)

``--export DIR`` writes the same tables plus the E33 score payloads (future spend, a new
market, a weekly EUR plan, a window of past weeks) as ``.zarr.zip`` files and the spec as
``eu-geo-mmm.yaml``, for uploading through the web app instead.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, TypeVar

import httpx
from mapi_spec import api as dto
from mapi_spec.jobs import DATASET_SCHEMA_ATTR
from mapi_spec.localdev import ENV_API_URL, LocalDev
from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)
ENDPOINTS = {e.operation_id: e for e in dto.M1_ENDPOINTS}

#: Fixture dataset refs in spec.yaml -> (dataset name, which table of the fixture).
DATASETS: dict[str, tuple[str, str]] = {
    "dataset://growth/eu_media_daily@latest": ("eu_media_daily", "data"),
    "dataset://growth/fx_daily@latest": ("fx_daily", "fx"),
    "dataset://shared/holidays_eu@v3": ("holidays_eu", "holidays"),
}
SPEC_NAME = "eu-geo-mmm"


class SeedError(RuntimeError):
    """A failed step, with the API's problem details when there were any."""


def path(operation_id: str, **params: Any) -> str:
    """The URL path of an M1 endpoint, with ``{params}`` filled in."""
    template = ENDPOINTS[operation_id].path
    try:
        return template.format(**{k: str(v) for k, v in params.items()})
    except KeyError as exc:
        raise SeedError(f"{operation_id} needs path parameter {exc}") from None


def _problem(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(body, dict):
        parts = [str(body.get(k)) for k in ("code", "title", "detail") if body.get(k)]
        return " - ".join(parts) or str(body)[:500]
    return str(body)[:500]


@dataclass
class Api:
    """Minimal typed client over ``httpx`` for the endpoints the seed needs."""

    http: httpx.Client

    def call(
        self,
        operation_id: str,
        model: type[M],
        *,
        body: BaseModel | None = None,
        params: Mapping[str, Any] | None = None,
        **path_params: Any,
    ) -> M:
        decl = ENDPOINTS[operation_id]
        resp = self.http.request(
            decl.method,
            path(operation_id, **path_params),
            json=None if body is None else body.model_dump(mode="json", exclude_none=True),
            params=dict(params or {}),
        )
        if resp.status_code != decl.success:
            raise SeedError(
                f"{decl.method} {resp.request.url.path} -> HTTP {resp.status_code}: "
                f"{_problem(resp)}"
            )
        return model.model_validate(resp.json())

    def pages(self, operation_id: str, item: type[M], **path_params: Any) -> Iterator[M]:
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": dto.PAGE_LIMIT_MAX}
            if cursor:
                params["cursor"] = cursor
            page = self.call(operation_id, dto.Page[item], params=params, **path_params)  # type: ignore[valid-type]
            yield from page.items
            if not page.next_cursor:
                return
            cursor = page.next_cursor

    def health(self) -> dto.Health:
        try:
            return self.call("health", dto.Health)
        except httpx.TransportError as exc:
            raise SeedError(
                f"the API is not reachable at {self.http.base_url} ({exc}); start the stack "
                "with `make dev` (or `make compose-up`) first"
            ) from exc


def ensure_dataset(api: Api, name: str, description: str) -> dto.Dataset:
    for ds in api.pages("list_datasets", dto.Dataset):
        if ds.name == name:
            return ds
    return api.call(
        "create_dataset", dto.Dataset, body=dto.DatasetCreate(name=name, description=description)
    )


def upload_version(
    api: Api,
    dataset: dto.Dataset,
    payload: bytes,
    *,
    filename: str,
    timeout: float,
    poll: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dto.DatasetVersion:
    session = api.call(
        "create_upload",
        dto.UploadSession,
        body=dto.UploadCreate(
            filename=filename, content_type="application/zip", size_bytes=len(payload)
        ),
        dataset_id=dataset.id,
    )
    put = session.upload
    resp = api.http.request(put.method, put.url, content=payload, headers=dict(put.headers))
    if resp.status_code not in (200, 201):
        raise SeedError(f"upload of {filename} failed: HTTP {resp.status_code}: {_problem(resp)}")
    version = api.call(
        "create_dataset_version",
        dto.DatasetVersion,
        body=dto.DatasetVersionCreate(
            upload_id=session.id,
            mapping=dto.IngestMapping(format="zarr_zip"),
            message="make seed: E33 demo data",
        ),
        dataset_id=dataset.id,
    )
    deadline = time.monotonic() + timeout
    while version.status == dto.DatasetVersionStatus.INGESTING:
        if time.monotonic() > deadline:
            raise SeedError(
                f"{version.ref}: ingest still running after {timeout:.0f}s "
                f"(run {version.ingest_run_id}); re-run with a larger --timeout"
            )
        sleep(poll)
        version = api.call(
            "get_dataset_version",
            dto.DatasetVersion,
            dataset_id=dataset.id,
            version=version.version,
        )
    if version.status == dto.DatasetVersionStatus.FAILED:
        issues = "; ".join(f"{i.code}: {i.message}" for i in version.issues) or "no details"
        raise SeedError(f"{version.ref}: ingest failed ({issues})")
    return version


def spec_yaml(refs: Mapping[str, str]) -> str:
    """The E33 fixture spec with its dataset refs replaced by ``refs`` (fixture ref -> new)."""
    text = (
        resources.files("mapi_conformance.fixtures.e33")
        .joinpath("spec.yaml")
        .read_text(encoding="utf-8")
    )
    for old, new in refs.items():
        if old not in text:
            raise SeedError(f"fixture spec no longer references {old}; update scripts/seed.py")
        text = text.replace(old, new)
    return text.replace("project: growth", f"project: {dto.DEFAULT_PROJECT_ID}", 1)


def ensure_spec(api: Api, yaml_text: str) -> dto.SpecRevision:
    for doc in api.pages("list_specs", dto.SpecDoc):
        if doc.name == SPEC_NAME:
            latest = api.call(
                "get_spec_revision",
                dto.SpecRevision,
                spec_id=doc.id,
                revision=doc.latest_revision,
            )
            if latest.yaml == yaml_text:
                return latest
            return api.call(
                "create_spec_revision",
                dto.SpecRevision,
                body=dto.SpecRevisionCreate(
                    yaml=yaml_text,
                    parent_revision=doc.latest_revision,
                    message="make seed: point at the seeded dataset versions",
                ),
                spec_id=doc.id,
            )
    created = api.call(
        "create_spec",
        dto.SpecDoc,
        body=dto.SpecCreate(yaml=yaml_text, message="make seed: E33 demo"),
    )
    if created.latest is not None:
        return created.latest
    return api.call(
        "get_spec_revision", dto.SpecRevision, spec_id=created.id, revision=created.latest_revision
    )


def fixture_tables(size: str) -> dict[str, bytes]:
    """The E33 tables as zarr-zip bytes: ``data``, ``fx`` and ``holidays``."""
    from mapi_conformance.fixtures.e33 import data as e33
    from mapi_sdk.wire import write_zarr_zip

    main_ds, side = e33.generate(seed=0, size=size)  # type: ignore[arg-type]
    # Ship the raw schema (market: new_levels, roles, units) with the data; without it ingest
    # infers one (every dim fixed, every variable a covariate) and new-market scoring fails.
    main_ds = main_ds.assign_attrs({DATASET_SCHEMA_ATTR: e33.schema().model_dump_json()})
    tables = {"data": main_ds, **side}
    out: dict[str, bytes] = {}
    for key, ds in tables.items():
        blob = write_zarr_zip(ds)
        assert blob is not None
        out[key] = blob
    return out


#: ``--export`` file name -> (E33 payload builder, description). See the fixture's
#: score_requests.yaml for how each one is scored.
EXPORT_PAYLOADS: dict[str, str] = {
    "eu_media_future": "future",
    "eu_media_future_fr": "future_new_market",
    "eu_media_weekly_plan": "weekly_plan",
    "eu_media_window": "window",
}


def export_files(dest: Path, size: str = "tiny") -> dict[str, Path]:
    """Write the demo tables, the score payloads and the spec into ``dest``.

    The spec refers to ``dataset://default/<name>@latest`` for each table, so uploading the
    files as datasets of those names makes it valid as is. Returns ``{name: path}``.
    """
    from mapi_conformance.fixtures.e33 import data as e33
    from mapi_sdk.wire import write_zarr_zip

    dest.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    tables = fixture_tables(size)
    for _, (name, table) in DATASETS.items():
        path = dest / f"{name}.zarr.zip"
        path.write_bytes(tables[table])
        out[name] = path
    data, _ = e33.generate(seed=0, size=size)  # type: ignore[arg-type]
    for name, payload in EXPORT_PAYLOADS.items():
        ds = getattr(e33, f"payload_{payload}")(data, 0, size)
        path = dest / f"{name}.zarr.zip"
        blob = write_zarr_zip(ds)
        assert blob is not None
        path.write_bytes(blob)
        out[name] = path
    spec = dest / f"{SPEC_NAME}.yaml"
    refs = {
        ref: f"dataset://{dto.DEFAULT_PROJECT_ID}/{name}@latest"
        for ref, (name, _) in DATASETS.items()
    }
    spec.write_text(spec_yaml(refs), encoding="utf-8")
    out[SPEC_NAME] = spec
    return out


def seed(
    api: Api,
    tables: Mapping[str, bytes],
    *,
    timeout: float,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> dto.SpecRevision:
    health = api.health()
    log(f"API {api.http.base_url} is {health.status} (version {health.version})")
    refs: dict[str, str] = {}
    for fixture_ref, (name, table) in DATASETS.items():
        dataset = ensure_dataset(api, name, f"E33 demo ({table}) loaded by make seed")
        log(f"uploading {name} ({len(tables[table]) / 1024:.0f} KiB) ...")
        version = upload_version(
            api, dataset, tables[table], filename=f"{name}.zarr.zip", timeout=timeout, sleep=sleep
        )
        log(f"  {version.ref} ready")
        refs[fixture_ref] = version.ref
    revision = seed_spec(api, refs)
    status = "valid" if revision.valid else f"INVALID ({len(revision.issues)} issue(s))"
    log(f"spec {revision.ref}: {status}")
    for issue in revision.issues:
        log(f"  {issue.severity} {issue.code} at {issue.pointer or '/'}: {issue.message}")
    return revision


def seed_spec(api: Api, refs: Mapping[str, str]) -> dto.SpecRevision:
    return ensure_spec(api, spec_yaml(refs))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load the E33 demo data and spec into the API.")
    parser.add_argument("--api-url", default=None, help=f"default: ${ENV_API_URL} or 127.0.0.1")
    parser.add_argument("--size", choices=["tiny", "full"], default="tiny")
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds per ingest")
    parser.add_argument(
        "--export",
        type=Path,
        metavar="DIR",
        help="only write the demo files (tables, score payloads, spec) into DIR",
    )
    args = parser.parse_args(argv)
    if args.export is not None:
        for name, path in export_files(args.export, args.size).items():
            print(f"{name}: {path}")
        return 0
    base = args.api_url or LocalDev.from_environ(os.environ).api_url
    with httpx.Client(base_url=base, timeout=60.0) as http:
        try:
            revision = seed(Api(http), fixture_tables(args.size), timeout=args.timeout)
        except SeedError as exc:
            print(f"seed failed: {exc}", file=sys.stderr)
            return 1
    return 0 if revision.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
