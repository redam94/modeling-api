"""``mapi plugin init|test|validate|doctor`` (v0.18): scaffolds for every kind and base, the
namespace rules, the validate report (golden), broken entry points and the doctor checks."""

from __future__ import annotations

import importlib.metadata
import json
import re
import tomllib
from collections.abc import Callable
from itertools import chain
from pathlib import Path
from typing import Any

import pytest
import respx
from mapi_cli import plugin_doctor
from mapi_cli.commands import plugin
from mapi_cli.commands.plugin import Base, Kind, plugin_names, render_plugin
from mapi_spec.catalog import Catalog, CatalogDiagnostic
from mapi_spec.errors import MapiError
from mapi_spec.namespaces import reserved_namespace
from typer.testing import Result

Invoke = Callable[..., Result]
AsJson = Callable[[Result], Any]

GOLDEN = Path(__file__).parent / "golden" / "plugin_validate.json"
ALL_KINDS = [k.value for k in Kind]
URL = "http://api.test"


def _kind_args(kinds: list[str]) -> list[str]:
    return list(chain.from_iterable(("--kind", k) for k in kinds))


def _init(mapi: Invoke, tmp_path: Path, name: str, *args: str) -> Path:
    res = mapi("plugin", "init", name, "--dir", str(tmp_path), *args)
    assert res.exit_code == 0, res.output
    return tmp_path / name


# ================================================================================== init


def test_plugin_init_and_contract_kit(mapi: Invoke, tmp_path: Path, as_json: AsJson) -> None:
    res = mapi("--json", "plugin", "init", "acme-demand", "--dir", str(tmp_path))
    assert res.exit_code == 0, res.output
    doc = as_json(res)
    assert doc["plugin_id"] == "acme_demand.group_mean" and doc["module"] == "acme_demand"
    assert doc["kinds"] == ["model"] and doc["ids"] == {"model": "acme_demand.group_mean"}
    root = tmp_path / "acme-demand"
    assert (root / "src/acme_demand/model.py").is_file()
    pyproject = (root / "pyproject.toml").read_text()
    assert 'acme_demand = "acme_demand.model:MODELS"' in pyproject
    assert "{{" not in (root / "src/acme_demand/model.py").read_text()
    res = mapi("--json", "plugin", "test", str(root))
    report = as_json(res)
    assert res.exit_code == 0, report["output"]
    assert report["passed"] is True and "2 passed" in report["output"]
    assert mapi("plugin", "init", "acme-demand", "--dir", str(tmp_path)).exit_code == 1


def test_plugin_init_rejects_reserved_namespaces(mapi: Invoke, tmp_path: Path) -> None:
    res = mapi("plugin", "init", "x", "--namespace", "mmm", "--dir", str(tmp_path))
    assert res.exit_code == 1 and "reserved" in res.stderr
    assert mapi("plugin", "init", "Bad Name", "--dir", str(tmp_path)).exit_code == 1


def test_plugin_test_needs_tests(mapi: Invoke, tmp_path: Path) -> None:
    res = mapi("plugin", "test", str(tmp_path))
    assert res.exit_code == 1 and "tests" in res.stderr


_COMBOS = [
    *[pytest.param([k], "custom", id=k) for k in ALL_KINDS],
    pytest.param(ALL_KINDS, "custom", id="every-kind"),
    pytest.param(["model"], "pymc", id="model-pymc"),
    pytest.param(["model"], "sklearn", id="model-sklearn"),
    pytest.param(["template", "node"], "custom", id="node-and-template"),
    pytest.param(["model", "recipe"], "pymc", id="pymc-and-recipe"),
]


@pytest.mark.parametrize(("kinds", "base"), _COMBOS)
def test_every_kind_and_base_renders(tmp_path: Path, kinds: list[str], base: str) -> None:
    names = plugin_names("acme-lab", None, [Kind(k) for k in kinds], Base(base))
    files = render_plugin(tmp_path, names, [Kind(k) for k in kinds], Base(base))
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"\{\{\w+\}\}", text), f"{f}: placeholder left"
        if f.suffix == ".py":
            compile(text, str(f), "exec")  # valid Python
    doc = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    groups = doc["project"]["entry-points"]
    assert sorted(groups) == sorted(plugin._KINDS[Kind(k)].group for k in kinds)
    for k in kinds:
        spec = plugin._KINDS[Kind(k)]
        assert groups[spec.group] == {"acme_lab": f"acme_lab.{spec.module}:{spec.export}"}
        assert (tmp_path / "src/acme_lab" / f"{spec.module}.py").is_file()
        assert (tmp_path / "tests" / spec.test_file).is_file()
    readme = (tmp_path / "README.md").read_text()
    assert readme.rstrip().split("\n\n")[-1].startswith("If something is missing")
    for heading in ("## Test it", "## See it on your stack (3 ways)", "## See it in the app"):
        assert heading in readme
    # v0.18 review: 'uv lock' alone never installed a new workspace member, and WHEELS is
    # read relative to the platform checkout, not the plugin
    assert "`make install`" in readme and "`uv lock`" not in readme
    assert 'WHEELS="path/to/acme-lab/dist/*.whl"' in readme
    if "template" in kinds:
        templates = (tmp_path / "src/acme_lab/templates.py").read_text()
        assert ("test.acme_lab_zero_share" in templates) == ("node" in kinds)


@pytest.mark.parametrize("base", list(Base))
def test_scaffold_titles_do_not_look_like_built_in_titles(tmp_path: Path, base: Base) -> None:
    # v0.19 live: the upload kit refuses a title whose skeleton matches a built-in title
    # (title_confusable), so a fresh `mapi plugin init --upload` scaffold must not use one
    from mapi_spec.abouts import builtin_abouts
    from mapi_spec.text_safety import skeleton

    builtin = {skeleton(a.title): a.title for a in builtin_abouts().values() if a is not None}
    kinds = tuple(Kind)
    names = plugin_names("acme-lab", None, kinds, base)
    files = render_plugin(tmp_path, names, kinds, base)
    titles: list[str] = []
    for f in files:
        if f.suffix != ".py" or f.parent.name == "tests":
            continue
        text = f.read_text(encoding="utf-8")
        titles += re.findall(r'\btitle="([^"{}]+)"', text)
        titles += re.findall(r'\babout\(\s*"([^"]+)"', text)
    assert len(titles) >= 4, titles
    clashes = {t: builtin[skeleton(t)] for t in titles if skeleton(t) in builtin}
    assert not clashes, f"scaffold titles that look like built-in titles: {clashes}"


def test_every_template_file_is_used(tmp_path: Path) -> None:
    root = Path(plugin.__file__).parents[1] / "plugin_template"
    on_disk = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*.tmpl")
        if not p.name.endswith(".md.tmpl") or p.parent.name == "common"
    }
    used: set[str] = set()
    for base in Base:
        kinds = tuple(Kind)
        names = plugin_names("acme-lab", None, kinds, base)
        used |= set(plugin._files(names, kinds, base))
    assert on_disk == used


@pytest.mark.parametrize(
    "namespace", ["mmm", "causal", "sci", "examples", "lib", "team", "t", "t_acme", "tenant_x"]
)
def test_reserved_namespaces_are_refused_with_the_reason(
    mapi: Invoke, tmp_path: Path, as_json: AsJson, namespace: str
) -> None:
    res = mapi("--json", "plugin", "init", "x", "--namespace", namespace, "--dir", str(tmp_path))
    assert res.exit_code == 1
    reason = reserved_namespace(namespace)
    assert reason is not None
    assert reason in as_json(res)["error"]["detail"]
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("import", "keyword"),
        ("class", "keyword"),
        ("match", "keyword"),
        ("json", "standard library"),
    ],
)
def test_keyword_and_stdlib_module_names_are_refused(
    mapi: Invoke, tmp_path: Path, as_json: AsJson, name: str, why: str
) -> None:
    # v0.18 review: 'import' scaffolded a package whose own tests failed with SyntaxError
    res = mapi("--json", "plugin", "init", name, "--dir", str(tmp_path))
    assert res.exit_code == 1
    assert why in as_json(res)["error"]["detail"]
    assert not (tmp_path / name).exists()
    with pytest.raises(MapiError):
        plugin_names(name, None)
    assert plugin_names(f"acme-{name}", None)["module"] == f"acme_{name}"


def test_a_template_namespace_head_must_be_free(mapi: Invoke, tmp_path: Path) -> None:
    """Template ids are ``<namespace>_<name>`` and the loaders read the namespace up to the
    first ``_``: ``team_x`` is fine for a model, not for a template."""
    res = mapi(
        "plugin", "init", "x", "--namespace", "team_x", "-k", "template", "--dir", str(tmp_path)
    )
    assert res.exit_code == 1 and "Template ids start with 'team'" in res.stderr
    _init(mapi, tmp_path, "y", "--namespace", "team_x", "-k", "node")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["mapi-spec"], "shadow a platform package"),
        (["acme", "--base", "pymc", "-k", "recipe"], "--base applies to --kind model"),
        (["acme", "--namespace", "Acme"], "namespace 'Acme' is not valid"),
        (["acme", "-k", "template", "--namespace", "a" * 60], "longer than 64 characters"),
    ],
)
def test_bad_names_are_refused(mapi: Invoke, tmp_path: Path, args: list[str], message: str) -> None:
    res = mapi("plugin", "init", *args, "--dir", str(tmp_path))
    assert res.exit_code == 1 and message in res.stderr


def test_every_kind_passes_its_contract_kit(mapi: Invoke, tmp_path: Path, as_json: AsJson) -> None:
    root = _init(mapi, tmp_path, "acme-lab", *_kind_args(ALL_KINDS))
    res = mapi("--json", "plugin", "test", str(root))
    report = as_json(res)
    assert res.exit_code == 0, report["output"]
    assert "12 passed" in report["output"]


@pytest.mark.slow
@pytest.mark.parametrize("base", ["pymc", "sklearn"])
def test_model_bases_pass_their_contract_kit(
    mapi: Invoke, tmp_path: Path, as_json: AsJson, base: str
) -> None:
    root = _init(mapi, tmp_path, f"acme-{base}", "--base", base)
    report = as_json(mapi("--json", "plugin", "test", str(root)))
    assert report["passed"], report["output"]
    res = mapi("--json", "plugin", "validate", str(root))
    assert res.exit_code == 0, res.output


# ============================================================================== validate


def _projection(report: dict[str, Any]) -> dict[str, Any]:
    """The stable part of a validate report (no paths, no full manifests)."""
    catalog = report["catalog"]
    return {
        "distribution": report["distribution"],
        "ok": report["ok"],
        "exit_code": report["exit_code"],
        "entry_points": [
            [e["group"], e["name"], e["value"], e["loaded"]] for e in report["entry_points"]
        ],
        "checks": [[c["kind"], c["subject"], c["name"], c["status"]] for c in report["checks"]],
        "catalog": {
            "models": [[m["manifest"]["id"], m.get("provider")] for m in catalog["models"]],
            "steps": [[s["id"], s.get("provider")] for s in catalog["steps"]],
            "plots": [[p["id"], p.get("provider")] for p in catalog["plots"]],
            "workflow_nodes": [[n["id"], n.get("plugin")] for n in catalog["workflow_nodes"]],
            "workflow_templates": [
                [t["id"], t.get("provider")] for t in catalog["workflow_templates"]
            ],
        },
    }


def test_validate_json_matches_the_golden(mapi: Invoke, tmp_path: Path, as_json: AsJson) -> None:
    root = _init(mapi, tmp_path, "acme-lab", *_kind_args(ALL_KINDS))
    res = mapi("--json", "plugin", "validate", str(root))
    report = as_json(res)
    assert res.exit_code == 0, json.dumps(report, indent=2)[:4000]
    Catalog.model_validate(report["catalog"])  # the fragment is a valid catalog
    assert _projection(report) == json.loads(GOLDEN.read_text())


def test_validate_prints_every_check(mapi: Invoke, tmp_path: Path) -> None:
    root = _init(mapi, tmp_path, "acme-lab", "-k", "step", "-k", "node")
    res = mapi("plugin", "validate", str(root))
    assert res.exit_code == 0, res.output
    assert "[PASS] step acme_lab.winsorize: about" in res.stdout
    assert "[PASS] node test.acme_lab_zero_share: contract" in res.stdout
    assert "all checks passed" in res.stdout


def test_a_broken_entry_point_exits_2_and_the_others_are_checked(
    mapi: Invoke, tmp_path: Path, as_json: AsJson
) -> None:
    root = _init(mapi, tmp_path, "acme-lab", "-k", "recipe", "-k", "node")
    plots = root / "src/acme_lab/plots.py"
    plots.write_text(plots.read_text() + "\nraise RuntimeError('the chart library is missing')\n")
    res = mapi("plugin", "validate", str(root))
    assert res.exit_code == 2
    assert "cannot load mapi.plots:acme_lab = acme_lab.plots:RECIPES" in res.stderr
    assert "RuntimeError: the chart library is missing" in res.stderr
    assert "[PASS] node test.acme_lab_zero_share: contract" in res.stdout
    report = as_json(mapi("--json", "plugin", "validate", str(root)))
    assert report["exit_code"] == 2 and report["ok"] is False
    broken = [e for e in report["entry_points"] if not e["loaded"]]
    assert [e["group"] for e in broken] == ["mapi.plots"]


def test_a_failed_check_exits_1(mapi: Invoke, tmp_path: Path, as_json: AsJson) -> None:
    root = _init(mapi, tmp_path, "acme-lab")
    model = root / "src/acme_lab/model.py"
    model.write_text(
        model.read_text().replace('id = "acme_lab.group_mean"', 'id = "mmm.group_mean"')
    )
    res = mapi("--json", "plugin", "validate", str(root))
    assert res.exit_code == 1
    failed = [c for c in as_json(res)["checks"] if c["status"] == "fail"]
    assert [(c["subject"], c["name"]) for c in failed] == [("mmm.group_mean", "namespace")]
    assert "belongs to the built-in plugins" in failed[0]["detail"]


def test_validate_needs_a_package(mapi: Invoke, tmp_path: Path) -> None:
    res = mapi("plugin", "validate", str(tmp_path))
    assert res.exit_code == 1 and "pyproject.toml" in res.stderr


def test_a_package_without_entry_points_fails(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "empty"\nversion = "0.1.0"\n')
    report = plugin_doctor.validate_package(tmp_path)
    assert report["exit_code"] == 1
    assert report["checks"][0]["detail"] == "pyproject.toml declares no mapi.* entry points"


# ================================================================================ doctor


def _doctor_fixture(digest: str) -> dict[str, Any]:
    """A doctor report as the subprocess prints it (one extension, nothing wrong)."""
    return {
        "ok": True,
        "exit_code": 0,
        "entry_points": [
            {
                "group": "mapi.plots",
                "name": "acme",
                "value": "acme_lab.plots:RECIPES",
                "distribution": "acme-lab",
                "builtin": False,
                "loaded": True,
                "error": None,
            }
        ],
        "extensions": ["acme-lab"],
        "diagnostics": [],
        "routing": [{"namespace": "acme_lab", "distribution": "acme-lab", "image": None}],
        "digest": {"catalog_digest": digest, "mode": "checkout"},
        "catalog_ids": {"models": [], "steps": [], "plots": ["acme_lab.weekly_total@1.0.0"]},
        "problems": [],
        "warnings": [],
    }


def _server_catalog(digest: str) -> dict[str, Any]:
    return {"digest": digest, "models": [], "steps": [], "plots": []}


def test_doctor_flags_a_stale_server_digest(
    mapi: Invoke, api: respx.MockRouter, as_json: AsJson, monkeypatch: pytest.MonkeyPatch
) -> None:
    local, served = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    monkeypatch.setattr(plugin, "_run_child", lambda args, env=None: _doctor_fixture(local))
    api.get(f"{URL}/v1/catalog").respond(200, json=_server_catalog(served))
    res = mapi("--url", URL, "--json", "plugin", "doctor", "--server")
    assert res.exit_code == 1
    report = as_json(res)
    assert report["server"]["stale"] is True
    assert report["server"]["missing_on_server"] == {"plots": ["acme_lab.weekly_total@1.0.0"]}
    (problem,) = report["problems"]
    assert problem["code"] == "stale_catalog" and "acme_lab.weekly_total" in problem["message"]
    text = mapi("--url", URL, "plugin", "doctor", "--server")
    assert text.exit_code == 1 and "make catalog-refresh" in text.stderr


def test_doctor_passes_when_the_server_digest_matches(
    mapi: Invoke, api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = "sha256:" + "c" * 64
    fixture = _doctor_fixture(digest)
    fixture["catalog_ids"]["plots"] = []
    monkeypatch.setattr(plugin, "_run_child", lambda args, env=None: fixture)
    api.get(f"{URL}/v1/catalog").respond(200, json=_server_catalog(digest))
    res = mapi("--url", URL, "plugin", "doctor", "--server")
    assert res.exit_code == 0, res.output
    assert "no problems found" in res.stdout and "acme_lab.plots:RECIPES" in res.stdout


def test_doctor_exits_2_when_the_server_cannot_be_read(
    mapi: Invoke, api: respx.MockRouter, as_json: AsJson, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        plugin, "_run_child", lambda args, env=None: _doctor_fixture("sha256:" + "d" * 64)
    )
    api.get(f"{URL}/v1/catalog").respond(
        503, json={"title": "down", "status": 503, "code": "UNAVAILABLE"}
    )
    res = mapi("--url", URL, "--json", "plugin", "doctor", "--server")
    assert res.exit_code == 2
    assert as_json(res)["problems"][0]["code"] == "server_unreachable"


class _Dist:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEP:
    def __init__(self, group: str, obj: Any, dist: str = "acme-lab") -> None:
        self.group, self.name, self.value = group, "acme", f"acme_lab:{group}"
        self.dist, self._obj = _Dist(dist), obj

    def load(self) -> Any:
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


def test_doctor_reports_a_broken_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eps = {"mapi.plots": [_FakeEP("mapi.plots", ImportError("no module named acme_lab"))]}
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: eps.get(group, []))
    report = plugin_doctor.doctor_report(cwd=tmp_path)
    assert report["exit_code"] == 1
    (diag,) = [CatalogDiagnostic.model_validate(d) for d in report["diagnostics"]]
    assert (diag.code, diag.group, diag.distribution) == ("load_error", "mapi.plots", "acme-lab")
    assert "no module named acme_lab" in (diag.untrusted_text or "")
    assert any(p["code"] == "load_error" for p in report["problems"])
    assert report["extensions"] == ["acme-lab"]


def test_the_doctor_digest_covers_listed_out_of_tree_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "plugins/inside/src").mkdir(parents=True)
    (checkout / "uv.lock").write_text("")
    outside = (tmp_path / "acme-lab").resolve()
    sources = {"inside": checkout / "plugins/inside", "acme-lab": outside}
    monkeypatch.setattr(plugin_doctor, "_source_of", lambda d: sources[d].resolve())
    info, problems = plugin_doctor._digest_coverage(["inside", "acme-lab"], checkout)
    assert info["mode"] == "checkout"
    assert [p["code"] for p in problems] == ["not_in_digest"] and "acme-lab" in problems[0][
        "message"
    ]
    (checkout / ".local").mkdir()
    (checkout / ".local/extra-plugins.txt").write_text(f"# make plugin-dev\n{outside}\n")
    assert plugin_doctor._digest_coverage(["inside", "acme-lab"], checkout)[1] == []


def test_an_image_without_an_extensions_lock_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAPI_EXTENSIONS_LOCK_PATH", str(tmp_path / "extensions.lock"))
    info, problems = plugin_doctor._digest_coverage(["acme-lab"], tmp_path)
    assert info["mode"] == "image" and [p["code"] for p in problems] == ["no_extensions_lock"]
    (tmp_path / "extensions.lock").write_text("acme-lab==0.1.0 sha256:...\n")
    assert plugin_doctor._digest_coverage(["acme-lab"], tmp_path)[1] == []
    assert plugin_doctor._digest_coverage([], tmp_path / "nowhere")[1] == []


def test_doctor_routing_reads_the_docker_images(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPI_DOCKER_IMAGES", '{"acme_lab": "acme/runner:1"}')
    assert plugin_doctor._docker_images() == {"acme_lab": "acme/runner:1"}
    monkeypatch.setenv("MAPI_DOCKER_IMAGES", "not json")
    assert plugin_doctor._docker_images() == {}


@pytest.mark.slow
def test_doctor_on_this_environment(mapi: Invoke, as_json: AsJson) -> None:
    """The real subprocess over every installed distribution: the built-ins load cleanly."""
    res = mapi("--json", "plugin", "doctor")
    report = as_json(res)
    assert report["diagnostics"] == [], report["diagnostics"]
    groups = {e["group"] for e in report["entry_points"]}
    assert {"mapi.models", "mapi.steps", "mapi.plots"} <= groups
    assert report["digest"]["catalog_digest"].startswith("sha256:")
