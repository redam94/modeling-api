"""v0.15: file checkpoint names and the in-memory sink."""

from __future__ import annotations

import re

import pytest
from mapi_sdk.checkpoints import (
    CHECKPOINT_KEY_RE,
    FileCheckpoint,
    FileCheckpointSink,
    MemoryFileCheckpointSink,
    checkpoint_names,
)
from mapi_spec.errors import MapiError

pytestmark = pytest.mark.unit

KEY = "0123456789abcdef0123456789abcdef"


def test_names_match_the_channel_pattern() -> None:
    names = checkpoint_names(KEY)
    assert names == {
        "weights": f"ckpt-{KEY}.weights.safetensors",
        "opt": f"ckpt-{KEY}.opt.safetensors",
        "meta": f"ckpt-{KEY}.meta.json",
    }
    channel = re.compile(
        r"^ckpt-[0-9a-f]{16,64}\.(?:meta\.json|draws\.zarr\.zip|weights\.safetensors|opt\.safetensors)$"
    )
    assert all(channel.match(n) for n in names.values())


@pytest.mark.parametrize("key", ["", "ABCDEF0123456789", "0" * 15, "0" * 65, "../etc/passwd00"])
def test_bad_keys_are_refused(key: str) -> None:
    assert not re.match(CHECKPOINT_KEY_RE, key)
    with pytest.raises(MapiError, match="checkpoint key"):
        checkpoint_names(key)


def test_memory_sink_keeps_the_newest_checkpoint_per_key() -> None:
    sink = MemoryFileCheckpointSink()
    assert isinstance(sink, FileCheckpointSink)
    assert sink.load_files(KEY) is None
    sink.save_files(KEY, {"weights": b"w1"}, {"epoch": 1}, 1)
    sink.save_files(KEY, {"weights": b"w2", "opt": b"o2"}, {"epoch": 2, "best": [0.5]}, 2)
    got = sink.load_files(KEY)
    assert got == FileCheckpoint(
        KEY, {"weights": b"w2", "opt": b"o2"}, {"epoch": 2, "best": [0.5]}, 2
    )
    assert sink.saves == 2
    assert sink.load_files("f" * 32) is None


@pytest.mark.parametrize(
    ("files", "meta", "progress", "message"),
    [
        ({"model": b"x"}, {}, 1, "only the files"),
        ({"weights": "text"}, {}, 1, "bytes"),
        ({"weights": b"x"}, {}, -1, "non-negative"),
        ({"weights": b"x"}, {"bad": float("nan")}, 1, "JSON"),
    ],
)
def test_malformed_checkpoints_are_refused(
    files: dict[str, object], meta: dict[str, object], progress: int, message: str
) -> None:
    with pytest.raises(MapiError, match=message):
        MemoryFileCheckpointSink().save_files(KEY, files, meta, progress)  # type: ignore[arg-type]
