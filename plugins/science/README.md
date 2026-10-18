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

Outputs that are fields declare a `grid` (`OutputDecl.grid`). The Model Explorer shows them in
its **Fields** section, and the `sci.*` plot recipes draw them.

Each `*_data` module has the raw schema, a small example spec, a synthetic generator with the
known truth and `from_challenges(path)`, which reads the real data file of the case study. Set
`MAPI_PYMC_CHALLENGES_DATA` to the `data/` directory of a local copy of pymc-challenges to run
the tests that use the real files.
