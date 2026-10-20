---
description: The two ways to keep images on the platform - an image folder that becomes one uint8 array, and an image collection that keeps each image as a file in shared, content-addressed packs - and how models, runs and quotas read each of them.
---

# Image data and collections <span class="mapi-badge">v0.14</span>

The platform keeps images in one of two forms. The form decides how you upload the images,
how many images you can keep and how a model reads them.

| | Image folder <span class="mapi-badge">v0.14</span> | Image collection <span class="mapi-badge">v0.15</span> |
|---|---|---|
| Upload | One zip of images and a manifest table (format `image_folder`) | A folder of images. The client uploads only the images that are new. |
| Layout of the version | `dataset`: one labelled array | `collection`: a manifest and packs of image files |
| Images | 20 000 by default, a maximum of 100 000 | 100 000 by default, a maximum of 1 000 000 |
| Sizes | One size for each dataset | Different sizes |
| What a model gets | `pixels(image, y, x, channel)` with the dtype `uint8` | Batches of images (a model that trains on batches), or a sample as `pixels(image, y, x, channel)` |
| Use it for | Small images, for example a few thousand images of 64 × 64 pixels | Large image sets, images that you add to again and again, and neural networks |

The guide [Image data](../guides/image-data.md) gives the procedures for the two forms. The
guide [Deep learning](../guides/deep-learning.md) tells how the `dl.*` models train on a
collection.

## Image folders: one array

The ingest of an `image_folder` upload reads all images into one array. It writes these
variables:

- `pixels(image, y, x, channel)`, with the dtype `uint8` (v0.14, values from 0 to 255);
- the target `label(image)`;
- the other columns of the manifest, as coordinates on `image`.

The dataset is then a usual labelled dataset ([Labelled data](labelled-data.md)). All models,
steps, plots and the Data Explorer can read it. The **Image gallery** section of the Data Explorer
shows the images. The array has limits: 8 bits for each channel, one size for each dataset
(the ingest can resize the images) and a maximum of 268 435 456 pixel values.

## Image collections: a manifest and packs { #collections }

A collection keeps each image as a file. A collection version has the layout `collection`
(`DataLayout`). The version holds these objects:

| Object | Contents |
|---|---|
| `manifest.parquet` | One row for each image: the id, the SHA-256, the pack and the position in the pack, the size, the format, the label, the split, the path and the other columns |
| `collection.json` | The summary (`CollectionInfo`): the counts, the classes, the splits, the sizes, the formats and the packs |
| `members.bin` | The SHA-256 of each image, sorted. The server uses it to find the images that a version does not have. |
| `schema.json` | The schema: `pixels(image, y, x, channel)` (a virtual variable) and `label(image)` |

The images are in **packs**. A pack is a zip file whose entries are named
`<sha256>.<ext>`. The tenant keeps each pack one time, at `t/{tenant}/blobs/packs/{digest}.zip`
in the data bucket. All versions that use a pack share it.

### Addresses that the server calculates

The address of a pack is `pack_digest(size, crc32c, md5)`: a SHA-256 over the size, CRC32C
and MD5 that the store calculated for the upload. This is the same basis as the address of a
dataset version. The platform trusts no hash that the client sends. Each image is identified
by the SHA-256 of its bytes:

- The ingest run makes sure that each image in a part has the SHA-256 of its name. It also
  reads the header of each image and does a check of its size.
- The runner calculates the digest of each pack again before it reads the pack. A pack with a
  different digest stops the run with `INTEGRITY_FAILURE`.
- Each time a run reads an image, it makes sure that the SHA-256 is correct.

### Only new images go up

A new version compares its images with a **base version** of the same dataset. By default,
this is the latest ready collection version. These steps occur:

1. The client calculates the SHA-256 of each image.
2. The client asks the server which images the base version does not have
   (`POST /v1/datasets/{dataset_id}/blobs/missing`).
3. The client puts these images into parts (zip files with no compression, 256 MB by
   default, a maximum of 64 new parts). It uploads each part with a signed URL.
4. The client creates the version from the manifest and the parts (`CollectionSource`).
5. A collection ingest run (class `M`) reads the parts in ranges and writes the summary and
   the explorer views of a sample.
6. The finalizer copies each part to the shared pack path. The new version uses the packs of
   the base version for the other images.

The parts are the same bytes each time that the client builds them. Thus an upload that stops
can continue. The client asks if the file of each part arrived
(`GET /v1/datasets/{dataset_id}/uploads/{upload_id}`). It signs the URL again for a part whose
URL expired (`POST .../uploads/{upload_id}/renew`). An upload whose file arrived has the
status `uploaded`. It stays usable after its URL expires. Thus an upload that takes more than
one hour, or that continues the next day, can make its version.

## How runs read a collection

There are two kinds of models:

- A model that declares `capabilities.input = "collection"` trains on batches of all images.
  It has the hooks `fit_batches` and `score_batches`
  ([Train on batches](../guides/plugins.md#fit-batches)). The harness gives it a loader of the
  rows of the split. The loader reads the images from the packs while the model trains. In
  v0.15, `dl.cnn_classifier` and `dl.finetune_classifier` are these models.
- All other models get a seeded sample of the collection as `pixels(image, y, x, channel)`.
  The spec sets the sample in `data.collection` (`CollectionRead`): the number of images (a
  maximum of 100 000), the size, the resize method, the colour mode, the stratification and
  the seed.

Other runs that read a collection as a dataset also read a sample. The Data Explorer shows a
sample of up to 2000 images. The sections of the explorer, priors, checks and workflows read
5000 images. The views of a fit read 500 images. If a sample needs more than 268 435 456
values, the run makes the images smaller and keeps their aspect.

Validation applies these rules to a collection:

- A collection takes row splits only: `column`, `stratified` or `leave_group_out`. It takes
  no time splits.
- With a model that trains on batches, the spec takes no pipeline steps. Put the augmentation
  in the model settings.
- A collection cannot be a side table or the input of a workflow.

## Weights datasets <span class="mapi-badge">v0.15</span> { #weights }

A file of network weights in the `safetensors` format becomes a dataset (`WeightsOptions`).
The dataset has one `float32` variable, `weights(param)`, with all tensors flattened. The
attribute `mapi_weights` gives the name, the shape and the position of each tensor, and the
architecture of the network ([Spec format](../reference/spec-format.md#safetensors)). A model
reads such a dataset as a side table. The manifest of the model
names the settings whose values are side tables (`side_table_params`, see
[Capabilities](capabilities.md#the-manifest)). For example, `dl.finetune_classifier` reads
its pretrained network from the side table that its `pretrained` setting names. A score
request never needs a side table that only the model reads. The SDK reads and writes
safetensors files with numpy only (`mapi_sdk.safetensors`). No file can run code when it
loads.

## Storage and the quota

The size of a collection version is the size of all the packs that it uses, or the size of
its images if that is larger. The storage quota of the tenant uses this size
([Quotas and cost](quotas-and-cost.md)). A part keeps all its images, also the images that
the manifest does not use. Thus put only the images of the manifest in the parts.

!!! note
    The tenant keeps the packs until the tenant is deleted. A deleted dataset version does
    not delete its packs, because other versions can use them.

## Known gaps

- The client compares images only with a base version of the same dataset.
- There are no thumbnails for each image. The gallery shows the sample of the ingest.
- A collection takes no pipeline steps with a model that trains on batches, and no time
  splits.
- Workflows and side tables do not take collections.
- `dl.embed` on a collection needs `data.collection` (a sample). To use all images, use
  `dl.finetune_classifier` with `freeze: backbone`.

Next: [Measure the effect of a change](causal-effects.md).
