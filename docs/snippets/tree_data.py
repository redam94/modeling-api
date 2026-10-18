"""Tree datasets: NetCDF groups, raw HDF5, multi-file versions and ``data.nodes`` (v0.8).

Included by ``docs/guides/tree-data.md``. The script runs offline: it writes the example
files, checks every JSON body and spec that the page shows against the contracts, ingests the
files with the runner's own ingest code (the code a sandboxed run uses), fits a tree-input
model and checks the client calls and CLI commands of the page against the real client and
CLI. The included sections are what readers see; the rest is the check.
"""

from __future__ import annotations

import inspect
import json
import shlex
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any

warnings.filterwarnings("ignore", category=FutureWarning)

SNIPPETS = Path(__file__).resolve().parent

# --8<-- [start:netcdf]
import numpy as np
import pandas as pd
import xarray as xr

rng = np.random.default_rng(0)
rows = np.round(np.arange(-1.6, 1.6, 0.05), 2)  # 64 labels, in arcsec
cols = np.round(np.arange(-1.6, 1.6, 0.05), 2)
image = xr.Dataset(
    {
        "image": (("row", "col"), rng.poisson(20.0, (64, 64)).astype("float32")),
        "noise": (("row", "col"), np.full((64, 64), 4.5, "float32")),
    },
    coords={"row": rows, "col": cols},
)
psf = xr.Dataset({"psf": (("prow", "pcol"), np.full((9, 9), 1 / 81))})

lens = xr.DataTree.from_dict({"/": image, "/instrument/psf": psf})
lens.to_netcdf("lens.nc", engine="h5netcdf")  # NetCDF-4: one HDF5 group for each node
# --8<-- [end:netcdf]

# --8<-- [start:schema]
from mapi_sdk.tree import infer_tree_schema, read_tree_zip, write_tree_zip

schema = infer_tree_schema(lens)
print(list(schema.nodes))  # ['/', '/instrument', '/instrument/psf']
print(schema.default_node())  # '/' (the root has variables)
write_tree_zip(lens, "lens.tree.zip")  # the 'data.tree.zip' wire format
assert read_tree_zip("lens.tree.zip").groups == lens.groups
# --8<-- [end:schema]
assert list(schema.nodes) == ["/", "/instrument", "/instrument/psf"]
assert schema.nodes["/instrument"].vars == {}  # a pure group

# --8<-- [start:raw-hdf5]
import h5py

with h5py.File("lens_raw.h5", "w") as f:  # no dim names, no coords: raw HDF5
    obs = f.create_group("observations")
    obs.create_dataset("image", data=image["image"].values, compression="gzip")
    obs.create_dataset("noise", data=image["noise"].values, compression="gzip")
    f.create_dataset("psf/kernel", data=psf["psf"].values)
    f.create_dataset("axes/col_arcsec", data=cols)
    f.create_dataset("meta/exposure_s", data=np.array([1200.0]))
# --8<-- [end:raw-hdf5]

# ------------------------------------------------- check: the inspection of the raw file
from mapi_runner.worker.ingest import build_upload
from mapi_runner.worker.inspection import inspect_upload
from mapi_runner.worker.tabular import IngestLimits
from mapi_spec.jobs import InspectJob

found = inspect_upload(Path("lens_raw.h5"), InspectJob())
assert found.format == "hdf5"
assert {a.path for a in found.arrays} == {
    "/observations/image",
    "/observations/noise",
    "/psf/kernel",
    "/axes/col_arcsec",
    "/meta/exposure_s",
}
image_info = next(a for a in found.arrays if a.path == "/observations/image")
assert image_info.shape == [64, 64] and image_info.dtype == "float32"
assert "deflate" in image_info.filters

# ------------------------------------- check: the HDF5 version body ingests as the page says
from mapi_spec.api import DatasetVersionCreate

body = DatasetVersionCreate.model_validate_json((SNIPPETS / "tree_version_hdf5.json").read_text())
assert body.mapping is not None
mapped = build_upload(Path("lens_raw.h5"), body.mapping, IngestLimits())
assert isinstance(mapped.data, xr.DataTree)
assert list(mapped.schema.nodes) == ["/", "/psf"]  # type: ignore[union-attr]
assert mapped.schema.nodes["/"].vars["image"].role.value == "target"  # type: ignore[union-attr]
assert mapped.schema.nodes["/psf"].dims["prow"].kind == "index"  # type: ignore[union-attr]
np.testing.assert_allclose(mapped.data["row"].values, rows)
np.testing.assert_allclose(mapped.data["col"].values, cols)
assert any("not mapped" in w and "/meta/exposure_s" in (t or "") for w, t in mapped.warnings)

# ---------------------------------------------- check: NetCDF groups become a tree version
from mapi_spec.ingest import IngestMapping

grouped = build_upload(Path("lens.nc"), IngestMapping(format="netcdf"), IngestLimits())
assert isinstance(grouped.data, xr.DataTree)
assert list(grouped.schema.nodes) == ["/", "/instrument", "/instrument/psf"]  # type: ignore[union-attr]
image.to_netcdf("image_only.nc", engine="h5netcdf")
flat = build_upload(Path("image_only.nc"), IngestMapping(format="netcdf"), IngestLimits())
assert isinstance(flat.data, xr.Dataset)  # a root group only: a plain dataset, as before

# --8<-- [start:files]
weeks = pd.date_range("2024-01-07", periods=40, freq="W-SUN")
channels = ["tv", "search"]


def market_sales(level: float) -> xr.Dataset:
    revenue = level + rng.normal(0.0, 5.0, 40)
    return xr.Dataset({"revenue": (("date",), revenue, {"units": "EUR"})}, coords={"date": weeks})


market_sales(300.0).to_netcdf("sales_nl.nc", engine="h5netcdf")  # one file for each market
market_sales(250.0).to_netcdf("sales_de.nc", engine="h5netcdf")
media = xr.Dataset(
    {"spend": (("market", "channel", "date"), rng.gamma(2.0, 50.0, (2, 2, 40)))},
    coords={"market": ["NL", "DE"], "channel": channels, "date": weeks},
)
media.to_netcdf("media.nc", engine="h5netcdf")
# --8<-- [end:files]

# ------------------------------------ check: the assembly bodies combine as the page says
from mapi_runner.worker.tree_ingest import assemble


def combined(name: str, paths: list[str]) -> Any:
    version = DatasetVersionCreate.model_validate_json((SNIPPETS / name).read_text())
    assert version.assembly is not None
    sources = version.assembly.sources
    uploads = [
        build_upload(Path(p), s.mapping, IngestLimits())
        for p, s in zip(paths, sources, strict=True)
    ]
    return assemble(version.assembly, uploads, IngestLimits())


sales_tree = combined("tree_version_assembly.json", ["sales_nl.nc", "sales_de.nc", "media.nc"])
assert isinstance(sales_tree.data, xr.DataTree)
assert list(sales_tree.schema.nodes) == ["/", "/sales", "/media"]
assert sales_tree.data["sales"].to_dataset()["market"].values.tolist() == ["NL", "DE"]

weather = xr.Dataset(
    {"temperature": (("market", "date"), rng.normal(12.0, 6.0, (2, 30)))},
    coords={"market": ["NL", "DE"], "date": weeks[10:]},
)
media.sum("channel").rename(spend="total_spend").to_netcdf("sales.nc", engine="h5netcdf")
weather.to_netcdf("weather.nc", engine="h5netcdf")
merged = combined("tree_version_merge.json", ["sales.nc", "weather.nc"])
assert isinstance(merged.data, xr.Dataset)  # every source in the root: a plain dataset
assert merged.data.sizes["date"] == 30  # join 'inner': the shared weeks only

# --8<-- [start:select]
from mapi_sdk.testing import toy
from mapi_sdk.tree import select_nodes

tree = toy.tree_dataset()  # '/sales' revenue, '/media' spend, '/adjust' offset
tree["plan"] = xr.DataTree(tree["media"].to_dataset(inherit=False) * 1.1)  # a 2nd 'spend'

flat = select_nodes(
    tree,
    {
        "sales": {"path": "/sales"},
        "media": {"path": "/media", "vars": ["spend"]},
        "plan": {"path": "/plan", "prefix": "plan_"},
    },
    join="exact",
)
print(sorted(flat.data_vars))  # ['plan_spend', 'revenue', 'spend']
# --8<-- [end:select]
assert sorted(flat.data_vars) == ["plan_spend", "revenue", "spend"]

# ----------------------------------------- check: the spec of the page validates and fits
from mapi_sdk.harness import fit_spec
from mapi_spec.loader import load_model_spec
from mapi_spec.tree import NodeSelectionError
from mapi_spec.validate import validate_spec

registry = toy.toy_registry()
spec_text = (SNIPPETS / "tree_spec.yaml").read_text()
spec = load_model_spec(spec_text)
tree_schema = infer_tree_schema(tree, targets=["revenue"])
checked = validate_spec(spec, registry.catalog(), tree_schema.root, tree_schema=tree_schema)
assert checked.ok, [(i.pointer, i.message) for i in checked.issues]
bundle = fit_spec(spec_text, tree, registry=registry)
assert bundle.manifest is not None
from mapi_spec.tree import NodeSelect

try:  # without a prefix, the two 'spend' variables clash
    tree_schema.flat({"media": NodeSelect(path="/media"), "plan": NodeSelect(path="/plan")})
    raise AssertionError("expected a clash")
except NodeSelectionError as exc:
    assert exc.key == "plan" and "prefix" in str(exc)

# side_tables.node (the YAML fragment of the page) is part of the contract
side = load_model_spec(
    spec_text.replace(
        "  bind:\n",
        "  side_tables:\n    fx: {ref: dataset://growth/reference@latest, node: /fx}\n  bind:\n",
        1,
    )
)
assert side.data.side_tables["fx"].node == "/fx"

# --8<-- [start:plugin]
from mapi_sdk.models import FitContext, FitOutput
from mapi_sdk.testing import TreeModelFixture, check_tree_model
from mapi_sdk.testing.toy import LinearModel, PanelSettings
from mapi_spec.spec import FitConfig


class NodeCountModel(LinearModel):
    """toy.linear that also reads the whole tree (capabilities.input = "tree")."""

    id = "demo.node_count"
    version = "1.0.0"
    capabilities = LinearModel.capabilities.model_copy(update={"input": "tree"})

    def fit(self, ds: xr.Dataset, s: PanelSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        out = super().fit(ds, s, cfg, ctx)  # ds: the flat merge of data.nodes, as always
        if ctx.tree is not None:  # the bound tree: binding renames applied in each node
            out.learned["nodes"] = sorted(str(node.path) for node in ctx.tree.subtree)
        return out


registry.register_model(NodeCountModel)
report = check_tree_model(
    NodeCountModel,
    TreeModelFixture(spec=toy.tree_spec("demo.node_count@^1"), tree=toy.tree_dataset(),
                     registry=registry),
)  # fmt: skip
print(report.ok)  # True
# --8<-- [end:plugin]
assert report.ok, report.summary()

# --------------------------------------------- check: the client calls of the page exist
from mapi_client import AssemblyFile, InspectedUpload
from mapi_client._sync import Datasets
from mapi_spec.api import DatasetVersion, UploadInspection, UploadSession


def _example(model: Any) -> Any:
    return model.model_validate(model.model_json_schema()["examples"][0])


class _CheckedDatasets:
    """Binds each call to the signature of the real ``client.datasets`` method."""

    def __getattr__(self, name: str) -> Any:
        method = getattr(Datasets, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            inspect.signature(method).bind(None, *args, **kwargs)
            if name == "inspect_file":
                return InspectedUpload(_example(UploadSession), _example(UploadInspection))
            if name == "inspect":
                return _example(UploadInspection)
            return _example(DatasetVersion)

        return call


api = SimpleNamespace(datasets=_CheckedDatasets())
assert body.mapping is not None
Path("mapping.json").write_text(json.dumps(body.mapping.model_dump(mode="json")))

# --8<-- [start:client-concat]
version = api.datasets.upload_files(
    ["sales_nl.nc", "sales_de.nc"],
    dataset="sales",
    concat="market",
    labels=["NL", "DE"],  # one label for each file, in file order
)
# --8<-- [end:client-concat]

# --8<-- [start:client-merge]
version = api.datasets.upload_files(
    ["sales.nc", "weather.nc"], dataset="sales_weather", merge=True, join="inner"
)
# --8<-- [end:client-merge]

# --8<-- [start:client-nodes]
version = api.datasets.upload_files(
    [
        AssemblyFile("sales_nl.nc", node="/sales", concat_dim="market", concat_label="NL"),
        AssemblyFile("sales_de.nc", node="/sales", concat_dim="market", concat_label="DE"),
        AssemblyFile("media.nc", node="/media"),
    ],
    dataset="sales_tree",
)
print(version.layout, list(version.tree_schema.nodes) if version.tree_schema else None)
# --8<-- [end:client-nodes]

# --8<-- [start:client-hdf5]
staged = api.datasets.inspect_file("lens_raw.h5", dataset="lens")  # upload, no ingest
for array in staged.inspection.arrays:
    print(array.path, array.shape, array.dtype, array.dims_hint)

mapping = IngestMapping.model_validate_json(Path("mapping.json").read_text())  # format hdf5
version = api.datasets.create_version(
    staged.dataset_id, upload_id=staged.upload_id, mapping=mapping
)
version = api.datasets.wait_version(staged.dataset_id, version.version)
# --8<-- [end:client-hdf5]
for name in ("upload", "upload_files", "inspect_file", "inspect", "create_version"):
    assert callable(getattr(Datasets, name))

# --------------------------------------------- check: the CLI commands of the page exist
import typer
from mapi_cli.main import app
from mapi_cli.multifile import assembly_files, is_multi_file

for name in ("image.nc", "psf.nc"):
    lens.to_netcdf(name, engine="h5netcdf")
command = typer.main.get_command(app)
lines = [
    line for line in (SNIPPETS / "cli_tree.sh").read_text().splitlines() if line.startswith("mapi ")
]
assert len(lines) == 6
for line in lines:
    argv = shlex.split(line)[1:]
    group = command.commands[argv[0]]  # type: ignore[attr-defined]
    sub = group.commands[argv[1]]
    ctx = sub.make_context(" ".join(argv[:2]), argv[2:])  # raises on an unknown option
    params = ctx.params
    if argv[1] == "upload":
        files = params["files"]
        multi = is_multi_file(files, params["node"] or [], params["concat"] or [], params["merge"])
        if multi:
            items, _shared = assembly_files(
                files,
                nodes=params["node"] or [],
                concat=params["concat"] or [],
                merge=params["merge"],
                join=params["join"],
            )
            assert len(items) == len(files)
