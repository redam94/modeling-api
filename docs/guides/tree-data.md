---
description: Tree datasets - NetCDF and HDF5 groups, raw HDF5 files with a mapping, versions made from several files (node, concat, merge), data.nodes in specs, tree-input models, limits and the HDF5 safety checks.
---

# Tree datasets, HDF5 and multi-file versions <span class="mapi-badge">v0.8</span>

Some data does not fit in one table of variables. A telescope file has an image and, in a
second group, the point-spread function (PSF) of the instrument. A sales study has one file
for each market, and a separate file for the media plan. From v0.8, a dataset version can
hold this data as a **tree**: an `xr.DataTree` with one node for each group.

This guide tells you how to:

- upload NetCDF-4 and HDF5 files that have groups ([tree datasets](#what-a-tree-dataset-is));
- map a raw HDF5 file, which has no dim names, after an [inspection](#raw-hdf5-inspect-then-map);
- make one version from [several files](#versions-made-from-several-files): one file for each
  node, files stacked along a dim, or files merged on shared coords;
- read nodes of a tree in a spec with [`data.nodes`](#read-a-tree-in-a-spec-datanodes);
- write a model that reads the [whole tree](#models-that-read-the-whole-tree);
- use a tree dataset in a [workflow](#tree-datasets-in-a-workflow) (v0.9).

All existing datasets, specs and plugins continue to work without changes. A file without
groups still gives a plain dataset version, as before.

The examples on this page are in `docs/snippets/tree_data.py`. The docs tests run this script.
The script ingests the example files with the code of the ingest run, and it checks each JSON
body, spec, client call and CLI command on this page.

## What a tree dataset is

A tree dataset has **nodes**. A **node path** identifies each node: `/` is the root,
`/instrument` is a group below the root and `/instrument/psf` is a group below `/instrument`.
Each node holds variables over its own dims. A node also sees the coords of the nodes above
it (xarray *inherits* them). Thus a dim that a node shares with a node above it must have the
same labels.

A node without variables is a *pure group*. The ingest keeps it as an empty node. It never
drops a group without a warning.

| Layout | Stored as | Schema | When |
|---|---|---|---|
| `dataset` (the default) | `data.zarr.zip` | `schema.json` (`DataSchema`) | A file with no groups, or a version whose sources are all in the root |
| `tree` | `data.tree.zip` | `tree_schema.json` (`TreeSchema`: one `DataSchema` for each node) | A file with groups, or a version with sources below the root |
| `collection` (v0.15) | `manifest.parquet`, `collection.json` and packs of images that the versions share | `schema.json` | A large set of images. It is not a tree. See [Large image sets: collections](image-data.md#large-image-sets-collections). |

A `DatasetVersion` gives the layout in `layout`. A tree version also has `tree_schema`, and
its `data_schema` is the schema of the root node. Thus an older reader sees the root node and
fails softly. `sources` lists the uploads of the version.

### NetCDF-4 and HDF5 files with groups

NetCDF-4 files are HDF5 files. xarray writes one HDF5 group for each node of a tree:

```python
--8<-- "tree_data.py:netcdf"
```

Upload `lens.nc` with format `netcdf`, as for any NetCDF file. The ingest opens the file as a
tree. The groups become nodes: `/`, `/instrument` (a pure group) and `/instrument/psf`. The
`dims` and `vars` of a NetCDF mapping rename and select variables in each node, by name, as
for a plain file.

- A NetCDF file that has only a root group gives a `dataset` version, as before v0.8.
- NetCDF-3 files have no groups. The ingest reads them with SciPy.
- A `.zarr.zip` with nested groups also gives a tree. The Python client writes an
  `xr.DataTree` in this format (`data.tree.zip`).

You can make and check a tree schema in Python:

```python
--8<-- "tree_data.py:schema"
```

### Node names

A node name starts with a letter or `_`. The other characters are letters, digits, `_`, `.`
and `-`, to a maximum of 64 characters. A tree has a maximum of 8 levels below the root. If
a file has a group with a name that is not valid, the ingest refuses the file. The error names
the group.

## Raw HDF5: inspect, then map

A *raw* HDF5 file (`.h5`, `.hdf5` or `.he5`, written with h5py or by an instrument) has
arrays but no dim names and no coords. For example:

```python
--8<-- "tree_data.py:raw-hdf5"
```

To ingest a raw HDF5 file, use format `hdf5` and a mapping (`mapping.hdf5`). The mapping
tells which arrays become which variables, in which nodes, with which dims and coords. If the
file is self-describing (written by xarray or netCDF4), use format `netcdf` instead. It does
not need a mapping.

### Step 1: inspect the upload

You do not have to know what is in the file. An **inspection** lists the groups and arrays of
an upload from its metadata. It does not read array data, and it does not make a dataset
version.

1. Make an upload session and `PUT` the file, as for any upload
   ([HTTP API guide](http-api.md)).
2. Start the inspection:
   `POST /v1/datasets/{dataset_id}/uploads/{upload_id}/inspect`. The server answers
   `202` with an `inspect` run.
3. Get the result:
   `GET /v1/datasets/{dataset_id}/uploads/{upload_id}/inspection`. While the run is not
   complete, the server answers `409 NOT_READY`. Try again after some seconds.

The result is an `UploadInspection` (shortened here to two of its five arrays):

```json
{
  "format": "hdf5",
  "size_bytes": 18042,
  "groups": ["/", "/axes", "/meta", "/observations", "/psf"],
  "arrays": [
    {"path": "/observations/image", "shape": [64, 64], "dtype": "float32",
     "chunks": [32, 64], "filters": ["deflate"], "dims_hint": null, "attrs": {}},
    {"path": "/psf/kernel", "shape": [9, 9], "dtype": "float64",
     "chunks": null, "filters": [], "dims_hint": null, "attrs": {}}
  ],
  "truncated": false,
  "warnings": []
}
```

The inspection runs the same [HDF5 safety checks](#security-the-hdf5-guard) as an ingest.
`dims_hint` gives dim names when the file has them (HDF5 dimension scales, zarr
`_ARRAY_DIMENSIONS` or NetCDF dims). The inspection of an upload occurs one time: a second
request gets the same run, or the finished result. If the run failed, a new request starts a
new run.

### Step 2: write the mapping

This is the full body of `POST /v1/datasets/{dataset_id}/versions` for the file above. It
ingests the staged upload with an `hdf5` mapping:

```json
--8<-- "tree_version_hdf5.json"
```

Each entry of `vars` is an `Hdf5Var`:

| Field | Meaning |
|---|---|
| `path` | The HDF5 path of the array in the file |
| `node` | The node of the variable (default `/`) |
| `name` | The name of the variable in the node |
| `dims` | One dim name for each axis of the array, in sequence. The ingest makes sure that the number of names is the same as the number of axes |
| `role`, `units`, `missing`, `description` | As for a column mapping (default role `covariate`, missing `forbid`) |

Each entry of `coords` is an `Hdf5Coord`. It gives the labels of one dim in one node, from
one of these sources:

| Source | Fields | Example |
|---|---|---|
| A 1-D array of the file | `path` | `{"dim": "col", "path": "/axes/col_arcsec"}` |
| A range of numbers | `start` and `step` | `{"dim": "row", "start": -1.6, "step": 0.05}` |
| A range of dates | `start` (a date) and `step` (a pandas frequency) | `{"dim": "date", "start": "2024-01-01", "step": "W-MON"}` |
| An integer index `0 … n-1` | neither `path` nor `start` | `{"dim": "prow", "node": "/psf"}` |

`spec` sets the `DimSpec` of the dim (kind, policy, frequency). Without it, the ingest infers
the spec from the labels. A dim without an `Hdf5Coord` has no coord. A coord in a node also
applies to the nodes below it. A date range must start on its frequency: `2024-01-02` with
`W-MON` is an error, because 2 January 2024 is not a Monday.

The result of the example is a tree with two nodes: `/` has `image` and `noise` over `row`
and `col`, and `/psf` has `psf` over `prow` and `pcol`. The ingest does not read the arrays
that the mapping does not name. One warning of the run lists them (here
`/meta/exposure_s`).

If all the variables of the mapping are in the root, the version is a plain `dataset`
version.

## Versions made from several files

One dataset version can come from as many as 64 uploads. Each upload is a **source**. Send an
`assembly` in `POST /v1/datasets/{dataset_id}/versions` instead of `upload_id` and `mapping`.
Each source of `assembly.sources` (an `AssemblySource`) gives:

| Field | Meaning |
|---|---|
| `upload_id` | A complete upload of this dataset. Each upload can be in one source only |
| `mapping` | The `IngestMapping` of this file (`{"format": "netcdf"}` for a self-describing file) |
| `node` | Where the file goes (default `/`) |
| `combine` | How the file joins the other sources of the same node: `node`, `concat` or `merge` |
| `concat_dim`, `concat_label` | For `concat`: the new dim, and the label of this file along it |
| `join` | How the coords align for `concat` and `merge`: `exact` (default), `inner` or `outer` |

The three rules are:

| `combine` | Sources in the node | What the ingest does |
|---|---|---|
| `node` (the default) | One | Puts the file at the node. A file that is a tree puts its groups *below* the node: group `/psf` of a file at node `/instrument` becomes `/instrument/psf` |
| `concat` | One or more (the CLI asks for two or more) | Stacks the files along a **new** dim `concat_dim`. Each file gives one label, for example one file for each market or each year. The files must have the same variables over the same dims |
| `merge` | One or more (the CLI asks for two or more) | Merges the variables of the files on their shared coords. A variable that is in two files must have the same values where the files overlap |

All sources of one node use the same rule. Sources of a `concat` use the same `concat_dim`
and different labels (all numbers, or all text). Sources of a `concat` or a `merge` use the
same `join`.

The server checks every upload before it uses any. If one source is not correct, the server
refuses the version and all the uploads stay available. One ingest run reads all the sources
(inputs `upload.0`, `upload.1`, …). The sum of the upload sizes sets the resource class of the
run.

### Example: one node from several files, and a second node

These files are one sales file for each market and one media file:

```python
--8<-- "tree_data.py:files"
```

This body stacks the two sales files along a new `market` dim in node `/sales`. It puts the
media file in node `/media`:

```json
--8<-- "tree_version_assembly.json"
```

The version is a tree with the nodes `/`, `/sales` and `/media`. `/sales` has
`revenue(market, date)` with the labels `NL` and `DE`.

### Example: merge two files

This body merges a sales file and a weather file in the root. `join: inner` keeps only the
weeks that both files have:

```json
--8<-- "tree_version_merge.json"
```

All sources are in the root. Thus the result is a plain `dataset` version.

### With the Python client

`client.datasets.upload_files` checks the full assembly before it uploads a byte. Then it
uploads each file, creates the version and waits for the ingest.

Stack files along a new dim (one label for each file):

```python
--8<-- "tree_data.py:client-concat"
```

Merge files on shared coords:

```python
--8<-- "tree_data.py:client-merge"
```

Give each file its own node and rule with `AssemblyFile`:

```python
--8<-- "tree_data.py:client-nodes"
```

`client.datasets.upload` also accepts an `xr.DataTree` (it uploads a `data.tree.zip`) and a
raw HDF5 file with an `IngestMapping(format="hdf5", hdf5=...)`. Without a mapping, the client
refuses a raw HDF5 file before it uploads anything. For a raw HDF5 file, inspect first, then
ingest the staged upload:

```python
--8<-- "tree_data.py:client-hdf5"
```

`inspect_file` uploads the file without an ingest (`stage_upload`) and waits for the
inspection (`inspect`). The async client has the same methods.

### With the CLI

`mapi datasets upload` takes one or more files. Several files, or one of the options
`--node`, `--concat` or `--merge`, make one version from all of the files:

```bash
--8<-- "cli_tree.sh:concat"
```

```bash
--8<-- "cli_tree.sh:merge"
```

```bash
--8<-- "cli_tree.sh:node"
```

- `--node PATH`: give it one time (for all the files) or one time for each file, in file
  order.
- `--concat DIM=LABEL`: one label for each file of the node, in file order. Repeat the option
  or give the labels as a list (`market=NL,DE`). Labels that look like numbers are numbers.
- `--merge`: merge the files of the same node.
- `--join exact|inner|outer`: only with `--concat` or `--merge`.
- `--mapping FILE`: one time (for all the files that it can read) or one time for each file.

For a raw HDF5 file:

```bash
--8<-- "cli_tree.sh:hdf5"
```

`mapi datasets inspect` shows a table of the arrays (path, shape, dtype, dims hint, chunks,
filters). With the global `--json` option (`mapi --json datasets inspect …`), the command
prints the upload id and the full `UploadInspection`.

## Read a tree in a spec: `data.nodes`

A pipeline step and most models read one flat `xr.Dataset`. For a tree dataset,
`data.nodes` selects the nodes. The platform merges their variables into one flat dataset
before the pipeline:

```yaml
--8<-- "tree_spec.yaml"
```

!!! note "The model of this example"
    `toy.tree_linear` is a reference model of the SDK test kit (`mapi_sdk.testing.toy`). The
    documentation tests use it to check and fit this spec. The platform and the CLI catalog do
    not have it, so `mapi check` reports `REF_UNRESOLVED` for it. Put a model of your platform
    in `model.use` (`mapi catalog` lists them).

Each entry of `data.nodes` is a `NodeSelect`:

| Field | Meaning |
|---|---|
| `path` | The node to read |
| `vars` | The variables to read from the node (default: all) |
| `prefix` | Text to put before each variable name of the node, for example `plan_`. Use it when two nodes have variables with the same name |

The rules of the merge are:

- Dims keep their names. A dim that two selected nodes use must have the same `DimSpec`.
- A variable name can come from one node only. If two nodes give the same name, validation
  tells you to add a `prefix`.
- `data.join` aligns the labels of the shared dims: `exact` (the default) refuses labels that
  are not equal, `inner` keeps the shared labels, and `outer` keeps all labels and adds
  missing values.
- `data.bind` applies to the merged names (for example `plan_spend`).

Static validation (`mapi check --remote`, `POST /v1/specs:validate`) uses the `tree_schema` of
the version. A local `mapi check spec.yaml --data lens.tree.zip` reads the tree schema of the
local tree dataset in the same way. It finds an unknown node or variable, a name clash and dims that are not the same
before a run starts. These rules also apply:

- If a spec reads a tree dataset without `data.nodes`, the platform reads the root node. If
  the root has no variables, validation reports `SCHEMA_INVALID`: "this dataset is a tree:
  pick nodes in data.nodes".
- `data.nodes` on a plain dataset is an error.
- A `data.join` other than `exact` without `data.nodes` is an error. (`exact` is the default
  and has no effect without nodes.)

The same merge is available in Python:

```python
--8<-- "tree_data.py:select"
```

### Side tables that are trees

A side table can be a tree dataset too. It reads one node, which `node` names:

```yaml
data:
  side_tables:
    fx: {ref: dataset://growth/reference@latest, node: /fx}
```

The server checks that the node exists in the version. A side table that is a plain dataset
does not need `node`.

### The Data Explorer

The Data Explorer shows one node at a time. A `DataExploreRequest` takes an optional `node`.
The default is the root, or the first node that has variables. The ingest writes the views of
each node.

The explorer finds the shapes of each node separately. For example, the E80 lens tree has
two images of 81 × 81 pixels (`image` and `weight`) in the node `/image`, and an image of
31 × 31 pixels (`psf`) in the node `/psf`. Thus each node has its own sections and its own
sequence of sections. Do not confuse a tree dataset with the section **Tree**. That section
shows a tree that is in the data of one node, for example the `parent` variable of a
phylogeny. Refer to [Scientific data](data-explorer.md#scientific-data).

## Models that read the whole tree

Some models need more than the flat merge, for example the PSF of an instrument next to the
image. A model declares `capabilities.input = "tree"` in its manifest. It then gets the whole
tree in `ctx.tree` (`FitContext` and `ScoreContext`), with the binding renames applied in each
node. `ds` is still the flat merge of `data.nodes`. Thus the steps of the pipeline work as
before. Models that do not declare it (`input = "dataset"`, the default) see only the flat
dataset.

`toy.tree_linear` in `mapi_sdk.testing.toy` is a reference model. This minimal plugin follows
it and passes the contract kit for tree-input models (`check_tree_model`):

```python
--8<-- "tree_data.py:plugin"
```

`check_tree_model` fits the model from a tree through the harness. It makes sure that `fit`
and `score` get the bound tree, that a tree payload scores the same as its flat merge plus
the tree, and that a bundle round trip keeps the scores. It also makes sure that a flat
payload without a tree scores, or the model refuses it with a typed error.

The platform gives the tree to tree-input models in fit, score, evaluate, the Model Explorer,
governance checks and workflow scoring. With a `data.split`, the evaluation fit and the
holdout score see only their part of the tree.

`sci.strong_lens` is a built-in tree-input model. When the tree has a `/psf` node with `psf`
over `psf_row` and `psf_col`, the fit uses that PSF. Otherwise it uses the flat `psf`, as
before. With the setting `psf_source: tree`, a spec can select only the image node in
`data.nodes`. Refer to the model's section in the [model reference](../reference/models.md).

## Tree datasets in a workflow <span class="mapi-badge">v0.9</span> { #tree-datasets-in-a-workflow }

A workflow input can be a tree dataset. In the local runner, it can be an `xr.DataTree`.
`model.fit` and `model.backtest` take the parameters `nodes` and `join`. They are the same as
`data.nodes` and `data.join` of a spec. If you do not give `nodes`, the node reads the root
node. `join` is `exact` (the default), `inner` or `outer`.

```yaml
- id: fit
  use: model.fit
  inputs: {data: inputs.data}
  with:
    model: toy.tree_linear@^1
    nodes: {sales: {path: /sales}, media: {path: /media}}
    join: inner
```

`model.score`, `model.forecast` and `plot.render` (`source: predictions`) send a tree on their
`data` input as a whole tree. They select the same nodes as the fit. A tree-input model gets
the tree of that input, not the tree of its fit. With `source: data`, `plot.render` keeps all
the groups of a tree input. For example, `sci.area_map` reads the group `geometry`, and
`sci.phylogeny` reads the group `tree`.

These limits apply:

- A backtest of a tree dataset gives the full tree to each fold. A tree-input model that
  reads its target from the tree can see the test window. The node shows a warning.
- `search.grid` has no `nodes` parameter.

Refer to [Tree datasets](workflows.md#tree-datasets) in the workflows guide.

## Limits

| Limit | Value | Where |
|---|---|---|
| Nodes of one tree | 256 (with the root and empty groups) | `MAX_TREE_NODES`, `IngestJob.max_nodes` |
| Levels below the root | 8 | `MAX_TREE_DEPTH` |
| Characters in one node name | 64 | `NodePath` |
| Entries of `data.nodes` | 64 | `DataBlock.nodes` |
| Characters in a `prefix` | 32 | `NodeSelect.prefix` |
| Sources of one version | 64 | `IngestAssembly.sources`, `IngestJob.max_sources` |
| Size of all the uploads of one version | 5 GB together | The upload limit of the server (resource class L) |
| Decoded size of all the sources | The scratch budget of the ingest run | `IngestJob.max_uncompressed_bytes` |
| Variables of one HDF5 mapping | 1,024 | `Hdf5Mapping.vars` |
| Coords of one HDF5 mapping | 64 | `Hdf5Mapping.coords` |
| Characters in an HDF5 path | 1,025: the leading `/` and 1,024 more | `Hdf5Var.path`, `Hdf5Coord.path` |
| HDF5 links (groups and datasets) in one file | 10,000 | `IngestJob.max_hdf5_objects` |
| HDF5 attributes in one file | 10,000, a separate count from the links (self-describing NetCDF: 64 for each variable the ingest accepts, if that is more) | `IngestJob.max_hdf5_objects`, `IngestJob.max_columns` |
| Dims of one array | 32 | `IngestJob.max_ndim` |
| Bytes of one attribute value | 64 KiB (self-describing NetCDF: the file size) | `IngestJob.max_attr_bytes` |
| Group levels in one HDF5 file | 32 | The HDF5 guard |
| User block before the HDF5 superblock | 64 KiB | The HDF5 guard |
| Arrays in one inspection | 2,000. Then `truncated` is `true` | `MAX_INSPECT_ARRAYS` |
| Attributes kept for each inspected array | 64 small values | `MAX_INSPECT_ATTRS` |
| Characters of one text attribute in an inspection | 1,024 | `MAX_INSPECT_ATTR_CHARS` |
| Size of one inspection | 8 MiB of JSON | `MAX_INSPECTION_BYTES` |
| Time of one inspect run | 300 s | `MAPI_INSPECT_TIMEOUT_S` |

The [limits of all uploads](guided-data.md#limits) also apply: the decompression ratio and the
uncompressed size.

## Security: the HDF5 guard

Uploads are not trusted. The control plane never opens them: only the sandbox of a run reads
the bytes. HDF5 is a large format, and some of its features can make a reader open other
files, load code or do work without limit. Before xarray or h5py opens an HDF5 file, the
runner walks the file with the low-level API of h5py. It never follows a link. The guard
refuses these files with `INGEST_UNSAFE_FILE` (422):

| The guard refuses | Why |
|---|---|
| External links and user-defined links | An external link can point to a different file, for example `/etc/passwd` |
| Soft links that do not resolve, that make a loop, that use `..`, or that point to a group | A reader that follows them never stops, or walks the tree again |
| Groups in a loop of hard links | The same: a walk that never ends |
| Datasets with external raw storage | The bytes of the dataset are in a different file |
| Virtual datasets | A virtual dataset is a mosaic of other files |
| Filters that are not deflate (gzip), shuffle, fletcher32, scaleoffset, nbit or lzf | Other filters, szip included, can load plugin code. The runner also points `HDF5_PLUGIN_PATH` at an empty directory |
| A user block larger than 64 KiB | The guard finds the HDF5 superblock at each offset where libhdf5 looks for it (0, 512, 1024, …). Thus a file that starts with other bytes is still checked. A file that starts with the NetCDF-3 magic (`CDF`) is read only by the NetCDF-3 reader of scipy, which never calls libhdf5. Thus a fake NetCDF-3 header cannot take an HDF5 file past the guard, and a NetCDF-3 file whose values hold the HDF5 signature still ingests |
| Too many objects, groups too deep, too many dims, attributes too large | Each of them can make a reader use too much memory or time |
| Arrays that declare more bytes than the budget of the run, or one chunk larger than the budget | A small compressed file can expand to a very large array |
| Variable-length sequences, compound, array, reference and opaque data types | Their size has no limit that the guard can find. Variable-length text is accepted, because the file size limits it. Before the guard reads a text attribute, its worst case (elements × file size) must fit the budget of the run |

The guard also checks self-describing NetCDF-4 files. For these files, the cap of one
attribute is the file size, and a variable can have 64 attributes, as before v0.8. The
inspect run uses the same guard. The error has a plain message and a suggested action. The
names from the file are only in `untrusted_text`.

Refer to the [security model](../concepts/security.md#uploads-and-the-hdf5-guard) for the
full list of controls.

## Where next

- [Labelled data](../concepts/labelled-data.md#tree-datasets): schemas, dims and roles.
- [Spec format](../reference/spec-format.md): `data.nodes`, `data.join` and
  `side_tables.node`.
- [HTTP API reference](../reference/http-api.md#m4): the inspect endpoints and the DTOs.
- [Write a step or model plugin](plugins.md): the contract kit.
- [Image data](image-data.md): image folders and image collections (v0.14 and v0.15).
