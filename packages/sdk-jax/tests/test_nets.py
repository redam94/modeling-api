"""Networks: shapes, feature widths, tensor names, loading pretrained tensors."""

from __future__ import annotations

import jax
import jax.numpy as jnp
import numpy as np
import pytest
from mapi_jax.nets import ArchSpec, build_network, load_tensors, network_tensors, param_count
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import ValidationError

RESNET = ArchSpec(kind="resnet", channels=1, widths=[4, 8], blocks=[1, 1], groups=2)


def test_resnet_shapes_and_features() -> None:
    net = build_network(RESNET, 3, seed=0)
    x = jnp.ones((2, 8, 8, 1))
    assert net(x).shape == (2, 3)
    assert net.features(x).shape == (2, RESNET.features) == (2, 8)
    names = list(network_tensors(net))
    assert names == sorted(names)
    assert {"head.kernel", "head.bias", "backbone.stem.conv.kernel"} <= set(names)
    assert all(n.startswith(("backbone.", "head.")) for n in names)
    # the second stage downsamples with a projected skip
    assert "backbone.stages.1.0.proj.conv.kernel" in names
    assert param_count(net) == sum(v.size for v in network_tensors(net).values())


def test_mlp_shapes_and_dropout_key() -> None:
    arch = ArchSpec(kind="mlp", in_features=5, hidden=[8, 4], dropout=0.5)
    net = build_network(arch, 2, seed=0)
    x = jnp.ones((3, 5))
    assert net(x).shape == (3, 2) and net.features(x).shape == (3, 4)
    np.testing.assert_array_equal(net(x), net(x))  # no key: deterministic
    a = net(x, jax.random.key(1))
    b = net(x, jax.random.key(1))
    c = net(x, jax.random.key(2))
    np.testing.assert_array_equal(a, b)
    assert not np.allclose(a, c)


def test_same_seed_same_weights() -> None:
    a = network_tensors(build_network(RESNET, 3, seed=5))
    b = network_tensors(build_network(RESNET, 3, seed=5))
    c = network_tensors(build_network(RESNET, 3, seed=6))
    assert all(np.array_equal(a[k], b[k]) for k in a)
    assert not all(np.array_equal(a[k], c[k]) for k in a)


def test_load_tensors_copies_the_backbone_only() -> None:
    src = build_network(RESNET, 2, seed=0)
    dst = build_network(RESNET, 5, seed=1)
    loaded = load_tensors(dst, network_tensors(src))
    assert loaded and all(n.startswith("backbone.") for n in loaded)
    x = jnp.ones((1, 8, 8, 1))
    np.testing.assert_allclose(dst.features(x), src.features(x), rtol=1e-6)
    assert dst(x).shape == (1, 5)  # the head keeps its own shape


def test_load_tensors_refusals() -> None:
    net = build_network(RESNET, 2, seed=0)
    tensors = network_tensors(net)
    missing = {k: v for k, v in tensors.items() if "stem" not in k}
    with pytest.raises(MapiError, match="lack") as err:
        load_tensors(net, missing)
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    load_tensors(net, missing, strict=False)
    wrong = dict(tensors)
    wrong["backbone.stem.conv.kernel"] = np.zeros((1, 1, 1, 1), dtype=np.float32)
    with pytest.raises(MapiError, match="shape"):
        load_tensors(net, wrong)


def test_arch_validation() -> None:
    with pytest.raises(ValidationError):
        ArchSpec(kind="resnet", widths=[4, 8], blocks=[1])
    with pytest.raises(ValidationError):
        ArchSpec(kind="resnet", widths=[0])
    with pytest.raises(ValidationError):
        ArchSpec(kind="mlp", in_features=3, unknown=1)  # type: ignore[call-arg]
    assert ArchSpec(kind="mlp", in_features=3, hidden=[]).features == 3
    assert ArchSpec.model_validate_json(RESNET.model_dump_json()) == RESNET
