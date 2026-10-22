"""``mapi plugin init|test|validate|doctor``: scaffold, test and check extension packages.

* ``init`` writes a package with one module, entry point and contract-kit test per kind
  (``--kind model|step|recipe|node|template``, repeatable; ``--base custom|pymc|sklearn``
  for models). Every scaffold has a complete About card and a README that ends in the app.
* ``test`` runs the package's tests (the contract kits) with this interpreter.
* ``validate`` loads only that package's entry points in a subprocess, runs every static
  check and prints the catalog fragment it would add.
* ``doctor`` checks every installed extension (all six entry-point groups), the load
  diagnostics, image routing and the image digest, and with ``--server`` compares the local
  catalog with the server's.

``validate`` and ``doctor`` exit 0 when everything passes, 1 when a check finds a problem
and 2 when the package or an entry point cannot be loaded (or the server cannot be read).
Who may use which namespace comes from :mod:`mapi_spec.namespaces`, the one list the SDK
registry and the workflow loaders use too.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Final

import typer
from mapi_client import ConfigError
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.namespaces import BUILTIN_NAMESPACES, PLATFORM_RESERVED, reserved_namespace
from mapi_spec.refs import UseRef
from rich.text import Text

from mapi_cli.output import Output
from mapi_cli.plugin_doctor import EXIT_BROKEN, EXIT_OK, EXIT_PROBLEMS, compare_server
from mapi_cli.state import cli_errors, state

app = typer.Typer(
    help="Extension authoring: scaffold, test, validate and diagnose plugin packages.",
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
    would shadow a platform package, and ids that do not fit their patterns.
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


def _render_context(names: dict[str, str], kinds: tuple[Kind, ...], base: Base) -> dict[str, str]:
    root = resources.files("mapi_cli.plugin_template")
    v = names["sdk_version"]
    ctx = dict(names)
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
    name: Annotated[str, typer.Argument(help="Distribution name, e.g. acme-demand")],
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
) -> None:
    """Scaffold an extension package: per kind a module, an entry point and contract tests."""
    out = state(ctx).output
    kinds = _ordered(kind)
    names = plugin_names(name, namespace, kinds, base)
    dest = directory / name
    if dest.exists() and any(dest.iterdir()) and not force:
        raise FileExistsError(f"{dest} exists and is not empty (use --force)")
    files = render_plugin(dest, names, kinds, base)
    ids = {k.value: names[f"{k.value}_id"] for k in kinds}
    if out.json_mode:
        out.emit_json(
            {
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
        )
        return
    out.success(f"created {dest}: " + ", ".join(f"{k} {i}" for k, i in ids.items()))
    for f in files:
        out.out.print(f"  {f.relative_to(dest)}", markup=False, highlight=False)
    out.out.print(
        f"next: mapi plugin test {dest} && mapi plugin validate {dest}",
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
