"""The v0.5 documentation is checked like code.

* every workflow document of the cookbook is valid against the node catalog (with the
  ``sci.*`` models of the science plugin) and runs to the end with the local runner, on
  data shaped like the pages say (the canonical one also runs against a real server:
  ``packages/server/tests/test_docs_client_v05.py``);
* the governance policy file is a valid ``GovernancePolicyCreate``;
* the client snippets compile and import only names that exist;
* every ``mapi`` command in the v0.5 CLI walkthrough exists, with every option it uses;
* the statsmodels page quotes each model's About card exactly as the catalog has it;
* the removed engines and importers are named nowhere in the docs but the changelog.
"""

from __future__ import annotations

import ast
import importlib
import re
import shlex
import warnings
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
SNIPPETS = DOCS / "snippets"
COOKBOOK = sorted(SNIPPETS.glob("cookbook_*.yaml"))


def _doc(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text("utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.mark.parametrize("path", COOKBOOK, ids=lambda p: p.name)
def test_cookbook_documents_are_valid(path: Path) -> None:
    from mapi_spec.workflow import validate_workflow
    from mapi_workflows.testing import test_registry

    res = validate_workflow(_doc(path), catalog=test_registry(science=True).catalog())
    assert res.valid, [(i.pointer, i.message) for i in res.issues]


def _run(name: str, data: Any, params: dict[str, Any], *, science: bool = False) -> Any:
    return _run_full(name, data, params, science=science)[0]


def _run_full(
    name: str, data: Any, params: dict[str, Any], *, science: bool = False
) -> tuple[dict[str, Any], Any]:
    from mapi_spec.workflow import WorkflowRunStatus, WorkflowSpec
    from mapi_workflows.runtime.local import LocalRunner
    from mapi_workflows.testing import test_registry

    spec = WorkflowSpec.model_validate(_doc(SNIPPETS / name))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = LocalRunner(registry=test_registry(science=science)).run(
            spec, inputs={"data": data}, params=params
        )
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    return {n.node_id: n for n in res.nodes}, res


# ================================================================ M5: ladders of sci models

_SCI_LADDERS = ("sci_spatial_ladder", "sci_network_ladder")
_LADDER_FIT = {"draws": 60, "tune": 60, "chains": 2, "seed": 3}
"""Tiny MCMC settings for the run tests (the templates default to 4 x 1000 draws)."""


@pytest.mark.parametrize("template", _SCI_LADDERS)
def test_the_sci_ladder_recipes_are_the_gallery_templates(template: str) -> None:
    """The cookbook document is the gallery template (only the name and project differ)."""
    import json

    from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES

    doc = _doc(SNIPPETS / f"cookbook_{template}.yaml")
    tpl = json.loads(json.dumps(WORKFLOW_TEMPLATES[template].document))
    doc.pop("metadata")
    tpl.pop("metadata")
    assert json.loads(json.dumps(doc)) == tpl
    defaults = {k: v.get("default") for k, v in tpl["params"].items()}
    assert defaults["draws"] == 1000 and defaults["chains"] == 4  # real fits by default


def _report_sections(res: Any) -> list[tuple[str, str]]:
    report = res.values["report"]
    assert isinstance(report, dict) and report["format"] == "mapi.report/v1"
    for s in report["sections"]:
        if s["kind"] == "plot":  # small plots are embedded in the report
            assert s["plot"] is not None, s["node_id"]
    return [(s["kind"], s["node_id"]) for s in report["sections"]]


@pytest.mark.slow
@pytest.mark.parametrize("b1", [1.0, 0.0], ids=["clear", "unclear"])
def test_the_spatial_ladder_ranks_the_rungs_and_checks_the_effect(b1: float) -> None:
    from mapi_plugin_science.areal_data import areal_dataset

    data = areal_dataset(n_row=4, n_col=5, truth={"b1": b1}, seed=1)
    params = {**_LADDER_FIT, "hdi_prob": 0.9 if b1 else 0.999}
    nodes, res = _run_full("cookbook_sci_spatial_ladder.yaml", data, params, science=True)
    rungs = ("iid", "icar", "bym2")
    elpd = {r: nodes[r].result["metrics"]["elpd_loo"] for r in rungs}
    assert nodes["compare"].result["best"] == max(elpd, key=lambda r: elpd[r])
    assert nodes["compare"].result["ranking"] == sorted(rungs, key=lambda r: -elpd[r])
    weights = nodes["weights"].result
    assert weights["method"] == "stacking" and set(weights["weights"]) == set(rungs)
    assert sum(weights["weights"].values()) == pytest.approx(1.0)
    best = nodes["best"].result
    assert best["selected"] == nodes["compare"].result["best"]
    assert best["model_version"] == nodes[best["selected"]].result["model_version"]
    b1_hdi = nodes["effect"].result["params"]["b1"]
    excludes_zero = b1_hdi["hdi_low"] > 0 or b1_hdi["hdi_high"] < 0
    assert excludes_zero is bool(b1)  # the truth decides the HDI at these settings
    agree = nodes["converged"].result["status"] not in {"fail", "error"}
    clear = agree and excludes_zero  # a failed convergence check sends the branch to else
    assert nodes["clear"].result == {"value": clear, "arm": "then" if clear else "else"}
    for nid in ("wide", "narrow", "effect_wide", "effect_narrow"):
        assert nodes[nid].status.value == ("succeeded" if clear else "skipped"), nid
    if clear:  # the refits use the kind of the winner and scaled prior widths
        wide, narrow = nodes["wide"].params["settings"], nodes["narrow"].params["settings"]
        assert wide["kind"] == narrow["kind"] == best["selected"]
        assert wide["slope_sd"] == 2.0 and narrow["slope_sd"] == 0.5
        assert nodes["effect_wide"].result["params"]["b1"]["hdi_low"] > 0
    assert nodes["converged"].result["status"] in {"pass", "warn", "fail"}
    plot = nodes["map"].result
    assert plot["recipe"].startswith("sci.area_map@") and plot["kind"] == "plotly"
    sections = _report_sections(res)
    assert ("decision", "clear") in sections and ("plot", "map") in sections
    tables = {n for k, n in sections if k == "table"}
    assert tables == ({"compare", "weights", "effect", "effect_wide", "effect_narrow"}
                      if clear else {"compare", "weights", "effect"})  # fmt: skip
    assert set(res.outputs) >= {"model", "comparison", "weights", "map", "report"}


@pytest.mark.slow
def test_the_spatial_ladder_reports_chains_that_do_not_agree() -> None:
    """A clear HDI does not count when the convergence check of the best rung fails."""
    from mapi_plugin_science.areal_data import areal_dataset

    data = areal_dataset(n_row=4, n_col=5, truth={"b1": 1.0}, seed=1)
    params = {"draws": 50, "tune": 50, "chains": 4, "seed": 3, "hdi_prob": 0.5}
    nodes, res = _run_full("cookbook_sci_spatial_ladder.yaml", data, params, science=True)
    assert nodes["converged"].result["status"] == "fail"  # 50 draws: the chains disagree
    assert nodes["clear"].result == {"value": False, "arm": "else"}
    for nid in ("wide", "narrow", "effect_wide", "effect_narrow"):
        assert nodes[nid].status.value == "skipped", nid
    report = res.values["report"]
    decision = next(s for s in report["sections"] if s["node_id"] == "clear")
    assert decision["status"] == "else" and "chains" in decision["text"]


def test_the_heat_plate_recipe_needs_two_chains_for_its_gate() -> None:
    """The gate reads R-hat, and one chain gives none: the recipe refuses chains=1."""
    from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES

    tpl = WORKFLOW_TEMPLATES["sci_calibrate_then_design"].document
    assert tpl["params"]["chains"]["minimum"] == 2
    condition = next(n for n in tpl["nodes"] if n["id"] == "good")["with"]["condition"]
    assert "coalesce(nodes.plate.max_rhat" in condition  # a null R-hat fails the gate


@pytest.mark.slow
def test_the_network_ladder_ranks_the_rungs_and_simulates_graphs() -> None:
    from mapi_plugin_science.network_data import network_dataset

    data = network_dataset(n_nodes=16, seed=1)
    nodes, res = _run_full(
        "cookbook_sci_network_ladder.yaml", data, {**_LADDER_FIT, "n_sims": 20}, science=True
    )
    rungs = ("covariates", "sociality", "latent_space")
    elpd = {r: nodes[r].result["metrics"]["elpd_loo"] for r in rungs}
    assert nodes["compare"].result["ranking"] == sorted(rungs, key=lambda r: -elpd[r])
    weights = nodes["weights"].result
    assert weights["method"] == "stacking"
    assert sum(weights["weights"].values()) == pytest.approx(1.0)
    best = nodes["best"].result["selected"]
    assert best == nodes["compare"].result["best"]
    params = nodes["effects"].result["params"]
    assert {"alpha", "beta[same_office]", "beta[same_practice]", "beta[years_gap]"} <= set(params)
    assert params["beta[same_office]"]["mean"] > 0  # truth 1.8
    assert nodes["graph"].result["recipe"].startswith("net.graph@")
    sim = nodes["simulate"].result
    assert sim["mode"] == "simulate" and sim["vars"] == ["graph_stat"]
    scores = res.values["simulations"].data
    stat = scores["graph_stat"]
    assert "transitivity" in stat["statistic"].values
    assert stat.size // stat.sizes["statistic"] == 20  # n_sims graphs
    assert nodes["ppc"].result["recipe"].startswith("core.ridge@")
    sections = _report_sections(res)
    assert ("plot", "graph") in sections and ("plot", "ppc") in sections
    assert {n for k, n in sections if k == "table"} == {"compare", "weights", "effects"}


def test_the_readiness_recipe_runs_and_decides() -> None:
    from mapi_workflows.testing import weekly_series

    nodes = _run(
        "cookbook_readiness_arimax.yaml",
        weekly_series(156, random_walk=True, beta=3.0),
        {"drivers": ["price"], "window": 80, "max_p": 2, "max_q": 2},
    )
    assert nodes["wanders"].result == {"value": True, "arm": "then"}
    assert nodes["folds"].result["n_folds"] == 6
    assert nodes["dm"].result["test"] == "test.diebold_mariano"
    assert nodes["select"].result["beats_baselines"] is True
    assert nodes["governance"].status.value == "succeeded"


@pytest.mark.parametrize("tied", [True, False], ids=["vecm", "var"])
def test_the_var_vecm_recipe_takes_the_right_branch(tied: bool) -> None:
    from mapi_workflows.testing import multi_series

    data = multi_series(220) if tied else multi_series(220, tied=False, seed=9)
    nodes = _run(
        "cookbook_var_vecm.yaml",
        data,
        {"target": "a", "driver": "b" if tied else "c", "maxlags": 4},
    )
    fitted, skipped = ("vecm", "var") if tied else ("var", "vecm")
    assert nodes[fitted].status.value == "succeeded"
    assert nodes[skipped].status.value == "skipped"
    assert nodes["forecast"].status.value == "succeeded"


def test_the_bakeoff_recipe_keeps_a_winner_only_if_it_beats_naive() -> None:
    from mapi_workflows.testing import weekly_series

    nodes = _run(
        "cookbook_forecast_bakeoff.yaml",
        weekly_series(160, drift=0.3, seed=21),
        {"horizons": [1, 4], "n_folds": 5},
    )
    best = nodes["best"].result
    assert nodes["winner"].result["arm"] == ("then" if best["beats_baselines"] else "else")
    expected = "succeeded" if best["beats_baselines"] else "skipped"
    assert nodes["refit"].status.value == expected
    assert nodes["dm_ets"].result["verdict_plain"]


# ============================================================ M5: science recipes (sci.*)

_SCI_COOKBOOK = {
    "sci_phylo_signal": "cookbook_sci_phylo_signal.yaml",
    "sci_calibrate_then_design": "cookbook_sci_calibrate_then_design.yaml",
}
_SCI_FIT = {"draws": 100, "tune": 150, "chains": 2, "seed": 3}
"""Fit sizes for the run tests (the recipes default to 4 x 1000 draws)."""


@pytest.mark.parametrize("template_id", sorted(_SCI_COOKBOOK))
def test_the_science_cookbook_documents_are_the_gallery_templates(template_id: str) -> None:
    from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES

    page = _doc(SNIPPETS / _SCI_COOKBOOK[template_id])
    template = dict(WORKFLOW_TEMPLATES[template_id].document)
    for doc in (page, template):
        doc.pop("metadata")
    assert page == template


def _run_sci(name: str, inputs: dict[str, Any], params: dict[str, Any]) -> tuple[Any, Any]:
    from mapi_spec.workflow import WorkflowRunStatus, WorkflowSpec
    from mapi_workflows.runtime.local import LocalRunner
    from mapi_workflows.testing import test_registry

    spec = WorkflowSpec.model_validate(_doc(SNIPPETS / name))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = LocalRunner(registry=test_registry(science=True)).run(
            spec, inputs=inputs, params={**_SCI_FIT, **params}
        )
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    return {n.node_id: n for n in res.nodes}, res.values


def _phylo_data(group_size_slope: float) -> Any:
    from mapi_plugin_science.phylo_data import PHYLO_SCHEMA, phylo_dataset

    ds = phylo_dataset(
        n_species=30, truth={"slope": {"body": 0.7, "group_size": group_size_slope}}, seed=4
    )
    ds.attrs["mapi_schema"] = PHYLO_SCHEMA.model_dump_json(exclude_none=True)
    return ds


def _sections(report: Any) -> list[tuple[str, str | None]]:
    return [(s["kind"], s.get("node_id")) for s in report["sections"]]


@pytest.mark.slow
@pytest.mark.parametrize(
    ("slope", "arm"), [(0.6, "then"), (0.0, "else")], ids=["clear", "not-clear"]
)
def test_the_phylo_recipe_ranks_the_trees_and_tests_the_slope(slope: float, arm: str) -> None:
    from mapi_workflows.payloads import TablePayload

    nodes, values = _run_sci("cookbook_sci_phylo_signal.yaml", {"data": _phylo_data(slope)}, {})
    # four fits, ranked by elpd_loo; the data have a lambda residual, so a tree wins
    kinds = ("iid", "brownian", "lambda", "ou")
    elpd = {k: nodes[k].result["metrics"]["elpd_loo"] for k in kinds}
    assert all(v is not None for v in elpd.values())
    ranking = nodes["compare"].result["ranking"]
    assert sorted(ranking) == sorted(kinds) and ranking[0] == max(elpd, key=elpd.__getitem__)
    best = nodes["best"].result
    assert best["selected"] == ranking[0] != "iid"
    weights = nodes["weights"].result["weights"]
    assert set(weights) == set(kinds) and abs(sum(weights.values()) - 1) < 1e-6
    # the slope of the focus predictor, read through `focus`, decides the branch
    focus = nodes["slope"].result["focus"]
    assert focus["name"] == "slope[group_size]"
    assert focus["hdi_low"] <= focus["mean"] <= focus["hdi_high"]
    assert focus["excludes_zero"] is (focus["hdi_low"] > 0 or focus["hdi_high"] < 0)
    assert nodes["clear"].result == {"value": arm == "then", "arm": arm}
    assert nodes["ppc"].status.value == ("succeeded" if arm == "then" else "skipped")
    if arm == "then":
        assert focus["hdi_low"] > 0 and focus["p_positive"] > 0.95
        assert nodes["ppc"].result["n_obs"] == 26  # 30 species, 4 without a trait
    # the tree plot and the report with its sections
    assert nodes["tree"].result["recipe"].startswith("sci.phylogeny@")
    assert values["tree"]["kind"] == "plotly"
    sections = _sections(values["report"])
    for want in [("decision", "clear"), ("table", "compare"), ("table", "weights"),
                 ("table", "slope"), ("plot", "tree")]:  # fmt: skip
        assert want in sections, sections
    assert (("metrics", "ppc") if arm == "then" else ("skipped", "ppc")) in sections
    table = TablePayload.model_validate(values["slopes"])
    assert {r["param"] for r in table.rows} == {"slope[body]", "slope[group_size]"}


def _heat_data(*, canonical: bool = False) -> Any:
    import json

    import xarray as xr
    from mapi_plugin_science.heat_plate_data import HEAT_SCHEMA, heat_plate_dataset

    ds = heat_plate_dataset()  # 3 runs with one 10 W heater each, 12 x 6 grid, 12 sensors
    schema = json.loads(HEAT_SCHEMA.model_dump_json(exclude_none=True))
    if canonical:
        names = {"y_cm": "y", "x_cm": "x", "heater_w": "heater_power",
                 "reading_c": "temperature"}  # fmt: skip
        ds = ds.rename(names)
        schema["dims"] = {names.get(k, k): v for k, v in schema["dims"].items()}
        schema["vars"] = {
            names[k]: {**v, "dims": [names.get(d, d) for d in v["dims"]]}
            for k, v in schema["vars"].items()
        }
    ds.attrs["mapi_schema"] = json.dumps(schema)
    power = "heater_power" if canonical else "heater_w"
    layout = xr.zeros_like(ds[power].isel(run=[0])).assign_coords(run=["design"])
    layout[0, 1, 2] = layout[0, 4, 9] = 5.0  # two 5 W heaters
    return ds, xr.Dataset({power: layout})


_HEAT_BIND = {"dims": {"y": "y_cm", "x": "x_cm"},
              "vars": {"heater_power": "heater_w", "temperature": "reading_c"}}  # fmt: skip


@pytest.mark.slow
def test_the_heat_plate_recipe_calibrates_then_predicts_the_layout() -> None:
    from mapi_workflows.payloads import TablePayload

    data, layout = _heat_data()
    nodes, values = _run_sci(
        "cookbook_sci_calibrate_then_design.yaml",
        {"data": data, "layout": layout},
        {"bind": _HEAT_BIND, "max_rhat": 1.5},  # tiny fits do not reach R-hat 1.01
    )
    assert nodes["holdout"].result["folds"][0]["test_groups"][0] in ("cal1", "cal2", "cal3")
    metrics = nodes["fit"].result["metrics"]
    assert metrics["rmse"] < 0.5  # the sensors read the field with 0.2 degC noise
    plate = nodes["plate"].result["params"]
    assert set(plate) == {"k", "h", "h_edge", "eta", "sigma", "spread_length"}
    assert 40 < plate["k"]["mean"] < 80  # the truth is k = 60 W/m/K
    assert nodes["good"].result == {"value": True, "arm": "then"}
    # the expected field of the new layout (one new run), drawn as a map
    design = values["design"].data
    assert list(design["run"].values) == ["design"]
    assert set(design["field"].dims) == {"chain", "draw", "run", "y_cm", "x_cm"}
    hot = design["field"].mean(("chain", "draw")).sel(run="design")
    assert float(hot.isel(y_cm=1, x_cm=2)) > float(hot.isel(y_cm=4, x_cm=0)) > 20.0
    assert nodes["field"].result["recipe"].startswith("sci.field_map@")
    assert nodes["residuals"].result["recipe"].startswith("sci.image_residuals@")
    assert values["field"]["kind"] == values["residuals"]["kind"] == "plotly"
    sections = _sections(values["report"])
    for want in [("decision", "good"), ("table", "plate"), ("metrics", "fit"),
                 ("plot", "residuals"), ("plot", "field")]:  # fmt: skip
        assert want in sections, sections
    assert TablePayload.model_validate(values["parameters"]).rows


@pytest.mark.slow
def test_the_heat_plate_recipe_with_model_names_and_no_layout() -> None:
    data, _ = _heat_data(canonical=True)
    nodes, values = _run_sci(
        "cookbook_sci_calibrate_then_design.yaml", {"data": data}, {"max_rhat": 1.5}
    )
    # without a layout the design step predicts the calibration runs
    assert nodes["good"].result["arm"] == "then"
    assert list(values["design"].data["run"].values) == ["cal1", "cal2", "cal3"]
    assert nodes["field"].status.value == "succeeded"


@pytest.mark.slow
def test_the_heat_plate_recipe_stops_before_the_design_when_the_error_is_too_large() -> None:
    data, layout = _heat_data()
    nodes, values = _run_sci(
        "cookbook_sci_calibrate_then_design.yaml",
        {"data": data, "layout": layout},
        {"bind": _HEAT_BIND, "max_error": 0.01},
    )
    assert nodes["good"].result == {"value": False, "arm": "else"}
    assert nodes["design"].status.value == nodes["field"].status.value == "skipped"
    assert "design" not in values and "field" not in values
    sections = _sections(values["report"])
    assert ("skipped", "design") in sections and ("plot", "residuals") in sections
    assert "Add calibration runs" in values["report"]["summary_plain"]


# ===================================================== M5: the Bayesian model-check templates

_BAYES_TEMPLATES = ("bayes_workflow_audit", "sci_image_model_check")
_BAYES_FIT = {"draws": 100, "tune": 100, "chains": 2, "seed": 3}
"""Tiny fits for the tests; the templates default to 4 x 1000 draws after 1000 tuning steps."""


@pytest.mark.parametrize("template", _BAYES_TEMPLATES)
def test_the_cookbook_shows_the_bayesian_templates_as_they_are(template: str) -> None:
    from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES

    path = SNIPPETS / f"cookbook_{template}.yaml"
    assert _doc(path) == WORKFLOW_TEMPLATES[template].document, (
        f"{path.name} is stale: it must equal the gallery template {template}"
    )


def _science_registry() -> Any:
    """The science registry plus the PyMC tabular models (a Bayesian model that is not a
    ``sci.*`` model)."""
    from mapi_plugin_tabular import PLUGINS as TABULAR
    from mapi_workflows.testing import science_registry

    reg = science_registry()
    reg.register(list(TABULAR))
    return reg


def _case(data: Any, schema: Any, spec_yaml: str, **settings: Any) -> tuple[Any, dict[str, Any]]:
    """A dataset with its schema and the model, settings and binding of its model spec."""
    data.attrs["mapi_schema"] = schema.model_dump_json(exclude_none=True)
    spec = yaml.safe_load(spec_yaml)
    params = {
        "model": spec["model"]["use"],
        "settings": {**spec["model"].get("settings", {}), **settings},
        "bind": spec["data"].get("bind", {}),
        **_BAYES_FIT,
    }
    return data, params


def _run_template(template: str, data: Any, params: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    from mapi_spec.workflow import WorkflowRunStatus
    from mapi_spec.workflow_templates import instantiate_template
    from mapi_workflows.payloads import ReportPayload
    from mapi_workflows.runtime.local import LocalRunner

    spec = instantiate_template(template, name="audit", project="science")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = LocalRunner(registry=_science_registry()).run(
            spec, inputs={"data": data}, params=params
        )
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    report = ReportPayload.model_validate(res.values["report"])
    return {n.node_id: n for n in res.nodes}, {
        s.node_id: s for s in report.sections if s.node_id is not None
    }


def _trusted(nodes: dict[str, Any]) -> bool:
    """The condition of the 'trusted' branch, from the results of the checks."""
    ok = nodes["ppc"].result is not None and nodes["ppc"].result["ok"] is True
    bad = {"fail", "error"}
    return (
        ok
        and nodes["sensitivity"].result["status"] not in bad
        and nodes["recovery"].result["status"] not in bad
    )


def _drew(node: Any, recipe: str, kind: str) -> None:
    assert node.status.value == "succeeded", node.error
    assert node.result["recipe"].startswith(recipe + "@") and node.result["kind"] == kind


@pytest.mark.slow
def test_the_audit_recipe_checks_a_hierarchical_glm() -> None:
    """A PyMC model that is not a sci model: radon in 8 counties."""
    from mapi_plugin_tabular.datasets import RADON_SCHEMA, RADON_SPEC_YAML, radon_dataset

    data, params = _case(radon_dataset(counties=8, homes=64), RADON_SCHEMA, RADON_SPEC_YAML)
    params |= {"prior_predictive": {"draws": 50}, "rhat_max": 1.1, "min_ess": 50, "n_sims": 5,
               "vars": ["intercept", "beta", "sigma"]}  # fmt: skip
    nodes, sections = _run_template("bayes_workflow_audit", data, params)
    assert nodes["has_prior"].result == {"value": True, "arm": "then"}
    _drew(nodes["prior_plot"], "core.prior_predictive", "vega-lite")
    _drew(nodes["pair_plot"], "core.pair", "vega-lite")
    summary = nodes["summary"].result
    assert summary["max_rhat"] < 1.1 and summary["min_ess"] >= 50
    assert {"intercept", "sigma"} <= set(summary["params"])
    assert nodes["converged"].result == {"value": True, "arm": "then"}
    ppc = nodes["ppc"].result
    assert ppc["ok"] is True and 0.75 <= ppc["coverage_90"] <= 1.0
    _drew(nodes["ppc_plot"], "core.ppc", "vega-lite")
    # the model's settings set its priors, so the sensitivity check refits with them
    assert nodes["sensitivity"].result["status"] in ("pass", "warn")
    assert nodes["recovery"].result["status"] in ("pass", "warn")
    assert nodes["trusted"].result == {"value": True, "arm": "then"}
    _drew(nodes["estimates"], "core.posterior_forest", "vega-lite")
    # the report: the decisions, the table, the scores and the four plots, embedded
    for nid in ("has_prior", "converged", "trusted"):
        assert sections[nid].kind == "decision"
    assert sections["summary"].kind == "table" and sections["ppc"].kind == "metrics"
    for nid in ("prior_plot", "pair_plot", "ppc_plot", "estimates"):
        assert sections[nid].kind == "plot" and sections[nid].plot is not None, nid


@pytest.mark.slow
def test_the_audit_recipe_checks_a_science_model_and_stops_when_chains_disagree() -> None:
    """sci.lgcp_grid at a tiny size: the default gate stops the audit, a looser gate (for
    the test only) lets every check run."""
    from mapi_plugin_science.lgcp_data import LGCP_SCHEMA, LGCP_SPEC_YAML, lgcp_dataset

    data, params = _case(lgcp_dataset(), LGCP_SCHEMA, LGCP_SPEC_YAML)
    params["prior_predictive"] = {"draws": 50}
    nodes, sections = _run_template("bayes_workflow_audit", data, params)
    assert nodes["summary"].result["max_rhat"] >= 1.01  # 2 x 100 draws do not converge
    assert nodes["converged"].result == {"value": False, "arm": "else"}
    for nid in ("ppc", "ppc_plot", "sensitivity", "recovery", "trusted", "estimates"):
        assert nodes[nid].status.value == "skipped", nid
        if nid != "trusted":  # a branch that did not run gives no report line
            assert sections[nid].kind == "skipped", nid
    assert "do not agree" in sections["converged"].text
    _drew(nodes["prior_plot"], "core.prior_predictive", "vega-lite")
    assert sections["prior_plot"].plot is not None

    nodes, sections = _run_template(
        "bayes_workflow_audit", data, params | {"rhat_max": 1.5, "min_ess": 5, "n_sims": 5}
    )
    assert nodes["converged"].result == {"value": True, "arm": "then"}
    assert isinstance(nodes["ppc"].result["ok"], bool)
    _drew(nodes["ppc_plot"], "core.ppc", "vega-lite")
    for nid in ("sensitivity", "recovery"):
        assert nodes[nid].status.value == "succeeded", nid
    trusted = _trusted(nodes)
    assert nodes["trusted"].result == {"value": trusted, "arm": "then" if trusted else "else"}
    assert nodes["estimates"].status.value == ("succeeded" if trusted else "skipped")
    assert sections["trusted"].kind == "decision"


@pytest.mark.slow
def test_the_audit_recipe_skips_the_prior_plot_without_prior_draws() -> None:
    """sci.areal_bym2 cannot sample its ICAR prior: prior_predictive {} keeps the fit."""
    from mapi_plugin_science.areal_data import AREAL_SCHEMA, AREAL_SPEC_YAML, areal_dataset

    data, params = _case(areal_dataset(), AREAL_SCHEMA, AREAL_SPEC_YAML)
    params |= {"prior_predictive": {}, "rhat_max": 1.5, "min_ess": 5, "n_sims": 5}
    nodes, sections = _run_template("bayes_workflow_audit", data, params)
    assert nodes["has_prior"].result == {"value": False, "arm": "else"}
    assert nodes["prior_plot"].status.value == "skipped"
    assert nodes["converged"].result == {"value": True, "arm": "then"}
    _drew(nodes["pair_plot"], "core.pair", "vega-lite")
    _drew(nodes["ppc_plot"], "core.ppc", "vega-lite")
    trusted = _trusted(nodes)
    assert nodes["trusted"].result["value"] is trusted
    assert sections["has_prior"].kind == "decision"


@pytest.mark.slow
def test_the_image_check_recipe_on_a_strong_lens() -> None:
    from mapi_plugin_science.lens_data import LENS_SCHEMA, LENS_SPEC_YAML, lens_dataset

    data, params = _case(lens_dataset(), LENS_SCHEMA, LENS_SPEC_YAML)
    nodes, sections = _run_template("sci_image_model_check", data, params | {"shuffle": True})
    _drew(nodes["residuals"], "sci.image_residuals", "plotly")
    _drew(nodes["grid"], "sci.ppc_image_grid", "plotly")
    assert nodes["residuals"].result["source"] == "predictions"
    ppc = nodes["ppc"].result
    assert ppc["ok"] is True and ppc["n_obs"] == data.sizes["row"] * data.sizes["col"]
    assert nodes["summary"].result["n_params"] > 0
    for nid in ("residuals", "grid"):
        assert sections[nid].kind == "plot" and sections[nid].plot is not None, nid
    assert sections["summary"].kind == "table" and sections["ppc"].kind == "metrics"


@pytest.mark.slow
def test_the_image_check_recipe_on_a_cmb_patch_keeps_its_plots_when_the_check_fails() -> None:
    """The tiny CMB spec keeps 8 predictive fields: too few for the predictive check (it
    needs 10). The check fails, the plots and the report do not."""
    from mapi_plugin_science.cmb_data import CMB_SCHEMA, CMB_SPEC_YAML, cmb_dataset

    data, params = _case(cmb_dataset(), CMB_SCHEMA, CMB_SPEC_YAML)
    nodes, sections = _run_template("sci_image_model_check", data, params | {"var": "sky"})
    _drew(nodes["residuals"], "sci.image_residuals", "plotly")
    _drew(nodes["grid"], "sci.ppc_image_grid", "plotly")
    assert nodes["ppc"].status.value == "failed"
    assert sections["ppc"].kind == "failed" and "10 predictive draws" in sections["ppc"].text
    for nid in ("residuals", "grid"):
        assert sections[nid].kind == "plot" and sections[nid].plot is not None, nid


def test_the_policy_file_is_a_valid_policy() -> None:
    from mapi_spec.api import GovernancePolicyCreate

    policy = GovernancePolicyCreate.model_validate(_doc(SNIPPETS / "forecast-policy.yaml"))
    assert policy.require_approval and {c.check for c in policy.checks} >= {
        "baseline.beats_naive",
        "null.permutation_target",
        "null.noise_regressors",
    }


@pytest.mark.parametrize("name", ["client_v05.py", "client_v05_more.py"])
def test_client_snippets_compile_and_import_what_exists(name: str) -> None:
    source = (SNIPPETS / name).read_text("utf-8")
    tree = ast.parse(source)
    compile(tree, name, "exec")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            module = importlib.import_module(node.module)
            for alias in node.names:
                assert hasattr(module, alias.name), f"{node.module}.{alias.name} does not exist"


def _attributes(source: str) -> set[str]:
    return {n.attr for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Attribute)}


def test_client_snippets_call_methods_the_client_has() -> None:
    """``api.workflows.upsert`` and friends: every resource method the snippets use exists on
    the sync or async client."""
    from mapi_client import AsyncClient, Client

    client = Client("http://127.0.0.1:9")
    aclient = AsyncClient("http://127.0.0.1:9")
    known: set[str] = set()
    for c in (client, aclient):
        for res in (
            "workflows",
            "governance",
            "validations",
            "explorations",
            "catalog",
            "datasets",
            "models",
        ):
            known |= {m for m in dir(getattr(c, res)) if not m.startswith("_")}
    source = "\n".join(
        (SNIPPETS / n).read_text("utf-8") for n in ("client_v05.py", "client_v05_more.py")
    )
    for res, method in re.findall(
        r"\b(?:api|aapi|reviewer)\.(workflows|governance|validations|explorations|catalog|datasets|models)\.(\w+)\(",
        source,
    ):
        assert method in known, f"{res}.{method} is not a client method"
    client.close()


def _cli_lines(text: str | None = None) -> list[list[str]]:
    if text is None:
        text = (SNIPPETS / "cli_v05.sh").read_text("utf-8")
    text = text.replace("\\\n", " ")
    out = []
    for line in text.splitlines():
        line = line.split(" #", 1)[0].strip()
        if line.startswith("mapi "):
            out.append(shlex.split(line))
    return out


_BASH = re.compile(r"^```bash\n(.*?)^```", re.M | re.S)


def _cookbook_cli_lines() -> list[list[str]]:
    page = (DOCS / "guides" / "workflow-cookbook.md").read_text("utf-8")
    return [argv for block in _BASH.findall(page) for argv in _cli_lines(block)]


_INDENTED_BASH = re.compile(r"^( *)```bash\n(.*?)^\1```", re.M | re.S)


def _tutorial_cli_lines() -> list[list[str]]:
    pages = sorted((DOCS / "tutorials" / "pymc-challenges").glob("*.md"))
    pages.append(DOCS / "tutorials" / "complex-workflows.md")
    lines = []
    for page in pages:
        for indent, block in _INDENTED_BASH.findall(page.read_text("utf-8")):
            text = "\n".join(line[len(indent) :] for line in block.splitlines())
            text = re.sub(r"<<'?EOF'?.*?^EOF$", "", text, flags=re.M | re.S)
            lines.extend(_cli_lines(text))
    return lines


def test_every_cli_command_and_option_in_the_science_tutorials_exists() -> None:
    _check_cli_lines(_tutorial_cli_lines(), minimum=60)


def test_every_cli_command_and_option_in_the_walkthrough_exists() -> None:
    _check_cli_lines(_cli_lines(), minimum=25)


def test_every_cli_command_and_option_in_the_cookbook_exists() -> None:
    _check_cli_lines(_cookbook_cli_lines(), minimum=10)


def _check_cli_lines(lines: list[list[str]], *, minimum: int) -> None:
    import typer
    from mapi_cli.main import app

    root = typer.main.get_command(app)
    assert len(lines) >= minimum
    for argv in lines:
        cmd: Any = root
        args = argv[1:]
        while args and args[0] == "--json":  # a global option before the command
            args.pop(0)
        while args and hasattr(cmd, "commands") and args[0] in cmd.commands:
            cmd = cmd.commands[args.pop(0)]
        assert not hasattr(cmd, "commands"), f"{' '.join(argv)}: not a command"
        options = {o for p in cmd.params for o in getattr(p, "opts", [])}
        options |= {"--json"}
        for token in args:
            if token.startswith("-") and not token.startswith("--8<--"):
                flag = token.split("=", 1)[0]
                assert flag in options, f"{' '.join(argv)}: {flag} is not an option"


_QUOTE = re.compile(r"^### `(ts\.[a-z_]+)`.*?\n\n> (.+?)\n", re.M | re.S)


def test_the_statsmodels_page_quotes_the_about_cards() -> None:
    from mapi_spec.abouts import builtin_abouts

    page = (DOCS / "models" / "statsmodels.md").read_text("utf-8")
    quotes = dict(_QUOTE.findall(page))
    cards = builtin_abouts()
    expected = {
        i for i in cards if i.startswith("ts.") and i not in ("ts.hsgp_trend", "ts.gaussian_hmm")
    }
    assert set(quotes) == expected
    for model_id, quote in quotes.items():
        assert quote == cards[model_id].summary_plain, model_id
    sections = re.findall(r'--8<-- "statsmodels_catalog\.py:(\w+)"', page)
    assert {f"ts.{s}" for s in sections if s != "common"} == expected


# spelled in pieces so the names themselves appear nowhere outside the changelog either
_GONE = ("at" + "las", "bayes" + "insight", "hierarchical" + "_geo", "pymc[-_]" + "marketing")
REMOVED = re.compile("|".join(_GONE), re.I)


def test_removed_engines_are_named_only_in_the_changelog() -> None:
    hits = []
    for path in DOCS.rglob("*"):
        if not path.is_file() or path.suffix not in {".md", ".py", ".sh", ".yaml", ".yml"}:
            continue
        if "generated" in path.parts or path.name in ("changelog.md", "architecture-plan.md"):
            continue
        for n, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if REMOVED.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:80]}")
    assert not hits, hits
