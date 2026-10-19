# pymc-challenges in the web app

These tutorials do the main analysis of 15 pymc-challenges notebooks in the web app. Each
notebook has a model in the science plugin (`sci.*`). You upload the data of the notebook, fit
the model, read the result in the Model Explorer and score the model. You do not write PyMC
code.

## Who these tutorials are for

These tutorials are for persons who know the pymc-challenges notebooks. You know the
questions of the notebooks and the PyMC models that answer them. You want to do the same
analysis on the platform, or show it to persons who do not write code.

Each tutorial tells:

1. The question that the notebook answers.
2. How to get the data with the export helper.
3. How to upload the dataset (**Datasets → New dataset**).
4. How to build the spec (**Build → Insert →** the model), with a table of the settings.
5. How to fit the model and watch the run.
6. How to read the result in the Model Explorer (**Fields**, **Plots**, **Parameters** and
   **Diagnostics**).
7. How to score or simulate (**Score**, or **Try it** on an endpoint).
8. How to run the recipe for the case (**Recipes → Run on my data**).
9. How the numbers compare with the notebook.

Each tutorial also gives the same steps with the CLI and the Python client, in tabs.

**You need:**

- The platform, started with `make dev`, and a project that you can upload datasets to.
- A local copy of pymc-challenges, for the real data. Without it, the export helper writes
  simulated data.
- The modeling-api repository, to run the export helper.

## The cases

| Case | The question of the notebook | Model | Main plot | Tutorial |
|---|---|---|---|---|
| E07 | Where and when do the induced earthquakes of Oklahoma occur, and how does the map change from one era to the next? | `sci.lgcp_grid@1` | `sci.field_map` | [E07](e07-oklahoma-earthquakes.md) |
| E50 | How fast do the cells of a scratch assay move and divide (Fisher-KPP)? | `sci.fisher_kpp@1` | `sci.profile_bands` | [E50](e50-scratch-assay.md) |
| E60 | Which forces does a cell colony apply to its substrate? | `sci.traction_force@1` | `sci.vector_field` | [E60](e60-traction-force.md) |
| E80 | What is the mass of the lens galaxy, and what does the source behind it look like? | `sci.strong_lens@1` | `sci.field_map` | [E80](e80-strong-lens.md) |
| E51 | What are the conductivity and the heat losses of a plate, and which field does a new heater layout give? | `sci.heat_plate@1` | `sci.field_map` | [E51](e51-heat-plate-design.md) |
| E61 | Do the early images of a pattern show a Turing instability, and which parameters cause it? | `sci.turing_pattern@1` | `sci.ppc_image_grid` | [E61](e61-turing-patterns.md) |
| E58 | Where are the single molecules in raw dSTORM frames, and how accurate is each position? | `sci.storm_localisation@1` | `sci.point_pattern` | [E58](e58-storm-localisation.md) |
| E31 | Which binary features make the images of handwritten digits? | `sci.ibp_features@1` | `sci.field_map` | [E31](e31-indian-buffet-digits.md) |
| E77 | How does a language divide the colour chips into colour terms? | `sci.colour_naming@1` | `sci.swatches` | [E77](e77-colour-naming.md) |
| E79 | What is the CMB sky behind the mask of the Large Magellanic Cloud, and what is its power spectrum? | `sci.cmb_field@1` | `sci.power_spectrum` | [E79](e79-cmb-lmc-mask.md) |
| E18 | Which districts of Scotland have a high risk of lip cancer, and does outdoor work explain it? | `sci.areal_bym2@1` | `sci.area_map` | [E18](e18-scottish-lip-cancer.md) |
| E21 | Which effects explain the coworker ties between the lawyers of a law firm? | `sci.network_latent_space@1` | `net.graph` | [E21](e21-law-firm-network.md) |
| E19 | Which proteins of a T cell have a direct effect on each other? | `sci.graphical_model@1` | `net.graph` | [E19](e19-protein-signalling.md) |
| E23 | Is brain size related to group size in primates when the model includes the phylogeny? | `sci.phylo_regression@1` | `sci.phylogeny` | [E23](e23-phylogenetic-regression.md) |
| E81 | How does the wind direction change with the hour and the season? When are the ocelot and its prey active? | `sci.circular_regression@1` | `sci.rose` | [E81](e81-wind-and-activity.md) |

The plots are in the [plot catalog](../../reference/plots.md). The models are in the
[model reference](../../reference/models.md).

These recipes of the workflow gallery use the science models:

| Recipe | Cases |
|---|---|
| `sci_spatial_ladder` | E18 |
| `sci_network_ladder` | E21 |
| `sci_phylo_signal` | E23 |
| `sci_calibrate_then_design` | E51 |
| `sci_image_model_check` | Image models: E31, E58, E61, E79 and E80 |
| `bayes_workflow_audit` | All the cases (see the notes on each page) |

The [complex workflows tutorial](../complex-workflows.md) tells how these recipes work and how
to change them.

## Get the data

The export helper `mapi_plugin_science.export` reads the data files of a notebook. It
prepares the data as the notebook does. Then it writes a file that you can upload in the web
app. Run it from the root of the modeling-api repository:

```bash
# the cases that you can export
uv run python -m mapi_plugin_science.export --list
# the real data of a case, and the suggested spec
uv run python -m mapi_plugin_science.export e18 \
    --challenges <path to pymc-challenges>/data \
    --out scotland_lip_cancer.nc --spec scotland_lip_cancer.yaml
# simulated data from the model (you do not need pymc-challenges)
uv run python -m mapi_plugin_science.export e18 --synthetic --out disease_map_synthetic.nc
```

How the helper works:

- `--challenges` is the `data/` directory of pymc-challenges. If you do not give it, the
  helper uses the environment variable `MAPI_PYMC_CHALLENGES_DATA`.
- The cases `e51` and `e61` need no data file, because the notebooks simulate their data.
- `--synthetic` writes data that the model simulates. Use it to try the steps when you do not
  have a copy of pymc-challenges.
- The suffix of `--out` sets the format: `.nc` (NetCDF-4), `.zarr.zip` or `.tree.zip` (a tree
  dataset). If you do not give `--out`, the file is `<dataset>.nc`.
- `--spec` also writes the suggested spec. The spec reads the suggested dataset
  (`dataset://<project>/<dataset>@latest`). Use that project and dataset name when you
  upload the file.
- The file keeps the schema of the model in the attribute `mapi_schema`. Thus the upload gives
  the correct role to each variable, and you do not map columns.

The [model reference](../../reference/models.md#export-the-data-of-a-case-study) lists the data
file and the suggested dataset of each case. The case `e81b` exports the second dataset of
E81, the camera-trap times at Barro Colorado.

## From notebook concepts to platform concepts

The notebooks and the platform use different words for the same things. This table gives the
platform equivalent of each notebook concept.

| In the notebook | On the platform |
|---|---|
| `with pm.Model() as model:` and the code of the model | A model of the science plugin (`sci.*`) and its **settings** in a spec. The settings select the variant of the model, for example `kind: bym2`. |
| Load the data with pandas or NumPy | A **dataset** version. Upload it once. Each fit reads a fixed version. |
| The names of the arrays in the model | `data.bind` in the spec. It maps the names in your dataset to the names of the model. |
| `pm.sample(draws=..., tune=..., chains=...)` | The `fit` block of the spec, and the **Fit** button. The result is a **model version**. |
| The `InferenceData` groups (`posterior`, `posterior_predictive`, `observed_data`, `sample_stats`, `constant_data`) | The **bundle** of the model version. The Model Explorer and the plots read the bundle. |
| `az.summary` | The **Parameters** and **Diagnostics** sections of the Model Explorer. In a workflow, the node `model.summary`. |
| `az.plot_*` | The **plot recipes**: the **Plots** tab of a model version, and **Catalog → Plots**. In a workflow, the node `plot.render`. |
| `az.loo` and `az.compare` | `evaluate: {metrics: [elpd_loo]}` on each fit, then `compare.models` (the ranking) and `compare.stacking` (the stacking weights). |
| `pm.sample_prior_predictive` | `fit: {prior_predictive: {draws: 500}}` in the spec, and the plot `core.prior_predictive`. |
| `pm.sample_posterior_predictive` | The **Predict** and **Simulate** modes of the **Score** page. In a workflow, `evaluate.ppc` and the plots `core.ppc` and `sci.ppc_image_grid`. |
| `pm.set_data` and a prediction on new data | A score run with your new values (**Score**, or **Try it** on an endpoint). In a workflow, `model.score`. |
| A loop over model variants | A **model ladder**: one `model.fit` node for each variant in a workflow. |
| The decision at the end of the notebook (a markdown cell) | `model.summary` and a decision node (`branch.if`) that reads its results, for example `nodes.s.params.b1.hdi_low > 0`. The run report gives the decision in plain words. |

## What is not ported yet

The tutorials cover 15 of the 88 notebooks (E01 to E85 and D01 to D03). The E61 tutorial covers only part A of
its notebook. These notebooks have no science model and no tutorial:

- D01 to D03 (data preparation, imperfect data, exploratory analysis).
- E01 to E06 and E08 to E17.
- E20 and E22.
- E24 to E30.
- E32 to E49.
- E52 to E57 and E59.
- E62 to E76 and E78.
- E82 to E85.

Some of these topics have a different model in the catalog, for example the marketing-mix and
time-series models. To find a model for your question, use **Help me choose**.

The ported cases also do not include all the parts of their notebooks. The section
"Differences from the notebook" of each tutorial tells what is not in the port.
