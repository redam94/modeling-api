"""Local commands: new, check, fit, score, catalog, spec upgrade, plugin, config, serve."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mapi_cli import __version__
from mapi_cli.output import safe, strip_control
from typer.testing import Result

Invoke = Callable[..., Result]
AsJson = Callable[[Result], Any]


def test_version(mapi: Invoke) -> None:
    res = mapi("--version")
    assert res.exit_code == 0 and res.stdout.strip() == f"mapi {__version__}"


# ------------------------------------------------------------------------------------ new


def test_new_lists_templates(mapi: Invoke, as_json: AsJson) -> None:
    res = mapi("--json", "new", "--list")
    assert res.exit_code == 0
    assert {t["name"] for t in as_json(res)} == {"geo-mmm", "forecast", "tabular", "glm"}
    assert "geo-mmm" in mapi("new").stdout


def test_new_writes_a_valid_spec(mapi: Invoke, tmp_path: Path) -> None:
    dest = tmp_path / "specs" / "radon.yaml"
    res = mapi("new", "glm", "-o", str(dest), "--name", "radon", "--project", "env")
    assert res.exit_code == 0, res.output
    text = dest.read_text()
    assert "name: radon" in text and "dataset://env/radon_homes@latest" in text
    assert "mapi fit" in res.stdout
    again = mapi("new", "glm", "-o", str(dest))
    assert again.exit_code == 1 and "exists" in again.stderr
    assert mapi("new", "glm", "-o", str(dest), "--force").exit_code == 0


def test_new_rejects_unknown_templates_and_names(mapi: Invoke, tmp_path: Path) -> None:
    assert mapi("new", "nope", "-o", str(tmp_path / "s.yaml")).exit_code == 2
    res = mapi("new", "glm", "-o", str(tmp_path / "s.yaml"), "--name", "Not A Slug")
    assert res.exit_code == 1


# ---------------------------------------------------------------------------------- check


def test_check_static(mapi: Invoke, tmp_path: Path, as_json: AsJson) -> None:
    spec = tmp_path / "spec.yaml"
    assert mapi("new", "forecast", "-o", str(spec)).exit_code == 0
    res = mapi("--json", "check", str(spec))
    assert res.exit_code == 0, res.output
    doc = as_json(res)
    assert doc["valid"] is True and doc["mode"] == "static"
    assert doc["issues"][-1]["code"] == "SCHEMA_DYNAMIC"


def test_check_reports_errors_with_pointers(mapi: Invoke, tmp_path: Path, as_json: AsJson) -> None:
    spec = tmp_path / "spec.yaml"
    assert mapi("new", "forecast", "-o", str(spec)).exit_code == 0
    text = spec.read_text().replace("core.standardize@^1", "core.nonexistent@^1")
    text = text.replace("max_extension: 24", "max_extension: -3")
    spec.write_text(text)
    res = mapi("--json", "check", str(spec))
    assert res.exit_code == 1
    issues = {(i["code"], i["pointer"]) for i in as_json(res)["issues"]}
    assert ("REF_UNRESOLVED", "/pipeline/1/use") in issues
    assert any(code == "SETTINGS_INVALID" for code, _ in issues)
    human = mapi("check", str(spec))
    assert human.exit_code == 1 and "REF_UNRESOLVED" in human.stdout
    assert "invalid" in human.stderr


def test_check_parse_errors(mapi: Invoke, tmp_path: Path, as_json: AsJson) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("apiVersion: mapi/v1\nkind: ModelSpec\nmetadata: &a {name: x}\n")
    res = mapi("--json", "check", str(bad))
    assert res.exit_code == 1
    assert as_json(res)["issues"][0]["code"] == "SPEC_PARSE_ERROR"


def test_check_with_data_runs_full_validation(
    mapi: Invoke, tmp_path: Path, as_json: AsJson
) -> None:
    spec = tmp_path / "spec.yaml"
    assert mapi("new", "tabular", "-o", str(spec), "--with-data").exit_code == 0
    data = tmp_path / "data.zarr.zip"
    res = mapi("--json", "check", str(spec), "--data", str(data))
    assert res.exit_code == 0, res.output
    doc = as_json(res)
    assert doc["mode"] == "full" and doc["valid"] is True
    names = [s["name"] for s in doc["stages"]]
    assert names == ["raw", "imp", "plags", "cal", "logy"]
    assert doc["stages"][0]["split"] is True
    # a binding to a variable the data lacks is caught only with data
    spec.write_text(spec.read_text().replace("promo: promo_flag", "promo: promo_missing"))
    res = mapi("--json", "check", str(spec), "--data", str(data))
    assert res.exit_code == 1
    assert any(i["code"] == "BINDING_INVALID" for i in as_json(res)["issues"])
    assert mapi("check", str(spec)).exit_code == 0  # static checks cannot see it


def test_check_with_schema_file(mapi: Invoke, tmp_path: Path) -> None:
    spec = tmp_path / "spec.yaml"
    assert mapi("new", "forecast", "-o", str(spec)).exit_code == 0
    schema = tmp_path / "schema.yaml"
    schema.write_text(
        "dims: {date: {kind: time, policy: extend, freq: MS}}\n"
        "vars: {co2: {dims: [date], dtype: float64, role: target, missing: allow}}\n"
    )
    res = mapi("check", str(spec), "--schema", str(schema), "--stages")
    assert res.exit_code == 0, res.output
    assert "Schema by stage" in res.stdout


def test_check_rejects_csv_data(mapi: Invoke, tmp_path: Path) -> None:
    spec = tmp_path / "spec.yaml"
    assert mapi("new", "forecast", "-o", str(spec)).exit_code == 0
    csv = tmp_path / "d.csv"
    csv.write_text("a\n1\n")
    res = mapi("check", str(spec), "--data", str(csv))
    assert res.exit_code == 1 and "zarr.zip" in res.stderr


# ----------------------------------------------------------------------------- fit/score


@pytest.mark.integration
def test_fit_and_score_locally(mapi: Invoke, tmp_path: Path, as_json: AsJson) -> None:
    spec = tmp_path / "spec.yaml"
    assert mapi("new", "tabular", "-o", str(spec), "--with-data").exit_code == 0
    data = tmp_path / "data.zarr.zip"
    bundle = tmp_path / "bundle"
    res = mapi("--json", "fit", str(spec), "--data", str(data), "--out", str(bundle))
    assert res.exit_code == 0, res.output
    doc = as_json(res)
    assert doc["state"] == "succeeded" and (bundle / "manifest.json").is_file()
    assert {"rmse", "mape"} <= set(doc["metrics"])
    again = mapi("fit", str(spec), "--data", str(data), "--out", str(bundle))
    assert again.exit_code == 1 and "not empty" in again.stderr

    req = tmp_path / "req.yaml"
    req.write_text("apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: model://default/t@v1\n")
    out = tmp_path / "result.zarr.zip"
    res = mapi(
        "--json", "score", str(req), "--bundle", str(bundle), "--data", str(data), "--out", str(out)
    )
    assert res.exit_code == 0, res.output
    doc = as_json(res)
    assert doc["attrs"]["representation"] == "quantiles"
    assert doc["variables"][0]["group"] == "/summary" and out.is_file()

    from mapi_sdk.wire import read_datatree_zip

    assert "summary" in read_datatree_zip(out).children
    human = mapi("score", str(req), "--bundle", str(bundle), "--data", str(data))
    assert human.exit_code == 0 and "quantiles" in human.stdout


def test_fit_argument_errors(mapi: Invoke, tmp_path: Path) -> None:
    spec = tmp_path / "spec.yaml"
    assert mapi("new", "geo-mmm", "-o", str(spec)).exit_code == 0
    res = mapi("fit", str(spec))
    assert res.exit_code == 1 and "--data" in res.stderr
    data = tmp_path / "d.zarr.zip"
    data.write_bytes(b"PK")
    res = mapi("fit", str(spec), "--data", str(data), "--out", str(tmp_path / "b"))
    assert res.exit_code == 1 and "side table 'fx'" in res.stderr
    res = mapi("fit", str(spec), "--data", str(data), "--side-table", "fx")
    assert res.exit_code == 1 and "NAME=PATH" in res.stderr


def test_score_argument_errors(mapi: Invoke, tmp_path: Path) -> None:
    req = tmp_path / "req.yaml"
    req.write_text("apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: model://default/t@v1\n")
    res = mapi("score", str(req))
    assert res.exit_code == 1 and "--bundle" in res.stderr
    res = mapi("score", str(req), "--bundle", str(tmp_path))
    assert res.exit_code == 1 and "not a bundle directory" in res.stderr
    res = mapi("score", str(req), "--remote", "@")
    assert res.exit_code == 1 and "--model" in res.stderr


# ------------------------------------------------------------------------------- catalog


def test_local_catalog(mapi: Invoke, as_json: AsJson) -> None:
    res = mapi("--json", "catalog")
    assert res.exit_code == 0, res.output
    doc = as_json(res)
    assert doc["digest"].startswith("sha256:")
    models = {m["manifest"]["id"] for m in doc["models"]}
    assert {"tabular.regressor", "mmm.bayesian", "mmm.frequentist"} <= models
    steps = mapi("--json", "catalog", "steps")
    assert set(as_json(steps)) == {"digest", "steps"}
    human = mapi("catalog", "models")
    assert "mmm.bayesian" in human.stdout


# ------------------------------------------------------------------------------- spec


def test_spec_upgrade(mapi: Invoke, tmp_path: Path, as_json: AsJson) -> None:
    spec = tmp_path / "spec.yaml"
    assert mapi("new", "glm", "-o", str(spec)).exit_code == 0
    original = spec.read_text()
    res = mapi("--json", "spec", "upgrade", str(spec), "--check")
    assert res.exit_code == 0 and as_json(res)["changed"] is False
    res = mapi("spec", "upgrade", str(spec))
    assert res.exit_code == 0 and res.stdout == original  # unchanged: comments kept
    out = tmp_path / "up.yaml"
    assert mapi("spec", "upgrade", str(spec), "-o", str(out)).exit_code == 0
    assert out.read_text() == original
    old = tmp_path / "old.yaml"
    old.write_text(original.replace("apiVersion: mapi/v1", "apiVersion: mapi/v0"))
    res = mapi("spec", "upgrade", str(old))
    assert res.exit_code == 1 and "UNSUPPORTED_API_VERSION" in res.stderr


def test_spec_upgrade_with_a_registered_converter(
    mapi: Invoke, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mapi_spec import loader

    def v0_to_v1(doc: dict[str, Any]) -> dict[str, Any]:
        doc = dict(doc)
        doc["metadata"] = {**doc["metadata"], "labels": {"migrated": "yes"}}
        return doc

    monkeypatch.setitem(loader._UPGRADERS, "mapi/v0", ("mapi/v1", v0_to_v1))
    spec = tmp_path / "spec.yaml"
    assert mapi("new", "glm", "-o", str(spec)).exit_code == 0
    spec.write_text(spec.read_text().replace("apiVersion: mapi/v1", "apiVersion: mapi/v0"))
    assert mapi("spec", "upgrade", str(spec), "--check").exit_code == 1
    res = mapi("spec", "upgrade", str(spec), "--in-place")
    assert res.exit_code == 0, res.output
    upgraded = loader.load_model_spec(spec.read_text())
    assert upgraded.metadata.labels == {"migrated": "yes"}


# ------------------------------------------------------------------------------ plugin


def test_plugin_init_and_contract_kit(mapi: Invoke, tmp_path: Path, as_json: AsJson) -> None:
    res = mapi("--json", "plugin", "init", "acme-demand", "--dir", str(tmp_path))
    assert res.exit_code == 0, res.output
    doc = as_json(res)
    assert doc["plugin_id"] == "acme_demand.group_mean" and doc["module"] == "acme_demand"
    root = tmp_path / "acme-demand"
    assert (root / "src/acme_demand/model.py").is_file()
    pyproject = (root / "pyproject.toml").read_text()
    assert 'acme_demand = "acme_demand:PLUGINS"' in pyproject
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


# ------------------------------------------------------------------------------ config


def test_config_show(
    mapi: Invoke, as_json: AsJson, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_env.write_text('[profiles.dev]\nurl = "https://dev.test"\n')
    monkeypatch.setenv("MAPI_TOKEN", "secret-token")
    res = mapi("--json", "--profile", "dev", "config", "show")
    doc = as_json(res)
    assert doc["url"] == "https://dev.test" and doc["token"] == "set"
    assert doc["sources"]["url"] == "file:dev" and doc["sources"]["token"] == "env:MAPI_TOKEN"
    assert "secret-token" not in res.output
    assert mapi("config", "path").stdout.strip() == str(isolated_env)
    bad = mapi("--profile", "nope", "config", "show")
    assert bad.exit_code == 1 and "configuration error" in bad.stderr


# ------------------------------------------------------------------------------- serve


def test_serve_without_server(mapi: Invoke, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda n, *a: None if n == "mapi_server" else real(n, *a)
    )
    res = mapi("serve")
    assert res.exit_code == 1 and "pip install mapi-server" in res.stderr


def test_serve_delegates(mapi: Invoke, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util
    import subprocess

    from mapi_cli.commands import serve as serve_mod

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda n, *a: object() if n == "mapi_server" else real(n, *a)
    )
    seen: list[list[str]] = []

    def fake_call(cmd: list[str], **kw: Any) -> int:
        seen.append(cmd)
        return 0

    monkeypatch.setattr(subprocess, "call", fake_call)
    res = mapi("serve", "--port", "8001", "--reload")
    assert res.exit_code == 0, res.output
    assert seen[0][1:] == ["-m", "mapi_server", "dev", "--port", "8001", "--reload"]
    assert serve_mod.server_command([])[1:4] == ["-m", "mapi_server", "dev"]


# ------------------------------------------------------------------------------ output


def test_untrusted_text_is_sanitised() -> None:
    evil = "ok\x1b[31mred\x1b[0m\x1b]8;;http://x\x07link\x1b]8;;\x07 [bold]x[/bold]\x07"
    assert strip_control(evil) == "okredlink [bold]x[/bold]"
    assert safe(evil).plain == "okredlink [bold]x[/bold]"  # markup is not interpreted
    assert json.dumps(strip_control("a\tb\nc")) == '"a\\tb\\nc"'


def test_bidi_overrides_are_stripped_from_untrusted_text() -> None:
    spoof = "invoice\u202egpj.exe\u202c and \u2066isolated\u2069 \u200fok"
    assert strip_control(spoof) == "invoicegpj.exe and isolated \u200fok"
    assert safe(spoof).plain == "invoicegpj.exe and isolated \u200fok"


def _terminal() -> tuple[Any, Any]:
    import io

    from rich.console import Console

    buf = io.StringIO()
    return buf, Console(file=buf, force_terminal=True, width=300, color_system="truecolor")


@pytest.mark.parametrize(
    "message",
    [
        "[link=https://evil.example/]Click to view results[/link]",
        "[/]",
        "[bold red]fake error[/bold red] [/nope]",
    ],
)
@pytest.mark.parametrize("kind", ["progress", "state"])
def test_progress_line_never_interprets_untrusted_markup(message: str, kind: str) -> None:
    """Run progress/state messages come from the untrusted worker: shown verbatim, never
    as rich markup (no OSC 8 hyperlink, no MarkupError aborting ``mapi fit``)."""
    from types import SimpleNamespace

    from mapi_cli.output import Output
    from mapi_cli.progress import EventRenderer
    from mapi_spec.executor import RunEventType

    buf, err = _terminal()
    if kind == "progress":
        event = SimpleNamespace(
            type=RunEventType.PROGRESS, payload={"fraction": 0.5, "message": message}
        )
    else:
        event = SimpleNamespace(
            type=RunEventType.STATE, payload={"state": "RUNNING", "message": message}
        )
    with EventRenderer(Output(err=err), "fit") as renderer:
        renderer.handle(event)
        renderer._progress.refresh()
    rendered = buf.getvalue()
    assert "\x1b]8;" not in rendered  # no terminal hyperlink
    assert message in strip_control(rendered)


def test_info_lines_do_not_interpret_markup() -> None:
    from mapi_cli.output import Output

    buf, err = _terminal()
    Output(err=err).info("submitted run [link=https://evil.example/]r1[/link] [/]")
    rendered = buf.getvalue()
    assert "\x1b]8;" not in rendered
    assert "[link=https://evil.example/]r1[/link] [/]" in strip_control(rendered)


TREE_SPEC = """\
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: sales-tree, project: growth}
data:
  dataset: dataset://growth/sales_tree@latest
  nodes:
    sales: {path: /sales}
    media: {path: /media, vars: [spend]}
  bind:
    dims: {geo: market, time: date}
    vars: {y: revenue}
  split: {kind: time_holdout, dim: time, last: 2}
pipeline: []
model:
  use: toy.tree_linear@^1
"""


def test_check_with_a_tree_dataset_resolves_data_nodes(
    mapi: Invoke, tmp_path: Path, as_json: AsJson
) -> None:
    from mapi_sdk.testing import toy
    from mapi_sdk.tree import write_tree_zip

    data = tmp_path / "sales.tree.zip"
    write_tree_zip(toy.tree_dataset(weeks=8), data)
    spec = tmp_path / "spec.yaml"
    spec.write_text(TREE_SPEC)
    doc = as_json(mapi("--json", "check", str(spec), "--data", str(data)))
    assert doc["mode"] == "full"
    pointers = {(i["code"], i["pointer"]) for i in doc["issues"]}
    # the nodes resolve against the tree schema: no "not a tree", no missing 'revenue'
    assert not any(p.startswith("/data") for _, p in pointers), pointers
    raw = doc["stages"][0]["data_schema"]
    assert set(raw["vars"]) == {"y", "spend"}  # after the binding
    assert set(raw["dims"]) >= {"geo", "channel", "time"}
    # a node the tree does not have is reported
    spec.write_text(TREE_SPEC.replace("path: /media,", "path: /nope,"))
    res = mapi("--json", "check", str(spec), "--data", str(data))
    assert res.exit_code == 1
    assert any("/nope" in i["message"] for i in as_json(res)["issues"])
