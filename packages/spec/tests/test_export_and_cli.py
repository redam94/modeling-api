from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import mapi_spec
import pytest
from jsonschema import Draft202012Validator
from mapi_spec import export_schemas
from mapi_spec.__main__ import main
from mapi_spec.examples import E33_YAML, SCORE_YAML, XGB_YAML
from mapi_spec.loader import canonical_json, load_document, parse_yaml

pytestmark = pytest.mark.unit


def test_export_schemas_are_valid_2020_12(tmp_path: Path) -> None:
    schemas = export_schemas(tmp_path)
    expected = {
        "modelspec.json",
        "scorerequest.json",
        "sweep.json",
        "backtest.json",
        "sbc.json",
        "campaign.json",
        "document.json",
        "dataschema.json",
        "modelmanifest.json",
        "plotspec.json",
        "problem.json",
        "catalog.json",
        "schemaeffect.json",
        "outputselector.json",
    }
    assert expected <= set(schemas)
    for name, schema in schemas.items():
        Draft202012Validator.check_schema(schema)
        assert schema["$id"].endswith(name)
        on_disk = json.loads((tmp_path / name).read_text())
        assert on_disk == schema


def test_examples_validate_against_exported_schemas() -> None:
    schemas = export_schemas()
    for text, name in (
        (E33_YAML, "modelspec.json"),
        (XGB_YAML, "modelspec.json"),
        (SCORE_YAML, "scorerequest.json"),
    ):
        raw = parse_yaml(text)
        assert list(Draft202012Validator(schemas[name]).iter_errors(raw)) == []
        assert list(Draft202012Validator(schemas["document.json"]).iter_errors(raw)) == []
        canon = json.loads(canonical_json(load_document(text)))
        assert list(Draft202012Validator(schemas[name]).iter_errors(canon)) == []


def test_modelspec_schema_rejects_extras() -> None:
    schema = export_schemas()["modelspec.json"]
    raw = parse_yaml(E33_YAML)
    assert isinstance(raw, dict)
    raw["unknown"] = 1
    assert list(Draft202012Validator(schema).iter_errors(raw))


def test_schema_export_is_deterministic() -> None:
    a = json.dumps(export_schemas(), sort_keys=True)
    b = json.dumps(export_schemas(), sort_keys=True)
    assert a == b


def test_cli_check_and_export(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    spec = tmp_path / "e33.yaml"
    spec.write_text(E33_YAML)
    assert main(["check", str(spec)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ModelSpec sha256:")
    assert main(["check", str(spec), "--canonical"]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "ModelSpec"
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: &x 1\nb: *x\n")
    assert main(["check", str(bad)]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "SPEC_PARSE_ERROR" and err["status"] == 400
    assert main(["export-schemas", str(tmp_path / "schemas")]) == 0
    assert (tmp_path / "schemas" / "modelspec.json").exists()


def test_module_entry_point(tmp_path: Path) -> None:
    spec = tmp_path / "s.yaml"
    spec.write_text(SCORE_YAML)
    out = subprocess.run(
        [sys.executable, "-m", "mapi_spec", "check", str(spec)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.startswith("ScoreRequest sha256:")


def test_public_api_surface() -> None:
    for name in mapi_spec.__all__:
        assert hasattr(mapi_spec, name), name
    assert isinstance(mapi_spec.__version__, str)


def test_no_array_libraries_imported() -> None:
    code = (
        "import sys, mapi_spec, mapi_spec.validate, mapi_spec.loader;"
        "bad = [m for m in ('numpy', 'xarray', 'pandas') if m in sys.modules];"
        "print(','.join(bad))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
