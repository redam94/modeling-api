"""The platform documentation is checked like code.

Default suite (no server needed):

* the local CLI walkthrough (``docs/snippets/cli_local.sh``) runs end to end;
* every other platform snippet parses (``bash -n``, ``compile``) and imports what exists;
* the generated reference pages (CLI ``--help``, server settings, the OpenAPI copy) cover
  every command and setting, and regenerate idempotently;
* the configuration reference names every ``MAPI_*`` variable found in the source tree;
* the vendored Redoc bundle matches its pinned hash and loads nothing from a CDN;
* every screenshot is at most 300 KB and used by a page.

``make e2e`` (marker ``e2e``) additionally runs the Python client, HTTP and remote CLI
walkthroughs against the running stack, exactly as the guides show them.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
SNIPPETS = DOCS / "snippets"
SHOTS = DOCS / "assets" / "screenshots"
CONFIG_PAGE = DOCS / "operations" / "configuration.md"
VENV_BIN = Path(sys.executable).parent
SHELL_SNIPPETS = sorted(SNIPPETS.glob("*.sh"))
UPLOAD = ("--name", "eu_media_future")

#: ``MAPI_*`` tokens in the source that are not configuration.
NOT_CONFIGURATION = {
    "MAPI_SERVICES",  # a tuple of compose service names in tests/devops
    "MAPI_SECRET",  # a dummy variable a test checks is scrubbed from the worker
    "MAPI_DEV_",  # the prefix of MAPI_DEV_<NAME>_CMD, documented as such
}
SCAN_DIRS = ("packages", "plugins", "conformance", "scripts", "images", "apps", "tests", "deploy")
SCAN_FILES = (
    "Makefile",
    "docker-compose.yml",
    "compose.override.example.yml",
    ".env.example",
    "pyproject.toml",
)
SKIP_PARTS = {"node_modules", "dist", ".venv", "__pycache__", "site", ".local", "screenshots"}
TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".mjs", ".js", ".json", ".yml", ".yaml", ".toml", ".sh", ".md",
    ".html", ".conf", ".template", ".inc", ".txt", "",
}  # fmt: skip


def _load(name: str) -> ModuleType:
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{VENV_BIN}{os.pathsep}{env.get('PATH', '')}"
    env.update(extra)
    return env


def _bash(script: Path, cwd: Path, env: dict[str, str], timeout: float) -> str:
    proc = subprocess.run(
        ["bash", str(script)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    tail = f"{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}"
    assert proc.returncode == 0, f"{script.name} failed:\n{tail}"
    return proc.stdout


needs_shell = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None, reason="needs bash and jq"
)


# ------------------------------------------------------------------------------ snippets


@pytest.mark.parametrize("script", SHELL_SNIPPETS, ids=lambda p: p.name)
def test_shell_snippets_parse(script: Path) -> None:
    if shutil.which("bash") is None:
        pytest.skip("needs bash")
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_client_snippet_compiles_and_imports_exist() -> None:
    source = (SNIPPETS / "client_tour.py").read_text("utf-8")
    tree = ast.parse(source)
    compile(tree, "client_tour.py", "exec")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            module = importlib.import_module(node.module)
            for alias in node.names:
                assert hasattr(module, alias.name), f"{node.module}.{alias.name} does not exist"


@pytest.mark.slow
@needs_shell
def test_cli_local_walkthrough(tmp_path: Path) -> None:
    out = _bash(SNIPPETS / "cli_local.sh", tmp_path, _env(), timeout=600)
    assert "spec.yaml: valid" in out
    assert (tmp_path / "bundles" / "tabular" / "bundle.zarr.zip").exists()
    assert "core.adstock" in out  # the jq'd step ids


# ------------------------------------------------------------------------- generated pages


def test_generated_reference_covers_cli_settings_and_openapi(tmp_path: Path) -> None:
    import typer
    from mapi_cli.main import app
    from mapi_server.settings import Settings

    gen = _load("gen_docs_reference.py")
    assert gen.generate(tmp_path), "the generator wrote nothing"
    cli = (tmp_path / "cli-reference.md").read_text("utf-8")
    for path, _ in gen._commands(typer.main.get_command(app), ("mapi",)):
        assert f"`{' '.join(path)}` {{ #{'-'.join(path)} }}" in cli, path
    assert "-h, --help" in cli and "Usage: mapi runs ls" in cli

    settings = (tmp_path / "server-settings.md").read_text("utf-8")
    for name in Settings.model_fields:
        assert f"`MAPI_{name.upper()}`" in settings, name

    copied = json.loads((tmp_path / "openapi.json").read_text("utf-8"))
    assert copied == json.loads((ROOT / "apps" / "web" / "openapi.json").read_text("utf-8"))
    assert copied["openapi"].startswith("3.1")
    assert gen.generate(tmp_path) == [], "regenerating unchanged content must not rewrite files"


def _source_files() -> Iterator[Path]:
    for name in SCAN_FILES:
        if (ROOT / name).is_file():
            yield ROOT / name
    for top in SCAN_DIRS:
        for path in (ROOT / top).rglob("*"):
            if SKIP_PARTS.intersection(path.relative_to(ROOT).parts) or not path.is_file():
                continue
            if path.name == "gen_docs_platform.py":  # names variables to explain them
                continue
            if path.suffix in TEXT_SUFFIXES or path.name == "Dockerfile":
                yield path


def test_configuration_reference_names_every_variable() -> None:
    from mapi_server.settings import Settings

    page = CONFIG_PAGE.read_text("utf-8")
    found: dict[str, Path] = {}
    pattern = re.compile(r"\bMAPI_[A-Z0-9_]+")
    for path in _source_files():
        try:
            text = path.read_text("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name in pattern.findall(text):
            found.setdefault(name, path)
    assert len(found) > 40, "the scan found suspiciously few variables"
    missing = []
    for name, where in sorted(found.items()):
        if name in NOT_CONFIGURATION:
            continue
        if re.fullmatch(r"MAPI_DEV_[A-Z_]+_CMD", name) and "MAPI_DEV_<NAME>_CMD" in page:
            continue
        if f"`{name}`" not in page and f"{name}%" not in page:
            missing.append(f"{name} (first seen in {where.relative_to(ROOT)})")
    for field in Settings.model_fields:
        if f"`MAPI_{field.upper()}`" not in page:
            missing.append(f"MAPI_{field.upper()} (mapi_server.settings.Settings)")
    assert not missing, "document these in docs/operations/configuration.md:\n" + "\n".join(missing)


def test_generated_platform_pages(tmp_path: Path) -> None:
    """Permissions, metrics, alerts, runbooks and the variable index come from the code."""
    from mapi_server.observability.metrics import Metrics
    from mapi_spec.tenancy import PERMISSIONS, Capability, ResourceKind
    from mapi_spec.tokens import ScopeName

    gen = _load("gen_docs_platform.py")
    assert set(gen.RESOURCE_LABELS) == {r.value for r in ResourceKind}
    assert gen.generate(tmp_path), "the generator wrote nothing"
    frag = tmp_path / "fragments"

    matrix = (frag / "roles-matrix.md").read_text("utf-8")
    rows = [ln for ln in matrix.splitlines() if ln.startswith("| ") and "---" not in ln]
    assert len(rows) == len(PERMISSIONS) + 1  # the header
    assert "`kill_switch`" in matrix and "internal services only" in matrix
    scopes = (frag / "token-scopes.md").read_text("utf-8")
    for scope in ScopeName:
        if scope is not ScopeName.ADMIN:
            assert ("`score:*`" if scope is ScopeName.SCORE else f"`{scope.value}`") in scopes
    # M3.5: approving needs the reviewer capability, which the matrix and its section name
    assert "✓ with `reviewer`" in matrix
    assert set(gen.CAPABILITY_TEXT) == {c.value for c in Capability}
    assert "| `reviewer` | `model` approve |" in (frag / "capabilities.md").read_text("utf-8")
    resources = [ln.split("|")[1].strip() for ln in rows[1:]]
    labelled = [r for r in resources if r]
    assert len(labelled) == len(set(labelled)), "a resource's actions are not together"
    assert "`public` | isolation ≥ gvisor" in (frag / "trust-tiers.md").read_text("utf-8")

    metrics = (frag / "metrics.md").read_text("utf-8")
    exported = Metrics().render().decode()
    for name in re.findall(r"^\| `(mapi_[a-z_]+)`", metrics, re.M):
        assert f"# TYPE {name} " in exported, name
    assert metrics.count("\n| `mapi_") >= 15

    alerts = (frag / "alerts.md").read_text("utf-8")
    assert "`MapiIntegrityFailure` | page" in alerts and "{{" not in alerts
    runbooks = (frag / "runbooks.md").read_text("utf-8")
    assert all(f"`{p.relative_to(ROOT).as_posix()}`" in runbooks
               for p in (ROOT / "ops" / "runbooks").glob("*.md"))  # fmt: skip

    env = (tmp_path / "environment-variables.md").read_text("utf-8")
    unexplained = [
        ln for ln in env.splitlines() if ln.startswith("| `MAPI_") and ln.endswith("| — |")
    ]
    assert not unexplained, (
        "explain these in configuration.md (or gen_docs_platform.NOT_READ):\n"
        + ("\n".join(unexplained))
    )
    assert gen.generate(tmp_path) == [], "regenerating unchanged content must not rewrite files"


def test_every_generated_fragment_marker_exists() -> None:
    gen = _load("gen_docs_platform.py")
    names = set(gen.fragment_names())
    used = set()
    for page in DOCS.rglob("*.md"):
        used |= set(
            re.findall(r"^<!-- generated: ([a-z0-9-]+) -->$", page.read_text("utf-8"), re.M)
        )
    assert used <= names, f"pages embed unknown fragments: {sorted(used - names)}"
    assert used == names, f"fragments no page embeds: {sorted(names - used)}"


# ---------------------------------------------------------------------- OpenAPI explorer


def test_redoc_bundle_is_pinned_and_local() -> None:
    vendor = _load("vendor_redoc.py")
    data = vendor.BUNDLE.read_bytes()
    assert hashlib.sha256(data).hexdigest() == vendor.BUNDLE_SHA256
    assert b"cdn.redoc.ly" not in data
    assert vendor.SOURCE_MAP.strip() not in data[-200:]  # the 3.9 MB map is not vendored
    assert (vendor.DEST / "LICENSE.txt").exists()

    viewer = (DOCS / "reference" / "openapi-viewer.html").read_text("utf-8")
    assert 'http-equiv="Content-Security-Policy"' in viewer
    assert "script-src 'self'" in viewer and "https://" not in viewer.split("<body>")[1]
    for ref in re.findall(r'(?:src|spec-url)="([^"]+)"', viewer):
        target = (DOCS / "reference" / ref).resolve()
        generated = DOCS / "reference" / "generated"
        assert target.exists() or target.parent == generated.resolve(), ref


# ---------------------------------------------------------------------------- screenshots


def test_screenshots_are_small_and_used() -> None:
    pages = "\n".join(p.read_text("utf-8") for p in DOCS.rglob("*.md"))
    shots = sorted(SHOTS.glob("*.png"))
    assert len(shots) >= 6
    for shot in shots:
        assert shot.stat().st_size <= 300 * 1024, f"{shot.name} is over 300 KB"
        assert f"assets/screenshots/{shot.name}" in pages, f"{shot.name} is not used by any page"


# ------------------------------------------------------------------- against a live stack


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, dict[str, str]]]:
    """A working directory with the demo files, against the running stack (make dev)."""
    from mapi_conformance.stack import stack_auth
    from mapi_spec.localdev import LocalDev

    local = LocalDev.from_environ(os.environ)
    try:
        ready = httpx.get(f"{local.api_url.rstrip('/')}/readyz", timeout=5)
    except httpx.HTTPError as exc:  # pragma: no cover - depends on the stack
        pytest.fail(f"the API at {local.api_url} is not reachable ({exc}); run make dev first")
    assert ready.status_code == 200, ready.text
    work = tmp_path_factory.mktemp("docs-e2e")
    (work / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
    env = _env(MAPI_API_URL=local.api_url)
    env.pop("MAPI_PROFILE", None)
    # a stack that requires sign-in (the dev profile of `make dev`): the walkthroughs run as
    # MAPI_TOKEN in MAPI_PROJECT, exactly as their docs tell a reader to
    data_dir = Path(local.data_dir)
    auth = stack_auth(
        local.api_url, data_dir=data_dir if data_dir.is_absolute() else ROOT / data_dir
    )
    if auth.token:
        env["MAPI_TOKEN"] = auth.token
        env["MAPI_PROJECT"] = auth.project or "default/default"
    for args in (
        [sys.executable, "scripts/seed.py", "--export", "demo/"],
        [sys.executable, "scripts/seed.py"],
        [str(VENV_BIN / "mapi"), "datasets", "upload", "demo/eu_media_future.zarr.zip", *UPLOAD],
    ):
        proc = subprocess.run(
            args, cwd=work, env=env, capture_output=True, text=True, timeout=900, check=False
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
    yield work, env


@pytest.mark.e2e
@pytest.mark.integration
@pytest.mark.slow
def test_python_client_walkthrough(stack: tuple[Path, dict[str, str]]) -> None:
    work, env = stack
    proc = subprocess.run(
        [sys.executable, str(SNIPPETS / "client_tour.py")],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    assert "end: succeeded" in proc.stdout
    assert "SIDE_TABLE_COVERAGE" in proc.stdout and "MODE_NOT_SUPPORTED" in proc.stdout


@pytest.mark.e2e
@pytest.mark.integration
@pytest.mark.slow
@needs_shell
def test_http_walkthrough(stack: tuple[Path, dict[str, str]]) -> None:
    work, env = stack
    out = _bash(SNIPPETS / "http_tour.sh", work, env, timeout=1800)
    assert '"state": "succeeded"' in out
    assert '"code": "MODE_NOT_SUPPORTED"' in out


@pytest.mark.e2e
@pytest.mark.integration
@pytest.mark.slow
@needs_shell
def test_remote_cli_walkthrough(stack: tuple[Path, dict[str, str]]) -> None:
    work, env = stack
    out = _bash(SNIPPETS / "cli_remote.sh", work, env, timeout=1800)
    assert "valid (remote checks" in out
    assert "wrote forecast.zarr.zip" in out


def _dev_stack_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """The environment of a walkthrough that signs in through the dev profile's IdP."""
    from mapi_spec.localdev import LocalDev

    local = LocalDev.from_environ(os.environ)
    try:
        me = httpx.get(f"{local.api_url.rstrip('/')}/v1/me", timeout=5)
    except httpx.HTTPError as exc:  # pragma: no cover - depends on the stack
        pytest.fail(f"the API at {local.api_url} is not reachable ({exc}); run make dev first")
    if me.status_code != 401:
        pytest.skip("the walkthrough needs a stack that requires sign-in (the dev profile)")
    env = _env(
        MAPI_API_URL=local.api_url,
        MAPI_CONFIG=str(tmp_path / "config.toml"),
        MAPI_CREDENTIALS=str(tmp_path / "credentials.json"),
        **extra,
    )
    for name in ("MAPI_PROFILE", "MAPI_TOKEN", "MAPI_PROJECT", "MAPI_URL"):
        env.pop(name, None)
    return env


@pytest.mark.e2e
@pytest.mark.integration
@needs_shell
def test_admin_walkthrough(tmp_path: Path) -> None:
    """The Administration guide, as the dev profile's operator and a tenant admin."""
    import secrets

    env = _dev_stack_env(tmp_path, TENANT=f"docs-{secrets.token_hex(3)}")
    out = _bash(SNIPPETS / "admin_tour.sh", tmp_path, env, timeout=600)
    assert env["TENANT"] in out and "growth" in out
    assert "viewer@dev.local\trunner\tgrowth" in out
    assert "analyst@dev.local\tadmin\t(tenant-wide)" in out
    assert "Revoked tok_" in out
    assert "admin tour: done" in out


@pytest.mark.e2e
@pytest.mark.integration
@needs_shell
def test_auth_walkthrough(tmp_path: Path) -> None:
    """The Authentication guide: a sign-in, a token, what its scopes allow, revocation."""
    out = _bash(SNIPPETS / "auth_tour.sh", tmp_path, _dev_stack_env(tmp_path), timeout=300)
    lines = out.splitlines()
    assert lines[0] == "anonymous: 401"
    assert '"auth":"session"' in lines[1] and '"auth":"token"' in out
    assert out.count('{"status":403,"code":"FORBIDDEN"}') == 3
    assert "revoke: 204" in out
    assert lines[-1] == '{"status":401,"code":"UNAUTHENTICATED"}'
