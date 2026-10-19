# mapi-plugin-science

Scientific model plugins for modeling-api (`sci.*`, entry point `mapi.models: science`). They
are ports of the case studies of [pymc-challenges](https://github.com/redam94/pymc-challenges).
Most of their data are maps, images and fields on a regular grid. Some read the areas of a map,
a graph, a network of variables, a phylogeny or angles (directions and times of day).

| Plugin | Model | Representation | Score modes |
|---|---|---|---|
| `sci.lgcp_grid@1` | Log-Gaussian Cox process on a space-time count grid, 2-D HSGP map for each era (E07) | draws | predict, forecast, evaluate |
| `sci.fisher_kpp@1` | Fisher-KPP reaction-diffusion PDE solved in the model graph (E50) | draws | predict, forecast, evaluate |
| `sci.traction_force@1` | Traction force microscopy as a Gaussian inverse problem in Fourier space (E60) | draws | predict, evaluate |
| `sci.strong_lens@1` | Pixel forward model of a galaxy-scale strong lens in an HST image (E80) | draws | predict, simulate, evaluate |
| `sci.heat_plate@1` | 2-D steady heat equation of a heated plate, a finite-volume system solved in the model graph (E51) | draws | predict, simulate, evaluate |
| `sci.turing_pattern@1` | Schnakenberg kinetics from early images of a growing Turing pattern, exact Fourier-mode likelihood (E61 part A) | draws | predict, simulate, evaluate |
| `sci.storm_localisation@1` | Single-molecule localisation in raw EMCCD STORM frames: detection and camera calibration in NumPy, batched pixel-integrated Gaussian PSF fit with Poisson counts (E58) | draws | predict, evaluate |
| `sci.ibp_features@1` | Latent binary features of small images: truncated Indian buffet process, patterns summed out (E31) | draws | predict, evaluate |
| `sci.colour_naming@1` | Colour naming of one language (World Color Survey): softmax over quadratic CIELAB logits, pooled or with speaker effects (E77) | draws | predict, simulate, evaluate |
| `sci.cmb_field@1` | Field-level inference of a masked CMB map with a Wandelt-Eriksen Gibbs sampler: sky draws behind the mask and bandpowers (E79) | draws | predict, simulate, evaluate |
| `sci.areal_bym2@1` | Disease mapping on a graph of areas: iid, ICAR, proper CAR or BYM2 area effects, polygons kept for the maps (E18) | draws | predict, simulate, evaluate |
| `sci.network_latent_space@1` | Ties between the nodes of a graph: pair covariates, sociality and Procrustes-aligned latent positions (E21) | draws | predict, simulate, evaluate |
| `sci.graphical_model@1` | Gaussian graphical model: partial correlations and edge probabilities, dense LKJ or horseshoe neighbourhood selection (E19) | draws | predict, simulate, evaluate |
| `sci.phylo_regression@1` | Regression across species with residuals correlated by a phylogeny: iid, Brownian, Pagel's lambda or OU (E23) | draws | predict, simulate, evaluate |
| `sci.circular_regression@1` | Von Mises regression with a canonical link on harmonics of the hour and the day of year, and log-Fourier activity densities (E81) | draws | predict, simulate, evaluate |

Outputs that are fields declare a `grid` (`OutputDecl.grid`). The Model Explorer shows them in
its **Fields** section, and the `sci.*` plot recipes draw them.

Each `*_data` module has the raw schema, a small example spec, a synthetic generator with the
known truth and `from_challenges(path)`, which reads the real data file of the case study (E51
and E61 simulate their data in the notebook: their loaders rebuild that experiment and read no
file). Set
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
