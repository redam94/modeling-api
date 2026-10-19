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


def _run(name: str, data: Any, params: dict[str, Any]) -> Any:
    from mapi_spec.workflow import WorkflowRunStatus, WorkflowSpec
    from mapi_workflows.runtime.local import LocalRunner
    from mapi_workflows.testing import test_registry

    spec = WorkflowSpec.model_validate(_doc(SNIPPETS / name))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = LocalRunner(registry=test_registry()).run(spec, inputs={"data": data}, params=params)
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    return {n.node_id: n for n in res.nodes}


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


# ===================================================== M5: the Bayesian model-check templates

_BAYES_TEMPLATES = ("bayes_workflow_audit", "sci_image_model_check")
_TINY_FIT = {"draws": 100, "tune": 100, "chains": 2, "seed": 3}
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
        **_TINY_FIT,
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
