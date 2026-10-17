"""The v0.5 documentation is checked like code.

* every workflow document of the cookbook is valid against the node catalog and runs to the
  end with the local runner, on data shaped like the pages say (the canonical one also runs
  against a real server: ``packages/server/tests/test_docs_client_v05.py``);
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

    res = validate_workflow(_doc(path), catalog=test_registry().catalog())
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


def _cli_lines() -> list[list[str]]:
    text = (SNIPPETS / "cli_v05.sh").read_text("utf-8").replace("\\\n", " ")
    out = []
    for line in text.splitlines():
        line = line.split(" #", 1)[0].strip()
        if line.startswith("mapi "):
            out.append(shlex.split(line))
    return out


def test_every_cli_command_and_option_in_the_walkthrough_exists() -> None:
    import typer
    from mapi_cli.main import app

    root = typer.main.get_command(app)
    lines = _cli_lines()
    assert len(lines) >= 25
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
