"""``mapi plugin init|test|validate|doctor|push|status|yank``: scaffold, test, check and
upload extension packages.

* ``init`` writes a package with one module, entry point and contract-kit test per kind
  (``--kind model|step|recipe|node|template``, repeatable; ``--base custom|pymc|sklearn``
  for models). Every scaffold has a complete About card and a README that ends in the app.
* ``test`` runs the package's tests (the contract kits) with this interpreter.
* ``validate`` loads only that package's entry points in a subprocess, runs every static
  check and prints the catalog fragment it would add.
* ``doctor`` checks every installed extension (all six entry-point groups), the load
  diagnostics, image routing and the image digest, and with ``--server`` compares the local
  catalog with the server's.
* ``init --upload`` (v0.19) claims a short name on the server (or reads your claim) and
  scaffolds with the namespace it gives (``t_<code>_<short>``, distribution
  ``t-<code>-<short>``); ``push`` builds the wheel and uploads it to your workspace,
  ``status`` shows where each version is and ``yank`` withdraws one (see the section
  "uploaded code" below for the exit statuses).

``validate`` and ``doctor`` exit 0 when everything passes, 1 when a check finds a problem
and 2 when the package or an entry point cannot be loaded (or the server cannot be read).
Who may use which namespace comes from :mod:`mapi_spec.namespaces`, the one list the SDK
registry and the workflow loaders use too.
"""

from __future__ import annotations

import functools
import hashlib
import json
import keyword
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Final
from urllib.parse import quote

import httpx
import typer
from mapi_client import Call, Client, ConfigError, TransportError
from mapi_spec.api import Page
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.namespaces import BUILTIN_NAMESPACES, PLATFORM_RESERVED, reserved_namespace
from mapi_spec.plugins import (
    PLUGIN_SHORT_RE,
    ArchiveLimits,
    Plugin,
    PluginClaim,
    PluginUploadComplete,
    PluginUploadTicket,
    PluginUploadTicketRequest,
    PluginUsage,
    PluginVersion,
    PluginVersionSummary,
    PluginYank,
    parse_upload_namespace,
    upload_dist_name,
)
from mapi_spec.refs import UseRef
from pydantic import BaseModel
from rich.text import Text

from mapi_cli.output import Output, strip_control
from mapi_cli.plugin_doctor import EXIT_BROKEN, EXIT_OK, EXIT_PROBLEMS, compare_server
from mapi_cli.state import EXIT_FAILURE, AppState, cli_errors, confirm, state

app = typer.Typer(
    help="Extension authoring: scaffold, test, validate, diagnose and upload plugin packages.",
    no_args_is_help=True,
)

_DIST_RE: Final = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_NAMESPACE_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_NODE_ID_RE: Final = re.compile(r"^[a-z]+\.[a-z0-9_]+$")
_TEMPLATE_ID_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PLACEHOLDER: Final = re.compile(r"\{\{(\w+)\}\}")

RESERVED_NAMESPACES: Final = BUILTIN_NAMESPACES | PLATFORM_RESERVED
"""Kept for compatibility: the exact namespaces no third-party plugin may use. The full
rule (with the reserved prefixes) is :func:`mapi_spec.namespaces.reserved_namespace`."""

_CHILD_TIMEOUT_S: Final = 600


class Kind(StrEnum):
    """What a scaffold contains (each kind is one module, entry point and test)."""

    MODEL = "model"
    STEP = "step"
    RECIPE = "recipe"
    NODE = "node"
    TEMPLATE = "template"


class Base(StrEnum):
    """The base class of a scaffolded model."""

    CUSTOM = "custom"
    PYMC = "pymc"
    SKLEARN = "sklearn"


@dataclass(frozen=True, slots=True)
class _KindFiles:
    """Where one kind's code goes and what it needs."""

    module: str  # the submodule of the package (``{module}.<module>``)
    group: str  # the entry-point group
    export: str  # the exported list
    deps: tuple[str, ...]  # runtime requirements ({v}: the platform's minor version)
    test_deps: tuple[str, ...]
    noun: str  # "a model", for the package description

    @property
    def test_file(self) -> str:
        return f"test_{_TEST_NAMES.get(self.module, self.module)}.py"


_TEST_NAMES: Final = {"steps": "step", "plots": "recipe", "nodes": "node", "templates": "template"}


_KINDS: Final[dict[Kind, _KindFiles]] = {
    Kind.MODEL: _KindFiles(
        "model", "mapi.models", "MODELS", ("mapi-sdk>={v}",), ("mapi-sdk[testing]>={v}",),
        "a model",
    ),
    Kind.STEP: _KindFiles(
        "steps", "mapi.steps", "STEPS", ("mapi-sdk>={v}", "xarray>=2025.1"),
        ("mapi-sdk[testing]>={v}",), "a preprocessing step",
    ),
    Kind.RECIPE: _KindFiles(
        "plots", "mapi.plots", "RECIPES",
        ("mapi-plots[recipes]>={v}", "altair>=5.4", "pandas>=2.2", "xarray>=2025.1"), (),
        "a plot recipe",
    ),
    Kind.NODE: _KindFiles(
        "nodes", "mapi.workflow_nodes", "NODES", ("mapi-workflows[runtime]>={v}",), (),
        "a workflow node",
    ),
    Kind.TEMPLATE: _KindFiles(
        "templates", "mapi.workflow_templates", "TEMPLATES", ("mapi-spec>={v}",),
        ("mapi-workflows[runtime]>={v}",), "a workflow template",
    ),
}  # fmt: skip
"""The kinds in the order they are written, listed and documented."""

_BASE_DEPS: Final[dict[Base, tuple[str, ...]]] = {
    Base.CUSTOM: ("numpy>=2.0", "xarray>=2025.1"),
    Base.PYMC: ("mapi-pymc>={v}", "xarray>=2025.1"),
    Base.SKLEARN: ("mapi-sklearn>={v}",),
}
_MODEL_NAMES: Final[dict[Base, tuple[str, str, str]]] = {
    # base -> (model name, class, example settings in a spec)
    Base.CUSTOM: ("group_mean", "GroupMeanModel", "{shrinkage: 0.1}"),
    Base.PYMC: ("trend", "TrendModel", "{slope_sigma: 2.0}"),
    Base.SKLEARN: ("ridge_demand", "RidgeDemand", "{}"),
}


def _sdk_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return ".".join(version("mapi-sdk").split(".")[:2])
    except PackageNotFoundError:  # pragma: no cover - the CLI depends on mapi-sdk
        return "0.1"


def _invalid(message: str) -> MapiError:
    return MapiError(message, code=ErrorCode.SCHEMA_INVALID)


def _ordered(kinds: list[Kind] | tuple[Kind, ...] | None) -> tuple[Kind, ...]:
    chosen = set(kinds or (Kind.MODEL,))
    return tuple(k for k in _KINDS if k in chosen)


def _pack_label(dist: str) -> str:
    words = re.split(r"[-_]+", dist.removeprefix("mapi-plugin-").removeprefix("mapi_plugin_"))
    return " ".join(words).capitalize()[:40]


def plugin_names(
    name: str,
    namespace: str | None,
    kinds: list[Kind] | tuple[Kind, ...] | None = None,
    base: Base = Base.CUSTOM,
) -> dict[str, str]:
    """Distribution, module, namespace and the id of each scaffolded item.

    Refuses a name or namespace the platform would refuse later: reserved namespaces (with
    the plain reason from :func:`~mapi_spec.namespaces.reserved_namespace`), a module that
    would shadow a platform package or a standard library module, a module that is a Python
    keyword (``import``, ``class``: no import statement can name it), and ids that do not
    fit their patterns.
    """
    if not _DIST_RE.match(name):
        raise _invalid(
            f"The plugin name {name!r} is not valid. Use lowercase letters and digits "
            "separated by '-' or '_'."
        )
    module = name.replace("-", "_")
    if module.startswith("mapi_") and not module.startswith("mapi_plugin_"):
        raise _invalid(
            f"The module {module!r} would shadow a platform package. Use a name that does not "
            "start with 'mapi-' (or use 'mapi-plugin-<name>')."
        )
    if keyword.iskeyword(module) or keyword.issoftkeyword(module):
        raise _invalid(
            f"The module {module!r} is a Python keyword, so no import statement can name it. "
            "Use another name (for example 'acme-" + module + "'), or keep it as the "
            "namespace with --namespace."
        )
    if module in sys.stdlib_module_names:
        raise _invalid(
            f"The module {module!r} would shadow the Python standard library module of that "
            "name. Use another name, or keep it as the namespace with --namespace."
        )
    ns = namespace or module.removeprefix("mapi_plugin_")
    if not _NAMESPACE_RE.match(ns):
        raise _invalid(
            f"The namespace {ns!r} is not valid. Use lowercase letters, digits and '_', "
            "starting with a letter."
        )
    order = _ordered(kinds)
    reason = reserved_namespace(ns)
    if reason is None and Kind.TEMPLATE in order:
        # a template id is "<namespace>_<name>": the loaders read the namespace up to the
        # first "_", so that part must be free too
        head = ns.partition("_")[0]
        if head != ns and (head_reason := reserved_namespace(head)) is not None:
            reason = f"Template ids start with {head!r}. {head_reason}"
    if reason is not None:
        raise _invalid(f"{reason} (reserved namespace; set another one with --namespace)")
    if base is not Base.CUSTOM and Kind.MODEL not in order:
        raise _invalid("--base applies to --kind model only.")
    return _scaffold_names(name, module, ns, order, base)


def upload_plugin_names(
    namespace: str,
    kinds: list[Kind] | tuple[Kind, ...] | None = None,
    base: Base = Base.CUSTOM,
    *,
    plugin_id: str,
) -> dict[str, str]:
    """The names of a package for upload (v0.19): the platform assigned ``namespace``
    (``t_<code>_<short>``), the distribution is its PEP 503 form (``t-<code>-<short>``) and
    the one top-level package is the namespace itself.

    The generic rules refuse every ``t_`` namespace (it is reserved for uploads), so they
    do not apply here; the platform checks the claimed namespace in the sandbox instead."""
    if parse_upload_namespace(namespace) is None:
        raise _invalid(f"{namespace!r} is not an uploaded namespace (t_<code>_<short>).")
    order = _ordered(kinds)
    if base is not Base.CUSTOM and Kind.MODEL not in order:
        raise _invalid("--base applies to --kind model only.")
    names = _scaffold_names(upload_dist_name(namespace), namespace, namespace, order, base)
    names["upload_plugin_id"] = plugin_id
    names["short_name"] = parse_upload_namespace(namespace)[1]  # type: ignore[index]
    names["pack_label"] = names["short_name"].capitalize()
    return names


def _scaffold_names(
    name: str, module: str, ns: str, order: tuple[Kind, ...], base: Base
) -> dict[str, str]:
    model_name, model_class, model_settings = _MODEL_NAMES[base]
    ids = {
        "model_id": f"{ns}.{model_name}",
        "step_id": f"{ns}.winsorize",
        "recipe_id": f"{ns}.weekly_total",
        "node_id": f"test.{ns}_zero_share",
        "template_id": f"{ns}_weekly_check",
    }
    for key in ("model_id", "step_id", "recipe_id"):
        UseRef.parse(ids[key])
    if not _NODE_ID_RE.match(ids["node_id"]):  # pragma: no cover - implied by the ns pattern
        raise _invalid(f"The node id {ids['node_id']!r} is not valid.")
    if Kind.TEMPLATE in order and not _TEMPLATE_ID_RE.match(ids["template_id"]):
        raise _invalid(
            f"The template id {ids['template_id']!r} is longer than 64 characters. Use a "
            "shorter --namespace."
        )
    first = {
        Kind.MODEL: "model_id",
        Kind.STEP: "step_id",
        Kind.RECIPE: "recipe_id",
        Kind.NODE: "node_id",
        Kind.TEMPLATE: "template_id",
    }[order[0]]
    return {
        "dist": name,
        "module": module,
        "namespace": ns,
        "kinds": ",".join(k.value for k in order),
        "base": base.value,
        "plugin_id": ids["model_id"] if Kind.MODEL in order else ids[first],
        "sdk_version": _sdk_version(),
        "model_class": model_class,
        "model_settings": model_settings,
        "pack_label": _pack_label(name),
        **ids,
    }


# ================================================================================= render


def _py_text(text: str, indent: int = 8) -> str:
    """``text`` as implicitly joined Python string literals of at most 70 characters each
    (generated code stays inside a 100-column limit)."""
    parts = textwrap.wrap(text, width=70, drop_whitespace=False)
    return ("\n" + " " * indent).join(json.dumps(p) for p in parts)


def _template_context(names: dict[str, str], kinds: tuple[Kind, ...]) -> dict[str, str]:
    """The workflow template's document, which uses the scaffolded node when there is one."""
    adf = '        {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},'
    if Kind.NODE in kinds:
        nodes = "\n".join(
            [
                "        {",
                '            "id": "zeros",',
                f'            "use": "{names["node_id"]}",',
                '            "with": {"var": "sales", "threshold": 0.1},',
                '            "inputs": {"data": "inputs.data"},',
                "        },",
                adf,
            ]
        )
        return {
            "template_nodes": nodes,
            "template_outputs": '"zeros": "zeros.result", "adf": "adf.result"',
            "template_summary": _py_text(
                "Checks weekly sales for too many zeros and for a level that wanders. Run it "
                "before you fit a model."
            ),
            "template_nodes_doc": " (the built-in nodes and this package's node)",
            "template_test_imports": "\nfrom " + names["module"] + ".nodes import ZERO_SHARE",
            "template_plugins": "[ZERO_SHARE.contract]",
        }
    kpss = '        {"id": "kpss", "use": "test.kpss", "inputs": {"data": "inputs.data"}},'
    return {
        "template_nodes": f"{adf}\n{kpss}",
        "template_outputs": '"adf": "adf.result", "kpss": "kpss.result"',
        "template_summary": _py_text(
            "Checks whether the level of weekly sales wanders, with two tests that agree or "
            "disagree. Run it before you fit a model."
        ),
        "template_nodes_doc": " (the built-in nodes)",
        "template_test_imports": "",
        "template_plugins": "[]",
    }


def _fill(text: str, context: dict[str, str], source: str) -> str:
    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in context:  # pragma: no cover - guarded by the scaffold tests
            raise RuntimeError(f"template {source} has an unknown placeholder {m.group(0)}")
        return context[key]

    return _PLACEHOLDER.sub(sub, text)


_KIT_ASSERT: Final = "    assert report.ok, report.summary()"


def _upload_context(names: dict[str, str]) -> dict[str, str]:
    """The placeholders that differ for a package made for upload (``init --upload``).

    The kits' generic namespace rule refuses every ``t_`` namespace, so an upload package's
    tests check the claimed namespace instead (as the platform's kit does in the sandbox)."""
    plugin_id = names.get("upload_plugin_id")
    if not plugin_id:
        return {
            "upload_tool": "",
            "readme_upload": "",
            "init_upload_note": "",
            "recipe_kit_assert": _KIT_ASSERT,
            "node_kit_assert": _KIT_ASSERT,
            "template_kit_assert": (
                '    assert issues == [], [f"{i.pointer}: {i.message}" for i in issues]'
            ),
        }
    ns, short, dist, module = (
        names["namespace"],
        names["short_name"],
        names["dist"],
        names["module"],
    )
    why = (
        f"    # {ns} is the namespace the platform gave you. The kit's generic rule refuses\n"
        "    # every 't_' namespace, so the claimed namespace is checked here instead (the\n"
        "    # platform runs the same check when you push).\n"
    )
    return {
        "upload_tool": (
            "\n# Uploaded code: `mapi plugin push` reads this to find your plugin.\n"
            "[tool.mapi.upload]\n"
            f'plugin_id = "{plugin_id}"\n'
            f'short_name = "{short}"\n'
            f'namespace = "{ns}"\n'
        ),
        "readme_upload": (
            "## Upload it\n\n"
            f"This package is ready to upload to your workspace as `{ns}` (distribution\n"
            f"`{dist}`). Keep `mapi plugin test` green, then:\n\n"
            "```bash\n"
            "mapi plugin push --wait            # build, upload, wait for the checks\n"
            "mapi plugin push --request-approval  # ...and ask an admin to approve it\n"
            f"mapi plugin status {short}          # where each version is\n"
            "```\n\n"
            "When the checks pass you can use it yourself straight away (unless your workspace\n"
            "turned private use off). Everyone else sees it after a second person approves it.\n"
            f"Keep one top-level package (`src/{module}`) and depend only on what the platform\n"
            "already has: the checks refuse anything else. Bump `version` for every push.\n\n"
        ),
        "init_upload_note": " (yours was given to you by the platform for uploaded code)",
        "recipe_kit_assert": (
            why + "    from mapi_spec.namespaces import check_namespace\n\n"
            '    failed = [name for name, _ in report.failures if name != "namespace"]\n'
            "    assert not failed, report.summary()\n"
            f'    assert check_namespace("{names["recipe_id"]}", None, claimed="{ns}") is None'
        ),
        "node_kit_assert": (
            why + "    from mapi_spec.workflow_nodes import plugin_contract_issues\n\n"
            '    failed = [r.name for r in report.failures if r.name != "contract"]\n'
            "    assert not failed, report.summary()\n"
            f'    issues = plugin_contract_issues(ZERO_SHARE.contract, claimed="{ns}")\n'
            "    assert issues == [], [i.message for i in issues]"
        ),
        "template_kit_assert": (
            why + "    from mapi_spec.namespaces import check_namespace\n\n"
            '    issues = [i for i in issues if i.pointer != "/id"]\n'
            '    assert issues == [], [f"{i.pointer}: {i.message}" for i in issues]\n'
            f'    assert WEEKLY_CHECK.id.startswith("{ns}_")\n'
            "    assert check_namespace(\n"
            f'        WEEKLY_CHECK.id, None, plugin="{ns}", claimed="{ns}"\n'
            "    ) is None"
        ),
    }


def _render_context(names: dict[str, str], kinds: tuple[Kind, ...], base: Base) -> dict[str, str]:
    root = resources.files("mapi_cli.plugin_template")
    v = names["sdk_version"]
    ctx = dict(names)
    ctx.update(_upload_context(names))
    if Kind.TEMPLATE in kinds:
        ctx.update(_template_context(names, kinds))

    deps: list[str] = []
    test_deps: list[str] = []
    for k in kinds:
        spec = _KINDS[k]
        extra = _BASE_DEPS[base] if k is Kind.MODEL else ()
        deps += [d.format(v=v) for d in (*spec.deps, *extra) if d.format(v=v) not in deps]
        test_deps += [d.format(v=v) for d in spec.test_deps if d.format(v=v) not in test_deps]
    test_deps.append("pytest>=8.3")
    ctx["dependencies"] = "\n".join(f'  "{d}",' for d in deps)
    ctx["test_dependencies"] = "\n".join(f'  "{d}",' for d in test_deps)
    ctx["entry_points"] = "\n\n".join(
        f'[project.entry-points."{_KINDS[k].group}"]\n'
        f'{names["module"]} = "{names["module"]}.{_KINDS[k].module}:{_KINDS[k].export}"'
        for k in kinds
    )
    nouns = [_KINDS[k].noun for k in kinds]
    listed = nouns[0] if len(nouns) == 1 else ", ".join(nouns[:-1]) + " and " + nouns[-1]
    ctx["description"] = f"modeling-api extensions: {listed}"
    ctx["init_contents"] = "\n".join(
        f"* ``{names['module']}.{_KINDS[k].module}``: {_KINDS[k].noun}\n"
        f"  (``{_KINDS[k].export}``, entry point ``{_KINDS[k].group}``)"
        for k in kinds
    )
    for part, sep in (("inside", "\n"), ("use", "\n\n"), ("app", "\n")):
        ctx[f"readme_{part}"] = sep.join(
            _fill((root / k.value / f"{part}.md.tmpl").read_text("utf-8"), ctx, k.value).strip()
            for k in kinds
        )
    return ctx


def _files(names: dict[str, str], kinds: tuple[Kind, ...], base: Base) -> dict[str, str]:
    """Template path -> package path."""
    module = names["module"]
    out = {
        "common/pyproject.toml.tmpl": "pyproject.toml",
        "common/README.md.tmpl": "README.md",
        "common/init.py.tmpl": f"src/{module}/__init__.py",
        "common/weekly.csv.tmpl": "examples/weekly.csv",
    }
    for k in kinds:
        spec = _KINDS[k]
        src = f"model/{base.value}" if k is Kind.MODEL else k.value
        out[f"{src}/{spec.module}.py.tmpl"] = f"src/{module}/{spec.module}.py"
        out[f"{src}/{spec.test_file}.tmpl"] = f"tests/{spec.test_file}"
    return out


def render_plugin(
    dest: Path,
    names: dict[str, str],
    kinds: list[Kind] | tuple[Kind, ...] | None = None,
    base: Base = Base.CUSTOM,
) -> list[Path]:
    """Write the package files under ``dest``; returns them (every placeholder filled)."""
    order = _ordered(kinds)
    root = resources.files("mapi_cli.plugin_template")
    ctx = _render_context(names, order, base)
    texts = {  # render everything first: a template error leaves no half-written package
        dest / rel: _fill((root / src).read_text(encoding="utf-8"), ctx, src)
        for src, rel in _files(names, order, base).items()
    }
    written: list[Path] = []
    for path, text in texts.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    typed = dest / "src" / names["module"] / "py.typed"
    typed.write_text("")
    written.append(typed)
    return written


@app.command("init")
@cli_errors
def init(
    ctx: typer.Context,
    name: Annotated[
        str,
        typer.Argument(
            help="Distribution name, e.g. acme-demand (with --upload: the short name, e.g. acme)"
        ),
    ],
    kind: Annotated[
        list[Kind] | None,
        typer.Option("--kind", "-k", help="What to scaffold (repeatable; default: model)"),
    ] = None,
    base: Annotated[
        Base, typer.Option("--base", help="Base class of the model (with --kind model)")
    ] = Base.CUSTOM,
    directory: Annotated[
        Path, typer.Option("--dir", help="Parent directory (default: current)")
    ] = Path("."),
    namespace: Annotated[
        str | None, typer.Option("--namespace", help="Id namespace (default: the module name)")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Write into a non-empty dir")] = False,
    upload: Annotated[
        bool,
        typer.Option(
            "--upload",
            help="Make a package to upload to your workspace: claims NAME (or reads your "
            "claim) on the server and uses the namespace it gives (t_<code>_<name>)",
        ),
    ] = False,
    title: Annotated[
        str | None,
        typer.Option("--title", help="With --upload: the plugin's title (default: from NAME)"),
    ] = None,
) -> None:
    """Scaffold an extension package: per kind a module, an entry point and contract tests.

    With --upload the package is for uploaded code: NAME is a short name (2-16 lowercase
    letters and digits); the server claims it and gives the namespace, which is also the
    distribution (t-<code>-<name>) and the one top-level package."""
    st = state(ctx)
    out = st.output
    kinds = _ordered(kind)
    claim: dict[str, Any] | None = None
    if upload:
        if namespace is not None:
            raise _invalid("--namespace cannot be used with --upload: the server gives it.")
        try:
            plugin, claimed = _claim_or_read(st, name, title)
        except TransportError as exc:
            out.error(exc)
            raise typer.Exit(EXIT_TRANSPORT) from None
        names = upload_plugin_names(plugin.namespace, kinds, base, plugin_id=plugin.id)
        claim = {
            "plugin_id": plugin.id,
            "short_name": plugin.short_name,
            "namespace": plugin.namespace,
            "claimed": claimed,
        }
    else:
        if title is not None:
            raise _invalid("--title applies to --upload only.")
        names = plugin_names(name, namespace, kinds, base)
    dest = directory / name
    if dest.exists() and any(dest.iterdir()) and not force:
        raise FileExistsError(f"{dest} exists and is not empty (use --force)")
    files = render_plugin(dest, names, kinds, base)
    ids = {k.value: names[f"{k.value}_id"] for k in kinds}
    if out.json_mode:
        doc: dict[str, Any] = {
            "path": str(dest),
            "dist": names["dist"],
            "module": names["module"],
            "namespace": names["namespace"],
            "kinds": [k.value for k in kinds],
            "base": base.value,
            "plugin_id": names["plugin_id"],
            "ids": ids,
            "files": [str(f) for f in files],
        }
        if claim is not None:
            doc["upload"] = claim
        out.emit_json(doc)
        return
    if claim is not None:
        verb = "claimed" if claim["claimed"] else "using your claim"
        out.info(f"{verb}: {claim['short_name']} -> namespace {claim['namespace']}")
    out.success(f"created {dest}: " + ", ".join(f"{k} {i}" for k, i in ids.items()))
    for f in files:
        out.out.print(f"  {f.relative_to(dest)}", markup=False, highlight=False)
    nxt = f"mapi plugin push {dest} --wait" if upload else f"mapi plugin validate {dest}"
    out.out.print(
        f"next: mapi plugin test {dest} && {nxt}",
        markup=False,
        highlight=False,
    )


# =================================================================================== test


def pytest_command(path: Path, *, contract_only: bool, extra: list[str]) -> list[str]:
    tests = path.resolve() / "tests"
    cmd = [sys.executable, "-m", "pytest", str(tests), "-p", "no:cacheprovider"]
    if contract_only:
        cmd += ["-m", "mapi_contract"]
    return cmd + extra


def _src_env(path: Path) -> dict[str, str]:
    """The environment with the package's ``src/`` first on ``PYTHONPATH`` (importable
    without installing it)."""
    env = os.environ.copy()
    src = path.resolve() / "src"
    if src.is_dir():
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(src), env.get("PYTHONPATH")) if p)
    return env


@app.command("test", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
@cli_errors
def test(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Plugin package directory")] = Path("."),
    contract_only: Annotated[
        bool, typer.Option("--contract-only", help="Only tests marked mapi_contract")
    ] = False,
) -> None:
    """Run the plugin's tests (contract kit) with this interpreter; extra args go to pytest."""
    out = state(ctx).output
    tests = path / "tests"
    if not tests.is_dir():
        raise FileNotFoundError(f"{tests} not found (run inside a plugin created by 'plugin init')")
    env = _src_env(path)
    cmd = pytest_command(path, contract_only=contract_only, extra=list(ctx.args))
    out.info("running: pytest " + " ".join(cmd[3:]))
    if out.json_mode:  # keep stdout a single JSON document
        proc = subprocess.run(cmd, cwd=path, env=env, capture_output=True, text=True, check=False)
        code = proc.returncode
        out.emit_json(
            {
                "path": str(path),
                "exit_code": code,
                "passed": code == 0,
                "output": (proc.stdout + proc.stderr)[-8000:],
            }
        )
    else:
        code = subprocess.call(cmd, cwd=path, env=env)
    raise typer.Exit(code)


# ============================================================================ validate/doctor


def _run_child(args: list[str], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run ``python -m mapi_cli.plugin_doctor ARGS`` and return its JSON report.

    The checks import plugin code: a subprocess keeps it (and whatever it prints or breaks)
    out of the CLI. A child that dies without a report is a broken package (exit 2)."""
    cmd = [sys.executable, "-m", "mapi_cli.plugin_doctor", *args]
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True, check=False, timeout=_CHILD_TIMEOUT_S
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"the checks did not finish in {_CHILD_TIMEOUT_S} s") from exc
    try:
        report: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError:
        tail = (proc.stderr or proc.stdout).strip()[-2000:]
        report = {
            "ok": False,
            "exit_code": EXIT_BROKEN,
            "error": f"the checks stopped (exit {proc.returncode}): {tail}",
            "checks": [],
        }
    return report


_STATUS_STYLE: Final = {"pass": "green", "fail": "red", "warn": "yellow", "skip": "dim"}


def _print_checks(out: Output, checks: list[dict[str, Any]]) -> None:
    for c in checks:
        status = str(c.get("status", "fail"))
        line = Text(f"[{status.upper():4}] ", style=_STATUS_STYLE.get(status, ""))
        line.append(f"{c.get('kind')} {c.get('subject')}: {c.get('name')}")
        if c.get("detail"):
            line.append(f" - {c['detail']}", style="dim" if status == "pass" else "")
        out.out.print(line)


def _print_catalog_counts(out: Output, catalog: dict[str, Any]) -> None:
    counts = {
        "models": len(catalog.get("models", [])),
        "steps": len(catalog.get("steps", [])),
        "plots": len(catalog.get("plots", [])),
        "workflow nodes": len(catalog.get("workflow_nodes", [])),
        "workflow templates": len(catalog.get("workflow_templates", [])),
    }
    out.out.print(
        "catalog: " + ", ".join(f"{n} {k}" for k, n in counts.items() if n),
        markup=False,
        highlight=False,
    )


@app.command("validate")
@cli_errors
def validate(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Plugin package directory")] = Path("."),
) -> None:
    """Load only this package's entry points and run every check (exit 0, 1 or 2).

    The checks run in a subprocess with the package's ``src/`` first on the path, so the
    package need not be installed. With --json the report (every check and the catalog
    fragment the package adds) is one JSON document."""
    out = state(ctx).output
    if not (path / "pyproject.toml").is_file():
        raise FileNotFoundError(f"{path / 'pyproject.toml'} not found (not a plugin package)")
    report = _run_child(["validate", str(path.resolve())], env=_src_env(path))
    code = int(report.get("exit_code", EXIT_BROKEN))
    if out.json_mode:
        out.emit_json(report)
        raise typer.Exit(code)
    if report.get("error"):
        out.err.print(Text("error: ", style="red") + Text(str(report["error"])))
    for ep in report.get("entry_points", []):
        if ep.get("error"):
            out.err.print(
                Text("cannot load ", style="red")
                + Text(f"{ep['group']}:{ep['name']} = {ep['value']}: {ep['error']}")
            )
    _print_checks(out, report.get("checks", []))
    if report.get("catalog"):
        _print_catalog_counts(out, report["catalog"])
    verdict = {EXIT_OK: "all checks passed", EXIT_PROBLEMS: "some checks failed"}
    if code in verdict:
        (out.success if code == EXIT_OK else out.warn)(verdict[code])
    else:
        out.warn("the package or an entry point could not be loaded")
    raise typer.Exit(code)


def _print_doctor(out: Output, report: dict[str, Any]) -> None:
    rows = report.get("entry_points", [])
    out.table(
        ["group", "entry point", "distribution", "built-in"],
        [
            (r["group"], f"{r['name']} = {r['value']}", r.get("distribution") or "", r["builtin"])
            for r in rows
            if not r["builtin"]
        ],
        title="Installed extensions (non-built-in entry points)",
    )
    diags = report.get("diagnostics", [])
    if diags:
        out.table(
            ["code", "group", "distribution", "id", "message", "detail (plugin text)"],
            [
                (
                    d["code"],
                    d["group"],
                    d.get("distribution") or "",
                    d.get("id") or "",
                    d["message"],
                    d.get("untrusted_text") or "",
                )
                for d in diags
            ],
            title="Diagnostics",
        )
    routing = report.get("routing", [])
    if routing:
        out.table(
            ["namespace", "distribution", "docker image"],
            [
                (r["namespace"], r.get("distribution") or "", r.get("image") or "default")
                for r in routing
            ],
            title="Routing (MAPI_DOCKER_IMAGES)",
        )
    digest = report.get("digest", {})
    out.out.print(
        f"local catalog digest {digest.get('catalog_digest', '?')}", markup=False, highlight=False
    )
    server = report.get("server")
    if server:
        out.out.print(
            f"server catalog digest {server.get('digest', '?')} ({server.get('url', '')})",
            markup=False,
            highlight=False,
        )
    for p in report.get("problems", []):
        out.warn(p["message"])
    for w in report.get("warnings", []):
        out.info(Text("note: ", style="yellow") + Text(w["message"]))


@app.command("doctor")
@cli_errors
def doctor(
    ctx: typer.Context,
    server: Annotated[
        bool, typer.Option("--server", help="Compare with the configured server's catalog")
    ] = False,
) -> None:
    """Check every installed extension: entry points, diagnostics, routing and digest.

    Exit 0 when nothing is wrong, 1 when a problem was found (a refused export, a stale
    server catalog, an extension the image digest does not cover) and 2 when the checks or
    the server could not run."""
    st = state(ctx)
    out = st.output
    report = _run_child(["doctor"])
    if server and report.get("catalog_ids") is not None:
        try:
            with st.client() as api:
                url = str(api.config.base_url)
                remote = api.catalog.get()
        except (MapiError, ConfigError) as exc:
            report.setdefault("problems", []).append(
                {"code": "server_unreachable", "message": f"the server catalog: {exc}"}
            )
            report["exit_code"] = EXIT_BROKEN
        else:
            compare_server(report, remote.model_dump(mode="json"), url=url)
    code = int(report.get("exit_code", EXIT_BROKEN))
    if out.json_mode:
        out.emit_json(report)
        raise typer.Exit(code)
    if report.get("error"):
        out.err.print(Text("error: ", style="red") + Text(str(report["error"])))
    else:
        _print_doctor(out, report)
    if code == EXIT_OK:
        out.success("no problems found")
    raise typer.Exit(code)


# ============================================================ uploaded code (v0.19, D21)
#
# ``push`` builds a wheel on this machine (``uv build --wheel``, else ``python -m build``),
# checks its tag and size, asks for an upload ticket, POSTs the wheel to the quarantine
# bucket with the ticket's POST policy and calls ``:complete``; ``--wait`` polls the version
# until its checks end. The CLI never installs or imports the wheel. Exit status: 0 the
# upload is accepted (and, with --wait, its checks passed), 1 the platform or a local check
# refused it or its checks failed, 2 the server or the storage could not be reached (or
# --wait ran out of time). An API token with the ``plugins:publish`` scope can push and read
# the status; ``yank`` needs a sign-in (``mapi login``).

EXIT_TRANSPORT: Final = 2
UPLOAD_MAX_BYTES: Final = ArchiveLimits().max_bytes
"""The platform's default wheel cap (20 MiB); the ticket names the deployment's cap."""

_WORKING_STATES: Final = frozenset({"uploaded", "inspecting", "describing"})
_GOOD_STATES: Final = frozenset({"kit_passed", "awaiting_approval", "approved"})
STATE_TEXT: Final[dict[str, str]] = {
    "uploaded": "Uploaded",
    "inspecting": "Checking the files",
    "inspect_failed": "Refused",
    "describing": "Running the tests",
    "kit_failed": "Tests failed",
    "kit_passed": "Ready for you",
    "awaiting_approval": "Waiting for a reviewer",
    "approved": "Approved for the team",
    "rejected": "Not approved",
    "yanked": "Withdrawn",
    "purged": "Removed",
}
_POLL_S: Final = 2.0
_PAGE: Final = 100
_BUILD_TIMEOUT_S: Final = 600
_WHEEL_NAME: Final = re.compile(
    r"^(?P<dist>[A-Za-z0-9_.]+)-(?P<version>[A-Za-z0-9_.!+]+)(?:-(?P<build>\d[A-Za-z0-9_.]*))?"
    r"-(?P<py>[A-Za-z0-9_.]+)-(?P<abi>[A-Za-z0-9_.]+)-(?P<plat>[A-Za-z0-9_.]+)\.whl$"
)
_SHORT: Final = re.compile(PLUGIN_SHORT_RE)
_NAMESPACE_NOISE: Final = (
    "are reserved for uploaded plugins",
    "The namespace 't' is reserved",
)
"""A local check that fails only because ``t_`` namespaces are reserved outside an upload
(the platform checks the claimed namespace in the sandbox instead)."""

_sleep: Callable[[float], None] = time.sleep
_clock: Callable[[], float] = time.monotonic


class YankReasonArg(StrEnum):
    """Why a version is withdrawn (``security`` also cancels runs that use it)."""

    WITHDRAWN = "withdrawn"
    SECURITY = "security"
    SUPERSEDED = "superseded"


def _refused(message: str) -> MapiError:
    return MapiError(message, code=ErrorCode.PLUGIN_REFUSED)


def _transport_exit[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Exit 2 (not 1) when the server or the storage cannot be reached."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except TransportError as exc:
            ctx = next((a for a in [*args, *kwargs.values()] if isinstance(a, typer.Context)), None)
            out = state(ctx).output if ctx is not None else Output()
            out.error(exc)
            raise typer.Exit(EXIT_TRANSPORT) from None

    return wrapper


# ------------------------------------------------------------------------------- calls


def _parse[M: BaseModel](model: type[M]) -> Callable[[httpx.Response], M]:
    def parse(response: httpx.Response) -> M:
        return model.model_validate_json(response.content)

    return parse


_PLUGINS: Final = "/v1/plugins"


def _plugin_path(plugin_id: str, *rest: str) -> str:
    return "/".join([_PLUGINS, quote(plugin_id, safe=""), *rest])


def _list_plugins_call(cursor: str | None) -> Call[Page[Plugin]]:
    params: dict[str, str | int] = {"limit": _PAGE}
    if cursor:
        params["cursor"] = cursor
    return Call("GET", _PLUGINS, _parse(Page[Plugin]), params=params)


def _claim_call(body: PluginClaim) -> Call[Plugin]:
    return Call(
        "POST", _PLUGINS, _parse(Plugin), frozenset({201}), json=body.model_dump(mode="json")
    )


def _get_plugin_call(plugin_id: str) -> Call[Plugin]:
    return Call("GET", _plugin_path(plugin_id), _parse(Plugin))


def _list_versions_call(plugin_id: str, cursor: str | None) -> Call[Page[PluginVersionSummary]]:
    params: dict[str, str | int] = {"limit": _PAGE}
    if cursor:
        params["cursor"] = cursor
    return Call(
        "GET",
        _plugin_path(plugin_id, "versions"),
        _parse(Page[PluginVersionSummary]),
        params=params,
    )


def _get_version_call(plugin_id: str, version_id: str) -> Call[PluginVersion]:
    path = _plugin_path(plugin_id, "versions", quote(version_id, safe=""))
    return Call("GET", path, _parse(PluginVersion))


def _ticket_call(plugin_id: str, body: PluginUploadTicketRequest) -> Call[PluginUploadTicket]:
    return Call(
        "POST",
        _plugin_path(plugin_id, "uploads"),
        _parse(PluginUploadTicket),
        frozenset({201}),
        json=body.model_dump(mode="json"),
    )


def _complete_call(
    plugin_id: str, upload_id: str, body: PluginUploadComplete
) -> Call[PluginVersion]:
    path = _plugin_path(plugin_id, "uploads", quote(upload_id, safe="") + ":complete")
    return Call(
        "POST", path, _parse(PluginVersion), frozenset({202}), json=body.model_dump(mode="json")
    )


def _usage_call(plugin_id: str, version_id: str) -> Call[PluginUsage]:
    path = _plugin_path(plugin_id, "versions", quote(version_id, safe=""), "usage")
    return Call("GET", path, _parse(PluginUsage))


def _yank_call(plugin_id: str, version_id: str, body: PluginYank) -> Call[PluginVersion]:
    path = _plugin_path(plugin_id, "versions", quote(version_id, safe="") + ":yank")
    return Call("POST", path, _parse(PluginVersion), json=body.model_dump(mode="json"))


# ------------------------------------------------------------------------------ lookups


@contextmanager
def _api(st: AppState, project: str | None) -> Iterator[Client]:
    """A client acting in ``project`` (else the configured one, else the caller's only
    project). Plugins are tenant resources reached from a project."""
    with st.client() as api:
        if project is not None:
            api.use(project)
        if api.project is None:
            options = [
                f"{p.tenant_id}/{p.project_id}" for t in api.me().tenants for p in t.projects
            ]
            if len(options) != 1:
                raise ConfigError(
                    "Choose a project: add --project <tenant>/<project>, or run "
                    "`mapi use <tenant>/<project>`."
                )
            api.use(options[0])
        yield api


def _project_slug(api: Client) -> str:
    return str(api.project).rsplit("/", 1)[-1]


def _plugins(api: Client) -> Iterator[Plugin]:
    cursor: str | None = None
    while True:
        page = api.send(_list_plugins_call(cursor))
        yield from page.items
        cursor = page.next_cursor
        if not cursor:
            return


def _find_plugin(api: Client, short: str) -> Plugin | None:
    return next((p for p in _plugins(api) if p.short_name == short), None)


def _plugin_by_short(api: Client, short: str) -> Plugin:
    found = _find_plugin(api, short)
    if found is None:
        raise MapiError(
            f"No plugin named {short!r} in this workspace. Claim it with "
            f"`mapi plugin init {short} --upload`.",
            code=ErrorCode.NOT_FOUND,
        )
    return found


def _versions(api: Client, plugin_id: str) -> Iterator[PluginVersionSummary]:
    cursor: str | None = None
    while True:
        page = api.send(_list_versions_call(plugin_id, cursor))
        yield from page.items
        cursor = page.next_cursor
        if not cursor:
            return


def _split_target(target: str) -> tuple[str, str | None]:
    short, sep, version = target.partition("@")
    if not _SHORT.match(short) or (sep and not version):
        raise _invalid(
            f"{target!r} is not a plugin name. Use SHORT or SHORT@VERSION (for example acme@0.1.0)."
        )
    return short, version or None


def _version_of(api: Client, plugin: Plugin, version: str) -> PluginVersion:
    for v in _versions(api, plugin.id):
        if v.version == version:
            return api.send(_get_version_call(plugin.id, v.id))
    raise MapiError(
        f"The plugin {plugin.short_name!r} has no version {version!r} that you can see.",
        code=ErrorCode.NOT_FOUND,
    )


def _claim_or_read(st: AppState, short: str, title: str | None) -> tuple[Plugin, bool]:
    """Claim ``short`` (``True``), or read the plugin you claimed before (``False``)."""
    if not _SHORT.match(short):
        raise _invalid(
            f"The short name {short!r} is not valid. Use 2 to 16 lowercase letters and digits, "
            "starting with a letter (no '-' or '_')."
        )
    body = PluginClaim(short_name=short, title=title or short.capitalize())
    with _api(st, None) as api:
        try:
            return api.send(_claim_call(body)), True
        except MapiError as exc:
            if exc.code is not ErrorCode.PLUGIN_NAMESPACE_TAKEN:
                raise
            found = _find_plugin(api, short)
            if found is None:
                raise
            return found, False


# -------------------------------------------------------------------------------- wheels


@dataclass(frozen=True, slots=True)
class _Wheel:
    path: Path
    dist: str
    version: str
    size: int
    sha256: str
    built: bool

    def doc(self) -> dict[str, Any]:
        return {
            "filename": self.path.name,
            "dist": self.dist,
            "version": self.version,
            "size": self.size,
            "sha256": self.sha256,
            "built": self.built,
        }


def _canonical(dist: str) -> str:
    return re.sub(r"[-_.]+", "-", dist).lower()


def check_wheel(path: Path, *, built: bool = False) -> _Wheel:
    """The local checks of a wheel before upload: its name, the ``py3-none-any`` tag and the
    size cap. The wheel is read only to hash it (never opened as an archive)."""
    m = _WHEEL_NAME.match(path.name)
    if m is None:
        raise _refused(
            f"{path.name!r} is not a wheel file name. Upload a wheel named "
            "<dist>-<version>-py3-none-any.whl. `mapi plugin push` builds one for you."
        )
    tag = f"{m['py']}-{m['abi']}-{m['plat']}"
    if tag != "py3-none-any":
        raise _refused(
            f"The wheel {path.name!r} is tagged {tag!r}. Uploads must be pure Python wheels "
            "tagged 'py3-none-any'."
        )
    size = path.stat().st_size
    if size == 0:
        raise _refused(f"The wheel {path.name!r} is empty.")
    if size > UPLOAD_MAX_BYTES:
        raise _refused(
            f"The wheel is {size} bytes. Uploads are limited to {UPLOAD_MAX_BYTES} bytes (20 MiB)."
        )
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return _Wheel(path, m["dist"], m["version"], size, "sha256:" + digest.hexdigest(), built)


def _child_env(path: Path) -> dict[str, str]:
    """The environment of a build or check subprocess: no API credentials."""
    env = _src_env(path)
    for key in [k for k in env if k.startswith("MAPI_") and "TOKEN" in k]:
        env.pop(key, None)
    return env


def _has_build_module() -> bool:
    import importlib.util

    return importlib.util.find_spec("build") is not None


def build_command(path: Path, outdir: Path) -> list[str] | None:
    """``uv build --wheel`` (else ``python -m build --wheel``); ``None`` when neither exists."""
    uv = shutil.which("uv")
    if uv:
        return [uv, "build", "--wheel", "--out-dir", str(outdir), str(path.resolve())]
    if _has_build_module():
        return [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir), str(path)]
    return None


def _redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret else text


def _build_wheel(st: AppState, path: Path, outdir: Path) -> Path:
    out = st.output
    cmd = build_command(path, outdir)
    if cmd is None:
        out.fail(
            "BUILD_TOOL_MISSING",
            "cannot build the wheel",
            "Building a wheel needs uv or the 'build' package, and neither was found. Install "
            "uv (https://docs.astral.sh/uv/), or build the wheel yourself and push the file: "
            "mapi plugin push dist/<dist>-<version>-py3-none-any.whl",
        )
        raise typer.Exit(EXIT_FAILURE)
    out.info("building: " + " ".join(Path(cmd[0]).name if i == 0 else a for i, a in enumerate(cmd)))
    try:
        proc = subprocess.run(
            cmd,
            env=_child_env(path),
            capture_output=True,
            text=True,
            check=False,
            timeout=_BUILD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"the wheel build did not finish in {_BUILD_TIMEOUT_S} s") from exc
    wheels = sorted(outdir.glob("*.whl"))
    if proc.returncode != 0 or len(wheels) != 1:
        tail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        why = f"exit {proc.returncode}" if proc.returncode else f"{len(wheels)} wheels"
        out.fail("BUILD_FAILED", "the wheel build failed", f"{why}: {_redact(tail, st.token)}")
        raise typer.Exit(EXIT_FAILURE)
    return wheels[0]


def _upload_config(path: Path) -> dict[str, Any]:
    """``[tool.mapi.upload]`` of a package made by ``init --upload`` (empty otherwise)."""
    import tomllib

    try:
        doc = tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise _invalid(f"{path / 'pyproject.toml'} is not valid TOML: {exc}") from None
    section = doc.get("tool", {}).get("mapi", {}).get("upload", {})
    return section if isinstance(section, dict) else {}


def _local_checks(st: AppState, path: Path) -> dict[str, Any]:
    """``validate`` before the upload, as warnings only (the platform runs the real kit)."""
    out = st.output
    out.info("checking the package locally (mapi plugin validate) ...")
    report = _run_child(["validate", str(path.resolve())], env=_child_env(path))
    warnings: list[str] = []
    if report.get("error"):
        warnings.append(str(report["error"]))
    for ep in report.get("entry_points", []):
        if ep.get("error"):
            warnings.append(f"cannot load {ep['group']}:{ep['name']}: {ep['error']}")
    for c in report.get("checks", []):
        detail = str(c.get("detail") or "")
        if c.get("status") != "fail" or any(n in detail for n in _NAMESPACE_NOISE):
            continue
        warnings.append(f"{c.get('kind')} {c.get('subject')}: {c.get('name')} - {detail}")
    warnings = [strip_control(_redact(w, st.token))[:1000] for w in warnings]
    for w in warnings:
        out.warn(Text("local check: ") + Text(w))
    return {
        "ran": True,
        "exit_code": int(report.get("exit_code", EXIT_BROKEN)),
        "warnings": warnings,
    }


def _post_wheel(api: Client, ticket: PluginUploadTicket, wheel: _Wheel) -> None:
    """POST the wheel with the ticket's policy: every field, then the file last."""
    post = ticket.post
    if not post.allows(str(post.fields.get("key", post.prefix)), wheel.size):
        raise _refused(f"The wheel is {wheel.size} bytes, outside what the upload ticket allows.")
    timeout = httpx.Timeout(api.config.timeout, read=300.0, write=300.0)
    with (
        httpx.Client(timeout=timeout, verify=api.config.verify, follow_redirects=False) as http,
        wheel.path.open("rb") as fh,
    ):
        try:
            resp = http.post(
                post.url,
                data=dict(post.fields),
                files={"file": (wheel.path.name, fh, "application/octet-stream")},
            )
        except httpx.TransportError as exc:
            raise TransportError(
                f"The upload of {wheel.path.name} did not reach the storage: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
    if resp.status_code not in (200, 201, 204):
        raise TransportError(
            f"The storage refused the upload of {wheel.path.name} (HTTP {resp.status_code}). "
            "Push again for a new ticket."
        )


def _resolve_plugin(
    api: Client, short: str | None, config: dict[str, Any], wheel: _Wheel
) -> Plugin:
    if short is not None:
        _split_target(short)
        return _plugin_by_short(api, short)
    plugin_id = config.get("plugin_id")
    if isinstance(plugin_id, str) and plugin_id:
        return api.send(_get_plugin_call(plugin_id))
    parsed = parse_upload_namespace(wheel.dist.replace("-", "_").replace(".", "_").lower())
    if parsed is not None:
        return _plugin_by_short(api, parsed[1])
    raise _invalid(
        "Which plugin is this? Add --plugin SHORT, or make the package with "
        "`mapi plugin init SHORT --upload`."
    )


def _wait(
    api: Client, out: Output, plugin: Plugin, version: PluginVersion, timeout: float
) -> PluginVersion:
    deadline = _clock() + timeout
    last = version.state
    out.info(f"{plugin.short_name} {version.version}: {STATE_TEXT.get(last, last)}")
    while version.state in _WORKING_STATES:
        if _clock() >= deadline:
            out.fail(
                "TIMEOUT",
                "timed out",
                f"{plugin.short_name} {version.version} is still "
                f"'{STATE_TEXT.get(version.state, version.state)}' after {timeout:g} s. See "
                f"later with: mapi plugin status {plugin.short_name}@{version.version}",
            )
            raise typer.Exit(EXIT_TRANSPORT)
        _sleep(_POLL_S)
        version = api.send(_get_version_call(plugin.id, version.id))
        if version.state != last:
            last = version.state
            out.info(f"{plugin.short_name} {version.version}: {STATE_TEXT.get(last, last)}")
    return version


# ------------------------------------------------------------------------------- output


def _print_version(out: Output, plugin: Plugin, v: PluginVersion) -> None:
    out.kv(
        [
            ("plugin", f"{plugin.short_name} ({plugin.namespace})"),
            ("version", v.version),
            ("state", f"{STATE_TEXT.get(v.state, v.state)} ({v.state})"),
            (
                "who can use it",
                {"private": "only you", "tenant": "your team", "none": "nobody"}[v.scope],
            ),
            ("id", v.id),
            ("uploaded", v.uploaded_at.strftime("%Y-%m-%d %H:%M UTC")),
            ("wheel sha256", v.overlay_sha256),
            ("exports", ", ".join(f"{n} {k}" for k, n in v.exports.items() if n)),
            ("withdrawn because", v.yank_reason),
        ]
    )
    if v.inspection is not None and v.inspection.problems:
        out.table(
            ["check", "file", "message"],
            [(p.check, p.path or "", p.message) for p in v.inspection.problems],
            title="Why the files were refused",
        )
    if v.kit is not None:
        failed = [c for c in v.kit.checks if c.status == "fail"]
        if failed:
            out.table(
                ["kind", "id", "check", "message (from the plugin)"],
                [(c.kind, c.entry_id or "", c.check, c.message) for c in failed],
                title="Tests that failed",
            )
    out.issues(v.issues, title="Problems with the exports")
    if v.problem is not None:
        out.warn(f"{v.problem.code}: {v.problem.detail}")
    for url in v.urls:
        out.info(Text("reference: ") + Text(url))


def _next_step(out: Output, plugin: Plugin, v: PluginVersion) -> None:
    hints = {
        "kit_passed": "You can use it yourself now. To share it with the team, ask for "
        "approval on its page in the web app (or push the next version with "
        "--request-approval).",
        "awaiting_approval": "A second admin of your workspace can now approve it for the team.",
        "approved": "Your team can use it.",
        "inspect_failed": "Fix the problems above, bump the version and push again.",
        "kit_failed": "Fix the failed tests (run `mapi plugin test`), bump the version and push "
        "again.",
    }
    hint = hints.get(v.state)
    if hint:
        out.out.print(f"next: {hint}", markup=False, highlight=False)
    if v.state in _WORKING_STATES:
        out.out.print(
            f"next: mapi plugin status {plugin.short_name}@{v.version}",
            markup=False,
            highlight=False,
        )


def _plugin_doc(plugin: Plugin) -> dict[str, Any]:
    return {"id": plugin.id, "short_name": plugin.short_name, "namespace": plugin.namespace}


# --------------------------------------------------------------------------------- push


@app.command("push")
@cli_errors
@_transport_exit
def push(
    ctx: typer.Context,
    path: Annotated[
        Path, typer.Argument(help="Package directory (default: current) or a built .whl file")
    ] = Path("."),
    plugin_short: Annotated[
        str | None,
        typer.Option("--plugin", help="The plugin's short name (default: from pyproject.toml)"),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", help="<tenant>/<project> that runs the checks"),
    ] = None,
    request_approval: Annotated[
        bool,
        typer.Option(
            "--request-approval", help="Ask a second admin to approve it once the checks pass"
        ),
    ] = False,
    wait: Annotated[
        bool, typer.Option("--wait", help="Wait until the checks end (exit 1 if they fail)")
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds --wait waits at most", min=1)
    ] = 900.0,
    local_checks: Annotated[
        bool,
        typer.Option(
            "--validate/--no-validate", help="Run `mapi plugin validate` first (warnings only)"
        ),
    ] = True,
) -> None:
    """Build the wheel, upload it to your workspace and start its checks.

    Exit 0 when the upload is accepted (with --wait: its checks passed), 1 when it was
    refused or its checks failed, 2 when the server or the storage could not be reached."""
    st = state(ctx)
    out = st.output
    given_wheel = path.is_file()
    config: dict[str, Any] = {}
    checks: dict[str, Any] = {"ran": False, "exit_code": None, "warnings": []}
    if given_wheel:
        if path.suffix != ".whl":
            raise _refused(
                f"{path.name!r} is not a wheel. Upload a wheel; `mapi plugin push` builds one "
                "from a package directory."
            )
    else:
        if not (path / "pyproject.toml").is_file():
            raise FileNotFoundError(f"{path / 'pyproject.toml'} not found (not a plugin package)")
        config = _upload_config(path)
        if local_checks:
            checks = _local_checks(st, path)
    with tempfile.TemporaryDirectory(prefix="mapi-push-") as tmp:
        wheel = (
            check_wheel(path)
            if given_wheel
            else check_wheel(_build_wheel(st, path, Path(tmp)), built=True)
        )
        with _api(st, project) as api:
            plugin = _resolve_plugin(api, plugin_short, config, wheel)
            expected = upload_dist_name(plugin.namespace)
            if _canonical(wheel.dist) != expected:
                raise _refused(
                    f"The wheel is for the distribution {wheel.dist!r}; the plugin "
                    f"{plugin.short_name!r} takes {expected!r} only."
                )
            ticket = api.send(
                _ticket_call(
                    plugin.id, PluginUploadTicketRequest(filename=wheel.path.name, size=wheel.size)
                )
            )
            if wheel.size > ticket.max_bytes:
                raise _refused(
                    f"The wheel is {wheel.size} bytes. This workspace takes at most "
                    f"{ticket.max_bytes} bytes."
                )
            out.info(f"uploading {wheel.path.name} ({wheel.size} bytes) ...")
            _post_wheel(api, ticket, wheel)
            version = api.send(
                _complete_call(
                    plugin.id,
                    ticket.upload_id,
                    PluginUploadComplete(
                        project=_project_slug(api), request_approval=request_approval
                    ),
                )
            )
            if wait:
                version = _wait(api, out, plugin, version, timeout)
    if version.overlay_sha256 is not None and version.overlay_sha256 != wheel.sha256:
        out.warn(
            f"the server has the sha256 {version.overlay_sha256}; the local wheel has "
            f"{wheel.sha256}"
        )
    code = EXIT_OK if (not wait or version.state in _GOOD_STATES) else EXIT_PROBLEMS
    if out.json_mode:
        out.emit_json(
            {
                "ok": code == EXIT_OK,
                "exit_code": code,
                "plugin": _plugin_doc(plugin),
                "wheel": {k: v for k, v in wheel.doc().items()},
                "upload_id": ticket.upload_id,
                "request_approval": request_approval,
                "waited": wait,
                "local_checks": checks,
                "version": version.model_dump(mode="json"),
            }
        )
        raise typer.Exit(code)
    out.success(
        f"uploaded {plugin.short_name} {version.version} ({wheel.path.name}, {wheel.size} bytes)"
    )
    if wait:
        _print_version(out, plugin, version)
    else:
        out.out.print(
            f"state: {STATE_TEXT.get(version.state, version.state)} ({version.state})",
            markup=False,
            highlight=False,
        )
    _next_step(out, plugin, version)
    raise typer.Exit(code)


# ------------------------------------------------------------------------------- status


@app.command("status")
@cli_errors
@_transport_exit
def status(
    ctx: typer.Context,
    target: Annotated[
        str | None,
        typer.Argument(help="SHORT (its versions) or SHORT@VERSION (one version in full)"),
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", help="<tenant>/<project> to read from")
    ] = None,
) -> None:
    """Where your uploaded code is: every plugin, one plugin's versions, or one version."""
    st = state(ctx)
    out = st.output
    with _api(st, project) as api:
        if target is None:
            plugins = list(_plugins(api))
            if out.json_mode:
                out.emit_json({"plugins": [p.model_dump(mode="json") for p in plugins]})
                return
            if not plugins:
                out.info("No uploaded code yet. Start with: mapi plugin init SHORT --upload")
                return
            out.table(
                ["plugin", "namespace", "title", "latest", "state", "who can use it"],
                [
                    (
                        p.short_name,
                        p.namespace,
                        p.title,
                        p.latest_version.version if p.latest_version else "",
                        STATE_TEXT.get(p.latest_version.state, "") if p.latest_version else "",
                        p.latest_version.scope if p.latest_version else "",
                    )
                    for p in plugins
                ],
                title="Uploaded code",
            )
            return
        short, version = _split_target(target)
        plugin = _plugin_by_short(api, short)
        if version is None:
            versions = list(_versions(api, plugin.id))
            if out.json_mode:
                out.emit_json(
                    {
                        "plugin": plugin.model_dump(mode="json"),
                        "versions": [v.model_dump(mode="json") for v in versions],
                    }
                )
                return
            out.table(
                ["version", "state", "who can use it", "uploaded", "id"],
                [
                    (
                        v.version,
                        f"{STATE_TEXT.get(v.state, v.state)} ({v.state})",
                        v.scope,
                        v.uploaded_at.strftime("%Y-%m-%d %H:%M"),
                        v.id,
                    )
                    for v in versions
                ],
                title=f"{plugin.short_name} ({plugin.namespace})",
            )
            return
        detail = _version_of(api, plugin, version)
    if out.json_mode:
        out.emit_json(
            {"plugin": plugin.model_dump(mode="json"), "version": detail.model_dump(mode="json")}
        )
        return
    _print_version(out, plugin, detail)
    _next_step(out, plugin, detail)


# --------------------------------------------------------------------------------- yank


@app.command("yank")
@cli_errors
@_transport_exit
def yank(
    ctx: typer.Context,
    target: Annotated[str, typer.Argument(help="SHORT@VERSION, e.g. acme@0.1.0")],
    reason: Annotated[
        YankReasonArg,
        typer.Option("--reason", help="withdrawn, security (also cancels its runs) or superseded"),
    ],
    note: Annotated[str | None, typer.Option("--note", help="Why, for your team")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")] = False,
    project: Annotated[
        str | None, typer.Option("--project", help="<tenant>/<project> to act in")
    ] = None,
) -> None:
    """Withdraw a version: it leaves every catalog at once and runs that need it cannot
    start. Needs a sign-in (`mapi login`): API tokens cannot withdraw uploaded code."""
    st = state(ctx)
    out = st.output
    short, version = _split_target(target)
    if version is None:
        raise _invalid(f"Name the version to withdraw: {short}@VERSION.")
    if st.config().token is not None:
        raise MapiError(
            "Withdrawing uploaded code needs a sign-in. API tokens cannot do it. Run "
            "`mapi login` (and unset MAPI_TOKEN or --token), then try again.",
            code=ErrorCode.FORBIDDEN,
        )
    body = PluginYank(reason=reason.value, note=note)
    with _api(st, project) as api:
        plugin = _plugin_by_short(api, short)
        current = _version_of(api, plugin, version)
        use = api.send(_usage_call(plugin.id, current.id))
        more = "+" if use.truncated else ""
        out.info(
            f"{short} {version} is used by {use.runs_30d} runs in the last 30 days, "
            f"{len(use.model_versions)}{more} model versions, {len(use.endpoints)}{more} "
            f"endpoints and {len(use.dashboards)}{more} dashboards."
        )
        confirm(st, f"Withdraw {short} {version} ({reason.value})?", yes=yes)
        done = api.send(_yank_call(plugin.id, current.id, body))
    if out.json_mode:
        out.emit_json({"plugin": _plugin_doc(plugin), "version": done.model_dump(mode="json")})
        return
    out.success(f"withdrew {short} {version} ({reason.value})")
