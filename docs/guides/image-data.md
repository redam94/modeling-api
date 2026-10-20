---
description: Upload a zip of small images with a manifest table, get a uint8 pixels dataset, look at the images in the Data Explorer gallery, split them for a test, and classify them with a Bayesian model. Upload large image sets as collections that keep each image as a file.
---

# Image data <span class="mapi-badge">v0.14</span>

You can upload a folder of small images, such as product photos, shapes or digits, as a
dataset. The platform reads the images into one array of 8-bit pixels. Then you can look at
the images in the Data Explorer, hold out a part of the images for a test, and classify the
images with a model.

An image folder is for small images of the same size, for example a few thousand images of
64 × 64 pixels or smaller. Refer to [Limits](#limits). For large image sets and images with
different sizes, use a [collection](#large-image-sets-collections) (v0.15).

| See also | |
|---|---|
| [Spec format: image folders and splits](../reference/spec-format.md#image-folders) | `ImageFolderOptions`, `uint8` and the split kinds |
| [Explore a dataset](data-explorer.md#gallery) | The gallery and the colour images |
| [Model catalog](../reference/models.md) | `tabular.image_logistic` and `sklearn.multiclass_classifier` |

## Prepare the zip

Put the images and a manifest table in one zip file:

```text
shapes.zip
├── manifest.csv
└── images/
    ├── 0001.png
    ├── 0002.png
    └── ...
```

The manifest has one row for each image. It can be a CSV file, a TSV file or a Parquet file.

```text
path,id,label,split
images/0001.png,0001,circle,train
images/0002.png,0002,square,test
images/0003.png,0003,cross,train
```

| Column | Necessary | Meaning |
|---|---|---|
| `path` | Yes | The path of the image in the zip, from the root of the zip |
| An id column | No | A unique name for each image. If there is no id column, the images get the numbers of the rows. |
| A label column | No | The class of each image, for example `circle`. The dataset gets a target variable `label`. |
| A split column | No | The part of the data for each image, for example `train` or `test` |
| Other columns | No | The ingest keeps them as coordinates of the images |

The images can be PNG, JPEG or TIFF files (`.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`). They
must have 8 bits for each channel.

- Gray images, palette images and images with transparency are accepted. The ingest removes
  the transparency (alpha) and changes palette colours to RGB.
- CMYK and YCbCr images become RGB images.
- A TIFF file with more than one page: the ingest reads only the first page.
- The ingest does not turn images that have an EXIF rotation. It uses the pixels as they are
  in the file.

!!! caution
    Do not upload images with 16 bits for each channel or with floating-point values. The
    ingest stops with an error. Convert the images to 8 bits first.

## Upload the zip

In the web app:

1. Open **Datasets** and click **Upload**.
2. Select the zip file.
3. For **Format**, select **image folder**.
4. Type the name of the manifest and the names of the columns.
5. If the images do not have the same size, set **Size** (height and width) and **Resize**.
6. Click **Upload**.

From Python:

```python
from mapi_client import Client
from mapi_spec import ImageFolderOptions, IngestMapping

api = Client()
mapping = IngestMapping(
    format="image_folder",
    images=ImageFolderOptions(id_column="id", label_column="label", split_column="split"),
)
version = api.datasets.upload("shapes.zip", dataset="shapes", mapping=mapping)
```

From the command line, write the mapping in a YAML file:

```yaml
format: image_folder
images:
  manifest: manifest.csv
  path_column: path
  id_column: id
  label_column: label
  split_column: split
```

```bash
mapi datasets upload shapes.zip --name shapes --mapping mapping.yaml
```

### Options

| Option | Default | Meaning |
|---|---|---|
| `manifest` | `manifest.csv` | The path of the manifest in the zip (`.csv`, `.tsv` or `.parquet`) |
| `path_column` | `path` | The column with the image paths |
| `id_column` | none | The column with a unique name for each image |
| `label_column` | none | The column with the class of each image |
| `split_column` | none | The column with the part of the data (`train`, `test`) |
| `columns` | all other columns | The other columns to keep. A maximum of 64. |
| `var` | `pixels` | The name of the pixel variable |
| `dim` | `image` | The name of the image dimension. You cannot use `sample`, `chain`, `draw`, `quantile`, `bound`, `moment`, `y`, `x` or `channel`. |
| `mode` | `auto` | `gray`, `rgb` or `auto`. With `auto`, the dataset is RGB when one or more images have colour. Else it is gray. |
| `size` | none | The height and the width (1 to 4096 each) of all images after the ingest |
| `resize` | `stretch` | How to change the size: `stretch` (change the aspect), `crop` (cut the centre to the aspect, then change the size) or `pad` (fit inside the size, then add black pixels). Use it only with `size`. |
| `max_images` | 20 000 | The maximum number of rows of the manifest (1 to 100 000) |

Without `size`, all images must have the same size. If they do not, the error gives the
first five images with a different size.

## The dataset

The ingest makes one dataset with these dimensions and variables:

| Name | Dimensions | Type | Contents |
|---|---|---|---|
| `pixels` | `image`, `y`, `x`, `channel` | `uint8` | The pixels, 0 to 255. The role is `covariate`. |
| `label` | `image` | text | The class of each image (only with `label_column`). The role is `target`. An empty cell is an empty text. Numbers become text: `3`, not `3.0`. |
| `image` | | coordinate | The ids, or the row numbers with the same number of digits (`000` to `199`) |
| `y`, `x` | | coordinate | 0 to height − 1 and 0 to width − 1 |
| `channel` | | coordinate | `gray`, or `r`, `g` and `b` |
| `path`, `split` and the other columns | `image` | coordinate | The values of the manifest |
| `source_height`, `source_width` | `image` | coordinate | The size of each image before the resize (only with `size`) |

`uint8` is a numeric type of the schema. A model that needs numbers can use `pixels`
directly. Values are whole numbers from 0 to 255.

!!! note
    The name of the image dimension is `image`, not `sample`. The platform uses the name
    `sample` for draws of a posterior. A dimension with that name would show as draws in the
    plots and the metrics.

## Look at the images

The Data Explorer of an image dataset starts with two sections:

- **Gallery** shows small pictures (thumbnails) of the images. Click a chip of a label or of
  a split to show only these images. Click a thumbnail to see a larger picture and the values
  of the image. When the dataset has more than 240 images, the gallery shows an even sample
  of each label and split. A note above the gallery tells you this.
- **Images and fields** shows one image at a time in the field viewer. Colour images show in
  colour. The mean image of each class (for example, the mean of all circles) shows below
  the image.

See [Explore a dataset](data-explorer.md#gallery).

## Hold out images for a test

Two split kinds are for images and other data that has no time order:

- `column` uses a column of the data. With the split column of the manifest, the test uses
  the images with `split = test`:

    ```yaml
    data:
      dataset: dataset://default/shapes@v1
      split:
        kind: column
        dim: image
        column: split
        holdout: [test]
    ```

    With `train: [train]`, the fit uses only the `train` images. Images with other values
    are not used.

- `stratified` holds out the same share of the images of each class, at random:

    ```yaml
      split:
        kind: stratified
        dim: image
        by: label
        fraction: 0.25
        seed: 7
    ```

    A class with one image stays in the training part. Images with an empty label stay in
    the training part.

For the full description, see [Spec format](../reference/spec-format.md#column-and-stratified-splits).

## Classify the images

`tabular.image_logistic` is a small Bayesian model for image classes. It changes the pixels
to the main patterns of the images (PCA), then it fits a multinomial logistic regression.
It gives the probability of each class for each image, with a range.

```yaml
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: shapes, project: default}
data:
  dataset: dataset://default/shapes@v1
  split: {kind: column, dim: image, column: split, holdout: [test]}
model:
  use: tabular.image_logistic@1
  settings:
    n_components: 16
    downsample: 1
  fit: {engine: nutpie, draws: 500, tune: 500, chains: 2}
evaluate:
  metrics: [accuracy, accuracy_naive, brier, auc]
```

Name the metrics in `evaluate.metrics`. The default metrics (`rmse`, `mae`, `mape`) are
for numbers, not for classes: without `evaluate.metrics` the fit gives no holdout metric.
The metrics for classes are `accuracy`, `accuracy_naive`, `accuracy_lift`, `log_score`,
`brier` and `auc`.

| Setting | Default | Meaning |
|---|---|---|
| `pixels`, `target`, `image_dim` | `pixels`, `label`, `image` | The names in the dataset |
| `features` | `pca` | `pca` (the main patterns) or `pixels` (the pixels themselves) |
| `n_components` | 16 | The number of patterns (2 to 64) |
| `downsample` | 1 | Use the mean of blocks of this size before the features (1 to 16) |
| `max_features` | 256 | With `pixels`: the maximum number of pixel values after `downsample` |
| `prior_sigma` | 1.0 | The prior scale of the coefficients |
| `class_order` | sorted | The sequence of the classes |

The fit logs the parameters of the features and the explained variance. In the run, the
**Media** tab shows two images: the first 8 patterns and the mean image of each class. The
plots are the ROC and precision-recall curves for each class, the confusion matrix and the
convergence of the chains.

For larger images or more classes, use `sklearn.multiclass_classifier` on features that you
calculate first. See [scikit-learn models](sklearn.md#multiclass). From v0.15, the step
`dl.embed` calculates the features with a pretrained network, and the `dl` models train
neural networks. See [Deep learning](deep-learning.md).

## Limits

| Limit | Value |
|---|---|
| Images in one dataset | 20 000 by default (`max_images`), a maximum of 100 000 |
| Pixels in one image | 4096 × 4096, from the header of the file, before the ingest reads the image |
| Bytes of one image file | 32 MiB |
| Values in `pixels` | 268 435 456 (images × height × width × channels) |
| Bits | 8 for each channel |
| Sizes | One size for each dataset (use `size` to make the images the same size) |

Before the ingest reads the images, it calculates the number of values. If the number is too
large, the ingest stops with an error such as: "The images need 402,653,184 values. The
maximum is 268,435,456 (about 21,845 images of this size). Make the images smaller (set
size) or use fewer images." Use `size` to make the images smaller, or upload fewer images.

The ingest also stops with an error when:

- the manifest is not in the zip, or the ingest cannot read it;
- a column is not in the manifest (the error gives up to 20 columns of the manifest);
- two images have the same id;
- a path is absolute, contains `..` or a backslash, or is not in the zip;
- a file is not a PNG, JPEG or TIFF file, or the image library cannot open it.

An `image_folder` upload must be the only file of a dataset version. You cannot combine it
with other files in one version.

## Large image sets: collections <span class="mapi-badge">v0.15</span> { #large-image-sets-collections }

An image collection keeps each image as a file. The platform does not read all images into
one array. Use a collection when:

- you have more images than an image folder can hold (more than 100 000 images, or more
  than 268 435 456 pixel values);
- the images have different sizes;
- you add images to the dataset again and again, and you do not want to upload all images
  each time;
- you train a neural network on all images (see [Deep learning](deep-learning.md)).

A collection can hold up to 1 000 000 images. Each image is identified by the SHA-256 of
its bytes.

### Prepare the folder

Put the images in a folder. Give the classes in one of two ways:

- **Labels from the folders**: put the images of each class in a folder with the name of
  the class.

    ```text
    photos/
    ├── bottle/
    │   ├── 0001.jpg
    │   └── ...
    └── can/
        └── ...
    ```

- **A manifest table**: a CSV, TSV or Parquet file with one row for each image. The `path`
  column gives the path of the image in the folder. The client adds the `sha256` column.

    ```text
    path,label,split
    bottle/0001.jpg,bottle,train
    can/0007.jpg,can,test
    ```

The images can be PNG, JPEG or TIFF files with 8 bits for each channel. The same rules as
for an image folder apply (see [Prepare the zip](#prepare-the-zip)).

### Upload the collection

The client calculates the SHA-256 of each image. Then it asks the server which images the
dataset does not have. It uploads only these images, in parts of 256 MB (zip files with no
compression). Then it creates the version.

From the command line:

```bash
mapi datasets upload-images product_photos ./photos --label-from-folder --size 64x64 --wait
```

| Option | Meaning |
|---|---|
| `--manifest FILE` | A manifest table. Without it, the client uses all images in the folder. |
| `--label-from-folder` | The label of each image is the name of its first folder |
| `--id-column`, `--label-column`, `--split-column`, `--path-column` | The columns of the manifest |
| `--size HxW`, `--resize` | The size that samples and models use by default (`stretch`, `crop` or `pad`) |
| `--mode` | `auto`, `gray` or `rgb` |
| `--base-version N`, `--no-base` | The version to compare with (default: the latest ready collection version) |
| `--part-size` | The size of each part (default 256MB) |
| `--state FILE` | The file that records the uploaded parts (to continue later) |
| `--wait` | Wait until the version is ready |

To see the number of new images before you upload, use
`mapi datasets missing-images product_photos ./photos`.

From Python:

```python
from pathlib import Path

from mapi_client import Client
from mapi_spec import CollectionOptions

api = Client()
version = api.datasets.upload_collection(
    "product_photos",
    Path("photos"),
    label_from_folder=True,
    options=CollectionOptions(size=(64, 64)),
    wait=True,
)
```

In the web app:

1. Open the dataset and click **Upload**.
2. Select **Folder of images**.
3. Select the folder. If you have a manifest, select it too.
4. Select how to get the labels: from the folders or from a column.
5. If necessary, set the size, the mode and the base version.
6. Click **Upload**. The app shows the progress of each stage. It calculates the SHA-256
   values, finds the missing images, uploads the parts and creates the version.

### Continue an upload

The upload of a large collection can stop, for example when the network fails. Start the
same command again with the same folder:

- The command line and the Python client read the state file. They skip the parts that the
  server has. They sign the upload URL again for a part whose URL is expired.
- In the web app, select the same folder again. The app skips the parts that the server
  has.

The parts are the same bytes each time that the client builds them. Thus a part that
arrived does not need an upload again.

### Add images to a collection

A new version compares its images with a base version of the same dataset. By default, this
is the latest ready collection version. The client uploads only the images that the base
version does not have. The new version uses the stored images of the base version for the
other images.

- `--base-version 3` compares with version 3.
- `--no-base` uploads all images.

The comparison is only with the versions of the same dataset. Images that are in another
dataset are uploaded again.

### What a version holds

A collection version has the layout `collection`. The version page shows the number of
images (new and reused), the classes (the first 10), the splits, the packs, the total size,
the range of image sizes and the base version.

| Object | Contents |
|---|---|
| `manifest.parquet` | One row for each image: the id, the SHA-256, where the bytes are, the size, the format, the label, the split, the path and the other columns |
| `collection.json` | A summary (`CollectionInfo`): counts, classes, splits, sizes, formats and the packs |
| `members.bin` | The SHA-256 of each image (the server uses it to find missing images) |
| `schema.json` | The schema: `pixels(image, y, x, channel)` (a virtual variable) and `label(image)` |
| Packs | Zip files with the images. The tenant keeps each pack one time. All versions that use it share it. |

The size of a collection version is the size of all the packs that it uses, or the size of
its images if that is larger. The storage quota uses this size. A part keeps all its images,
also the images that the manifest does not use. Thus put only the images of the manifest in
the parts.

The ingest run makes sure that each image in a part has the SHA-256 of its name. It also
reads the header of each image and checks the size. Each time a run reads an image, it
makes sure that the SHA-256 is correct.

### Explore a collection

The Data Explorer shows a sample of up to 2000 images (a stratified sample of the labels).
The gallery and the field viewer work as for an image folder. The sample has the size of the
collection, or the most frequent size with a maximum of 64 × 64.

Other runs that read a collection as a dataset (Data Explorer sections, priors, checks and
workflows) read a sample of 5000 images. The sample has the size of the collection, or
64 × 64 if the images do not have one size. The views of a fit read 500 images in the same
way. If a sample needs more than 268,435,456 values, the run makes the images smaller and
keeps their aspect.

### Use a collection in a spec

There are two kinds of models:

- Models that train on batches (`dl.cnn_classifier` and `dl.finetune_classifier`) read all
  images. The loader reads the images from the packs while the model trains.
- All other models read a sample of the collection as `pixels(image, y, x, channel)`. Set the
  sample in `data.collection`:

    ```yaml
    data:
      dataset: dataset://default/product_photos@v5
      collection:
        max_samples: 5000     # 1 to 100 000
        size: [32, 32]        # default: the size of the collection
        resize: stretch       # stretch, crop or pad
        mode: auto            # auto, gray or rgb
        stratify: label       # keep the share of each label (null: a uniform sample)
        seed: 7               # default: the fit seed
      split: {kind: column, dim: image, column: split, holdout: [test]}
    model:
      use: tabular.image_logistic@1
    ```

Validation shows a note with the sample size. A collection has these rules:

- A collection takes row splits only: `column`, `stratified` or `leave_group_out`.
- With a model that trains on batches, the spec takes no pipeline steps. Put augmentation
  in the model settings.
- Side tables and the inputs of workflows must be datasets, not collections.

### Limits of collections

| Limit | Value |
|---|---|
| Images in one version | 1 000 000 (`max_images`: 100 000 by default) |
| Parts in one version | 64 new parts |
| Bytes of one part | 2 GB − 64 MB (256 MB by default) |
| Images in one part | 60 000 |
| Packs that one version reads | 128 (new parts and packs of the base version) |
| Size of the manifest upload | 256 MB |
| SHA-256 values in one missing-images request | 5000 (the client sends more in groups) |
| Classes in `collection.json` | 1000 are listed. The count of the others is `other_classes`. |
| Sample of `data.collection` | 100 000 images |

If a version needs more than 128 packs, upload all images again without a base version
(`--no-base`). The new version then has new, larger packs.

!!! note
    The tenant keeps the packs until the tenant is deleted. A deleted dataset version does
    not delete its packs, because other versions can use them.

## What comes next

The known gaps of image collections in v0.15:

- The client compares images only with a base version of the same dataset.
- The platform keeps packs until the tenant is deleted.
- There are no thumbnails for each image. The gallery shows the sample of the ingest.
- A collection takes no pipeline steps with a model that trains on batches, and no time
  splits.
- Workflows and side tables do not take collections.
