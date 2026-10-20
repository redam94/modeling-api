# mapi-jax

Deep-learning models for modeling-api on [JAX](https://github.com/jax-ml/jax),
[flax](https://github.com/google/flax) (nnx) and [optax](https://github.com/google-deepmind/optax).
Apache-2.0.

| Plugin | Kind | Input | What it does |
|---|---|---|---|
| `dl.mlp` | model | table (`settings.rows`) | Multilayer perceptron: regression or classification |
| `dl.cnn_classifier` | model | images or an image collection | Small GroupNorm ResNet trained from scratch |
| `dl.finetune_classifier` | model | images or an image collection | Pretrained ResNet backbone (a weights side table) with a new head |
| `dl.embed` | step | images | Features from a frozen pretrained backbone, for a sklearn or Bayesian head |

All models give draws (`chain = 1`, `draw`): a last-layer Laplace approximation (default),
MC dropout, or one deterministic draw (`uncertainty.method`). They stream training and
validation metrics (label `split`), log validation images and confusion matrices, stop early,
save and resume epoch checkpoints (`fit.checkpoint.every_epochs`) and give the same weights
for the same seed on the same backend.

Image models implement both `fit` (an image dataset: `pixels(image, y, x, channel)`) and
`fit_batches` (a lazy, seeded loader over an image collection). Pretrained weights are a
dataset version made by a `safetensors` ingest, named in `data.side_tables` (sandboxes
have no network access).

## Trainer API

```python
from mapi_jax import ArchSpec, ClassificationTask, TrainConfig, Trainer, build_network

net = build_network(ArchSpec(kind="resnet", channels=1, widths=[16, 32]), n_out=3, seed=0)
result = Trainer(
    net,
    ClassificationTask(["a", "b", "c"]),
    TrainConfig(epochs=10),
    ctx,
    seed=0,
    preprocess=lambda px: px / 255.0,
).fit(train_loader, val_loader)
result.history  # the bundle's "training" group
result.misclassified  # the bundle's "misclassified" group (images, with validation data)
```

## Install

The workspace installs the CPU build of jax. The `cuda12` extra adds the CUDA 12 wheels
(only the `base-dl` runner image installs it; the GPU path is not tested yet):

```bash
pip install "mapi-jax[cuda12]"
```

See `docs/guides/deep-learning.md`.
