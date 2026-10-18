"""Write the data of a pymc-challenges case study to a file you can upload (NetCDF, zarr zip).

The ``from_challenges`` loaders build the dataset of a ``sci.*`` model from a local copy of
pymc-challenges. This module writes that dataset to a file, so a web-app user can upload
it (Datasets -> New dataset) and fit the model with the suggested spec::

    python -m mapi_plugin_science.export --list
    python -m mapi_plugin_science.export e07 --challenges ~/pymc-challenges/data --out quakes.nc
    python -m mapi_plugin_science.export e80 --out lens.nc --spec lens.yaml
    python -m mapi_plugin_science.export e50 --synthetic --out assay.nc

``--challenges`` defaults to ``$MAPI_PYMC_CHALLENGES_DATA``; ``--synthetic`` writes the
model's own simulated data instead (no pymc-challenges copy needed). The format comes from
the suffix of ``--out``: ``.nc`` (NetCDF-4 through h5netcdf; a tree dataset keeps one HDF5
group for each node), ``.zarr.zip`` (a plain dataset) or ``.tree.zip`` (a tree dataset, the
``data.tree.zip`` wire format). The file carries the raw schema of the model's data
contract (the ``mapi_schema`` attribute, or ``mapi_tree_schema`` on the root of a tree), so
the ingest gives each variable its role.

Registering a case
------------------
:data:`EXPORTS` maps a case id (``e07``: the notebook number) to an :class:`ExportCase`.
A new model module registers its case with ONE entry at the end of this file, next to the
others::

    _register(
        ExportCase(
            case="e18",
            title="Disease mapping of lip cancer in Scotland",
            notebook="E18_areal_disease_mapping.py",
            model="sci.areal_bym2",
            files=(areal_data.CHALLENGES_FILE,),
            loader=lambda d: areal_data.from_challenges(d / areal_data.CHALLENGES_FILE),
            synthetic=lambda: areal_data.areal_dataset(),
            schema=areal_data.AREAL_SCHEMA,
            project="epidemiology",
            dataset="scotland_lip_cancer",
            spec=areal_data.SCOTLAND_SPEC_YAML,
        )
    )

``loader`` receives the pymc-challenges ``data/`` directory; ``spec`` is a YAML string (or a
function of the exported data, when its settings depend on it) whose ``data.dataset`` names
``dataset://<project>/<dataset>@latest``; ``schema`` is the model's raw ``DataSchema`` (a
``TreeSchema`` for a tree case). Import the data module at the top of this file.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_sdk.tree import TREE_SCHEMA_ATTR, write_tree_zip
from mapi_sdk.wire import write_zarr_zip
from mapi_spec.schema import DataSchema
from mapi_spec.tree import TreeSchema

from mapi_plugin_science import (
    heat_plate_data,
    kpp_data,
    lens_data,
    lgcp_data,
    traction_data,
    turing_data,
)
from mapi_plugin_science.challenges import CHALLENGES_ENV

__all__ = [
    "EXPORTS",
    "FORMATS",
    "SCHEMA_ATTR",
    "ExportCase",
    "ExportData",
    "export_case",
    "main",
    "write_export",
]

ExportData = xr.Dataset | xr.DataTree
"""What a case exports: a plain dataset or (M4) a tree dataset."""

SCHEMA_ATTR: Final = "mapi_schema"
"""Dataset attribute with the raw ``DataSchema`` JSON (read by the ingest)."""

FORMATS: Final = (".nc", ".zarr.zip", ".tree.zip")
"""The output suffixes :func:`write_export` knows."""


@dataclass(frozen=True, slots=True)
class ExportCase:
    """One case study whose data can be exported (see the module docstring)."""

    case: str
    """Case id: ``e`` + the notebook number (``e07``)."""
    title: str
    notebook: str
    """The pymc-challenges notebook (``notebook_src/examples/<notebook>``)."""
    model: str
    """The catalog model that fits the data (``sci.lgcp_grid``)."""
    files: tuple[str, ...]
    """The files the loader reads from the pymc-challenges ``data/`` directory (empty when
    the notebook simulates its data, as E51 and E61 part A do: the loader then needs no
    directory)."""
    loader: Callable[[Path], ExportData]
    """Builds the data from the ``data/`` directory (``from_challenges``)."""
    synthetic: Callable[[], ExportData]
    """The model's own simulated data, in the same layout (no files needed)."""
    schema: DataSchema | TreeSchema
    """The raw schema of the model's data contract (stored in the file)."""
    project: str
    dataset: str
    """The suggested dataset name (the spec reads ``dataset://<project>/<dataset>``)."""
    spec: str | Callable[[ExportData], str]
    """The suggested spec YAML, or a function of the exported data that makes it."""

    @property
    def tree(self) -> bool:
        """True when the case exports a tree dataset."""
        return isinstance(self.schema, TreeSchema)

    def missing(self, data_dir: Path) -> list[str]:
        """The files of :attr:`files` that ``data_dir`` does not have."""
        return [f for f in self.files if not (data_dir / f).is_file()]

    def load(self, data_dir: Path) -> ExportData:
        """The real data, from the pymc-challenges ``data/`` directory."""
        missing = self.missing(data_dir)
        if missing:
            raise FileNotFoundError(
                f"{self.case}: {data_dir} has no {', '.join(missing)} (set --challenges or "
                f"{CHALLENGES_ENV} to the data/ directory of pymc-challenges)"
            )
        return self.loader(data_dir)

    def spec_yaml(self, data: ExportData) -> str:
        """The suggested spec for ``data``."""
        return self.spec if isinstance(self.spec, str) else self.spec(data)


EXPORTS: dict[str, ExportCase] = {}
"""Every exportable case, by case id."""


def _register(case: ExportCase) -> ExportCase:
    if case.case in EXPORTS:
        raise ValueError(f"export case {case.case!r} is registered twice")
    EXPORTS[case.case] = case
    return case


# ==================================================================================== write


def _attr(value: Any) -> Any:
    """An attribute value NetCDF can store (``None`` -> drop, bool -> int, else JSON)."""
    if value is None:
        return None
    if isinstance(value, bool | np.bool_):
        return int(value)
    if isinstance(value, str | int | float | np.integer | np.floating):
        return value
    if isinstance(value, np.ndarray) and value.dtype.kind in "iuf":
        return value
    return json.dumps(value, default=str)


def _clean(attrs: Mapping[Any, Any]) -> dict[str, Any]:
    return {str(k): v for k, v in ((k, _attr(v)) for k, v in attrs.items()) if v is not None}


def _netcdf_ready(ds: xr.Dataset) -> xr.Dataset:
    out = ds.copy()
    out.attrs = _clean(ds.attrs)
    for name in out.variables:
        out[name].attrs = _clean(out[name].attrs)
    return out


def _with_schema(data: ExportData, schema: DataSchema | TreeSchema | None) -> ExportData:
    if schema is None:
        return data
    text = schema.model_dump_json(exclude_none=True)
    if isinstance(data, xr.DataTree):
        if not isinstance(schema, TreeSchema):
            raise TypeError("a tree dataset needs a TreeSchema")
        out = data.copy()
        out.attrs = {**out.attrs, TREE_SCHEMA_ATTR: text}
        return out
    if not isinstance(schema, DataSchema):
        raise TypeError("a plain dataset needs a DataSchema")
    ds = data.copy()
    ds.attrs = {**ds.attrs, SCHEMA_ATTR: text}
    return ds


def _suffix(path: Path) -> str:
    name = path.name.lower()
    for s in FORMATS:
        if name.endswith(s):
            return s
    raise ValueError(f"{path}: unknown format (use one of {', '.join(FORMATS)})")


def write_export(
    data: ExportData, out: str | Path, *, schema: DataSchema | TreeSchema | None = None
) -> Path:
    """Write ``data`` to ``out`` (format by suffix, see :data:`FORMATS`) with ``schema``
    stored in its attributes. Returns the path."""
    path = Path(out)
    kind = _suffix(path)
    data = _with_schema(data, schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == ".nc":
        if isinstance(data, xr.DataTree):
            tree = data.copy()
            for node in tree.subtree:
                node.dataset = _netcdf_ready(node.to_dataset(inherit=False))
            tree.to_netcdf(path, engine="h5netcdf")
        else:
            _netcdf_ready(data).to_netcdf(path, engine="h5netcdf")
    elif kind == ".tree.zip":
        if not isinstance(data, xr.DataTree):
            raise ValueError(f"{path}: a plain dataset is written as .nc or .zarr.zip")
        write_tree_zip(data, path)
    else:
        if isinstance(data, xr.DataTree):
            raise ValueError(f"{path}: a tree dataset is written as .nc or .tree.zip")
        write_zarr_zip(data, path)
    return path


def export_case(
    case: str | ExportCase,
    out: str | Path,
    *,
    data_dir: str | Path | None = None,
    synthetic: bool = False,
    spec_out: str | Path | None = None,
) -> tuple[Path, ExportData]:
    """Load a case (its real data from ``data_dir``, or its synthetic data) and write it to
    ``out``; with ``spec_out`` also write the suggested spec there. Returns the file and
    the data."""
    c = EXPORTS[case] if isinstance(case, str) else case
    if synthetic:
        data = c.synthetic()
    else:
        if data_dir is None and c.files:
            raise ValueError(f"{c.case}: give the pymc-challenges data/ directory")
        data = c.load(Path(data_dir or ".").expanduser())
    path = write_export(data, out, schema=c.schema)
    if spec_out is not None:
        sp = Path(spec_out)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(c.spec_yaml(data).lstrip("\n"), encoding="utf-8")
    return path, data


# ====================================================================================== CLI


def _summary(data: ExportData) -> str:
    if isinstance(data, xr.DataTree):
        parts = [
            f"{node.path} ({', '.join(str(v) for v in node.data_vars) or 'group'})"
            for node in data.subtree
        ]
        return "tree: " + "; ".join(parts)
    dims = ", ".join(f"{k}={v}" for k, v in data.sizes.items())
    return f"variables {', '.join(str(v) for v in data.data_vars)} on {dims}"


def _list() -> str:
    lines = []
    for c in EXPORTS.values():
        kind = "tree" if c.tree else "dataset"
        lines.append(
            f"{c.case}  {c.model:<22} {kind:<7} {c.title}\n"
            f"     notebook: {c.notebook}; "
            f"files: {', '.join(c.files) or 'none (the notebook simulates the data)'}; "
            f"suggested dataset: {c.project}/{c.dataset}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m mapi_plugin_science.export`` (see the module docstring)."""
    import os

    p = argparse.ArgumentParser(
        prog="python -m mapi_plugin_science.export",
        description="Write the data of a pymc-challenges case study to an uploadable file.",
    )
    p.add_argument("case", nargs="?", help="case id, e.g. e07 (see --list)")
    p.add_argument("--list", action="store_true", help="list the cases and exit")
    p.add_argument(
        "--challenges",
        default=os.environ.get(CHALLENGES_ENV),
        help=f"the pymc-challenges data/ directory (default: ${CHALLENGES_ENV})",
    )
    p.add_argument("--out", help="output file: .nc, .zarr.zip or .tree.zip (default: <dataset>.nc)")
    p.add_argument("--spec", help="also write the suggested spec YAML to this file")
    p.add_argument("--synthetic", action="store_true", help="write simulated data instead")
    args = p.parse_args(argv)
    if args.list:
        print(_list())
        return 0
    if not args.case:
        p.error("give a case id (or --list)")
    case = EXPORTS.get(str(args.case).lower())
    if case is None:
        p.error(f"unknown case {args.case!r}; known: {', '.join(EXPORTS)}")
    if not args.synthetic and not args.challenges and case.files:
        p.error(f"give --challenges (or set {CHALLENGES_ENV}), or use --synthetic")
    out = args.out or f"{case.dataset}.nc"
    try:
        path, data = export_case(
            case, out, data_dir=args.challenges, synthetic=args.synthetic, spec_out=args.spec
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {path} ({_summary(data)})")
    print(f"upload it as dataset {case.project}/{case.dataset} and fit {case.model}")
    if args.spec:
        print(f"wrote the suggested spec to {args.spec}")
    return 0


# ==================================================================================== cases

_KPP_REAL_SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: scratch-assay-pc3-kpp, project: biology}
data:
  dataset: dataset://biology/scratch_assay_pc3@latest
  bind:
    dims: {t: hours, x: position}
    vars: {count: cells}
  split: {kind: time_holdout, dim: t, last: 1}
model:
  use: sci.fisher_kpp@^1
  settings: {steps_per_hour: 10, delay: true, max_extension: 4}
  fit: {mode: evaluate_then_refit, seed: 50, tune: 1000, draws: 1000, chains: 4, engine: nutpie}
evaluate: {metrics: [rmse, mae, coverage_90, elpd_loo]}
outputs: {plots: [core.convergence, core.posterior_forest, sci.field_map]}
"""
"""E50: the PC-3 assay with the notebook grid; the 48 h counts are held out."""


def _traction_spec(data: ExportData) -> str:
    assert isinstance(data, xr.Dataset)
    s = traction_data.challenges_settings(data)
    settings = ", ".join(f"{k}: {json.dumps(v)}" for k, v in s.items())
    return f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: pytfm-colony-traction, project: biophysics}}
data:
  dataset: dataset://biophysics/pytfm_colony_ko04@latest
model:
  use: sci.traction_force@^1
  settings: {{{settings}}}
  fit: {{seed: 60, tune: 1000, draws: 1000, chains: 4}}
evaluate: {{metrics: [rmse, mae]}}
outputs: {{plots: [core.convergence, core.posterior_forest, sci.field_map]}}
"""


_register(
    ExportCase(
        case="e07",
        title="Induced earthquakes in Oklahoma (space-time counts)",
        notebook="E07_spatiotemporal_earthquakes.py",
        model="sci.lgcp_grid",
        files=(lgcp_data.CHALLENGES_CSV,),
        loader=lambda d: lgcp_data.from_challenges(d / lgcp_data.CHALLENGES_CSV),
        synthetic=lambda: lgcp_data.lgcp_dataset(),
        schema=lgcp_data.LGCP_SCHEMA,
        project="geoscience",
        dataset="oklahoma_quakes_grid",
        spec=lgcp_data.OKLAHOMA_SPEC_YAML,
    )
)
_register(
    ExportCase(
        case="e50",
        title="Scratch assay of PC-3 cells (Fisher-KPP)",
        notebook="E50_reaction_diffusion_signaling.py",
        model="sci.fisher_kpp",
        files=("scratch_assay_pc3.csv",),
        loader=lambda d: kpp_data.from_challenges(d / "scratch_assay_pc3.csv"),
        synthetic=lambda: kpp_data.kpp_dataset(),
        schema=kpp_data.KPP_SCHEMA,
        project="biology",
        dataset="scratch_assay_pc3",
        spec=_KPP_REAL_SPEC,
    )
)
_register(
    ExportCase(
        case="e60",
        title="Traction forces of a cell colony (pyTFM)",
        notebook="E60_traction_force_microscopy.py",
        model="sci.traction_force",
        files=(traction_data.CHALLENGES_FILE,),
        loader=lambda d: traction_data.from_challenges(d / traction_data.CHALLENGES_FILE),
        synthetic=lambda: traction_data.traction_dataset(),
        schema=traction_data.TRACTION_SCHEMA,
        project="biophysics",
        dataset="pytfm_colony_ko04",
        spec=_traction_spec,
    )
)
_register(
    ExportCase(
        case="e80",
        title="Strong lens SDSS J1627-0053 (HST F814W)",
        notebook="E80_strong_lensing.py",
        model="sci.strong_lens",
        files=(lens_data.CHALLENGES_NPZ,),
        loader=lambda d: lens_data.from_challenges(d / lens_data.CHALLENGES_NPZ),
        synthetic=lambda: lens_data.lens_dataset(),
        schema=lens_data.LENS_SCHEMA,
        project="astronomy",
        dataset="slacs_j1627_f814w",
        spec=lens_data.SLACS_SPEC_YAML,
    )
)
_register(
    ExportCase(
        case="e51",
        title="Calibration of a heated plate (2-D steady heat equation)",
        notebook="E51_heat_equation_design.py",
        model="sci.heat_plate",
        files=(),
        loader=lambda d: heat_plate_data.from_challenges(d),
        synthetic=lambda: heat_plate_data.heat_plate_dataset(),
        schema=heat_plate_data.HEAT_SCHEMA,
        project="engineering",
        dataset="heat_plate_e51",
        spec=heat_plate_data.E51_SPEC_YAML,
    )
)

_register(
    ExportCase(
        case="e61",
        title="Early images of a growing Turing pattern (Schnakenberg, part A)",
        notebook=turing_data.NOTEBOOK,
        model="sci.turing_pattern",
        files=(),
        loader=lambda d: turing_data.from_challenges(d),
        synthetic=lambda: turing_data.turing_dataset(),
        schema=turing_data.TURING_SCHEMA,
        project="biology",
        dataset="e61_turing_early_images",
        spec=turing_data.NOTEBOOK_SPEC_YAML,
    )
)


if __name__ == "__main__":  # pragma: no cover - the CLI
    raise SystemExit(main())
