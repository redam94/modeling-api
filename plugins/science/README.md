# mapi-plugin-science

Scientific model plugins for modeling-api (`sci.*`, entry point `mapi.models: science`). They
are ports of the case studies of [pymc-challenges](https://github.com/redam94/pymc-challenges).
Their data are maps, images and fields on a regular grid.

| Plugin | Model | Representation | Score modes |
|---|---|---|---|
| `sci.lgcp_grid@1` | Log-Gaussian Cox process on a space-time count grid, 2-D HSGP map for each era (E07) | draws | predict, forecast, evaluate |
| `sci.fisher_kpp@1` | Fisher-KPP reaction-diffusion PDE solved in the model graph (E50) | draws | predict, forecast, evaluate |
| `sci.traction_force@1` | Traction force microscopy as a Gaussian inverse problem in Fourier space (E60) | draws | predict, evaluate |
| `sci.strong_lens@1` | Pixel forward model of a galaxy-scale strong lens in an HST image (E80) | draws | predict, simulate, evaluate |
| `sci.heat_plate@1` | 2-D steady heat equation of a heated plate, a finite-volume system solved in the model graph (E51) | draws | predict, simulate, evaluate |
| `sci.turing_pattern@1` | Schnakenberg kinetics from early images of a growing Turing pattern, exact Fourier-mode likelihood (E61 part A) | draws | predict, simulate, evaluate |

Outputs that are fields declare a `grid` (`OutputDecl.grid`). The Model Explorer shows them in
its **Fields** section, and the `sci.*` plot recipes draw them.

Each `*_data` module has the raw schema, a small example spec, a synthetic generator with the
known truth and `from_challenges(path)`, which reads the real data file of the case study (E51
simulates its data in the notebook: its loader rebuilds that experiment and reads no file). Set
`MAPI_PYMC_CHALLENGES_DATA` to the `data/` directory of a local copy of pymc-challenges to run
the tests that use the real files.

`python -m mapi_plugin_science.export <case> --challenges <data dir> --out <file.nc>` writes
the dataset of a case study to a file that you can upload (`--list` shows the cases,
`--synthetic` writes the simulated data, `--spec` also writes the suggested spec). See
"Export the data of a case study" in `docs/reference/models.md`.

## Tree datasets

`sci.strong_lens` declares `capabilities.input = "tree"`, so it also reads a tree dataset version
(for example an HDF5 or NetCDF file with groups). `data.nodes` selects the node with `image` and
`weight` (and `mask`). When the tree has a `/psf` node with `psf(psf_row, psf_col)`, the fit uses
that PSF. Otherwise it uses the flat `psf`, as for a plain dataset. Set `psf_source: tree` to
select only the image node. The `/psf` node is then required. `lens_data.to_tree(ds)` turns a
lens dataset into this layout (`LENS_TREE_SCHEMA`), and `LENS_TREE_SPEC_YAML` fits it:

```yaml
data:
  dataset: dataset://astronomy/mock_lens_tree@latest
  nodes: {image: {path: /image}}
model:
  use: sci.strong_lens@^1
  settings: {psf_source: tree, pixel_scale: 0.1}
```
