"""``mapi plugin init --upload|push|status|yank`` (v0.19 WP11) against a respx-mocked API.

Test wheels are written in ``tmp_path`` with :mod:`zipfile` and never installed or imported;
the one real build (``uv build``) also stays in ``tmp_path`` and is only read back by the
platform's static inspection, which never imports it.
"""

from __future__ import annotations

import copy
import io
import json
import shutil
import sys
import tomllib
import zipfile
from collections.abc import Callable, Iterator
from itertools import chain
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from mapi_cli.commands import plugin
from mapi_cli.commands.plugin import Kind, check_wheel
from mapi_spec.api import Me
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.plugins import (
    Plugin,
    PluginUploadTicket,
    PluginUsage,
    PluginVersion,
    PluginVersionSummary,
)
from typer.testing import Result

Invoke = Callable[..., Result]
AsJson = Callable[[Result], Any]
Ex = Callable[..., dict[str, Any]]

URL = "http://127.0.0.1:18000"
V1 = f"{URL}/v1"
STORAGE = "http://storage.test/mapi-local-quarantine"
PROJECT = "acme/growth"
TOKEN = "mapi_pat_s3cr3t0k3nvalue0123456789abcdef"
NS = "t_k3vq7m_acme"
DIST = "t-k3vq7m-acme"
PLG = "plg_01jbz3q2x8m9v7k5h4g3f2e1d0"
PLV = "plv_01jbz3q2x8m9v7k5h4g3f2e1d1"
PUP = "pup_01jbz3q2x8m9v7k5h4g3f2e1d2"
ALL_KINDS = [k.value for k in Kind]
WHEEL_NAME = f"{NS}-0.1.0-py3-none-any.whl"


# ================================================================================ helpers


@pytest.fixture(autouse=True)
def _me(api: respx.MockRouter, example: Ex) -> respx.Route:
    """``GET /v1/me`` (the caller; uploaded code not paused) for every test."""
    return api.get(f"{V1}/me").respond(200, json=example(Me))


def _page(items: list[Any], cursor: str | None = None) -> dict[str, Any]:
    return {"items": items, "next_cursor": cursor}


def _plugin(example: Ex) -> dict[str, Any]:
    return example(Plugin)


def _version(example: Ex, state: str, **changes: Any) -> dict[str, Any]:
    doc = example(PluginVersion, state=state, **changes)
    if state in ("inspecting", "describing", "inspect_failed"):
        doc.update(kit=None, describe_digest=None)
    return doc


def _ticket(example: Ex) -> dict[str, Any]:
    doc = example(PluginUploadTicket)
    doc["post"]["url"] = STORAGE
    return doc


def _problem(code: ErrorCode, detail: str) -> dict[str, Any]:
    return MapiError(detail, code=code).to_problem().model_dump(mode="json")


def make_wheel(folder: Path, name: str = WHEEL_NAME, *, top: str = NS, pad: int = 0) -> Path:
    """A small deterministic wheel (stored members, fixed dates). Never installed."""
    folder.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for member, text in (
            (f"{top}/__init__.py", '"""Uploaded test package."""\n' + "#" * pad),
            (f"{top}-0.1.0.dist-info/METADATA", f"Metadata-Version: 2.1\nName: {DIST}\n"),
            (f"{top}-0.1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\n"),
        ):
            info = zipfile.ZipInfo(member, date_time=(2026, 1, 1, 0, 0, 0))
            zf.writestr(info, text)
    path = folder / name
    path.write_bytes(buf.getvalue())
    return path


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin, "_sleep", lambda _s: None)


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("MAPI_TOKEN", TOKEN)
    return TOKEN


@pytest.fixture
def fake_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """``build_command`` copies a prepared wheel into the out dir (a subprocess, like uv)."""
    src = make_wheel(tmp_path / "prebuilt")

    def command(path: Path, outdir: Path) -> list[str]:
        return [
            sys.executable,
            "-c",
            "import shutil, sys; shutil.copy(sys.argv[1], sys.argv[2])",
            str(src),
            str(outdir),
        ]

    monkeypatch.setattr(plugin, "build_command", command)
    yield src


def _upload_routes(
    api: respx.MockRouter, example: Ex, *states: str, complete_state: str = "inspecting"
) -> dict[str, respx.Route]:
    """The happy path: list plugins, ticket, storage POST, complete, then ``states``."""
    routes = {
        "list": api.get(f"{V1}/plugins").respond(200, json=_page([_plugin(example)])),
        "get": api.get(f"{V1}/plugins/{PLG}").respond(200, json=_plugin(example)),
        "ticket": api.post(f"{V1}/plugins/{PLG}/uploads").respond(201, json=_ticket(example)),
        "storage": api.post(STORAGE).respond(204),
        "complete": api.post(f"{V1}/plugins/{PLG}/uploads/{PUP}:complete").respond(
            202, json=_version(example, complete_state)
        ),
    }
    poll = api.get(f"{V1}/plugins/{PLG}/versions/{PLV}")
    if states:
        poll.side_effect = [httpx.Response(200, json=_version(example, s)) for s in states]
    routes["poll"] = poll
    return routes


def _init_upload(mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path) -> Path:
    api.post(f"{V1}/plugins").respond(201, json=_plugin(example))
    kinds = list(chain.from_iterable(("--kind", k) for k in ALL_KINDS))
    res = mapi(
        "--url", URL, "--project", PROJECT, "plugin", "init", "acme", "--upload",
        *kinds, "--dir", str(tmp_path / "pkgs"),
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    return tmp_path / "pkgs" / "acme"


# =================================================================================== init


def test_init_upload_claims_and_uses_the_given_namespace(
    mapi: Invoke, api: respx.MockRouter, example: Ex, as_json: AsJson, tmp_path: Path
) -> None:
    claim = api.post(f"{V1}/plugins").respond(201, json=_plugin(example))
    kinds = list(chain.from_iterable(("--kind", k) for k in ALL_KINDS))
    res = mapi(
        "--url", URL, "--project", PROJECT, "--json", "plugin", "init", "acme", "--upload",
        "--title", "Acme store tools", *kinds, "--dir", str(tmp_path),
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    doc = as_json(res)
    assert doc["dist"] == DIST and doc["module"] == NS and doc["namespace"] == NS
    assert doc["ids"] == {
        "model": f"{NS}.group_mean",
        "step": f"{NS}.winsorize",
        "recipe": f"{NS}.weekly_total",
        "node": f"test.{NS}_zero_share",
        "template": f"{NS}_weekly_check",
    }
    assert doc["upload"] == {
        "plugin_id": PLG,
        "short_name": "acme",
        "namespace": NS,
        "claimed": True,
    }
    sent = claim.calls.last.request
    assert json.loads(sent.content) == {"short_name": "acme", "title": "Acme store tools"}
    assert sent.headers["x-mapi-project"] == PROJECT
    root = tmp_path / "acme"
    # exactly one top-level package, named the namespace
    assert [p.name for p in (root / "src").iterdir()] == [NS]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    assert pyproject["project"]["name"] == DIST
    assert pyproject["tool"]["mapi"]["upload"] == {
        "plugin_id": PLG,
        "short_name": "acme",
        "namespace": NS,
    }
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [f"src/{NS}"]
    for group in pyproject["project"]["entry-points"].values():
        assert list(group) == [NS]
    readme = (root / "README.md").read_text()
    assert "## Upload it" in readme and "mapi plugin push --wait" in readme
    assert readme.rstrip().split("\n\n")[-1].startswith("If something is missing")
    for f in root.rglob("*"):
        if f.is_file():
            assert "{{" not in f.read_text(encoding="utf-8"), f


def test_the_upload_scaffold_passes_its_contract_tests(
    mapi: Invoke, api: respx.MockRouter, example: Ex, as_json: AsJson, tmp_path: Path
) -> None:
    root = _init_upload(mapi, api, example, tmp_path)
    res = mapi("--json", "plugin", "test", str(root))
    report = as_json(res)
    assert res.exit_code == 0, report["output"]
    assert "12 passed" in report["output"]


def test_init_upload_reads_your_earlier_claim(
    mapi: Invoke, api: respx.MockRouter, example: Ex, as_json: AsJson, tmp_path: Path
) -> None:
    api.post(f"{V1}/plugins").respond(
        409, json=_problem(ErrorCode.PLUGIN_NAMESPACE_TAKEN, "The short name is taken.")
    )
    api.get(f"{V1}/plugins").respond(200, json=_page([_plugin(example)]))
    api.get(f"{V1}/me").respond(200, json=example(Me))  # the plugin's created_by
    res = mapi(
        "--url", URL, "--project", PROJECT, "--json", "plugin", "init", "acme", "--upload",
        "--dir", str(tmp_path),
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert as_json(res)["upload"]["claimed"] is False


def test_init_upload_refuses_a_colleagues_plugin_you_can_see(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    """web+cli+docs-2: a tenant admin (or a teammate of an approved version) sees Bea's
    plugin; ``init --upload`` must not adopt it as theirs."""
    api.post(f"{V1}/plugins").respond(
        409, json=_problem(ErrorCode.PLUGIN_NAMESPACE_TAKEN, "The short name is taken.")
    )
    theirs = _plugin(example) | {"created_by": "usr_01jbz3q2x8m9v7k5h4g3f2e1d9"}
    api.get(f"{V1}/plugins").respond(200, json=_page([theirs]))
    api.get(f"{V1}/me").respond(200, json=example(Me))
    res = mapi(
        "--url", URL, "--project", PROJECT, "plugin", "init", "acme", "--upload",
        "--dir", str(tmp_path),
    )  # fmt: skip
    assert res.exit_code == 1 and "PLUGIN_NAMESPACE_TAKEN" in res.stderr
    assert "Somebody else" in res.stderr
    assert not (tmp_path / "acme").exists()


def test_init_upload_refuses_a_name_taken_by_someone_else(
    mapi: Invoke, api: respx.MockRouter, tmp_path: Path
) -> None:
    api.post(f"{V1}/plugins").respond(
        409, json=_problem(ErrorCode.PLUGIN_NAMESPACE_TAKEN, "The short name is taken.")
    )
    api.get(f"{V1}/plugins").respond(200, json=_page([]))
    res = mapi(
        "--url", URL, "--project", PROJECT, "plugin", "init", "acme", "--upload",
        "--dir", str(tmp_path),
    )  # fmt: skip
    assert res.exit_code == 1 and "PLUGIN_NAMESPACE_TAKEN" in res.stderr
    assert not (tmp_path / "acme").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["acme-lab", "--upload"], "not valid"),
        (["a", "--upload"], "not valid"),
        (["acme", "--upload", "--namespace", "acme"], "--namespace cannot be used"),
        (["acme", "--title", "Acme"], "--title applies to --upload only"),
    ],
)
def test_init_upload_refuses_bad_input_before_any_request(
    mapi: Invoke, api: respx.MockRouter, tmp_path: Path, args: list[str], message: str
) -> None:
    route = api.post(f"{V1}/plugins").respond(500)
    res = mapi("--url", URL, "plugin", "init", *args, "--dir", str(tmp_path))
    assert res.exit_code == 1 and message in res.stderr
    assert not route.called


def test_init_upload_exits_2_when_the_server_is_down(
    mapi: Invoke, api: respx.MockRouter, tmp_path: Path
) -> None:
    api.post(f"{V1}/plugins").mock(side_effect=httpx.ConnectError("refused"))
    res = mapi(
        "--url", URL, "--project", PROJECT, "plugin", "init", "acme", "--upload",
        "--dir", str(tmp_path),
    )  # fmt: skip
    assert res.exit_code == 2, res.output


# ============================================================================ local checks


def test_check_wheel_takes_a_pure_python_wheel(tmp_path: Path) -> None:
    wheel = check_wheel(make_wheel(tmp_path))
    assert wheel.dist == NS and wheel.version == "0.1.0"
    assert wheel.sha256.startswith("sha256:") and wheel.size > 0


@pytest.mark.parametrize(
    ("name", "message"),
    [
        (f"{NS}-0.1.0-cp312-cp312-macosx_14_0_arm64.whl", "tagged 'cp312-cp312"),
        (f"{NS}-0.1.0-py2.py3-none-any.whl", "tagged 'py2.py3-none-any'"),
        (f"{NS}-0.1.0.tar.gz", "not a wheel"),
    ],
)
def test_push_refuses_a_wheel_that_is_not_py3_none_any(
    mapi: Invoke, api: respx.MockRouter, tmp_path: Path, name: str, message: str
) -> None:
    route = api.route(host="127.0.0.1").respond(500)
    path = make_wheel(tmp_path, name)
    res = mapi("--url", URL, "--project", PROJECT, "plugin", "push", str(path))
    assert res.exit_code == 1 and message in res.stderr, res.output
    assert not route.called  # refused locally, before any request


def test_push_refuses_more_than_20_mib_locally(
    mapi: Invoke, api: respx.MockRouter, tmp_path: Path, as_json: AsJson
) -> None:
    route = api.route(host="127.0.0.1").respond(500)
    path = make_wheel(tmp_path, pad=plugin.UPLOAD_MAX_BYTES)
    assert path.stat().st_size > 20 * 1024 * 1024
    res = mapi("--url", URL, "--project", PROJECT, "--json", "plugin", "push", str(path))
    assert res.exit_code == 1
    error = as_json(res)["error"]
    assert error["code"] == "PLUGIN_REFUSED" and "20 MiB" in error["detail"]
    assert not route.called


def test_push_says_how_to_build_without_uv_or_build(
    mapi: Invoke, monkeypatch: pytest.MonkeyPatch, api: respx.MockRouter, example: Ex,
    tmp_path: Path,
) -> None:  # fmt: skip
    root = _init_upload(mapi, api, example, tmp_path)
    monkeypatch.setattr(plugin.shutil, "which", lambda _name: None)
    monkeypatch.setattr(plugin, "_has_build_module", lambda: False)
    res = mapi("--url", URL, "plugin", "push", str(root), "--no-validate")
    assert res.exit_code == 1
    assert "neither was found" in res.stderr and "uv" in res.stderr
    assert "mapi plugin push dist/" in res.stderr


def test_build_command_prefers_uv_then_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(plugin.shutil, "which", lambda name: f"/bin/{name}")
    cmd = plugin.build_command(tmp_path, tmp_path / "out")
    assert cmd is not None and cmd[:3] == ["/bin/uv", "build", "--wheel"]
    monkeypatch.setattr(plugin.shutil, "which", lambda _name: None)
    monkeypatch.setattr(plugin, "_has_build_module", lambda: True)
    cmd = plugin.build_command(tmp_path, tmp_path / "out")
    assert cmd is not None and cmd[:4] == [sys.executable, "-m", "build", "--wheel"]


def test_push_refuses_a_wheel_of_another_plugin(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    routes = _upload_routes(api, example)
    other = make_wheel(tmp_path, "t_k3vq7m_other-0.1.0-py3-none-any.whl", top="t_k3vq7m_other")
    res = mapi("--url", URL, "--project", PROJECT, "plugin", "push", str(other), "--plugin", "acme")
    assert res.exit_code == 1 and f"takes '{DIST}' only" in res.stderr
    assert not routes["ticket"].called


# =================================================================================== push


GOLDEN_PUSH: dict[str, Any] = {
    "ok": True,
    "exit_code": 0,
    "plugin": {"id": PLG, "short_name": "acme", "namespace": NS},
    "wheel": {
        "filename": WHEEL_NAME,
        "dist": NS,
        "version": "0.1.0",
        "size": "<size>",
        "sha256": "<sha256>",
        "built": False,
    },
    "upload_id": PUP,
    "request_approval": True,
    "waited": True,
    "local_checks": {"ran": False, "exit_code": None, "warnings": []},
    "version": "<PluginVersion example, state awaiting_approval>",
}


def test_push_wait_json_matches_the_golden(
    mapi: Invoke, api: respx.MockRouter, example: Ex, as_json: AsJson, tmp_path: Path,
    no_sleep: None, token: str,
) -> None:  # fmt: skip
    routes = _upload_routes(api, example, "describing", "awaiting_approval")
    wheel = make_wheel(tmp_path)
    res = mapi(
        "--url", URL, "--json", "plugin", "push", str(wheel), "--project", PROJECT,
        "--request-approval", "--wait",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    doc = as_json(res)
    golden = copy.deepcopy(GOLDEN_PUSH)
    local = check_wheel(wheel)
    golden["wheel"].update(size=local.size, sha256=local.sha256)
    golden["version"] = PluginVersion.model_validate(
        _version(example, "awaiting_approval")
    ).model_dump(mode="json")
    assert doc == golden
    # the ticket names the wheel; :complete pays from the project and asks for approval
    assert json.loads(routes["ticket"].calls.last.request.content) == {
        "filename": WHEEL_NAME,
        "size": local.size,
    }
    assert json.loads(routes["complete"].calls.last.request.content) == {
        "project": "growth",
        "request_approval": True,
    }
    assert routes["poll"].call_count == 2
    for name in ("list", "ticket", "complete", "poll"):
        sent = routes[name].calls.last.request
        assert sent.headers["x-mapi-project"] == PROJECT
        assert sent.headers["authorization"] == f"Bearer {token}"


def test_push_posts_the_policy_fields_then_the_file(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path, token: str
) -> None:
    routes = _upload_routes(api, example)
    wheel = make_wheel(tmp_path)
    res = mapi("--url", URL, "--project", PROJECT, "plugin", "push", str(wheel))
    assert res.exit_code == 0, res.output
    assert "Checking the files" in res.stdout
    sent = routes["storage"].calls.last.request
    assert "authorization" not in sent.headers  # never the API credentials at storage
    assert sent.headers["content-type"].startswith("multipart/form-data")
    body = sent.content
    fields = _ticket(example)["post"]["fields"]
    positions = [body.index(f'name="{k}"'.encode()) for k in fields]
    file_at = body.index(b'name="file"; filename="' + WHEEL_NAME.encode() + b'"')
    assert positions == sorted(positions) and max(positions) < file_at
    assert wheel.read_bytes() in body
    assert not routes["poll"].called  # no --wait


def test_push_builds_the_package_and_resolves_the_plugin_from_pyproject(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path, as_json: AsJson,
    fake_build: Path, no_sleep: None,
) -> None:  # fmt: skip
    root = _init_upload(mapi, api, example, tmp_path)
    routes = _upload_routes(api, example, "describing", "kit_passed")
    res = mapi(
        "--url", URL, "--project", PROJECT, "--json", "plugin", "push", str(root), "--wait",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    doc = as_json(res)
    assert doc["wheel"]["built"] is True and doc["version"]["state"] == "kit_passed"
    # the scaffold's local checks pass: the t_ namespace rule is the platform's job
    assert doc["local_checks"]["ran"] is True and doc["local_checks"]["warnings"] == []
    assert routes["get"].called and not routes["list"].called  # plugin_id from pyproject
    assert fake_build.read_bytes() in routes["storage"].calls.last.request.content


def test_push_wait_exits_1_when_the_tests_fail(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path, no_sleep: None
) -> None:
    failed = _version(example, "kit_failed")
    failed["kit"] = {
        "ok": False,
        "checks": [
            {
                "entry_id": f"{NS}.winsorize",
                "kind": "step",
                "check": "check_step",
                "status": "fail",
                "message": "transform learned from its input",
            }
        ],
    }
    routes = _upload_routes(api, example)
    routes["poll"].side_effect = [
        httpx.Response(200, json=_version(example, "describing")),
        httpx.Response(200, json=failed),
    ]
    res = mapi(
        "--url", URL, "--project", PROJECT, "plugin", "push", str(make_wheel(tmp_path)), "--wait"
    )
    assert res.exit_code == 1, res.output
    assert "Tests failed" in res.stdout and "transform learned from its input" in res.stdout
    assert "Running the tests" in res.stderr  # progress goes to stderr


def test_push_wait_exits_1_when_the_files_are_refused(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path, no_sleep: None,
    as_json: AsJson,
) -> None:  # fmt: skip
    refused = _version(example, "inspect_failed")
    refused["inspection"]["ok"] = False
    refused["inspection"]["problems"] = [
        {"check": "pth_file", "path": "evil.pth", "message": "The wheel has a .pth file."}
    ]
    routes = _upload_routes(api, example)
    routes["poll"].side_effect = [httpx.Response(200, json=refused)]
    res = mapi(
        "--url", URL, "--project", PROJECT, "--json", "plugin", "push",
        str(make_wheel(tmp_path)), "--wait",
    )  # fmt: skip
    assert res.exit_code == 1
    doc = as_json(res)
    assert doc["ok"] is False and doc["exit_code"] == 1
    assert doc["version"]["inspection"]["problems"][0]["check"] == "pth_file"


def test_a_platform_refusal_exits_1(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path, as_json: AsJson
) -> None:
    routes = _upload_routes(api, example)
    routes["complete"].respond(
        409, json=_problem(ErrorCode.PLUGIN_VERSION_EXISTS, "The version 0.1.0 exists.")
    )
    res = mapi(
        "--url", URL, "--project", PROJECT, "--json", "plugin", "push", str(make_wheel(tmp_path))
    )
    assert res.exit_code == 1
    assert as_json(res)["error"]["code"] == "PLUGIN_VERSION_EXISTS"


@pytest.mark.parametrize("where", ["ticket", "storage", "complete", "poll"])
def test_transport_failures_exit_2(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path, no_sleep: None,
    where: str,
) -> None:  # fmt: skip
    routes = _upload_routes(api, example)
    routes[where].mock(side_effect=httpx.ConnectError("connection refused"))
    res = mapi(
        "--url", URL, "--project", PROJECT, "plugin", "push", str(make_wheel(tmp_path)), "--wait"
    )
    assert res.exit_code == 2, res.output


def test_a_storage_refusal_exits_2(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    routes = _upload_routes(api, example)
    routes["storage"].respond(403, text="<Error>policy expired</Error>")
    res = mapi("--url", URL, "--project", PROJECT, "plugin", "push", str(make_wheel(tmp_path)))
    assert res.exit_code == 2 and "HTTP 403" in res.stderr
    assert not routes["complete"].called


def test_wait_gives_up_after_the_timeout(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    ticks = iter(float(t) for t in range(0, 10_000, 100))
    monkeypatch.setattr(plugin, "_clock", lambda: next(ticks))
    monkeypatch.setattr(plugin, "_sleep", lambda _s: None)
    routes = _upload_routes(api, example)
    routes["poll"].respond(200, json=_version(example, "describing"))
    res = mapi(
        "--url", URL, "--project", PROJECT, "plugin", "push", str(make_wheel(tmp_path)),
        "--wait", "--timeout", "250",
    )  # fmt: skip
    assert res.exit_code == 2
    assert "still 'Running the tests'" in res.stderr
    assert "mapi plugin status acme@0.1.0" in res.stderr


def test_wait_and_status_say_when_the_platform_paused_uploaded_code(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path, _me: respx.Route,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """web+cli+docs-3: the operator's pause (``GET /v1/me`` ``plugins_paused``) is said, so a
    check waiting in the queue does not look stuck."""
    ticks = iter(float(t) for t in range(0, 10_000, 100))
    monkeypatch.setattr(plugin, "_clock", lambda: next(ticks))
    monkeypatch.setattr(plugin, "_sleep", lambda _s: None)
    _me.respond(200, json=example(Me) | {"plugins_paused": True})
    routes = _upload_routes(api, example)
    routes["poll"].respond(200, json=_version(example, "inspecting"))
    res = mapi(
        "--url", URL, "--project", PROJECT, "plugin", "push", str(make_wheel(tmp_path)),
        "--wait", "--timeout", "250",
    )  # fmt: skip
    assert "Paused by the platform" in res.stderr
    api.get(f"{V1}/plugins").respond(200, json=_page([_plugin(example)]))
    api.get(f"{V1}/plugins/{PLG}/versions").respond(
        200, json=_page([example(PluginVersionSummary)])
    )
    api.get(f"{V1}/plugins/{PLG}/versions/{PLV}").respond(200, json=_version(example, "describing"))
    res = mapi("--url", URL, "--project", PROJECT, "plugin", "status", "acme@0.1.0")
    assert res.exit_code == 0 and "Paused by the platform" in res.stderr
    _me.respond(200, json=example(Me))
    res = mapi("--url", URL, "--project", PROJECT, "plugin", "status", "acme@0.1.0")
    assert res.exit_code == 0 and "Paused" not in res.stderr


def test_the_token_never_appears_in_the_output(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path, no_sleep: None,
    token: str,
) -> None:  # fmt: skip
    for json_flag in ([], ["--json"]):
        for poll_state in ("kit_passed", "kit_failed"):
            api.reset()
            routes = _upload_routes(api, example, poll_state)
            res = mapi(
                "--url", URL, "--project", PROJECT, *json_flag, "plugin", "push",
                str(make_wheel(tmp_path)), "--wait",
            )  # fmt: skip
            assert routes["complete"].called
            assert token not in res.stdout and token not in res.stderr
    # an error path too (the server refuses the ticket)
    api.reset()
    routes = _upload_routes(api, example)
    routes["ticket"].respond(403, json=_problem(ErrorCode.FORBIDDEN, "Not allowed."))
    res = mapi("--url", URL, "--project", PROJECT, "plugin", "push", str(make_wheel(tmp_path)))
    assert res.exit_code == 1 and token not in res.stdout + res.stderr


def test_push_picks_your_only_project(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    api.get(f"{V1}/me").respond(200, json=example(Me))  # one tenant, one project: acme/growth
    routes = _upload_routes(api, example)
    res = mapi("--url", URL, "plugin", "push", str(make_wheel(tmp_path)))
    assert res.exit_code == 0, res.output
    assert routes["ticket"].calls.last.request.headers["x-mapi-project"] == "acme/growth"


# ================================================================================= status


def test_status_lists_plugins_versions_and_one_version(
    mapi: Invoke, api: respx.MockRouter, example: Ex, as_json: AsJson
) -> None:
    api.get(f"{V1}/plugins").respond(200, json=_page([_plugin(example)]))
    api.get(f"{V1}/plugins/{PLG}/versions").respond(
        200, json=_page([example(PluginVersionSummary)])
    )
    api.get(f"{V1}/plugins/{PLG}/versions/{PLV}").respond(200, json=_version(example, "kit_passed"))
    base = ("--url", URL, "--project", PROJECT)
    res = mapi(*base, "plugin", "status")
    assert res.exit_code == 0 and NS in res.stdout and "Ready for you" in res.stdout
    res = mapi(*base, "--json", "plugin", "status", "acme")
    assert [v["version"] for v in as_json(res)["versions"]] == ["0.1.0"]
    res = mapi(*base, "plugin", "status", "acme@0.1.0")
    assert res.exit_code == 0, res.output
    assert "only you" in res.stdout and "https://example.org/acme" in res.stderr
    res = mapi(*base, "--json", "plugin", "status", "acme@0.1.0")
    assert as_json(res)["version"]["state"] == "kit_passed"
    res = mapi(*base, "plugin", "status", "acme@9.9.9")
    assert res.exit_code == 1 and "NOT_FOUND" in res.stderr
    res = mapi(*base, "plugin", "status", "nope")
    assert res.exit_code == 1 and "mapi plugin init nope --upload" in res.stderr


# =================================================================================== yank


def test_yank_refuses_an_api_token_without_a_request(
    mapi: Invoke, api: respx.MockRouter, token: str, as_json: AsJson
) -> None:
    route = api.route(host="127.0.0.1").respond(500)
    res = mapi(
        "--url", URL, "--project", PROJECT, "--json", "plugin", "yank", "acme@0.1.0",
        "--reason", "withdrawn", "--yes",
    )  # fmt: skip
    assert res.exit_code == 1
    error = as_json(res)["error"]
    assert error["code"] == "FORBIDDEN" and "needs a sign-in" in error["detail"]
    assert "mapi login" in error["detail"] and token not in res.stdout
    assert not route.called


def test_yank_with_a_sign_in(
    mapi: Invoke, api: respx.MockRouter, example: Ex, as_json: AsJson
) -> None:
    api.get(f"{V1}/plugins").respond(200, json=_page([_plugin(example)]))
    api.get(f"{V1}/plugins/{PLG}/versions").respond(
        200, json=_page([example(PluginVersionSummary)])
    )
    api.get(f"{V1}/plugins/{PLG}/versions/{PLV}").respond(200, json=_version(example, "approved"))
    api.get(f"{V1}/plugins/{PLG}/versions/{PLV}/usage").respond(200, json=example(PluginUsage))
    yank = api.post(f"{V1}/plugins/{PLG}/versions/{PLV}:yank").respond(
        200, json=_version(example, "yanked", yank_reason="security")
    )
    res = mapi(
        "--url", URL, "--project", PROJECT, "--json", "plugin", "yank", "acme@0.1.0",
        "--reason", "security", "--note", "Leaks data.", "--yes",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert as_json(res)["version"]["state"] == "yanked"
    assert json.loads(yank.calls.last.request.content) == {
        "reason": "security",
        "note": "Leaks data.",
    }
    assert "runs in the last 30 days" in res.stderr


def test_yank_asks_first(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/plugins").respond(200, json=_page([_plugin(example)]))
    api.get(f"{V1}/plugins/{PLG}/versions").respond(
        200, json=_page([example(PluginVersionSummary)])
    )
    api.get(f"{V1}/plugins/{PLG}/versions/{PLV}").respond(200, json=_version(example, "approved"))
    api.get(f"{V1}/plugins/{PLG}/versions/{PLV}/usage").respond(200, json=example(PluginUsage))
    yank = api.post(f"{V1}/plugins/{PLG}/versions/{PLV}:yank").respond(500)
    res = mapi(
        "--url", URL, "--project", PROJECT, "plugin", "yank", "acme@0.1.0", "--reason",
        "withdrawn", input="n\n",
    )  # fmt: skip
    assert res.exit_code == 1 and not yank.called


def test_yank_needs_a_version(mapi: Invoke) -> None:
    res = mapi("plugin", "yank", "acme", "--reason", "withdrawn", "--yes")
    assert res.exit_code == 1 and "acme@VERSION" in res.stderr


# ========================================================================= real build


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_a_real_build_of_the_upload_scaffold_passes_the_platform_inspection(
    mapi: Invoke,
    api: respx.MockRouter,
    example: Ex,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``uv build`` in tmp, then the platform's static inspection (it reads the archive and
    never imports it): one top-level package named the namespace, the right dist name and
    entry points only in the allowed groups."""
    from mapi_runner.plugin_inspect import inspect_wheel, installed_index
    from mapi_spec.jobs import PluginInspectJob

    root = _init_upload(mapi, api, example, tmp_path)
    out = tmp_path / "dist"
    cmd = plugin.build_command(root, out)
    assert cmd is not None
    import subprocess

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=600)
    if proc.returncode != 0:
        pytest.skip(f"uv could not build offline here: {proc.stderr[-500:]}")
    (path,) = out.glob("*.whl")
    wheel = check_wheel(path, built=True)
    assert wheel.path.name == WHEEL_NAME
    job = PluginInspectJob(
        plugin_version_id=PLV,
        namespace=NS,
        dist_name=DIST,
        version="0.1.0",
        expected_sha256=wheel.sha256,
    )
    report = inspect_wheel(path, job=job, index=installed_index())
    assert report.top_level == [NS]
    assert report.ok, [p.model_dump() for p in report.problems]
