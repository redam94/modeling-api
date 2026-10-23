"""The v0.20 chat and MCP docs are real: the pages are in the nav, every snippet runs, the
words, settings, error codes and anchors that the pages name exist, the MCP configurations
are the ones the app copies and the bridge reads, and the snippets work against an
in-process API with a fixture dashboard and the fake chat model.

Nothing here calls Gemini or the network: the chat model is ``FakeChatModel`` and the API
runs in-process (``httpx.ASGITransport``) over an embedded PostgreSQL (``pgserver``, or
``DATABASE_URL``).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
SNIPPETS = DOCS / "snippets"
WEB = ROOT / "apps" / "web" / "src"
SERVER_TESTS = ROOT / "packages" / "server" / "tests"

ASK = DOCS / "guides" / "ask-about-your-dashboard.md"
MCP = DOCS / "guides" / "connect-a-chat-tool-with-mcp.md"
WHATS_NEW = DOCS / "whats-new-0.20.md"
PAGES = [ASK, MCP, WHATS_NEW]
LINK_OUTS = ["client-dashboards", "for-clients", "sharing-dashboards-securely", "administration"]

APP_LABELS = {
    ASK: [
        "Ask about this dashboard",
        "Turn on chat for this dashboard",
        "Who can chat",
        "Team members",
        "Client users",
        "Share links",
        "Tokens each day for the dashboard",
        "Tokens each day for each share link",
        "Kept conversations",
        "Upgrade the layout to use chat.",
        "New chat",
        "Check these numbers on the dashboard. Some of them are not in what the assistant read.",
        "Some data was too long to read in full, so the answer may leave things out.",
        "This dashboard has used most of its chat allowance for today.",
        "Its analysts can read them.",
        "You asked many questions in a short time. Wait a minute, then try again.",
        "Chat is off for this dashboard now.",
        "This chat cannot continue. Start a new chat to ask again.",
        "The assistant did not answer. Try again.",
        "This link has ended or was removed.",
        "Read the latest readout",
    ],
    MCP: [
        "Connect a chat tool",
        "Make a read token",
        "Ends after",
        "Copy read token",
        "Add it to your chat tool",
        "Desktop app (stdio)",
        "Web or remote (HTTP)",
        "Copy configuration",
        "Your read tokens",
        "Revoke now",
        "Show everyone's tokens",
    ],
}
"""Text that the pages tell the reader to look for or to select, per page."""

NOT_ERROR_CODES = {"DASHBOARD_TOOLS", "K_SERVICE", "CLOUD_RUN_JOB", "TF_VAR_GEMINI_API_KEY"}


def _text(path: Path) -> str:
    return path.read_text("utf-8")


def _flat(path: Path) -> str:
    return re.sub(r"\s+", " ", _text(path))


def _web() -> str:
    return "\n".join(
        p.read_text("utf-8")
        for p in WEB.rglob("*.ts*")
        if p.is_file() and ".test." not in p.name and "mocks" not in p.parts
    )


def _run(script: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("MAPI_API_URL", "MAPI_DASHBOARD_TOKEN")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT, capture_output=True, text=True, timeout=300, check=False, env=env,
    )  # fmt: skip


# ------------------------------------------------------------------------------- the pages


def test_the_pages_are_in_the_nav() -> None:
    nav = _text(ROOT / "mkdocs.yml")
    for page in PAGES:
        rel = page.relative_to(DOCS).as_posix()
        assert page.is_file(), rel
        assert re.search(rf"[:\-]\s+{re.escape(rel)}\s*$", nav, re.M), f"{rel} is not in the nav"
    home = nav.split("  - Home:", 1)[1].split("\n  - ", 1)[0]
    assert home.index("whats-new-0.20.md") < home.index("whats-new-0.19.md")


@pytest.mark.parametrize("name", LINK_OUTS)
def test_the_dashboard_guides_link_to_the_new_pages(name: str) -> None:
    text = _text(DOCS / "guides" / f"{name}.md")
    assert "(ask-about-your-dashboard.md" in text or "(connect-a-chat-tool-with-mcp.md" in text


def test_the_pages_include_the_tested_snippets() -> None:
    def includes(page: Path) -> set[str]:
        return set(re.findall(r'^\s*--8<--\s+"([^"]+)"\s*$', _text(page), re.M))

    assert {"chat_fake_turn.py:local", "chat_fake_turn.py:ask", "chat_fake_turn.py:clean"} <= (
        includes(ASK)
    )
    assert {"mcp_http_session.py:session", "mcp_stdio_config.json"} <= includes(MCP)


def test_the_learn_more_links_of_the_app_land_on_a_heading() -> None:
    """The chat panel links notices to fixed sections of the guide (``docs-links.ts``)."""
    links = _text(WEB / "features" / "dashboards" / "chat" / "docs-links.ts")
    base = re.search(r'const ASK = "([^"]+)"', links)
    assert base is not None and base.group(1) == "guides/ask-about-your-dashboard/"
    anchors = set(re.findall(r"\$\{ASK\}#([a-z0-9-]+)", links))
    assert anchors >= {
        "check-the-numbers",
        "what-the-assistant-can-read",
        "daily-limits",
        "kept-chats",
    }
    page = _text(ASK)
    for anchor in anchors:
        assert re.search(rf"^#+ .*\{{ #{anchor} \}}$", page, re.M), f"no heading #{anchor}"


def test_screenshots_exist() -> None:
    for page in PAGES:
        for target in re.findall(r"!\[[^\]]*\]\(([^)]+\.png)\)", _text(page)):
            path = (page.parent / target).resolve()
            assert path.is_file(), f"{page.name}: {target}"
            assert path.stat().st_size <= 300 * 1024, target
    assert "screenshots/chat-panel.png" in _text(ASK)
    assert "screenshots/connect-dialog.png" in _text(MCP)


@pytest.mark.parametrize("page", list(APP_LABELS), ids=lambda p: p.name)
def test_the_pages_name_what_the_app_shows(page: Path) -> None:
    text, web = _flat(page), _web()
    for label in APP_LABELS[page]:
        assert label in text, f"{page.name} does not name {label!r}"
        assert label in web, f"{page.name} names {label!r}, which the web app does not show"


def test_error_codes_named_by_the_pages_exist() -> None:
    from mapi_spec.errors import ErrorCode

    names = {c.value for c in ErrorCode}
    for page in [*PAGES, ROOT / "CHANGELOG.md"]:
        text = _text(page)
        if page.name == "CHANGELOG.md":
            text = text.split("### Ask about your dashboard (v0.20)", 1)[1].split("\n### ", 1)[0]
        for code in re.findall(r"`(?:\d{3} )?([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)`", text):
            if code.startswith("MAPI_") or code in NOT_ERROR_CODES:
                continue
            assert code in names, f"{page.name} names the unknown error code {code}"


def test_the_audit_actions_named_by_the_pages_exist() -> None:
    from mapi_spec.audit import AuditAction

    actions = {a.value for a in AuditAction}
    for page in [ASK, MCP]:
        for action in re.findall(r"`(dashboard\.[a-z_]+)`", _text(page)):
            assert action in actions, f"{page.name}: {action}"


def test_the_guides_cover_privacy_cost_and_risk() -> None:
    ask, mcp = _flat(ASK), _flat(MCP)
    for item in (
        "store: false",  # what goes to Google
        "paid-tier key",
        "MAPI_CHAT_ALLOWED_RESIDENCIES",
        "30 days",  # kept chats
        "200,000",  # daily limits
        "50,000",
        "`get_prediction` and `get_batch_summary`",  # the shared principal of a link
        "UPLOADED_CODE_NOT_SHAREABLE",
        "never fully safe",  # prompt-injection limits
    ):
        assert item in ask, item
    for item in (
        "Connect only tools that you trust",  # agents with other tools
        "never from its command line",
        "chmod 600",
        "90 days",
        "5 active tokens",
        "30 minutes",  # share sessions
        "`get_prediction` and `get_batch_summary`",
        "UPLOADED_CODE_NOT_SHAREABLE",
        "?token=",
    ):
        assert item in mcp, item


# ------------------------------------------------------------------------------ settings


def _defaults() -> dict[str, Any]:
    from mapi_server.settings import Settings

    return {
        f"MAPI_{name.upper()}": field.get_default(call_default_factory=True)
        for name, field in Settings.model_fields.items()
    }


@pytest.mark.parametrize("page", [ASK, MCP], ids=lambda p: p.name)
def test_the_settings_tables_show_the_defaults(page: Path) -> None:
    defaults = _defaults()
    operators = _text(page).split("## For operators", 1)[1]
    rows = [ln for ln in operators.splitlines() if re.match(r"^\| `MAPI_[A-Z_]+` \|", ln)]
    assert rows
    for row in rows:
        env = row.split("`")[1]
        assert env in defaults, f"{env} is not a setting"
        shown = row.split("|")[2].strip()
        default = defaults[env]
        if default is None:
            assert shown == "(none)", env
        elif isinstance(default, list):
            assert shown == ("(empty)" if not default else f"`{','.join(default)}`"), env
        else:
            want = str(default).lower() if isinstance(default, bool) else str(default)
            assert shown == f"`{want}`", f"{env}: the page shows {shown}, the default is {want}"
    for env in re.findall(r"`(MAPI_(?:CHAT|MCP|GEMINI|DASHBOARD_READ)[A-Z_]*)`", operators):
        if not env.endswith("_"):  # a family such as the price settings
            assert env in defaults, env


def test_the_fake_model_refusals_are_the_ones_the_page_lists() -> None:
    from mapi_server.settings import Settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="the profile is multi-tenant"):
        Settings(chat_provider="fake", profile="multi-tenant", trusted_proxies="none")  # type: ignore[arg-type]
    page = _flat(ASK)
    for item in ("multi-tenant profile", "`cloudrun-jobs`", "`gcs`", "Cloud Run"):
        assert item in page, item


def test_the_bridge_reads_the_variables_the_page_names() -> None:
    credentials = _text(ROOT / "packages" / "mcp" / "src" / "mapi_mcp" / "credentials.py")
    main = _text(ROOT / "packages" / "mcp" / "src" / "mapi_mcp" / "__main__.py")
    page = _text(MCP)
    named = set(re.findall(r"`(MAPI_(?:API_URL|DASHBOARD_TOKEN|SHARE_\w+|MCP_\w+))`", page))
    # server settings, not bridge variables
    named -= {"MAPI_SHARE_SESSION_TTL_S", "MAPI_MCP_ENABLED", "MAPI_MCP_ALLOWED_ORIGINS"}
    named -= {"MAPI_MCP_RATE_PER_MINUTE"}
    assert {"MAPI_API_URL", "MAPI_DASHBOARD_TOKEN", "MAPI_SHARE_LINK", "MAPI_MCP_CONFIG"} <= named
    for env in named:
        assert env in credentials + main, f"the bridge does not read {env}"
    for key in ("api_url", "dashboard_token", "share_link", "share_passcode"):
        assert f"`{key}`" in page and f'"{key}"' in credentials, key


# --------------------------------------------------------------------- MCP configurations


def _web_snippets() -> str:
    return _text(WEB / "features" / "dashboards" / "connect" / "mcp-snippets.ts")


def test_the_stdio_configuration_is_the_one_the_app_copies() -> None:
    doc = json.loads(_text(SNIPPETS / "mcp_stdio_config.json"))
    (server,) = doc["mcpServers"].values()
    web = _web_snippets()
    command = re.search(r'MCP_BRIDGE_COMMAND = "([^"]+)"', web)
    placeholder = re.search(r'TOKEN_PLACEHOLDER = "([^"]+)"', web)
    assert command and placeholder
    assert server["command"] == command.group(1)
    assert "args" not in server, "the bridge takes no arguments"
    assert set(server["env"]) == {"MAPI_API_URL", "MAPI_DASHBOARD_TOKEN"}
    assert server["env"]["MAPI_DASHBOARD_TOKEN"] == placeholder.group(1)
    assert server["env"]["MAPI_API_URL"].startswith("https://")
    pyproject = _text(ROOT / "packages" / "mcp" / "pyproject.toml")
    assert f'{server["command"]} = "mapi_mcp.__main__:main"' in pyproject


def test_the_http_configuration_is_the_one_the_app_copies() -> None:
    block = _text(MCP).split("### Web or remote (HTTP)", 1)[1].split("```json", 1)[1]
    doc = json.loads(block.split("```", 1)[0])
    (server,) = doc["mcpServers"].values()
    assert server["type"] == "http" and server["url"].endswith("/v1/mcp")
    assert "token" not in server["url"]
    placeholder = re.search(r'TOKEN_PLACEHOLDER = "([^"]+)"', _web_snippets())
    assert placeholder is not None
    assert server["headers"] == {"Authorization": f"Bearer {placeholder.group(1)}"}


def test_the_page_lists_the_protocol_revisions_and_methods() -> None:
    from mapi_spec.mcp import MCP_METHODS, MCP_PROTOCOL_VERSIONS, MCP_SERVER_NAME

    page = _text(MCP)
    for version in MCP_PROTOCOL_VERSIONS:
        assert f"`{version}`" in page, version
    for method in MCP_METHODS - {"notifications/initialized"}:
        assert f"| `{method}` |" in page, method
    assert f"`{MCP_SERVER_NAME}`" in page


# ----------------------------------------------------------------------------- the snippets


def test_the_chat_snippet_runs_without_a_server() -> None:
    proc = _run(SNIPPETS / "chat_fake_turn.py")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.splitlines() == [
        "fake-chat",
        "14",
        "refused: True",
        "refused: True",
        "Sales were **1,234,567**. chart Read more TV",
        "[]",
        "['1,300,000']",
        "['get_latest_readout']",
    ]


def test_the_mcp_snippet_runs_without_a_server() -> None:
    proc = _run(SNIPPETS / "mcp_http_session.py")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.splitlines() == [
        "mapi-dashboards 2025-11-25",
        "14 get_dashboard",
        "2 Weekly readout",
    ]


def test_the_snippets_never_put_a_token_in_a_url() -> None:
    source = _text(SNIPPETS / "mcp_http_session.py")
    assert "?token=" not in source and "?key=" not in source
    assert 'f"Bearer {read_token}"' in source


# ------------------------------------------------- the snippets against an in-process API


def _load(name: str) -> Any:
    import runpy

    return runpy.run_path(str(SNIPPETS / name), run_name="docs_snippet")


@pytest.fixture(scope="module")
def pg_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A fresh, migrated database: ``DATABASE_URL`` (a server) or an embedded PostgreSQL."""
    import psycopg
    from mapi_server.db.migrate import upgrade

    srv = None
    admin = os.environ.get("DATABASE_URL")
    if not admin:
        pgserver = pytest.importorskip("pgserver")
        srv = pgserver.get_server(str(tmp_path_factory.mktemp("pg")), cleanup_mode="stop")
        admin = srv.get_uri()
    name = f"mapi_docs_{secrets.token_hex(4)}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = re.sub(r"/[^/?]*(\?|$)", rf"/{name}\1", admin.split("://", 1)[1], count=1)
    url = f"{admin.split('://', 1)[0]}://{url}"
    upgrade(url)
    try:
        yield url
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        if srv is not None:
            srv.cleanup()


@pytest.fixture
async def api(pg_url: str, tmp_path: Path) -> AsyncIterator[tuple[Any, httpx.AsyncClient]]:
    """The API in-process with the fake chat model, and an HTTP client of it."""
    if str(SERVER_TESTS) not in sys.path:
        sys.path.insert(0, str(SERVER_TESTS))
    from mapi_server.app import build_context, shutdown, startup
    from mapi_server.services.signing import BundleSigner
    from mapi_server.settings import ObjectStoreKind, Settings
    from mapi_server.storage import MemoryObjectStore
    from mapi_server_testkit.builders import CATALOG, EXEC_TOKEN
    from test_mcp_endpoint import mcp_app

    settings = Settings(
        database_url=pg_url, object_store=ObjectStoreKind.MEMORY, data_dir=tmp_path,
        image_digest="sha256:" + "ab" * 32, catalog_path=CATALOG,
        gcs_endpoint="http://store.invalid", exec_token=EXEC_TOKEN,
        chat_provider="fake", dashboard_layout1_create=True,
    )  # fmt: skip
    store = MemoryObjectStore(
        base_url=settings.gcs_endpoint, buckets=settings.buckets.all().values()
    )
    ctx = build_context(settings, store=store, signer=BundleSigner.generate())
    await startup(ctx)
    try:
        app = mcp_app(ctx)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as c:
            yield ctx, c
    finally:
        await shutdown(ctx)


@pytest.mark.integration
async def test_the_snippets_work_against_the_api(api: tuple[Any, httpx.AsyncClient]) -> None:
    from mapi_server_testkit.identity import Caller
    from mapi_spec.dashboards import SHARE_SESSION_HEADER
    from test_chat_loop import chat_kit
    from test_mcp_endpoint import read_token

    ctx, http = api
    kit = await chat_kit(ctx, http)  # a published layout 2 dashboard, chat on for everyone
    dash = kit["dash_id"]
    chat = _load("chat_fake_turn.py")

    # a team member asks, then asks again with the signed history (the fake model reads
    # the latest readout of the fixture dashboard and quotes its numbers)
    first = "What does the latest readout show?"
    res = await chat["ask"](kit["eve"], f"/v1/dashboards/{dash}/chat", first)
    assert "1,234,567" in res.reply.text and res.reply.sig
    assert [c.name for c in res.tool_calls] == ["get_latest_readout"] and res.notices == []
    again = await chat["ask"](kit["eve"], f"/v1/dashboards/{dash}/chat",
                              "What changed since the last readout?", chat["turns"](first, res),
                              res.conversation_id)  # fmt: skip
    assert again.reply.sig and again.conversation_id

    # a share link reader asks through the share session
    reader = Caller(http, {SHARE_SESSION_HEADER: kit["share_session"]})
    shared = await chat["ask"](reader, "/v1/shared/chat", first)
    assert "1,234,567" in shared.reply.text

    # a client user makes a read token and runs the MCP session of the guide
    secret = await read_token(kit["cleo"], dash, name="Laptop assistant")
    mcp = _load("mcp_http_session.py")
    agent = httpx.AsyncClient(
        transport=http._transport, base_url=str(http.base_url),
        headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"},
    )  # fmt: skip
    async with agent:
        readout = await mcp["session"](agent)
    assert readout["seq"] == 2  # the published readout; the private one (3) is not visible


# ---------------------------------------------------------------------- make and changelog


def test_make_docs_test_runs_this_file() -> None:
    makefile = _text(ROOT / "Makefile")
    docs_test = makefile.split("\ndocs-test:", 1)[1].split("\n\n", 1)[0]
    assert "conformance/tests/test_docs_chat_mcp.py" in docs_test


def test_changelog_has_the_v020_section() -> None:
    text = _text(ROOT / "CHANGELOG.md")
    unreleased = text.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert "### Ask about your dashboard (v0.20)" in unreleased
    assert unreleased.index("(v0.20)") < unreleased.index("(v0.19)")
    section = unreleased.split("### Ask about your dashboard (v0.20)", 1)[1].split("\n### ", 1)[0]
    for item in ("#### Known gaps", "No real Gemini call", "0024_dashboard_chat", "292 KiB"):
        assert item in section, item
