"""Network weights and optimiser state through the safetensors codec of mapi_sdk."""

from __future__ import annotations

import numpy as np
import optax
import pytest
from flax import nnx
from mapi_jax.checkpoint import optimizer_bytes, restore_optimizer, restore_weights, weights_bytes
from mapi_jax.nets import ArchSpec, build_network, network_tensors
from mapi_jax.testing import TINY_ARCH, tiny_pretrained, tiny_weights_dataset
from mapi_jax.weights import ARCH_METADATA, read_pretrained
from mapi_sdk.bundle import assert_no_pickle
from mapi_sdk.safetensors import read_safetensors, tensors_from_dataset
from mapi_spec.errors import MapiError


def test_weights_round_trip_and_no_pickle() -> None:
    net = build_network(TINY_ARCH, 3, seed=0)
    data = weights_bytes(net, {ARCH_METADATA: TINY_ARCH.model_dump_json()})
    assert_no_pickle({"weights.safetensors": data})
    tensors, meta = read_safetensors(data)
    assert set(tensors) == set(network_tensors(net))
    assert ArchSpec.model_validate_json(meta[ARCH_METADATA]) == TINY_ARCH
    other = build_network(TINY_ARCH, 3, seed=9)
    restore_weights(other, data)
    a, b = network_tensors(net), network_tensors(other)
    assert all(np.array_equal(a[k], b[k]) for k in a)
    with pytest.raises(MapiError, match="shape"):
        restore_weights(build_network(TINY_ARCH, 4, seed=0), data)
    with pytest.raises(MapiError, match="do not match"):
        restore_weights(build_network(ArchSpec(kind="mlp", in_features=2), 3, seed=0), data)


def test_safetensors_package_reads_our_bytes() -> None:
    st = pytest.importorskip("safetensors.numpy")
    data = tiny_pretrained()
    theirs = st.load(data)
    ours, _ = read_safetensors(data)
    assert set(theirs) == set(ours)
    for k in ours:
        np.testing.assert_array_equal(theirs[k], ours[k])


def test_optimizer_state_round_trip() -> None:
    net = build_network(ArchSpec(kind="mlp", in_features=3, hidden=[4]), 2, seed=0)
    opt = nnx.Optimizer(net, optax.adamw(1e-2), wrt=nnx.Param)
    data = optimizer_bytes(opt)  # uint32 step counters are stored as int64
    tensors, _ = read_safetensors(data)
    assert any(k.endswith("count") for k in tensors)
    fresh = nnx.Optimizer(net, optax.adamw(1e-2), wrt=nnx.Param)
    restore_optimizer(fresh, data)
    other = nnx.Optimizer(net, optax.sgd(1e-2), wrt=nnx.Param)
    with pytest.raises(MapiError, match="does not match"):
        restore_optimizer(other, data)


def test_tiny_weights_dataset_is_a_weights_side_table() -> None:
    ds = tiny_weights_dataset()
    tensors, doc = tensors_from_dataset(ds)
    assert doc["arch"] == TINY_ARCH.model_dump(mode="json")
    assert all(k.startswith("backbone.") for k in tensors)
    pre = read_pretrained(ds)
    assert pre.arch == TINY_ARCH and pre.sha256 == doc["sha256"]
    net = pre.network(4, seed=0)
    assert network_tensors(net)["head.kernel"].shape == (TINY_ARCH.features, 4)


def test_weights_without_an_architecture_are_refused() -> None:
    ds = tiny_weights_dataset()
    tensors, _ = tensors_from_dataset(ds)
    from mapi_sdk.safetensors import weights_dataset

    bare = weights_dataset(tensors)
    with pytest.raises(MapiError, match="no architecture"):
        read_pretrained(bare, name="w")
