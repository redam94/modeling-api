"""v0.15: image loaders - deterministic order, shards, augmentation, samples, cache."""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.collection import (
    IMAGE_ATTR,
    ArrayImageLoader,
    Augment,
    Batch,
    Collection,
    CollectionLoader,
    ImageLoader,
    collection_read,
)
from mapi_sdk.testing.images import image_dataset, local_collection
from mapi_spec.collection import CollectionRead
from mapi_spec.errors import ErrorCode, MapiError

pytestmark = pytest.mark.unit

AUG = Augment(flip_x=True, rotate90=True, crop_pad=2, brightness=0.2, contrast=0.2, noise=0.05)


@pytest.fixture(scope="module")
def col(tmp_path_factory: pytest.TempPathFactory) -> Collection:
    # 3 classes x 30 images of 8 px in several packs: several chunks and windows
    return local_collection(
        tmp_path_factory.mktemp("c"), n_per_class=30, size=8, part_bytes=6000, rgb=True
    )


def _epoch(loader: ImageLoader, **kw: object) -> list[Batch]:
    return list(loader.batches(7, **kw))  # type: ignore[arg-type]


def _same(a: list[Batch], b: list[Batch]) -> bool:
    return len(a) == len(b) and all(
        np.array_equal(x.index, y.index) and np.array_equal(x.pixels, y.pixels)
        for x, y in zip(a, b, strict=True)
    )


class _Counting:
    def __init__(self, inner: object) -> None:
        self.inner = inner
        self.bytes = 0
        self.lock = threading.Lock()

    def read(self, pack: str, offset: int, size: int) -> bytes:
        with self.lock:
            self.bytes += size
        return self.inner.read(pack, offset, size)  # type: ignore[attr-defined]


def test_loader_basics(col: Collection) -> None:
    loader = col.loader()
    assert isinstance(loader, ImageLoader) and isinstance(loader, CollectionLoader)
    assert loader.n == 90 and loader.dim == "image" and loader.size == (8, 8)
    assert loader.channels == 3 and loader.classes == ["circle", "cross", "square"]
    batches = _epoch(loader)
    assert [len(b.index) for b in batches] == [7] * 12 + [6]
    assert np.concatenate([b.index for b in batches]).tolist() == list(range(90))
    b = batches[0]
    assert b.pixels.shape == (7, 8, 8, 3) and b.pixels.dtype == np.uint8
    assert b.labels is not None and b.codes is not None
    assert [loader.classes[c] for c in b.codes] == b.labels.tolist()
    assert _epoch(loader, drop_last=True)[-1].index.size == 7


def test_thread_count_never_changes_batches(col: Collection) -> None:
    kw = {"shuffle": True, "seed": 5, "epoch": 2, "augment": AUG}
    one = _epoch(col.loader(threads=1), prefetch=0, **kw)
    many = _epoch(col.loader(threads=8), prefetch=4, **kw)
    assert _same(one, many)


def test_seed_and_epoch_change_the_order(col: Collection) -> None:
    loader = col.loader()
    order = lambda **kw: np.concatenate([b.index for b in _epoch(loader, shuffle=True, **kw)])  # noqa: E731
    base = order(seed=1, epoch=0)
    assert sorted(base.tolist()) == list(range(90))
    assert np.array_equal(base, order(seed=1, epoch=0))
    assert not np.array_equal(base, order(seed=1, epoch=1))
    assert not np.array_equal(base, order(seed=2, epoch=0))
    assert not np.array_equal(base, np.arange(90))


def test_shards_cover_every_row_once(col: Collection) -> None:
    loader = col.loader()
    seen = [
        np.concatenate([b.index for b in _epoch(loader, shuffle=True, seed=3, shard=(i, 3))])
        for i in range(3)
    ]
    all_rows = np.concatenate(seen)
    assert sorted(all_rows.tolist()) == list(range(90))
    assert all(s.size > 0 for s in seen)
    with pytest.raises(ValueError):
        _epoch(loader, shard=(3, 3))


def test_augmentation_is_deterministic_per_position(col: Collection) -> None:
    loader = col.loader()
    plain = {int(i): p for b in _epoch(loader) for i, p in zip(b.index, b.pixels, strict=True)}
    aug_a = {
        int(i): p
        for b in _epoch(loader, augment=AUG, seed=4)
        for i, p in zip(b.index, b.pixels, strict=True)
    }
    shuffled = {
        int(i): p
        for b in _epoch(loader, augment=AUG, seed=4, shuffle=True)
        for i, p in zip(b.index, b.pixels, strict=True)
    }
    assert all(np.array_equal(aug_a[k], shuffled[k]) for k in aug_a)  # order-free
    assert any(not np.array_equal(aug_a[k], plain[k]) for k in aug_a)
    x = plain[0]
    rng = np.random.default_rng(0)
    assert AUG.apply(x, rng).shape == x.shape and AUG.apply(x, rng).dtype == np.uint8
    assert not Augment().active and AUG.active
    wide = np.zeros((4, 6, 1), dtype=np.uint8)
    assert Augment(rotate90=True).apply(wide, np.random.default_rng(1)).shape == wide.shape


def test_subset_split_and_settings(col: Collection) -> None:
    loader = col.loader()
    ids = loader.ids[[5, 1, 9]]
    sub = loader.subset(ids)
    assert sub.ids.tolist() == ids.tolist()
    assert loader.subset(np.array([5, 1, 9])).ids.tolist() == ids.tolist()
    mask = np.zeros(90, dtype=bool)
    mask[[1, 5]] = True
    assert loader.subset(mask).ids.tolist() == loader.ids[[1, 5]].tolist()
    with pytest.raises(MapiError, match="not in the loader"):
        loader.subset(["nope"])
    rest, held = loader.split(0.2, seed=0)
    assert rest.n + held.n == 90 and held.n == 18
    assert held.labels is not None
    assert {c: int((held.labels == c).sum()) for c in loader.classes} == {
        "circle": 6,
        "cross": 6,
        "square": 6,
    }
    assert not set(rest.ids.tolist()) & set(held.ids.tolist())
    small = loader.with_size((4, 6), "pad").with_mode("gray").with_classes(["square", "zzz"])
    b = next(iter(small.batches(4)))
    assert b.pixels.shape == (4, 4, 6, 1)
    assert b.codes is not None and set(b.codes.tolist()) <= {-1, 0}
    assert small.classes == ["square", "zzz"] and loader.classes == ["circle", "cross", "square"]


def test_manifest_of_a_loader(col: Collection) -> None:
    m = col.loader().subset(np.arange(4)).manifest()
    assert m.sizes == {"image": 4}
    assert "label" in m.data_vars and {"split", "sha256", "path"} <= set(m.coords)


def test_to_xarray_matches_the_image_layout_and_stratifies(col: Collection) -> None:
    loader = col.loader()
    ds = loader.to_xarray(30, seed=1)
    assert ds["pixels"].dims == ("image", "y", "x", "channel")
    assert ds["pixels"].dtype == np.uint8 and ds["pixels"].attrs == {IMAGE_ATTR: "1"}
    assert ds["channel"].values.tolist() == ["r", "g", "b"]
    assert ds["y"].values.tolist() == list(range(8))
    assert ds.sizes["image"] == 30
    labels = ds["label"].values.tolist()
    assert {c: labels.count(c) for c in set(labels)} == {"circle": 10, "cross": 10, "square": 10}
    pos = [loader.ids.tolist().index(i) for i in ds["image"].values]
    assert pos == sorted(pos)  # kept in loader order
    assert loader.to_xarray(30, seed=1).identical(ds)
    assert not loader.to_xarray(30, seed=2)["image"].equals(ds["image"])
    full = loader.to_xarray(size=(4, 4))
    assert full.sizes["image"] == 90 and full.sizes["y"] == 4
    ref = image_dataset(n_per_class=30, size=8, part_bytes=6000, rgb=True)
    assert set(ref.data_vars) == set(full.data_vars) and ref.sizes["image"] == 90


def test_to_xarray_refuses_too_many_values(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mapi_sdk.collection as mod

    monkeypatch.setattr(mod, "IMAGE_FOLDER_MAX_CELLS", 100)
    with pytest.raises(MapiError) as err:
        col.loader().to_xarray(10)
    assert err.value.code is ErrorCode.PAYLOAD_TOO_LARGE


def test_array_loader_parity(col: Collection) -> None:
    ds = col.loader().to_xarray()
    arr = ArrayImageLoader(ds)
    assert arr.ids.tolist() == col.loader().ids.tolist()
    assert arr.classes == col.loader().classes and arr.size == (8, 8) and arr.channels == 3
    kw = {"augment": AUG, "seed": 2, "epoch": 1}
    assert _same(_epoch(arr, **kw), _epoch(col.loader(), **kw))
    gray_a = next(iter(arr.with_mode("gray").with_size((5, 5)).batches(9)))
    gray_c = next(iter(col.loader().with_mode("gray").with_size((5, 5)).batches(9)))
    assert np.abs(gray_a.pixels.astype(int) - gray_c.pixels.astype(int)).max() <= 1
    shuffled = np.concatenate([b.index for b in _epoch(arr, shuffle=True, seed=1)])
    assert sorted(shuffled.tolist()) == list(range(90))


def test_array_loader_inputs() -> None:
    px = np.random.default_rng(0).random((3, 4, 5))
    ds = xr.Dataset({"img": (("item", "y", "x"), px)}, coords={"item": ["a", "b", "c"]})
    loader = ArrayImageLoader(ds, pixels="img", target=None, dim="item")
    assert loader.channels == 1 and loader.labels is None and loader.size == (4, 5)
    b = next(iter(loader.batches(3)))
    assert b.labels is None and b.codes is None
    np.testing.assert_array_equal(b.pixels[..., 0], np.rint(px * 255).astype(np.uint8))
    out = loader.to_xarray()
    assert out["img"].dims == ("item", "y", "x", "channel")
    with pytest.raises(MapiError, match=r"0\.\.255"):
        ArrayImageLoader(ds.assign(img=ds["img"] * 1000), pixels="img", dim="item")
    with pytest.raises(MapiError, match="no pixel variable"):
        ArrayImageLoader(ds)


def test_cache_dir_serves_later_epochs(col: Collection, tmp_path: Path) -> None:
    counting = _Counting(col.source)
    c = Collection(col.info, col.manifest, counting, col.schema)
    loader = c.loader(cache_dir=tmp_path / "cache", cache_bytes=10**8)
    first = _epoch(loader, shuffle=True, seed=1, epoch=0)
    read = counting.bytes
    assert read > 0
    second = _epoch(loader, shuffle=True, seed=1, epoch=1)
    assert counting.bytes == read  # epoch 1 read nothing from the source
    assert sorted(np.concatenate([b.index for b in second]).tolist()) == list(range(90))
    assert not _same(first, second)
    nocache = c.loader()
    _epoch(nocache, shuffle=True, seed=1, epoch=0)
    assert counting.bytes == 2 * read


def test_cache_budget_is_respected(col: Collection, tmp_path: Path) -> None:
    counting = _Counting(col.source)
    c = Collection(col.info, col.manifest, counting, col.schema)
    loader = c.loader(cache_dir=tmp_path / "small", cache_bytes=1)
    _epoch(loader, shuffle=True)
    _epoch(loader, shuffle=True)
    assert not any((tmp_path / "small").glob("*"))


def test_corrupt_bytes_fail_the_batch(col: Collection) -> None:
    class Flip:
        def read(self, pack: str, offset: int, size: int) -> bytes:
            data = bytearray(col.source.read(pack, offset, size))
            data[len(data) // 2] ^= 0xFF
            return bytes(data)

    bad = Collection(col.info, col.manifest, Flip(), col.schema)
    with pytest.raises(MapiError) as err:
        _epoch(bad.loader())
    assert err.value.code is ErrorCode.INTEGRITY_FAILURE


def test_collection_read(col: Collection) -> None:
    ds = collection_read(col, CollectionRead(max_samples=12, size=(4, 4), mode="gray"), seed=0)
    assert ds.sizes == {"image": 12, "y": 4, "x": 4, "channel": 1}
    default = collection_read(col, None, seed=3)
    assert default.sizes["image"] == 90 and default.sizes["y"] == 8
    seeded = collection_read(col, CollectionRead(max_samples=9, seed=7), seed=0)
    assert seeded["image"].equals(
        collection_read(col, CollectionRead(max_samples=9), seed=7)["image"]
    )
    info = col.info.model_copy(update={"size": None})
    sizeless = Collection(info, col.manifest, col.source, col.schema)
    with pytest.raises(MapiError) as err:
        collection_read(sizeless, None, seed=0)
    assert err.value.pointer == "/data/collection/size"
    with pytest.raises(MapiError, match="one size"):
        _ = sizeless.loader().size
