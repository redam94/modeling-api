"""Tree-dataset fixtures (M4), built programmatically in temp dirs: NetCDF files with and
without groups, raw HDF5 files, and an in-process worker context for ingest/inspect runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.run import RunnerPhase

WEEKS = pd.date_range("2024-01-01", periods=8, freq="W-MON")


def root_dataset() -> xr.Dataset:
    rng = np.random.default_rng(0)
    return xr.Dataset(
        {
            "sales": (("time", "geo"), 100 + rng.normal(size=(8, 2))),
            "tv": (("time", "geo"), rng.gamma(2.0, 10.0, size=(8, 2))),
        },
        coords={"time": WEEKS, "geo": ["de", "fr"]},
    )


def tree_dict() -> dict[str, xr.Dataset]:
    rng = np.random.default_rng(1)
    return {
        "/": root_dataset(),
        "/instrument": xr.Dataset({"temp": (("time",), np.arange(8.0))}),
        "/instrument/psf": xr.Dataset(
            {"kernel": (("prow", "pcol"), rng.random((3, 3)))},
            coords={"prow": [0, 1, 2], "pcol": [0, 1, 2]},
        ),
    }


def write_netcdf_tree(path: Path) -> Path:
    xr.DataTree.from_dict(tree_dict()).to_netcdf(path, engine="h5netcdf")
    return path


def write_netcdf_root(path: Path, *, engine: str = "h5netcdf") -> Path:
    root_dataset().to_netcdf(path, engine=engine)  # type: ignore[call-overload]
    return path


def write_raw_hdf5(path: Path) -> Path:
    """A raw h5py file (no dim metadata): an image, a PSF, a date axis and a spare array."""
    rng = np.random.default_rng(2)
    with h5py.File(path, "w") as f:
        f["observations/image"] = rng.random((4, 5))
        f["observations/flux"] = rng.random(6)
        f["psf/kernel"] = rng.random((3, 3))
        f["axes/rows"] = np.array([10.0, 20.0, 30.0, 40.0])
        f["axes/labels"] = np.array(["a", "b", "c"], dtype=h5py.string_dtype())
        f["spare/unused"] = np.zeros(7)
    return path


class _Cancel:
    cancelled = False

    def check(self) -> None:
        return None


class IngestCtx:
    """An in-process stand-in for :class:`~mapi_runner.worker.context.WorkerContext`."""

    def __init__(self, root: Path, inputs: dict[str, Path]) -> None:
        self.root = root
        self.inputs = inputs
        self.cancel = _Cancel()
        self.warnings: list[tuple[str, str, str | None]] = []
        self.outputs: dict[str, bytes] = {}

    # inputs
    def has_input(self, name: str) -> bool:
        return name in self.inputs

    def input_names(self, prefix: str) -> list[str]:
        return sorted(n for n in self.inputs if n.startswith(prefix))

    def download(self, name: str, *, required: bool = True) -> Path | None:
        return self.inputs.get(name)

    # events
    def state(self, phase: RunnerPhase, message: str | None = None) -> None:
        return None

    def progress(self, *_a: Any, **_k: Any) -> None:
        return None

    def log(self, _m: str) -> None:
        return None

    def warning(self, code: str, message: str, untrusted_text: str | None = None) -> None:
        self.warnings.append((code, message, untrusted_text))

    # outputs
    def write_output(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.outputs[name] = data
        return path

    def write_json(self, name: str, obj: Any) -> Path:
        data = json.dumps(obj, indent=2, sort_keys=True, allow_nan=False, default=str).encode()
        return self.write_output(name, data)

    def json(self, name: str) -> Any:
        return json.loads(self.outputs[name])
