---
description: Train neural networks on tables and images with the dl models of mapi-jax, read the training curves and the wrong images, stop early, resume from checkpoints, get draws, use the GPU class, and put pretrained image features in front of a scikit-learn or Bayesian head.
---

# Deep learning <span class="mapi-badge">v0.15</span>

The `mapi-jax` package adds neural networks to the platform. The networks use
[JAX](https://jax.readthedocs.io/), [Flax](https://flax.readthedocs.io/) (the `nnx` API) and
[Optax](https://optax.readthedocs.io/). They train in the same sandboxed runs as the other
models. They give draws, so the scorecard, the plots and the comparisons work as for a
Bayesian model.

| Model or step | Use it for |
|---|---|
| `dl.mlp` | A value or a class for each row of a table |
| `dl.cnn_classifier` | The class of each image, trained from the start |
| `dl.finetune_classifier` | The class of each image, from a pretrained network |
| `dl.embed` (step) | Image features from a pretrained network, for a scikit-learn or a Bayesian head |

| See also | |
|---|---|
| [Image data](image-data.md#large-image-sets-collections) | Image collections: large image sets for the networks |
| [Track metrics, params and tags](tracking.md) | The metrics and media that the training logs |
| [Spec format](../reference/spec-format.md#checkpoints) | `fit.checkpoint.every_epochs`, `data.collection`, the GPU class |
| [Model catalog](../reference/models.md#deep-learning-mapi-jax) | The settings of each model |

## Choose a model

Use the table to select a model:

| Your data | Labels | Model |
|---|---|---|
| A table with thousands of rows | A number or a class | `dl.mlp` |
| Up to a few thousand small images | A class | `tabular.image_logistic`, or `dl.embed` and a head |
| Thousands of images or more | A class | `dl.cnn_classifier` |
| Images, and a network that learned on similar images | A class | `dl.finetune_classifier` |

For a few hundred rows, a neural network is not better than a regression or a tree model.
See [Choosing a model](choosing-a-model.md).

## Train an image classifier

1. Upload the images as an [image collection](image-data.md#large-image-sets-collections),
   or as an [image folder](image-data.md) for a few thousand small images.
2. Write a spec with `dl.cnn_classifier`:

    ```yaml
    apiVersion: mapi/v1
    kind: ModelSpec
    metadata: {name: products, project: default}
    data:
      dataset: dataset://default/product_photos@v5
      split: {kind: column, dim: image, column: split, holdout: [test]}
    model:
      use: dl.cnn_classifier@1
      settings:
        image_size: [64, 64]
        widths: [32, 64, 128]
        blocks: [2, 2, 2]
        augment: {flip_x: true, crop_pad: 4, brightness: 0.1}
        train:
          epochs: 30
          batch_size: 128
          learning_rate: 0.001
          early_stopping: {monitor: val_loss, patience: 5}
      fit:
        seed: 7
        checkpoint: {every_epochs: 2}
    evaluate:
      metrics: [accuracy, accuracy_naive, log_score, brier]
    execution: {resource_class: GPU, timeout: 6h}
    ```

3. Submit the spec. Open the run and look at the **Metrics** tab while the network trains.

A collection dataset takes no pipeline steps. Put augmentation in the `augment` setting of
the model. The split of a collection must be a row split: `column`, `stratified` or
`leave_group_out`.

### Settings of the image models

| Setting | Default | Meaning |
|---|---|---|
| `pixels`, `target`, `image_dim` | `pixels`, `label`, `image` | The names in the dataset |
| `image_size` | the size of the collection | The height and width that the network reads. Necessary when the images have different sizes and the collection has no `size`. |
| `mode` | `auto` | `gray`, `rgb` or `auto` (the mode of the collection) |
| `widths` | `[16, 32, 64]` | The channels of each stage of the network |
| `blocks` | `[1, 1, 1]` | The residual blocks of each stage |
| `groups` | 8 | The maximum number of GroupNorm groups |
| `stem_stride` | 1 | The stride of the first convolution (1 or 2). Use 2 for large images. |
| `dropout` | 0.1 | The dropout rate before the last layer |
| `normalize` | `unit` | `unit` (pixels / 255) or `standardize` (the mean and the standard deviation of the training images) |
| `augment` | none | The random changes to the training images (see below) |
| `train` | see below | The training settings |
| `uncertainty` | Laplace | How the model makes draws (see [Draws](#draws)) |
| `class_order` | sorted labels | The classes in a fixed sequence |

`augment` changes each training image at random. The validation and the scored images do
not change.

| Field | Default | Change |
|---|---|---|
| `flip_x`, `flip_y` | `false` | Turn the image left to right, or top to bottom |
| `rotate90` | `false` | Turn the image by 0, 90, 180 or 270 degrees |
| `crop_pad` | 0 | Add this number of edge pixels, then cut a random part of the original size (0 to 64) |
| `brightness`, `contrast` | 0 | The maximum change (0 to 1) |
| `noise` | 0 | The standard deviation of random noise, in units of 0 to 1 (a maximum of 0.5) |

### Training settings

All `dl` models use the same `train` block:

| Field | Default | Meaning |
|---|---|---|
| `epochs` | 20 | The maximum number of passes through the training data (1 to 10 000) |
| `batch_size` | 64 | Rows or images in each step |
| `optimizer` | `adamw` | `adamw`, `adam` or `sgd` |
| `learning_rate` | 0.001 | The step size |
| `weight_decay` | 0.0001 | The weight decay of `adamw` |
| `schedule` | `warmup_cosine` | `constant`, `cosine` or `warmup_cosine` |
| `warmup_epochs` | 1.0 | The epochs that increase the learning rate from zero |
| `grad_clip` | 1.0 | The maximum norm of the gradient (none: no limit) |
| `label_smoothing` | 0 | Classification only |
| `val_fraction` | 0.1 | The share of the training rows for validation, stratified by class |
| `val_split_value` | `val` | If the data has a `split` coordinate, the rows with this value are the validation data |
| `early_stopping` | on | See [Stop early](#stop-early) |
| `log_every_steps` | automatic | How often the training logs the batch metrics (about 1000 points for each run) |
| `eval_every_epochs` | 1 | How often the training calculates the validation metrics |
| `media_every_epochs` | automatic | How often the training logs images (a maximum of 20 times) |
| `max_steps` | none | Stop after this number of steps |

!!! note
    The validation data is a part of the training rows of the spec split. The holdout of
    `data.split` stays for the evaluation of the scorecard. The network never sees it
    during the training.

## Read the training

The training logs metrics with the label `split`. The **Metrics** tab of the run shows one
line for each split:

| Metric | Labels | When |
|---|---|---|
| `loss` | `split=train` | Every `log_every_steps` steps (the batch value) |
| `loss` | `split=val` | At the end of each evaluation epoch |
| `accuracy` (classification), `rmse` and `mae` (regression) | `split=train`, `split=val` | As `loss` |
| `lr`, `grad_norm` | none | With the batch metrics |
| `epoch` | none | At the end of each epoch |

At the end of each epoch, the **Logs** tab shows one line with the fields `epoch`,
`train_loss`, `val_loss` and `images_per_second`. The speed is not a metric, because it
changes from one fit to a different fit with the same seed.

The run also logs the params `dl.arch.*`, `dl.train.*`, `dl.backend` (`cpu` or `gpu`),
`dl.device` and `dl.parameters` (the number of weights).

The **Media** tab shows two images at a maximum of 20 epochs:

- `val_predictions`: up to 16 validation images with the true class and the predicted class.
- `confusion`: the confusion matrix of the validation data.

The fit adds three plots to the run:

| Plot | Shows |
|---|---|
| `ml.training_curves` | The loss and the accuracy (or the error) for the training data and the validation data. The caption gives the best validation value and tells you when the network starts to overfit. |
| `ml.confusion_epochs` | The confusion matrix at up to 6 epochs: the first, some epochs between, the best and the last |
| `ml.misclassified` | Up to 24 validation images that the network classifies incorrectly, from the best epoch |

Read [Plots](plots.md#deep-learning) for the settings of these plots.

### Stop early

Early stopping stops the training when the validation metric does not become better. The
fit then keeps the weights of the best epoch.

| Field | Default | Meaning |
|---|---|---|
| `monitor` | `val_loss` | `val_loss`, `val_accuracy` or `val_rmse` |
| `patience` | 5 | The number of evaluations without a better value before the training stops |
| `min_delta` | 0 | The smallest change that counts as better |
| `restore_best` | `true` | Keep the weights of the best epoch, not of the last epoch |

To turn early stopping off, set `early_stopping: null`.

## Checkpoints and resume

A long training can save a checkpoint every N epochs. If the run stops (for example, the
executor stops the machine), you can resume the run. The new attempt starts from the epoch
after the last checkpoint.

1. Set `fit.checkpoint.every_epochs` in the spec:

    ```yaml
    model:
      fit:
        checkpoint: {every_epochs: 2}
    ```

2. If a run stops with the state `partial`, open the run and click **Resume**. From Python,
   call `api.runs.resume(run_id)`.

A checkpoint holds the weights, the state of the optimizer, the epoch, the early-stopping
state and the history of the metrics. The log of the new attempt shows "Resumed from epoch
N." A resumed fit gives the same weights as a fit that did not stop.

Samplers use `checkpoint.every_draws`. A checkpoint has `every_draws` or `every_epochs`, not
both. If the model cannot save checkpoints, validation shows a warning and the run ignores
the setting.

## Draws

Every `dl` model gives draws with the dims `(chain, draw)` (one chain). The `uncertainty`
setting selects how:

| `method` | Draws |
|---|---|
| `laplace` (default) | A Laplace approximation of the last layer. The other weights stay at their trained values. |
| `mc_dropout` | Passes through the network with dropout on. Each pass is one draw. |
| `none` | One draw: the trained network |

| Field | Default | Meaning |
|---|---|---|
| `draws` | `fit.draws`, else 100 | The number of draws (1 to 1000) |
| `hessian` | `auto` | `full` or `diag`. `auto` uses `full` when (features + 1) × outputs ≤ 2048. |
| `max_rows` | 20 000 | The training rows for the Laplace approximation |
| `prior_precision` | automatic | The prior precision of the last layer. Automatic: the value with the largest evidence on a grid from 0.0001 to 10 000. |

The draws show how sure the network is about its last layer. They do not include the
uncertainty of the other weights. A network trained by gradient descent has no pointwise
likelihood, so `elpd_loo` is not valid for the `dl` models.

## Use the GPU class

The resource class `GPU` gives a run one NVIDIA L4 GPU, 8 vCPU and 32 GiB of memory. Use it
for `dl.cnn_classifier` and `dl.finetune_classifier` with thousands of images.

```yaml
execution: {resource_class: GPU, timeout: 6h}
```

- Validation refuses the GPU class when the model does not use a GPU. For example,
  `dl.embed` in front of a `sklearn.*` model runs on a CPU class, because the model of the
  spec sets the image and the class.
- Validation shows a note when the model can use the GPU but the spec selects a CPU class.
- The GPU class is not in the retry ladder. After `RESOURCE_EXHAUSTED`, the platform offers
  the next CPU class only.
- Quotas: the public and verified tiers stop at class `S` and class `L`. Only the internal
  and the self-hosted tiers can use the GPU class by default. An administrator can change
  the quota of a tenant.

When no GPU is available, the run uses the CPU. This occurs in local development, with the
local-process executor, and with the Docker executor when `MAPI_DOCKER_GPUS` is not `true`.
The run then shows the warning `GPU_UNAVAILABLE`: "The run asked for the GPU class, but no
GPU is visible. It runs on the CPU." The param `dl.backend` tells you what the run used.

!!! warning
    The GPU path is not tested. The project has no GPU and no GCP project to test it. The
    `base-dl` image and the Cloud Run GPU job are written to the documentation of Docker,
    NVIDIA and Cloud Run, but they are not verified. Do a test with a small run before you
    use them. See [Cloud Run](../operations/cloud-run.md#gpu-jobs).

## Pretrained weights

A sandbox has no internet access. Thus a network cannot download pretrained weights during
a run. Upload the weights as a dataset first. Then give the dataset to the model as a side
table.

1. Save the weights as a [safetensors](https://huggingface.co/docs/safetensors) file. The
   tensor names and shapes must match the `mapi-jax` ResNet layout (`backbone.*`). Put the
   architecture in the metadata `mapi_arch` of the file, or give it with `--arch`.
2. Upload the file:

    ```bash
    mapi datasets upload-weights tiny-resnet resnet.safetensors --arch arch.json
    ```

    From Python, use the mapping `{"format": "safetensors"}`:

    ```python
    from mapi_spec import IngestMapping, WeightsOptions

    mapping = IngestMapping(format="safetensors", weights=WeightsOptions())
    version = api.datasets.upload("resnet.safetensors", dataset="tiny-resnet", mapping=mapping)
    ```

3. Name the dataset as a side table in the spec:

    ```yaml
    data:
      dataset: dataset://default/leaf_photos@v2
      side_tables:
        pretrained: {ref: dataset://default/tiny-resnet@v1, score_policy: pinned}
      split: {kind: stratified, dim: image, by: label, fraction: 0.2, seed: 1}
    model:
      use: dl.finetune_classifier@1
      settings:
        pretrained: pretrained
        freeze: none
        head_epochs: 1
        backbone_lr_scale: 0.1
        train: {epochs: 10, learning_rate: 0.0005}
    ```

The weights dataset has one variable `weights(param)` (float32) and the tensor table in its
attributes. The ingest reads the file in a sandbox. It refuses a header of more than 8 MiB,
tensors that overlap, and more parameters than `max_parameters` (200 million by default).

| Setting of `dl.finetune_classifier` | Default | Meaning |
|---|---|---|
| `pretrained` | `pretrained` | The name of the side table |
| `freeze` | `none` | `none` trains all weights. `backbone` trains only the last layer. |
| `head_epochs` | 1 | The epochs that train only the last layer before all weights train |
| `backbone_lr_scale` | 0.1 | The learning rate of the backbone, as a fraction of `learning_rate` |

Gray images in a network for RGB images use the gray channel three times. A side table that
only the model reads is not necessary when you score: the model version keeps the trained
weights.

!!! note
    There is no conversion from PyTorch or TensorFlow checkpoints. The weights must use the
    `mapi-jax` ResNet layout. `mapi_jax.testing.tiny_pretrained()` writes a small example
    file.

## Image features and a simple head

The step `dl.embed` changes each image into a short list of numbers (an embedding) with a
frozen pretrained network. A simple model after the step then learns the classes. This is
fast, and it works with a few hundred labelled images.

With a scikit-learn head:

```yaml
data:
  dataset: dataset://default/leaf_photos@v2
  side_tables:
    backbone: {ref: dataset://default/tiny-resnet@v1, score_policy: pinned}
  split: {kind: stratified, dim: image, by: label, fraction: 0.25, seed: 3}
pipeline:
  - id: embed
    use: dl.embed@^1
    with: {weights: backbone}
model:
  use: sklearn.multiclass_classifier@1
  settings:
    estimator: {kind: sklearn.LogisticRegression}
    target: label
    rows: [image]
    time_kind: none
```

With a Bayesian head, use `tabular.image_logistic` with the features input:

```yaml
model:
  use: tabular.image_logistic@^1.1
  settings: {input: features, pixels: embedding}
```

| Setting of `dl.embed` | Default | Meaning |
|---|---|---|
| `pixels` | `pixels` | The pixel variable |
| `weights` | `weights` | The name of the side table with the weights |
| `out` | `embedding` | The name of the new variable `embedding(image, feature)` |
| `feature_dim` | `feature` | The name of the feature dimension |
| `image_dim` | `image` | The image dimension |
| `size` | the network size | The height and width that the network reads |
| `batch_size` | 64 | Images in each batch |
| `keep_pixels` | `false` | Keep the pixel variable after the step |

The step runs in the image of the head model, on the CPU. A collection dataset takes no
pipeline steps. For a collection, set `data.collection` to read a sample of the images, or
use `dl.finetune_classifier` with `freeze: backbone` for all images.

## Tables: `dl.mlp`

`dl.mlp` is a small neural network for tables. It reads number variables along the row dims
of the target. It standardises them with the mean and the standard deviation of the training
rows.

```yaml
model:
  use: dl.mlp@1
  settings:
    target: delivery_hours
    rows: [order]
    features: [distance_km, weight_kg, hour]
    task: regression
    hidden: [64, 64]
    train: {epochs: 50, batch_size: 256}
```

| Setting | Default | Meaning |
|---|---|---|
| `target` | `y` | The target variable |
| `rows` | necessary | The dims of one row (1 to 8), for example `[order]` or `[store, week]` |
| `target_dtype` | `float64` | The type of the target: `float64` (numbers), `str` or `int64` (class labels) |
| `features` | all number variables | The variables that explain the target (a maximum of 256 variables and 4096 columns) |
| `task` | `auto` | `regression`, `classification` or `auto` (text and integer labels are classes) |
| `new_levels` | `pooled` | What to do with new row levels when you score: `pooled`, `zero` or `error` |
| `hidden` | `[64, 64]` | The width of each hidden layer |
| `dropout` | 0.1 | The dropout rate |
| `class_order` | sorted labels | The classes in a fixed sequence |
| `train`, `uncertainty` | | As for the image models |

A regression gives `y_hat(chain, draw, row dims)`. For the kind `predictive`, the draws add
noise with the residual variance of the training. A classification gives
`y_hat(chain, draw, row dims, class)` with the probability of each class.

## Determinism

A fit with the same seed, the same data and the same image gives the same weights on the
CPU:

- The network, the dropout, the validation split and the order of the batches use the seed
  of `fit.seed`.
- The loader gives each image its random changes from the seed, the epoch and the position
  of the image. The number of threads does not change the result.
- All batches have the same shape, so the network compiles one time.

On a GPU, some operations can give small differences between runs. The `base-dl` image sets
`XLA_FLAGS=--xla_gpu_deterministic_ops=true` to decrease them. This is not verified.

## Known gaps

- The GPU path (the `base-dl` CUDA image and the Cloud Run GPU job) is not tested on a GPU or
  on GCP.
- One GPU for each run. There is no training on more than one GPU or more than one machine.
- Pretrained weights must use the `mapi-jax` ResNet layout. There is no conversion from
  PyTorch checkpoints.
- `dl.embed` on a collection reads only a sample (`data.collection`). Use
  `dl.finetune_classifier` with `freeze: backbone` for all images.
- The draws come from the last layer (Laplace) or from dropout. They are approximations.
