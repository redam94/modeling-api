"""The v0.19 upload docs are real: the pages are in the nav, every snippet runs, every CLI
command and option the pages name exists, the app labels they name are in the web app, and
the operator page agrees with the settings, the placement rule and the dev-flag validator.

The snippets also run in-process through ``test_docs_snippets.py``; here they run in a
subprocess so that their output can be checked.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
SNIPPETS = DOCS / "snippets"
WEB = ROOT / "apps" / "web" / "src"

UPLOAD = DOCS / "extend" / "upload.md"
REVIEW = DOCS / "extend" / "review-uploads.md"
OPERATOR = DOCS / "operations" / "uploaded-code.md"
WHATS_NEW = DOCS / "whats-new-0.19.md"
STUB = DOCS / "extend" / "upload-roadmap.md"
PAGES = [UPLOAD, REVIEW, OPERATOR, WHATS_NEW]
CLI_SNIPPET = SNIPPETS / "upload_cli.sh"

APP_LABELS = {
    UPLOAD: [
        "Upload your own code",
        "Start a new plugin",
        "Drop a wheel here, or choose one",
        "Checking the files",
        "Running the tests",
        "Ready for you",
        "Only you can use this",
        "Ask a reviewer to approve it for the team",
        "Waiting for a reviewer",
        "Approved for the team",
        "Not approved",
        "Withdraw this version",
        "Uploaded code",
        "Who can upload",
    ],
    REVIEW: [
        "Review this upload",
        "What's in the file",
        "Software bill of materials",
        "Links in the About cards",
        "Test report",
        "What changed",
        "Your decision",
        "Approve for the team",
        "Don't approve",
        "Withdraw this version",
    ],
    OPERATOR: [
        "Kill switches",
        "Allow uploaded code to run",
        "Cancel runs of uploaded code",
        "Paused by the platform",
        "Development only: uploaded code runs without a sandbox here.",
    ],
}
"""Text that the pages tell the reader to look for or to select, per page."""


NOT_ERROR_CODES = {
    "DEV_MODE_ISOLATION",
    "TIER_PLACEMENT",
    "K_SERVICE",
    "CLOUD_RUN_JOB",
    "RLIMIT_NPROC",
}
"""Upper-case names in the pages that are not error codes (a run event, a table, env)."""


def _text(path: Path) -> str:
    return path.read_text("utf-8")


def _web() -> str:
    return "\n".join(
        p.read_text("utf-8")
        for p in WEB.rglob("*.ts*")
        if p.is_file() and ".test." not in p.name and "mocks" not in p.parts
    )


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run(args: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False, env=_env()
    )


# ------------------------------------------------------------------------------- the pages


def test_the_pages_are_in_the_nav() -> None:
    nav = _text(ROOT / "mkdocs.yml")
    for page in PAGES:
        rel = page.relative_to(DOCS).as_posix()
        assert page.is_file(), rel
        assert re.search(rf"[:\-]\s+{re.escape(rel)}\s*$", nav, re.M), f"{rel} is not in the nav"
    home = nav.split("  - Home:", 1)[1].split("\n  - ", 1)[0]
    assert home.index("whats-new-0.19.md") < home.index("whats-new-0.18.md")


def test_the_old_roadmap_page_points_to_the_new_ones() -> None:
    nav = _text(ROOT / "mkdocs.yml")
    stub = _text(STUB)
    assert "(upload.md)" in stub and "(review-uploads.md)" in stub
    assert "../operations/uploaded-code.md" in stub
    assert "extend/upload-roadmap.md" in nav.split("\nnav:", 1)[0], "list it under not_in_nav"
    # the pages of this release link the new pages, not the stub (older pages may still
    # link the stub: it stays as a pointer)
    for page in [*PAGES, *(DOCS / "extend").rglob("*.md")]:
        if page == STUB:
            continue
        assert "upload-roadmap.md" not in _text(page), f"{page.relative_to(ROOT)} links the stub"


def test_the_extend_pages_link_to_upload() -> None:
    pages = ("extend/index.md", "extend/develop/index.md", "extend/develop/run-on-your-stack.md")
    for rel in pages:
        text = _text(DOCS / rel)
        assert re.search(r"\]\((?:\.\./)?upload\.md\)", text), f"{rel} has no link to upload.md"


def test_the_tutorial_includes_the_tested_snippets() -> None:
    text = _text(UPLOAD)
    includes = set(re.findall(r'^\s*--8<--\s+"([^"]+)"\s*$', text, re.M))
    assert {
        "upload_cli.sh:init",
        "upload_cli.sh:push",
        "upload_cli.sh:approval",
        "upload_cli.sh:yank",
        "upload_namespace.py:namespace",
        "upload_wheel_check.py:inspect",
        "upload_wheel_check.py:refused",
    } <= includes, includes
    review = set(re.findall(r'^\s*--8<--\s+"([^"]+)"\s*$', _text(REVIEW), re.M))
    assert {"upload_decision.py:decision", "upload_decision.py:yank"} <= review, review


def test_the_tutorial_walks_the_whole_flow_in_order() -> None:
    """init --upload -> push -> test report -> ask for approval -> private use -> approved."""
    text = _text(UPLOAD)
    steps = [
        "## 1. Claim a name and scaffold the package",
        "## 2. Push it",
        "## 3. Use it yourself",
        "## 4. Ask for approval",
        "## 5. Use it with your team",
    ]
    where = [text.index(s) for s in steps]
    assert where == sorted(where)


def test_screenshots_exist() -> None:
    for page in PAGES:
        for target in re.findall(r"!\[[^\]]*\]\(([^)]+\.png)\)", _text(page)):
            assert (page.parent / target).resolve().is_file(), f"{page.name}: {target}"
    assert "screenshots/plugins-upload-done.png" in _text(UPLOAD)
    assert "screenshots/plugins-review.png" in _text(REVIEW)


@pytest.mark.parametrize("page", list(APP_LABELS), ids=lambda p: p.name)
def test_the_pages_name_what_the_app_shows(page: Path) -> None:
    text, web = re.sub(r"\s+", " ", _text(page)), _web()
    for label in APP_LABELS[page]:
        assert label in text, f"{page.name} does not name {label!r}"
        assert label in web, f"{page.name} names {label!r}, which the web app does not show"


def test_the_review_checklist_is_the_one_the_panel_shows() -> None:
    """web+cli+docs-6: the page says "The panel shows this checklist"; the panel's CHECKLIST
    (reviewer-panel.tsx) has the same items, in the same order."""
    page = _text(REVIEW).split("## The review checklist", 1)[1].split("\n## ", 1)[0]
    items = [
        re.sub(r"\s+", " ", m.group(1)).strip()
        for m in re.finditer(r"^- \[ \] \*\*(.+?)\*\*", page, flags=re.M | re.S)
    ]
    panel = (WEB / "features" / "plugins" / "reviewer-panel.tsx").read_text("utf-8")
    block = panel.split("export const CHECKLIST = [", 1)[1].split("];", 1)[0]
    shown = re.findall(r'^\s*"(.+)",$', block, flags=re.M)
    assert len(items) == len(shown) == 6, (items, shown)
    for doc, app in zip(items, shown, strict=True):
        words = [w for w in re.findall(r"[a-z]+", doc.lower()) if len(w) > 3]
        assert all(w in app.lower() for w in words), (doc, app)
    assert "necessary" in shown[1] and "What changed" in shown[5]


def test_error_codes_named_by_the_pages_exist() -> None:
    from mapi_spec.errors import ErrorCode

    names = {c.value for c in ErrorCode}
    for page in [*PAGES, DOCS / "extend" / "index.md"]:
        for code in re.findall(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)`", _text(page)):
            if code.startswith("MAPI_") or code in NOT_ERROR_CODES:
                continue
            assert code in names, f"{page.name} names the unknown error code {code}"


# ----------------------------------------------------------------------------- the snippets


def test_namespace_snippet() -> None:
    proc = _run([sys.executable, str(SNIPPETS / "upload_namespace.py")], ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "t_k3vq7m_acme\n('k3vq7m', 'acme')\nt-k3vq7m-acme\n" in out
    assert re.search(r"core\.adstock\s+uploaded=False", out)
    assert re.search(r"transform\.t_k3vq7m_acme_zero_share\s+uploaded=True", out)


def test_decision_snippet() -> None:
    import json

    from mapi_spec.plugins import PluginDecision, PluginYank

    proc = _run([sys.executable, str(SNIPPETS / "upload_decision.py")], ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    approve, reject, yank = (json.loads(ln) for ln in lines)
    PluginDecision.model_validate(approve)
    assert PluginDecision.model_validate(reject).note
    PluginYank.model_validate(yank)
    assert "refused: 3 fields" in proc.stdout


def test_wheel_check_snippet_uses_the_sandbox_inspector() -> None:
    proc = _run([sys.executable, "-I", str(SNIPPETS / "upload_wheel_check.py")], ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "ok: True | top level: ['t_k3vq7m_acme']" in out
    assert "entry point: mapi.steps winsorize = t_k3vq7m_acme.steps:STEPS" in out
    assert "ok: False" in out and "pth_file:" in out
    source = _text(SNIPPETS / "upload_wheel_check.py")
    assert "from mapi_runner.plugin_inspect import inspect_wheel" in source
    assert "extract" not in source.replace("extracts", ""), "the snippet never extracts"


def test_shell_snippet_parses() -> None:
    if shutil.which("bash") is None:
        pytest.skip("needs bash")
    proc = subprocess.run(["bash", "-n", str(CLI_SNIPPET)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------------- the CLI


def _cli_lines() -> list[str]:
    lines = []
    for source in [CLI_SNIPPET, *PAGES, DOCS / "extend" / "develop" / "run-on-your-stack.md"]:
        text = _text(source)
        if source.suffix == ".md":
            text = "\n".join(re.findall(r"`([^`\n]*mapi [^`\n]*)`", text))
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if re.match(r"^mapi\b", line):
                lines.append(line)
    return lines


def _command(path: list[str]) -> Any:
    import typer
    from mapi_cli.main import app

    cmd: Any = typer.main.get_command(app)
    for name in path:
        ctx = cmd.context_class(cmd, info_name=name)
        cmd = cmd.get_command(ctx, name)
        assert cmd is not None, f"mapi {' '.join(path)} does not exist"
    return cmd


def test_every_cli_command_and_option_exists() -> None:
    root = _command([])
    global_opts = {o for p in root.params for o in (*p.opts, *p.secondary_opts)}
    seen: set[str] = set()
    for line in _cli_lines():
        words = shlex.split(line.replace("...", ""))[1:]
        words = [w for w in words if w not in global_opts]
        sub = words[:2] if words[:1] == ["plugin"] else words[:1]
        if not sub or sub[0].startswith("-"):
            continue
        cmd = _command(sub)
        opts = {o for p in cmd.params for o in (*p.opts, *p.secondary_opts)}
        for word in words[len(sub) :]:
            if word.startswith("--"):
                assert word.split("=", 1)[0] in opts, f"`{line}`: {word} is not an option"
        seen.add(" ".join(sub))
    assert {"plugin init", "plugin push", "plugin status", "plugin yank", "plugin test"} <= seen


def test_upload_needs_the_upload_flag_and_tokens_cannot_yank() -> None:
    init = _command(["plugin", "init"])
    assert "--upload" in {o for p in init.params for o in p.opts}
    yank = _command(["plugin", "yank"])
    reasons = next(p for p in yank.params if "--reason" in p.opts)
    assert set(reasons.type.choices) == {"withdrawn", "security", "superseded"}
    assert "mapi login" in _text(UPLOAD) and "API tokens can't do it" in _text(UPLOAD)


# ------------------------------------------------------------------------------ operators


def _plugin_settings() -> dict[str, Any]:
    from mapi_server.settings import Settings

    return {
        f"MAPI_{name.upper()}": field.default
        for name, field in Settings.model_fields.items()
        if name.startswith("plugin") or name == "dev_allow_local_untrusted"
    }


def test_the_operator_page_names_every_setting_with_its_default() -> None:
    page = _text(OPERATOR)
    for env, default in _plugin_settings().items():
        assert re.search(rf"`{env}[`=]", page), f"the operator page does not name {env}"
        rows = [ln for ln in page.splitlines() if ln.startswith(f"| `{env}` |")]
        if not rows:
            continue
        shown = rows[0].split("|")[2].strip().strip("`").split(" (")[0].replace(" ", "")
        want = str(default).lower() if isinstance(default, bool) else str(default)
        assert shown == want, f"{env}: the page shows {shown}, the default is {want}"
    prefix = "MAPI" + "_PLUGIN_"  # split: the configuration test scans for MAPI_ names
    for env in re.findall(rf"`({prefix}[A-Z_]+)`", page):
        assert env in _plugin_settings(), f"{env} is not a setting"


def test_env_example_documents_the_dev_flag_with_a_warning() -> None:
    text = _text(ROOT / ".env.example")
    for env in _plugin_settings():
        assert env in text, env
    block = text.split("MAPI_DEV_ALLOW_LOCAL_UNTRUSTED", 1)[0].rsplit("\n\n", 1)[-1]
    assert "WARNING" in block and "WITHOUT A" in block


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"profile": "multi-tenant", "trusted_proxies": "none"}, "the profile is multi-tenant"),
        ({"K_SERVICE": "api"}, "the server runs on Cloud Run (K_SERVICE is set)"),
    ],
)
def test_the_dev_flag_refusals_are_the_ones_the_page_lists(
    override: dict[str, str], reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mapi_server.settings import Settings
    from pydantic import ValidationError

    for var in ("K_SERVICE", "CLOUD_RUN_JOB"):
        monkeypatch.delenv(var, raising=False)
    fields = {k: v for k, v in override.items() if k.islower()}
    for var, value in override.items():
        if var.isupper():
            monkeypatch.setenv(var, value)
    with pytest.raises(ValidationError) as err:
        Settings(dev_allow_local_untrusted=True, **fields)  # type: ignore[arg-type]
    assert reason in str(err.value)
    page = _text(OPERATOR)
    assert "It is refused because the\n    profile is multi-tenant" in page
    for item in ("`multi-tenant`", "`cloudrun-jobs`", "`gcs`", "`K_SERVICE`", "`CLOUD_RUN_JOB`"):
        assert item in page


def test_the_placement_table_matches_the_rule() -> None:
    from mapi_spec.executor import ExecutorCaps, Isolation
    from mapi_spec.tenancy import untrusted_placement_problem as problem

    local = ExecutorCaps(isolation=Isolation.PROCESS)
    docker = ExecutorCaps(isolation=Isolation.PROCESS, runs_untrusted_code=True)
    gvisor = ExecutorCaps(
        isolation=Isolation.GVISOR, egress_enforced=True, dns_enforced=True,
        runs_untrusted_code=True,
    )  # fmt: skip
    assert problem(local, multi_tenant=False) and problem(local, multi_tenant=True)
    assert problem(docker, multi_tenant=False) is None and problem(docker, multi_tenant=True)
    assert problem(gvisor, multi_tenant=True) is None
    rows = {
        ln.split("|")[1].strip(): ln for ln in _text(OPERATOR).splitlines() if ln.startswith("| `")
    }
    assert "| No." in rows["`local-process`"] and "| Never |" in rows["`local-process`"]
    assert "| Yes |" in rows["`docker`"] and "| No (needs gVisor" in rows["`docker`"]
    assert "| Yes, when" in rows["`docker-gvisor`"]
    assert "| Yes, when" in rows["`cloudrun-jobs`"]


def test_the_operator_page_lists_the_cloud_gates() -> None:
    page = _text(OPERATOR)
    assert "## Cloud gates" in page
    for item in ("docker-gvisor", "metadata_token", "plugins` bucket", "not verified"):
        assert item in page, item


# ------------------------------------------------------------------- generated, make, changelog


def test_the_reference_lists_the_uploaded_plugins(tmp_path: Path) -> None:
    import importlib.util
    from typing import get_args

    from mapi_spec.api import V019_ENDPOINTS
    from mapi_spec.plugins import PLUGIN_VERSION_STATES, InspectCheck

    spec = importlib.util.spec_from_file_location(
        "gen_docs_reference", ROOT / "scripts" / "gen_docs_reference.py"
    )
    assert spec is not None and spec.loader is not None
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    page = gen.extension_points_page()
    assert "\n## Uploaded plugins\n" in page
    for check in get_args(InspectCheck):
        assert f"| `{check}` |" in page, check
    for state in PLUGIN_VERSION_STATES:
        assert f"| `{state}`" in page, state
    for e in V019_ENDPOINTS:
        assert f"| `{e.method}` | `{e.path}` |" in page, e.path
    assert "`UPLOADED_CODE_NOT_SHAREABLE` | 403" in page
    assert "<tenant code>" not in page.split("### Routes", 1)[1], "escape HTML in cells"


def _make(*args: str) -> subprocess.CompletedProcess[str]:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is not installed")
    return _run([make, "--no-print-directory", *args], ROOT)


def test_make_targets() -> None:
    makefile = _text(ROOT / "Makefile")
    targets = set(re.findall(r"^([a-zA-Z0-9][a-zA-Z0-9_.-]*):", makefile, re.M))
    assert {"security-uploads", "plugin-push"} <= targets
    docs_test = makefile.split("\ndocs-test:", 1)[1].split("\n\n", 1)[0]
    assert "conformance/tests/test_docs_upload.py" in docs_test
    for page in PAGES:
        text = _text(page)
        code = "\n".join(re.findall(r"^```[^\n]*\n(.*?)^```", text, re.M | re.S))
        code += "\n" + "\n".join(re.findall(r"`([^`\n]+)`", text))
        for target in re.findall(r"(?:^|[\s;&(])make ([a-z][a-z0-9-]+)", code, re.M):
            assert target in targets, f"{page.name} names `make {target}`"


def test_plugin_push_builds_the_cli_call() -> None:
    proc = _make("plugin-push")
    assert proc.returncode != 0 and "set PLUGIN" in proc.stderr
    pkg = SNIPPETS / "extend_pkg"
    proc = _make(
        "plugin-push", f"PLUGIN={pkg}", "PROJECT=acme/growth", "APPROVAL=1", "JSON=1",
        "PLUGIN_CLI=echo mapi",
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (
        f"mapi --json plugin push {pkg} --project acme/growth --request-approval --wait"
        in proc.stdout
    )
    proc = _make("plugin-push", f"PLUGIN={pkg}", "WAIT=0", "PLUGIN_CLI=echo mapi")
    assert proc.returncode == 0 and "--wait" not in proc.stdout.splitlines()[-1]


def test_changelog_has_the_v019_section() -> None:
    text = _text(ROOT / "CHANGELOG.md")
    unreleased = text.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert "### Upload your own code (v0.19)" in unreleased
    assert unreleased.index("(v0.19)") < unreleased.index("(v0.18)")
    section = unreleased.split("### Upload your own code (v0.19)", 1)[1].split("\n### ", 1)[0]
    assert "#### Known gaps" in section and "not verified" in section
