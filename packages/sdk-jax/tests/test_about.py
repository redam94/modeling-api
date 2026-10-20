"""Catalog entries: manifests, About cards, plots, capabilities and the step."""

from __future__ import annotations

import pytest
from mapi_jax.models import MLP, CNNClassifier, FinetuneClassifier
from mapi_jax.plugins import PLUGINS
from mapi_jax.steps import STEPS, EmbedStep
from mapi_sdk.registry import Registry
from mapi_spec.capabilities import Representation

IMAGE_PLOTS = [
    "ml.training_curves",
    "ml.confusion_epochs",
    "ml.misclassified",
    "core.roc_pr",
    "core.confusion",
]


def test_entry_points_register_everything(registry: Registry) -> None:
    catalog = registry.catalog()
    models = {m.id for m in catalog.models}
    assert {"dl.mlp", "dl.cnn_classifier", "dl.finetune_classifier"} <= models
    assert "dl.embed" in {s.id for s in catalog.steps}
    assert [MLP, CNNClassifier, FinetuneClassifier] == PLUGINS and [EmbedStep] == STEPS


@pytest.mark.parametrize("plugin", PLUGINS)
def test_manifest(plugin: type) -> None:
    m = plugin.manifest()
    assert m.version == "1.0.0" and m.about is not None
    caps = m.capabilities
    assert caps.inference == "custom" and caps.representation is Representation.DRAWS
    assert caps.score_modes == frozenset({"predict", "evaluate"})
    assert caps.loo_valid is False and "pointwise log likelihood" in (caps.loo_invalid_reason or "")
    assert caps.checkpointable and caps.deterministic_given_seed
    if plugin is MLP:
        assert caps.input == "dataset" and list(m.plots) == ["ml.training_curves"]
        assert m.resources.gpu is False
    else:
        assert caps.input == "collection" and list(m.plots) == IMAGE_PLOTS
        assert m.resources.gpu is True and m.resources.default_class.value == "M"
    assert m.side_table_params == (["pretrained"] if plugin is FinetuneClassifier else [])


def test_step_about() -> None:
    entry = EmbedStep.catalog_entry()
    assert entry.about is not None and entry.side_table_params == ["weights"]


def test_import_does_not_initialise_a_backend() -> None:
    import subprocess
    import sys

    code = (
        "import mapi_jax.plugins, mapi_jax.steps, jax\n"
        "from jax._src import xla_bridge as xb\n"
        "print(len(xb._backends))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "0"
