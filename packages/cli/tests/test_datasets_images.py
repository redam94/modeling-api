"""v0.15 on the command line: ``mapi datasets upload-images``, ``missing-images`` and
``upload-weights`` (the client's dataset calls are replaced by a fake that records what the
CLI asks for), and ``GPU`` among the ``--resource-class`` choices."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mapi_client import Client
from mapi_spec.api import BlobMissing, DatasetVersion
from mapi_spec.collection import DEFAULT_PART_BYTES
from typer.testing import Result

Invoke = Callable[..., Result]
Ex = Callable[..., dict[str, Any]]

URL = "http://api.test"
DS = "ds_01jaf3a1b2c3d4e5f6g7h8j9k0"


def _folder(root: Path) -> Path:
    for c, label in enumerate(("cat", "dog")):
        (root / label).mkdir(parents=True)
        for i in range(2):
            (root / label / f"{i}.png").write_bytes(b"\x89PNG" + bytes([c, i]) * 8)
    return root


class FakeDatasets:
    """Records the calls of the CLI; answers with schema examples."""

    def __init__(self, example: Ex) -> None:
        self.example = example
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _version(self) -> DatasetVersion:
        return DatasetVersion.model_validate(self.example(DatasetVersion, 2))

    def upload_collection(self, *args: Any, **kw: Any) -> DatasetVersion:
        self.calls.append(("upload_collection", args, kw))
        progress = kw["progress"]
        progress("hash", 4, 4)
        progress("check", 4, 4)
        progress("upload", 10, 20)
        progress("create", 0, 1)
        return self._version()

    def missing_images(self, *args: Any, **kw: Any) -> BlobMissing:
        self.calls.append(("missing_images", args, kw))
        shas = args[1]
        return BlobMissing(base_version=3, missing=shas[:1], present=len(shas) - 1)

    def upload_weights(self, *args: Any, **kw: Any) -> DatasetVersion:
        self.calls.append(("upload_weights", args, kw))
        return DatasetVersion.model_validate(self.example(DatasetVersion))

    def find(self, name: str) -> Any:
        return type("D", (), {"id": DS})()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, example: Ex) -> FakeDatasets:
    datasets = FakeDatasets(example)
    original = Client.__init__

    def init(self: Client, *args: Any, **kw: Any) -> None:
        original(self, *args, **kw)
        self.datasets = datasets  # type: ignore[assignment]

    monkeypatch.setattr(Client, "__init__", init)
    return datasets


def test_upload_images_passes_the_options(mapi: Invoke, fake: FakeDatasets, tmp_path: Path) -> None:
    root = _folder(tmp_path / "pets")
    state = tmp_path / "state.json"
    res = mapi(
        "--url", URL, "datasets", "upload-images", "pets", str(root),
        "--label-from-folder", "--split-column", "fold", "--size", "32x48",
        "--resize", "pad", "--mode", "rgb", "--no-base", "--part-size", "64MiB",
        "--state", str(state), "--wait", "-m", "first",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    ((name, args, kw),) = fake.calls
    assert name == "upload_collection" and args == ("pets", root)
    opts = kw["options"]
    assert (opts.split_column, opts.size, opts.resize, opts.mode) == (
        "fold",
        (32, 48),
        "pad",
        "rgb",
    )
    assert kw["label_from_folder"] is True and kw["base_version"] == 0
    assert kw["part_bytes"] == 64 * 1024**2 and kw["state_file"] == state
    assert kw["wait"] is True and kw["message"] == "first"
    assert "calculated the sha256 of 4 images" in res.output
    assert "uploading parts: 10 of 20 bytes" in res.output
    assert "images" in res.stdout and "classes" in res.stdout  # the collection summary


def test_upload_images_defaults_and_refusals(
    mapi: Invoke, fake: FakeDatasets, tmp_path: Path
) -> None:
    root = _folder(tmp_path / "pets")
    res = mapi("--url", URL, "datasets", "upload-images", DS, str(root), "--base-version", "2")
    assert res.exit_code == 0, res.output
    kw = fake.calls[-1][2]
    assert kw["base_version"] == 2 and kw["part_bytes"] == DEFAULT_PART_BYTES
    assert kw["options"].size is None and kw["wait"] is False
    for bad in (
        ["--base-version", "2", "--no-base"],
        ["--size", "big"],
        ["--label-from-folder", "--label-column", "c"],
    ):
        res = mapi("--url", URL, "datasets", "upload-images", DS, str(root), *bad)
        assert res.exit_code != 0


def test_missing_images_counts_without_uploading(
    mapi: Invoke, fake: FakeDatasets, tmp_path: Path
) -> None:
    root = _folder(tmp_path / "pets")
    res = mapi("--url", URL, "datasets", "missing-images", "pets", str(root))
    assert res.exit_code == 0, res.output
    ((name, args, kw),) = fake.calls
    assert name == "missing_images" and args[0] == DS and len(args[1]) == 4
    assert kw == {"base_version": None}
    assert "missing" in res.stdout and "present" in res.stdout
    res = mapi("--url", URL, "--json", "datasets", "missing-images", DS, str(root), "--no-base")
    assert res.exit_code == 0, res.output
    assert '"missing": 1' in res.stdout and '"present": 3' in res.stdout
    assert fake.calls[-1][2] == {"base_version": 0}


def test_upload_weights(mapi: Invoke, fake: FakeDatasets, tmp_path: Path) -> None:
    weights = tmp_path / "net.safetensors"
    weights.write_bytes(b"\x08" + bytes(7) + b"{}      ")
    arch = tmp_path / "arch.json"
    arch.write_text('{"kind": "resnet"}')
    res = mapi(
        "--url", URL, "datasets", "upload-weights", "backbone", str(weights), "--arch", str(arch)
    )
    assert res.exit_code == 0, res.output
    ((name, args, kw),) = fake.calls
    assert name == "upload_weights" and args == (weights,)
    assert kw["dataset"] == "backbone" and kw["dataset_id"] is None and kw["arch"] == arch


@pytest.mark.parametrize(
    "command", [["runs", "retry", "--help"], ["fit", "--help"], ["workflows", "run", "--help"]]
)
def test_resource_class_help_names_gpu(mapi: Invoke, command: list[str]) -> None:
    res = mapi(*command)
    assert res.exit_code == 0
    assert "GPU" in res.stdout
