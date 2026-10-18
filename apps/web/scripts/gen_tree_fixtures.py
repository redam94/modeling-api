"""Write ``apps/web/src/mocks/tree.gen.json``: the M4 tree-data fixtures of the mock API.

The mock API (``src/mocks/tree.ts``: ``vite --mode mock`` and the Playwright smoke suite)
serves what the real runner writes, so the tree screens are exercised against real outputs:

* **A tree dataset version** (``lens_cubes`` v1): an :class:`~mapi_spec.ingest.IngestAssembly`
  of three NetCDF files ingested by the runner's own ``run_ingest`` - two yearly files
  concatenated along ``year`` at the root, and an instrument file (with a ``/psf`` group)
  mounted at ``/instrument``. Kept: the version DTO (as the server builds it from the
  outputs), ``tree_schema.json``, ``summary.json`` (one summary per node), ``profile.json``,
  ``preview.json`` and the Data Explorer index of every node with its overview and
  distributions views (``ui/explore/nodes/...``; other sections are dropped to keep the file
  small and show as "not computed").
* **An inspection** of a raw h5py file (no dim metadata, one array with dimension scales),
  by the runner's ``inspect_upload`` after its HDF5 guard.
* **The mapping** the Playwright test builds from that inspection, checked by ingesting the
  raw file with it (the run must succeed), so the mock's expected request is a real one.

Usage::

    uv run python apps/web/scripts/gen_tree_fixtures.py [--check]

``--check`` exits 1 when the committed file differs from what would be written.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any, Final

import h5py
import numpy as np
import pandas as pd
import xarray as xr
from mapi_runner.jobs import IngestJob
from mapi_runner.worker.ingest import run_ingest
from mapi_runner.worker.inspection import inspect_upload
from mapi_spec.api import Dataset, DatasetVersion
from mapi_spec.ingest import IngestAssembly, IngestMapping
from mapi_spec.jobs import InspectJob, upload_input
from mapi_spec.run import RunnerPhase
from mapi_spec.schema import DataSchema
from mapi_spec.tree import TreeSchema

WEB: Final = Path(__file__).resolve().parent.parent
OUT: Final = WEB / "src" / "mocks" / "tree.gen.json"

DS_ID: Final = "ds_01jaf3a1b2c3d4e5f6g7h8tree"
RUN_ID: Final = "run_01jaf3q2x8m9v7k5h4g3f2tree"
UPLOADS: Final = [
    "upl_01jaf3d1e2f3g4h5j6k7m8tr00",
    "upl_01jaf3d1e2f3g4h5j6k7m8tr01",
    "upl_01jaf3d1e2f3g4h5j6k7m8tr02",
]
T0: Final = "2026-10-14T09:00:00Z"
SHA: Final = "sha256:" + "cd" * 32
#: Explorer sections kept per node (the rest show as "not computed" in mock mode).
KEEP_SECTIONS: Final = ("overview", "distributions")
MAX_BYTES: Final = 300 * 1024

WEEKS = pd.date_range("2024-01-01", periods=6, freq="W-MON")

#: The assembly the fixture version is built from (upload ids are filled in per source).
_NC: Final = {"format": "netcdf"}
ASSEMBLY: Final = [
    {"mapping": _NC, "combine": "concat", "concat_dim": "year", "concat_label": 2024},
    {"mapping": _NC, "combine": "concat", "concat_dim": "year", "concat_label": 2025},
    {"mapping": _NC, "node": "/instrument"},
]

#: What the Playwright raw-HDF5 test maps from the inspection (see e2e/tree-data.spec.ts).
HDF5_MAPPING: Final = {
    "format": "hdf5",
    "hdf5": {
        "vars": [
            {"path": "/observations/image", "node": "/", "name": "image", "dims": ["row", "col"],
             "role": "target", "units": "counts"},
            {"path": "/psf/kernel", "node": "/psf", "name": "kernel", "dims": ["prow", "pcol"]},
        ],
        "coords": [
            {"dim": "row", "node": "/", "path": "/axes/rows"},
            {"dim": "col", "node": "/", "start": 0, "step": 0.5},
        ],
    },
}  # fmt: skip


# ---------------------------------------------------------------------------- the files


def _year(year: int) -> xr.Dataset:
    rng = np.random.default_rng(year)
    return xr.Dataset(
        {
            "sales": (("time", "geo"), 100 + rng.normal(size=(6, 2)).round(3)),
            "tv": (("time", "geo"), rng.gamma(2.0, 10.0, size=(6, 2)).round(3)),
        },
        coords={"time": WEEKS, "geo": ["de", "fr"]},
    )


def _instrument() -> xr.DataTree:
    rng = np.random.default_rng(7)
    return xr.DataTree.from_dict(
        {
            "/": xr.Dataset(
                {"temp": (("hour",), (20 + rng.normal(size=4)).round(3))},
                coords={"hour": [0, 6, 12, 18]},
            ),
            "/psf": xr.Dataset(
                {"kernel": (("prow", "pcol"), rng.random((3, 3)).round(4))},
                coords={"prow": [0, 1, 2], "pcol": [0, 1, 2]},
            ),
        }
    )


def write_raw_hdf5(path: Path) -> Path:
    """A raw h5py file: an image, a PSF, axes, a spare array and one dimension-scaled cube."""
    rng = np.random.default_rng(3)
    with h5py.File(path, "w") as f:
        f.create_dataset(
            "observations/image",
            data=rng.random((4, 5)).astype("float32"),
            chunks=(4, 5),
            compression="gzip",
            shuffle=True,
        )
        f["observations/image"].attrs["units"] = "counts"
        f["psf/kernel"] = rng.random((3, 3))
        f["axes/rows"] = np.array([10.0, 20.0, 30.0, 40.0])
        f["spare/unused"] = np.zeros(7)
        f["cube/band"] = np.array([1.0, 2.0])
        f["cube/pixel"] = np.array([0, 1, 2])
        f["cube/flux"] = rng.random((2, 3))
        f["cube/band"].make_scale("band")
        f["cube/pixel"].make_scale("pixel")
        f["cube/flux"].dims[0].attach_scale(f["cube/band"])
        f["cube/flux"].dims[1].attach_scale(f["cube/pixel"])
    return path


# ------------------------------------------------------------------------- the runner


class _Cancel:
    cancelled = False

    def check(self) -> None:
        return None


class _Ctx:
    """An in-process stand-in for the worker context (outputs kept in memory)."""

    def __init__(self, inputs: dict[str, Path]) -> None:
        self.inputs = inputs
        self.cancel = _Cancel()
        self.outputs: dict[str, bytes] = {}
        self.warnings: list[str] = []

    def has_input(self, name: str) -> bool:
        return name in self.inputs

    def input_names(self, prefix: str) -> list[str]:
        return sorted(n for n in self.inputs if n.startswith(prefix))

    def download(self, name: str, *, required: bool = True) -> Path | None:
        return self.inputs.get(name)

    def state(self, phase: RunnerPhase, message: str | None = None) -> None:
        return None

    def progress(self, *_a: Any, **_k: Any) -> None:
        return None

    def log(self, _m: str) -> None:
        return None

    def warning(self, code: str, message: str, untrusted_text: str | None = None) -> None:
        self.warnings.append(f"{code}: {message}")

    def write_output(self, name: str, data: bytes) -> Path:
        self.outputs[name] = data
        return Path(name)

    def write_json(self, name: str, obj: Any) -> Path:
        data = json.dumps(obj, sort_keys=True, allow_nan=False, default=str).encode()
        return self.write_output(name, data)

    def json(self, name: str) -> Any:
        return json.loads(self.outputs[name])


def _keep_sections(index: dict[str, Any]) -> dict[str, Any]:
    sections = [s for s in index["sections"] if s["section"] in KEEP_SECTIONS]
    for s in sections:
        s.pop("plots", None)  # recipe plots are left out (size)
    return {**index, "sections": sections}


def build_tree(tmp: Path) -> dict[str, Any]:
    paths = [tmp / "sales_2024.nc", tmp / "sales_2025.nc", tmp / "instrument.nc"]
    _year(2024).to_netcdf(paths[0], engine="h5netcdf")
    _year(2025).to_netcdf(paths[1], engine="h5netcdf")
    _instrument().to_netcdf(paths[2], engine="h5netcdf")
    assembly = IngestAssembly.model_validate(
        {"sources": [{**s, "upload_id": u} for s, u in zip(ASSEMBLY, UPLOADS, strict=True)]}
    )
    ctx = _Ctx({upload_input(i): p for i, p in enumerate(paths)})
    run_ingest(ctx, IngestJob(assembly=assembly, dataset="dataset://default/lens_cubes@v1"))  # type: ignore[arg-type]
    tree_schema = TreeSchema.model_validate_json(ctx.outputs["tree_schema.json"])
    root = DataSchema.model_validate_json(ctx.outputs["schema.json"])
    summary = ctx.json("summary.json")
    artifacts: dict[str, Any] = {
        name: ctx.json(name)
        for name in ("tree_schema.json", "summary.json", "profile.json", "preview.json")
    }
    artifacts["profile.json"]["sha256"] = SHA  # the zip's hash is not part of the contract
    nodes = ctx.json("ui/explore/nodes/index.json")
    artifacts["ui/explore/nodes/index.json"] = nodes
    artifacts["ui/explore/index.json"] = _keep_sections(ctx.json("ui/explore/index.json"))
    for name in [f"ui/explore/{s}.json" for s in KEEP_SECTIONS]:
        if name in ctx.outputs:
            artifacts[name] = ctx.json(name)
    for entry in nodes["nodes"].values():
        index = _keep_sections(ctx.json(entry["index"]))
        artifacts[entry["index"]] = index
        for s in index["sections"]:
            if (s.get("status") or "ok") == "ok":
                artifacts[s["view"]] = ctx.json(s["view"])
    version = DatasetVersion.model_validate(
        {
            "dataset_id": DS_ID,
            "tenant_id": "default",
            "project_id": "default",
            "version": 1,
            "ref": "dataset://default/lens_cubes@v1",
            "status": "ready",
            "sha256": SHA,
            "size_bytes": sum(p.stat().st_size for p in paths),
            "data_schema": root.model_dump(mode="json"),
            "summary": {k: v for k, v in summary.items() if k != "nodes"},
            "profile": {"name": "profile.json", "size": 2048, "content_type": "application/json"},
            "preview": {"name": "preview.json", "size": 2048, "content_type": "application/json"},
            "ingest_run_id": RUN_ID,
            "issues": [],
            "message": "Two years of sales and the instrument",
            "created_at": T0,
            "ready_at": T0,
            "layout": "tree",
            "tree_schema": tree_schema.model_dump(mode="json"),
            "sources": UPLOADS,
        }
    )
    dataset = Dataset.model_validate(
        {
            "id": DS_ID,
            "tenant_id": "default",
            "project_id": "default",
            "name": "lens_cubes",
            "ref": "dataset://default/lens_cubes",
            "description": "Weekly sales per year with the instrument tree (M4)",
            "latest_version": 1,
            "created_at": T0,
            "updated_at": T0,
        }
    )
    return {
        "dataset": dataset.model_dump(mode="json", exclude_none=True),
        "version": version.model_dump(mode="json", exclude_none=True),
        "artifacts": artifacts,
    }


def build_inspection(tmp: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = write_raw_hdf5(tmp / "lens_raw.h5")
    inspection = inspect_upload(path, InspectJob(format="hdf5"))
    mapping = IngestMapping.model_validate(HDF5_MAPPING)
    ctx = _Ctx({"upload": path})
    run_ingest(ctx, IngestJob(mapping=mapping))  # type: ignore[arg-type]
    schema = TreeSchema.model_validate_json(ctx.outputs["tree_schema.json"])
    assert set(schema.nodes) == {"/", "/psf"}, schema.nodes
    return (
        inspection.model_dump(mode="json"),
        mapping.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
    )


def build() -> str:
    with tempfile.TemporaryDirectory() as tmp, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        doc = build_tree(Path(tmp))
        inspection, mapping = build_inspection(Path(tmp))
    doc["inspection"] = inspection
    doc["hdf5_mapping"] = mapping
    text = json.dumps(doc, indent=None, separators=(",", ":"), sort_keys=True) + "\n"
    if len(text) > MAX_BYTES:
        raise SystemExit(f"tree.gen.json would be {len(text):,} bytes (max {MAX_BYTES:,})")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed file is stale")
    args = parser.parse_args()
    text = build()
    if args.check:
        if not OUT.exists() or OUT.read_text("utf-8") != text:
            print(f"{OUT.relative_to(WEB)} is stale: run this script", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(WEB)} ({len(text):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
