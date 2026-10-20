"""Epoch checkpoints: an interrupted fit resumes and ends with the uninterrupted weights."""

from __future__ import annotations

import pytest
import xarray as xr
from mapi_jax.models import CNNClassifier
from mapi_sdk.checkpoints import MemoryFileCheckpointSink, checkpoint_names
from mapi_sdk.errors import RunCancelled
from mapi_sdk.models import FitContext
from mapi_sdk.steps import CancelToken
from mapi_spec.spec import FitConfig

from .conftest import TINY_CNN

KEY = "0123456789abcdef0123456789abcdef"
SETTINGS = {
    **TINY_CNN,
    "train": {"epochs": 3, "batch_size": 6, "learning_rate": 0.02, "early_stopping": None},
    "uncertainty": {"method": "none"},
}
CFG = FitConfig.model_validate({"seed": 4, "checkpoint": {"every_epochs": 1}})


def _fit(ds: xr.Dataset, sink: MemoryFileCheckpointSink, **ctx_kw: object) -> bytes:
    model = CNNClassifier()
    s = model.parse_settings(SETTINGS)
    ctx = FitContext(seed=4, checkpoint_sink=sink, fit_key=KEY, **ctx_kw)  # type: ignore[arg-type]
    out = model.fit(ds, s, CFG, ctx)
    return out.native["weights.safetensors"]


def test_resume_matches_an_uninterrupted_fit(shapes: xr.Dataset) -> None:
    ds = shapes.where(shapes["split"] == "train", drop=True)
    ds["pixels"] = ds["pixels"].astype("uint8")
    full_sink = MemoryFileCheckpointSink()
    uninterrupted = _fit(ds, full_sink)
    assert full_sink.saves == 2  # after epochs 1 and 2 (not after the last one)

    sink = MemoryFileCheckpointSink()
    token = CancelToken()

    def progress(fraction: float, message: str) -> None:
        if sink.saves >= 1:
            token.cancel("interrupted")

    with pytest.raises(RunCancelled):
        _fit(ds, sink, cancel=token, on_progress=progress)
    saved = sink.checkpoints[KEY]
    assert saved.progress == 1 and set(saved.files) == {"weights", "opt"}
    assert saved.meta["epoch"] == 1
    lines: list[str] = []
    resumed = _fit(ds, sink, log=lines.append)
    assert any("Resumed from epoch 1." in line for line in lines)
    assert resumed == uninterrupted
    assert set(checkpoint_names(KEY)) == {"weights", "opt", "meta"}
