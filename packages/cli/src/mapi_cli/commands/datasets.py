"""``mapi datasets ls|upload|ingest|inspect|upload-images|missing-images|upload-weights``
(remote)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Literal

import typer
from mapi_client import Client
from mapi_client.collections import parse_part_size, scan_images
from mapi_spec.api import DatasetVersion, IngestMapping, UploadInspection
from mapi_spec.collection import DEFAULT_PART_BYTES, CollectionOptions
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.loader import parse_text

from mapi_cli.multifile import assembly_files, is_multi_file
from mapi_cli.output import Output
from mapi_cli.state import EXIT_FAILURE, cli_errors, state

_UPLOAD_HELP = """Upload one or more files as a new dataset version (signed PUT + sandboxed
ingest run).

One FILE without --node/--concat/--merge is a plain single-file upload. Several files (or
any of those flags) make ONE version from all of them:

\b
  mapi datasets upload 2024.nc 2025.nc --name cubes --concat year=2024,2025
  mapi datasets upload a.nc b.nc --name obs --merge --join outer
  mapi datasets upload img.nc psf.nc --name lens --node / --node /instrument

--node PATH: where a file goes in the tree, once (every file) or once per file in file
order (default /). Files that share a node are combined by --concat DIM=LABEL (one label
per combined file in file order: repeat the flag or list LABEL,LABEL,...; numeric labels
are numbers) or by --merge (variables on shared coords); --join exact|inner|outer aligns
their coords. --mapping is given once (every file it can read) or once per file. Raw HDF5
(.h5/.hdf5/.he5) needs a mapping with an 'hdf5' section: see 'mapi datasets inspect'.
"""


def _read_mapping(path: Path) -> IngestMapping:
    return IngestMapping.model_validate(parse_text(path.read_text(encoding="utf-8")))


app = typer.Typer(help="Datasets on the API.", no_args_is_help=True)


@app.command("ls")
@cli_errors
def ls(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1)] = 50,
) -> None:
    """List datasets."""
    st = state(ctx)
    with st.client() as api:
        items = api.datasets.list(limit=min(limit, 200)).take(limit)
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["id", "name", "ref", "latest", "updated"],
        [
            (d.id, d.name, d.ref, d.latest_version or "-", d.updated_at.strftime("%Y-%m-%d %H:%M"))
            for d in items
        ],
    )


@app.command("upload", help=_UPLOAD_HELP)
@cli_errors
def upload(
    ctx: typer.Context,
    files: Annotated[
        list[Path],
        typer.Argument(
            help=".zarr.zip, .nc, .h5/.hdf5/.he5, .csv or .parquet (several: one version)",
            exists=True,
            dir_okay=False,
        ),
    ],
    name: Annotated[str, typer.Option("--name", help="Dataset name (created when missing)")],
    mapping: Annotated[
        list[Path] | None,
        typer.Option(
            "--mapping",
            help="IngestMapping YAML/JSON (required for csv/parquet/raw HDF5); once or per file",
        ),
    ] = None,
    node: Annotated[
        list[str] | None,
        typer.Option("--node", help="Tree node of the files (once) or of each file (in order)"),
    ] = None,
    concat: Annotated[
        list[str] | None,
        typer.Option(
            "--concat",
            metavar="DIM=LABEL",
            help="Stack the files sharing a node along DIM, one label per file (or DIM=L1,L2)",
        ),
    ] = None,
    merge: Annotated[
        bool, typer.Option("--merge", help="Merge the variables of the files sharing a node")
    ] = False,
    join: Annotated[
        Literal["exact", "inner", "outer"] | None,
        typer.Option("--join", help="Coord alignment for --concat/--merge (default exact)"),
    ] = None,
    message: Annotated[str | None, typer.Option("--message", "-m")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for the ingest run")] = True,
    timeout: Annotated[float | None, typer.Option("--timeout")] = None,
) -> None:
    st = state(ctx)
    out = st.output
    mappings = [_read_mapping(m) for m in mapping or []]
    nodes, concats = node or [], concat or []
    with st.client() as api:
        if not is_multi_file(files, nodes, concats, merge):
            file = files[0]
            if len(mappings) > 1:
                raise ValueError("one file takes one --mapping")
            out.info(f"uploading {file} ({file.stat().st_size} bytes) to dataset {name!r}")
            version = api.datasets.upload(
                file,
                dataset=name,
                mapping=mappings[0] if mappings else None,
                message=message,
                wait=wait,
                timeout=timeout,
            )
        else:
            items, shared = assembly_files(
                files, nodes=nodes, concat=concats, merge=merge, join=join, mappings=mappings
            )
            for file, item in zip(files, items, strict=True):
                where = f"{item.node} ({item.combine}"
                if item.concat_dim is not None:
                    where += f" {item.concat_dim}={item.concat_label}"
                out.info(f"uploading {file} to dataset {name!r} at node {where})")
            version = api.datasets.upload_files(
                items, dataset=name, mapping=shared, message=message, wait=wait, timeout=timeout
            )
    _show_version(out, version)


def _show_version(out: Output, version: DatasetVersion) -> None:
    if out.json_mode:
        out.emit_json(version)
    else:
        out.kv(
            [
                ("ref", version.ref),
                ("status", version.status.value),
                ("sha256", version.sha256),
                ("size", version.size_bytes),
                ("layout", version.layout if version.layout != "dataset" else None),
                ("nodes", ", ".join(version.tree_schema.nodes) if version.tree_schema else None),
                ("sources", len(version.sources) if len(version.sources) > 1 else None),
                *_collection_rows(version),
                ("ingest run", version.ingest_run_id),
            ]
        )
        out.issues(version.issues)
    if version.status.value == "failed":
        raise typer.Exit(EXIT_FAILURE)


def _collection_rows(version: DatasetVersion) -> list[tuple[str, object]]:
    info = version.collection
    if info is None:
        return []
    classes = ", ".join(f"{c.label} ({c.count})" for c in info.classes[:10])
    return [
        ("images", info.n_images),
        ("new images", info.new_images),
        ("reused images", info.reused_images),
        ("classes", classes or None),
        ("packs", len(info.packs)),
    ]


@app.command("ingest")
@cli_errors
def ingest(
    ctx: typer.Context,
    dataset: Annotated[str, typer.Argument(help="Dataset name or id")],
    upload_id: Annotated[
        str, typer.Argument(help="A staged upload (from 'mapi datasets inspect')")
    ],
    mapping: Annotated[
        Path, typer.Option("--mapping", help="IngestMapping YAML/JSON (e.g. an 'hdf5' mapping)")
    ],
    message: Annotated[str | None, typer.Option("--message", "-m")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for the ingest run")] = True,
    timeout: Annotated[float | None, typer.Option("--timeout")] = None,
) -> None:
    """Ingest an already uploaded file as a new dataset version, e.g. a raw HDF5 file after
    'mapi datasets inspect' showed which arrays to map."""
    st = state(ctx)
    ingest_mapping = _read_mapping(mapping)
    with st.client() as api:
        ds_id = dataset if dataset.startswith("ds_") else _dataset_id(api, dataset)
        version = api.datasets.create_version(
            ds_id,
            upload_id=upload_id,
            mapping=ingest_mapping,
            message=message,
            idempotency_key=str(uuid.uuid4()),
        )
        if wait:
            version = api.datasets.wait_version(ds_id, version.version, timeout=timeout)
    _show_version(st.output, version)


@app.command("inspect")
@cli_errors
def inspect(
    ctx: typer.Context,
    dataset: Annotated[
        str,
        typer.Argument(help="Dataset name or id (a file is staged into it, created when missing)"),
    ],
    target: Annotated[
        str, typer.Argument(help="A local file to upload and inspect, or an upload id (upl_...)")
    ],
    timeout: Annotated[float | None, typer.Option("--timeout")] = 300.0,
) -> None:
    """List the groups and arrays of an upload (path, shape, dtype, dims hint) without
    ingesting it - what a raw-HDF5 mapping needs. A local file is uploaded first; ingest it
    later with 'mapi datasets ingest DATASET UPLOAD_ID --mapping FILE'."""
    st = state(ctx)
    out = st.output
    path = Path(target).expanduser()
    with st.client() as api:
        if path.is_file():
            out.info(f"uploading {path} ({path.stat().st_size} bytes) to dataset {dataset!r}")
            ds_id = dataset if dataset.startswith("ds_") else None
            staged = api.datasets.inspect_file(
                path,
                dataset=None if ds_id else dataset,
                dataset_id=ds_id,
                timeout=timeout,
            )
            upload_id, found = staged.upload_id, staged.inspection
        elif target.startswith("upl_"):
            ds_id = dataset if dataset.startswith("ds_") else _dataset_id(api, dataset)
            upload_id = target
            found = api.datasets.inspect(ds_id, upload_id, timeout=timeout)
        else:
            raise FileNotFoundError(f"{target} is neither a file nor an upload id (upl_...)")
    if out.json_mode:
        out.emit_json({"upload_id": upload_id, "inspection": found})
        return
    _show_inspection(out, upload_id, found)


def _dataset_id(api: Client, name: str) -> str:
    found = api.datasets.find(name)
    if found is None:
        raise MapiError(f"no dataset called {name!r} in this project", code=ErrorCode.NOT_FOUND)
    return found.id


def _show_inspection(out: Output, upload_id: str, found: UploadInspection) -> None:
    out.kv(
        [
            ("upload", upload_id),
            ("format", found.format),
            ("size", found.size_bytes),
            ("groups", ", ".join(found.groups) or "-"),
            ("truncated", "yes (more arrays than listed)" if found.truncated else None),
        ]
    )
    out.table(
        ["path", "shape", "dtype", "dims hint", "chunks", "filters"],
        [
            (
                a.path,
                "x".join(str(d) for d in a.shape) or "scalar",
                a.dtype,
                ", ".join(a.dims_hint) if a.dims_hint else "-",
                "x".join(str(c) for c in a.chunks) if a.chunks else "-",
                ", ".join(a.filters) or "-",
            )
            for a in found.arrays
        ],
        title="Arrays",
    )
    for warning in found.warnings:
        out.warn(warning)


# =============================================================================== v0.15

_IMAGES_HELP = """Upload a folder of images as a new image collection version.

The command calculates the sha256 of each image, asks the server which images the base
version does not have, and uploads only those, in zip parts. With --state, an interrupted
upload continues from where it stopped: run the same command again.

\b
  mapi datasets upload-images pets ./pets --label-from-folder --wait
  mapi datasets upload-images scans ./scans --manifest labels.csv --label-column class
"""


def _size_hw(text: str | None) -> tuple[int, int] | None:
    if text is None:
        return None
    h, sep, w = text.lower().partition("x")
    if not sep or not h.strip().isdigit() or not w.strip().isdigit():
        raise typer.BadParameter("use HxW, for example 64x64")
    return int(h), int(w)


def _base(base_version: int | None, no_base: bool) -> int | None:
    if no_base and base_version is not None:
        raise typer.BadParameter("use --base-version or --no-base, not both")
    return 0 if no_base else base_version


def _options(
    *,
    id_column: str | None,
    label_column: str | None,
    split_column: str | None,
    path_column: str,
    size: str | None,
    resize: str,
    mode: str,
) -> CollectionOptions:
    return CollectionOptions.model_validate(
        {
            "id_column": id_column,
            "label_column": label_column,
            "split_column": split_column,
            "path_column": path_column,
            "size": _size_hw(size),
            "resize": resize,
            "mode": mode,
        }
    )


_Dataset = Annotated[str, typer.Argument(help="Dataset name (created when missing) or id")]
_Dir = Annotated[
    Path, typer.Argument(help="The folder with the images", exists=True, file_okay=False)
]
_Manifest = Annotated[
    Path | None,
    typer.Option(
        "--manifest",
        help="A CSV, TSV or Parquet table with one row per image (column path: the file)",
        exists=True,
        dir_okay=False,
    ),
]
_LabelFromFolder = Annotated[
    bool,
    typer.Option("--label-from-folder", help="Use the first folder of each path as its label"),
]
_PathColumn = Annotated[
    str, typer.Option("--path-column", help="The manifest column with the file paths")
]
_BaseVersion = Annotated[
    int | None,
    typer.Option(
        "--base-version",
        min=1,
        help="Compare with this version (default: the latest image collection version)",
    ),
]
_NoBase = Annotated[
    bool, typer.Option("--no-base", help="Upload all images (do not compare with a version)")
]


@app.command("upload-images", help=_IMAGES_HELP)
@cli_errors
def upload_images(
    ctx: typer.Context,
    dataset: _Dataset,
    directory: _Dir,
    manifest: _Manifest = None,
    label_from_folder: _LabelFromFolder = False,
    id_column: Annotated[
        str | None, typer.Option("--id-column", help="The manifest column with image ids")
    ] = None,
    label_column: Annotated[
        str | None, typer.Option("--label-column", help="The manifest column with labels")
    ] = None,
    split_column: Annotated[
        str | None,
        typer.Option("--split-column", help="The manifest column with train/test values"),
    ] = None,
    path_column: _PathColumn = "path",
    size: Annotated[
        str | None,
        typer.Option("--size", metavar="HxW", help="The image size for samples and models"),
    ] = None,
    resize: Annotated[
        Literal["stretch", "crop", "pad"],
        typer.Option("--resize", help="How images get the --size"),
    ] = "stretch",
    mode: Annotated[
        Literal["auto", "gray", "rgb"], typer.Option("--mode", help="The image channels")
    ] = "auto",
    base_version: _BaseVersion = None,
    no_base: _NoBase = False,
    part_size: Annotated[
        str | None,
        typer.Option("--part-size", help="Maximum size of one zip part (for example 256MB)"),
    ] = None,
    state_file: Annotated[
        Path | None,
        typer.Option("--state", help="A file that records the upload, to continue it later"),
    ] = None,
    message: Annotated[str | None, typer.Option("--message", "-m")] = None,
    wait: Annotated[bool, typer.Option("--wait", help="Wait for the ingest run")] = False,
    timeout: Annotated[float | None, typer.Option("--timeout")] = None,
) -> None:
    st = state(ctx)
    out = st.output
    if label_from_folder and label_column is not None:
        raise typer.BadParameter("use --label-from-folder or --label-column, not both")
    options = _options(
        id_column=id_column,
        label_column=label_column,
        split_column=split_column,
        path_column=path_column,
        size=size,
        resize=resize,
        mode=mode,
    )
    part_bytes = DEFAULT_PART_BYTES if part_size is None else parse_part_size(part_size)
    shown: dict[str, int] = {}

    def progress(stage: str, done: int, total: int) -> None:
        if stage == "upload" and total and done < total:
            out.info(f"uploading parts: {done:,} of {total:,} bytes")
        elif shown.get(stage) != done:
            shown[stage] = done
            if stage == "hash":
                out.info(f"calculated the sha256 of {done:,} images")
            elif stage == "check" and done:
                out.info(f"compared {done:,} images with the base version")
            elif stage == "create" and not done:
                out.info("creating the version")
            elif stage == "wait":
                out.info("waiting for the ingest run")

    with st.client() as api:
        version = api.datasets.upload_collection(
            dataset,
            directory,
            manifest=manifest,
            label_from_folder=label_from_folder,
            options=options,
            base_version=_base(base_version, no_base),
            part_bytes=part_bytes,
            state_file=state_file,
            message=message,
            wait=wait,
            timeout=timeout,
            progress=progress,
        )
    _show_version(out, version)


@app.command("missing-images")
@cli_errors
def missing_images(
    ctx: typer.Context,
    dataset: Annotated[str, typer.Argument(help="Dataset name or id")],
    directory: _Dir,
    manifest: _Manifest = None,
    path_column: _PathColumn = "path",
    base_version: _BaseVersion = None,
    no_base: _NoBase = False,
) -> None:
    """Show how many images of a folder the dataset does not have yet (nothing is
    uploaded)."""
    st = state(ctx)
    out = st.output
    plan = scan_images(directory, manifest, path_column=path_column)
    shas = plan.shas
    with st.client() as api:
        ds_id = dataset if dataset.startswith("ds_") else _dataset_id(api, dataset)
        found = api.datasets.missing_images(ds_id, shas, base_version=_base(base_version, no_base))
    by_sha = plan.by_sha
    new_bytes = sum(by_sha[s].size for s in found.missing)
    if out.json_mode:
        out.emit_json(
            {
                "images": len(plan.images),
                "distinct": len(shas),
                "base_version": found.base_version,
                "present": found.present,
                "missing": len(found.missing),
                "missing_bytes": new_bytes,
            }
        )
        return
    out.kv(
        [
            ("images", len(plan.images)),
            ("distinct images", len(shas)),
            ("base version", found.base_version if found.base_version is not None else "none"),
            ("present", found.present),
            ("missing", len(found.missing)),
            ("missing size", f"{new_bytes:,} bytes"),
        ]
    )


@app.command("upload-weights")
@cli_errors
def upload_weights(
    ctx: typer.Context,
    dataset: _Dataset,
    file: Annotated[
        Path,
        typer.Argument(help="A .safetensors file of network weights", exists=True, dir_okay=False),
    ],
    arch: Annotated[
        Path | None,
        typer.Option(
            "--arch",
            help="A JSON file with the network architecture (when the file does not have it)",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
    message: Annotated[str | None, typer.Option("--message", "-m")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for the ingest run")] = True,
    timeout: Annotated[float | None, typer.Option("--timeout")] = None,
) -> None:
    """Upload pretrained network weights (a .safetensors file) as a new dataset version. A
    model reads the version as a side table."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        out.info(f"uploading {file} ({file.stat().st_size} bytes) to dataset {dataset!r}")
        ds_id = dataset if dataset.startswith("ds_") else None
        version = api.datasets.upload_weights(
            file,
            dataset=None if ds_id else dataset,
            dataset_id=ds_id,
            arch=arch,
            message=message,
            wait=wait,
            timeout=timeout,
        )
    _show_version(out, version)
