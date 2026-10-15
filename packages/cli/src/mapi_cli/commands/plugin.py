"""``mapi plugin init|test``: scaffold a model plugin package and run its contract kit."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import Annotated, Final

import typer
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.refs import UseRef

from mapi_cli.state import cli_errors, state

app = typer.Typer(help="Plugin authoring: scaffold and test model plugins.", no_args_is_help=True)

_DIST_RE: Final = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_NAMESPACE_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
RESERVED_NAMESPACES: Final = frozenset({"core", "mmm", "ml", "sklearn", "ts", "tabular", "toy"})
"""Built-in (T0) namespaces plus the SDK's toy references; tenant plugins may not use them."""

# template file -> path inside the new package ({module} is substituted)
_FILES: Final = {
    "pyproject.toml.tmpl": "pyproject.toml",
    "README.md.tmpl": "README.md",
    "init.py.tmpl": "src/{module}/__init__.py",
    "model.py.tmpl": "src/{module}/model.py",
    "test_contract.py.tmpl": "tests/test_contract.py",
}


def _sdk_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return ".".join(version("mapi-sdk").split(".")[:2])
    except PackageNotFoundError:  # pragma: no cover - the CLI depends on mapi-sdk
        return "0.1"


def plugin_names(name: str, namespace: str | None) -> dict[str, str]:
    """Distribution, module, namespace and model id for a plugin called ``name``."""
    if not _DIST_RE.match(name):
        raise MapiError(
            f"plugin name {name!r} must be lowercase letters/digits separated by '-' or '_'",
            code=ErrorCode.SCHEMA_INVALID,
        )
    module = name.replace("-", "_")
    ns = namespace or module.removeprefix("mapi_plugin_")
    if not _NAMESPACE_RE.match(ns):
        raise MapiError(f"invalid namespace {ns!r}", code=ErrorCode.SCHEMA_INVALID)
    if ns in RESERVED_NAMESPACES:
        raise MapiError(
            f"namespace {ns!r} is reserved for built-in plugins; pass --namespace",
            code=ErrorCode.SCHEMA_INVALID,
        )
    plugin_id = f"{ns}.group_mean"
    UseRef.parse(plugin_id)
    return {
        "dist": name,
        "module": module,
        "namespace": ns,
        "plugin_id": plugin_id,
        "sdk_version": _sdk_version(),
    }


def render_plugin(dest: Path, names: dict[str, str]) -> list[Path]:
    """Write the package files; returns them."""
    root = resources.files("mapi_cli.plugin_template")
    written: list[Path] = []
    for src, rel in _FILES.items():
        text = (root / src).read_text(encoding="utf-8")
        for key, value in names.items():
            text = text.replace("{{" + key + "}}", value)
        leftover = re.search(r"\{\{\w+\}\}", text)
        if leftover:  # pragma: no cover - guarded by the template test
            raise RuntimeError(f"template {src} has an unfilled placeholder {leftover.group(0)}")
        path = dest / rel.format(module=names["module"])
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
    directory: Annotated[
        Path, typer.Option("--dir", help="Parent directory (default: current)")
    ] = Path("."),
    namespace: Annotated[
        str | None, typer.Option("--namespace", help="Plugin id namespace (default: module)")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Write into a non-empty dir")] = False,
) -> None:
    """Scaffold a plugin package (model, entry point and contract-kit tests)."""
    out = state(ctx).output
    names = plugin_names(name, namespace)
    dest = directory / name
    if dest.exists() and any(dest.iterdir()) and not force:
        raise FileExistsError(f"{dest} exists and is not empty (use --force)")
    files = render_plugin(dest, names)
    if out.json_mode:
        out.emit_json({"path": str(dest), **names, "files": [str(f) for f in files]})
        return
    out.success(f"created {dest} with model {names['plugin_id']}")
    for f in files:
        out.out.print(f"  {f.relative_to(dest)}", markup=False, highlight=False)
    out.out.print(f"next: mapi plugin test {dest}", markup=False, highlight=False)


def pytest_command(path: Path, *, contract_only: bool, extra: list[str]) -> list[str]:
    tests = path.resolve() / "tests"
    cmd = [sys.executable, "-m", "pytest", str(tests), "-p", "no:cacheprovider"]
    if contract_only:
        cmd += ["-m", "mapi_contract"]
    return cmd + extra


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
    env = os.environ.copy()
    src = path.resolve() / "src"
    if src.is_dir():  # importable without installing the package
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(src), env.get("PYTHONPATH")) if p)
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
