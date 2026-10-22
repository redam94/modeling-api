"""The Extend docs are real (v0.18): every page is in the nav, every kind page shows code
that runs, the tutorial's package is what ``mapi plugin init`` writes, and the ``make``
targets the pages name exist and behave.

``docs/snippets/extend_library.py`` and ``extend_tutorial.py`` also run in-process through
``test_docs_snippets.py``; here they run in a subprocess, so nothing they import (the
tutorial's ``acme`` package, loaded by path) leaks into this test session.
"""

from __future__ import annotations

import filecmp
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
SNIPPETS = DOCS / "snippets"
PKG = SNIPPETS / "extend_pkg"
MAPI = Path(sys.executable).parent / "mapi"

EXTEND_PAGES = [
    "extend/index.md",
    "extend/your-model.md",
    "extend/prep-presets.md",
    "extend/chart-templates.md",
    "extend/workflow-templates.md",
    "extend/develop/index.md",
    "extend/develop/models.md",
    "extend/develop/steps.md",
    "extend/develop/plot-recipes.md",
    "extend/develop/workflow-nodes.md",
    "extend/develop/workflow-templates.md",
    "extend/develop/run-on-your-stack.md",
    "extend/develop/contract-kits.md",
    "extend/upload-roadmap.md",
    "tutorials/your-first-extensions.md",
    "guides/start-here.md",
    "reference/generated/extension-points.md",
    "whats-new-0.18.md",
]
"""Every page v0.18 adds; each must be in the mkdocs nav."""

KIND_PAGES = [
    "extend/your-model.md",
    "extend/prep-presets.md",
    "extend/chart-templates.md",
    "extend/workflow-templates.md",
    "extend/develop/models.md",
    "extend/develop/steps.md",
    "extend/develop/plot-recipes.md",
    "extend/develop/workflow-nodes.md",
    "extend/develop/workflow-templates.md",
]
"""One page per extension kind (no code and with Python); each includes a tested snippet."""

ALL_KINDS = ["model", "step", "recipe", "node", "template"]
SCAFFOLD = ["init", "--kind", "model", "--kind", "step", "--kind", "recipe"]
SCAFFOLD += ["--kind", "node", "--kind", "template"]


def _pages() -> list[Path]:
    pages = [p for p in (DOCS / "extend").rglob("*.md")]
    pages += [DOCS / "tutorials" / "your-first-extensions.md", DOCS / "guides" / "start-here.md"]
    return pages


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    env.pop("MAPI_EXTENSIONS_LOCK_PATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # keep docs/snippets/extend_pkg free of caches
    return env


def _run(args: list[str], cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False, env=_env()
    )


# ------------------------------------------------------------------------------- the pages


def test_every_extend_page_is_in_the_nav() -> None:
    nav = (ROOT / "mkdocs.yml").read_text("utf-8")
    hand_written = [p for p in EXTEND_PAGES if "generated" not in p]
    for page in hand_written:
        assert (DOCS / page).is_file(), f"missing page docs/{page}"
    for page in EXTEND_PAGES:
        assert re.search(rf"[:\-]\s+{re.escape(page)}\s*$", nav, re.M), f"{page} is not in the nav"
    # and nothing under docs/extend is left out of the nav
    for page in (DOCS / "extend").rglob("*.md"):
        rel = page.relative_to(DOCS).as_posix()
        assert rel in EXTEND_PAGES, f"docs/{rel} is not listed in EXTEND_PAGES (and the nav)"


def test_the_extend_section_follows_guides() -> None:
    tops = re.findall(r"^  - ([^:]+):", (ROOT / "mkdocs.yml").read_text("utf-8"), re.M)
    assert tops.index("Extend") == tops.index("Guides") + 1, tops


@pytest.mark.parametrize("page", KIND_PAGES)
def test_each_kind_page_includes_a_snippet(page: str) -> None:
    text = (DOCS / page).read_text("utf-8")
    includes = re.findall(r'^\s*--8<--\s+"([^"]+)"\s*$', text, re.M)
    assert includes, f"docs/{page} includes no tested snippet"
    for target in includes:
        name = target.partition(":")[0]
        assert (SNIPPETS / name).exists() or (ROOT / name).exists(), f"{page}: {target}"


def test_make_targets_named_by_the_pages_exist() -> None:
    makefile = (ROOT / "Makefile").read_text("utf-8")
    targets = set(re.findall(r"^([a-zA-Z0-9][a-zA-Z0-9_.-]*):", makefile, re.M))
    for page in _pages():
        text = page.read_text("utf-8")
        blocks = re.findall(r"^```[^\n]*\n(.*?)^```", text, re.M | re.S)
        code = "\n".join(
            [*blocks, *re.findall(r"`([^`\n]+)`", re.sub(r"(?ms)^```.*?^```", "", text))]
        )
        for target in re.findall(r"(?:^|[\s;&(])make ([a-z][a-z0-9-]+)", code, re.M):
            assert target in targets, f"{page.relative_to(ROOT)} names `make {target}`"
    for target in ("plugin-new", "plugin-dev", "plugin-undev", "plugin-doctor", "plugin-image"):
        assert target in targets
    assert "catalog-refresh" in targets


def test_cli_flags_are_where_the_cli_takes_them() -> None:
    """``--json`` is a global option: ``mapi --json plugin validate``, never after it."""
    for page in [*_pages(), SNIPPETS / "extend_cli.sh"]:
        text = page.read_text("utf-8")
        bad = re.findall(r"mapi plugin (?:validate|doctor)[^\n`]*--json", text)
        assert not bad, f"{page.relative_to(ROOT)}: {bad}"
        for sub in re.findall(r"\bmapi plugin ([a-z-]+)", text):
            assert sub in {"init", "test", "validate", "doctor"}, f"{page}: mapi plugin {sub}"


# --------------------------------------------------------------------------- the snippets


def test_snippet_package_is_what_init_writes(tmp_path: Path) -> None:
    proc = _run([str(MAPI), "plugin", *SCAFFOLD, "acme"], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    fresh = tmp_path / "acme"

    def files(root: Path) -> list[str]:
        skip = {"__pycache__", ".pytest_cache"}
        return sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and not skip & set(p.relative_to(root).parts)
        )

    ours, theirs = files(PKG), files(fresh)
    assert ours == theirs, "regenerate docs/snippets/extend_pkg with `mapi plugin init`"
    # README.md is not shown in the docs; every other file is the scaffold, byte for byte
    _, mismatch, errors = filecmp.cmpfiles(
        PKG, fresh, [f for f in ours if f != "README.md"], shallow=False
    )
    assert not mismatch and not errors, (
        f"docs/snippets/extend_pkg differs from `mapi plugin init`: {mismatch + errors}. "
        "Regenerate it: cd docs/snippets && rm -rf extend_pkg && mapi plugin init acme "
        "--kind model --kind step --kind recipe --kind node --kind template && mv acme extend_pkg"
    )


def test_tutorial_snippet_loads_the_package_by_path(tmp_path: Path) -> None:
    """Hermetic: ``acme`` is not installed; the snippet puts ``extend_pkg/src`` on the path,
    compiles a model preset over ``acme.group_mean``, applies a prep preset with
    ``acme.winsorize``, a chart template over ``acme.weekly_total``'s catalog example and
    validates a workflow template with the package's node ``test.acme_zero_share``."""
    if importlib.util.find_spec("acme") is not None:
        pytest.skip("a package named acme is installed in this environment")
    proc = _run([sys.executable, str(SNIPPETS / "extend_tutorial.py")], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "['acme.group_mean']" in out
    assert "'use': 'acme.group_mean@^0.1'" in out
    assert "('p_cap', 'acme.winsorize@^0.1')" in out
    assert "lib.weekly-total-line" in out


def test_library_snippet_compiles_every_kind(tmp_path: Path) -> None:
    proc = _run([sys.executable, str(SNIPPETS / "extend_library.py")], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "tabular.declarative_glm@^1" in out  # a ModelRecipe compiled
    assert "ts.ets@^1 13" in out  # a preset used with an exposed setting
    assert "'id': 'p_promo_price'" in out  # a prep preset with a formula step applied
    assert "lib.fit-small-multiples" in out  # a chart template on a catalog example
    assert out.strip().endswith('{"ok": true}')


def test_library_yaml_matches_the_plan_example() -> None:
    """The declarative example on docs/extend/your-model.md is the one the app's YAML tab
    shows for the same choices (and the plan's §3.1 example)."""
    from mapi_spec.library import ModelRecipeDoc, parse_library_text

    doc = parse_library_text((SNIPPETS / "extend_model_recipe.yaml").read_text("utf-8"))
    assert isinstance(doc, ModelRecipeDoc) and doc.declarative is not None
    assert [t.var for t in doc.declarative.terms] == ["price", "promo"]
    assert doc.declarative.terms[1].varies_by == "store"


@pytest.mark.slow
def test_cli_snippet_runs(tmp_path: Path) -> None:
    proc = _run(["bash", str(SNIPPETS / "extend_cli.sh")], tmp_path, timeout=900)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    assert "all checks passed" in proc.stdout
    report = json.loads((tmp_path / "acme-report.json").read_text("utf-8"))
    assert report["ok"] is True and report["exit_code"] == 0


@pytest.mark.slow
@pytest.mark.parametrize(
    "kind",
    [
        ["--kind", "model"],
        ["--kind", "model", "--base", "pymc"],
        ["--kind", "model", "--base", "sklearn"],
        *(["--kind", k] for k in ALL_KINDS[1:]),
    ],
    ids=["model", "model-pymc", "model-sklearn", *ALL_KINDS[1:]],
)
def test_scaffold_every_kind_validates(kind: list[str], tmp_path: Path) -> None:
    proc = _run([str(MAPI), "plugin", "init", "acme-lab", *kind], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    proc = _run([str(MAPI), "--json", "plugin", "validate", "acme-lab"], tmp_path)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    failed = [c for c in report.get("checks", []) if c.get("status") not in (None, "pass")]
    assert not failed, failed


# ----------------------------------------------------------------------- the make targets


def _make(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is not installed")
    return _run([make, "--no-print-directory", *args], cwd)


def test_plugin_dev_and_undev_edit_the_extra_list(tmp_path: Path) -> None:
    """``UV=echo``: the install itself is not run (it would change this environment)."""
    plugin = tmp_path / "acme"
    shutil.copytree(PKG, plugin)
    listed = tmp_path / "extra-plugins.txt"
    common = ["UV=echo uv", f"EXTRA_PLUGINS={listed}", f"PLUGIN_VENV={Path(sys.prefix)}"]
    proc = _make("plugin-dev", f"PLUGIN={plugin}/", *common)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (
        f"pip install --python {Path(sys.prefix)}/bin/python --no-deps -e {plugin}/" in proc.stdout
    )
    assert listed.read_text("utf-8").splitlines() == [str(plugin)]
    proc = _make("plugin-dev", f"PLUGIN={plugin}", *common)
    assert proc.returncode == 0 and "already in" in proc.stdout
    assert listed.read_text("utf-8").splitlines() == [str(plugin)]
    proc = _make("plugin-undev", f"PLUGIN={plugin}", *common)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "pip uninstall" in proc.stdout and proc.stdout.rstrip().endswith(
        "describe the catalog again."
    )
    assert "uv pip uninstall --python" in proc.stdout and " acme" in proc.stdout
    assert listed.read_text("utf-8") == ""


def test_plugin_targets_name_their_missing_arguments() -> None:
    for target, var in (
        ("plugin-new", "NAME"),
        ("plugin-dev", "PLUGIN"),
        ("plugin-undev", "PLUGIN"),
    ):
        proc = _make(target)
        assert proc.returncode != 0
        assert f"set {var}" in proc.stderr, proc.stderr
    proc = _make("plugin-image", "DOCKER=true")
    assert proc.returncode != 0 and "set BASE, NAME and WHEELS" in proc.stderr


def test_plugin_image_pins_each_wheel_by_hash(tmp_path: Path) -> None:
    """``DOCKER=echo``: the build context and the command, not a real build."""
    wheel = tmp_path / "acme_lab-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"not really a wheel")
    out = tmp_path / "images"
    proc = _make(
        "plugin-image", "DOCKER=echo", "BASE=base-bayes", "NAME=acme-lab", "TAG=0.1.0",
        f"WHEELS={wheel}", f"PLUGIN_IMAGE_DIR={out}",
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr
    import hashlib

    digest = hashlib.sha256(b"not really a wheel").hexdigest()
    req = (out / "acme-lab" / "requirements.txt").read_text("utf-8")
    assert req == f"acme-lab==0.1.0 --hash=sha256:{digest}\n"
    assert "build -f images/extend/Dockerfile --build-arg BASE=mapi/base-bayes:" in proc.stdout
    assert "-t mapi/acme-lab:0.1.0" in proc.stdout
