"""Every M1 endpoint has a request builder whose method, path and success statuses match
``mapi_spec.api.M1_ENDPOINTS`` (the contract the server's route table is tested against)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest
from mapi_client import _calls as c
from mapi_client._http import Call
from mapi_spec.api import (
    M1_ENDPOINTS,
    DatasetCreate,
    DatasetVersionCreate,
    EndpointDecl,
    IngestMapping,
    PlotSpec,
    RunCreate,
    RunRetry,
    ScoreCall,
    SpecCreate,
    SpecRevisionCreate,
    UploadCreate,
    ValidateRequest,
)

_YAML = "apiVersion: mapi/v1\n"
_SCORE = ScoreCall.model_validate(ScoreCall.model_json_schema()["examples"][0])
_PLOT = PlotSpec.model_validate(PlotSpec.model_json_schema()["examples"][0])

BUILDERS: dict[str, Callable[[], Call[Any]]] = {
    "health": c.health,
    "ready": c.ready,
    "list_projects": lambda: c.list_projects(None, None),
    "get_project": lambda: c.get_project("default"),
    "get_catalog": c.get_catalog,
    "list_catalog_models": lambda: c.list_catalog_models(None, None),
    "get_catalog_model": lambda: c.get_catalog_model("mmm.geo", "1.0.0"),
    "list_catalog_steps": lambda: c.list_catalog_steps(None, None),
    "list_catalog_plots": lambda: c.list_catalog_plots(None, None),
    "get_schema": lambda: c.get_schema("modelspec.json"),
    "create_dataset": lambda: c.create_dataset(DatasetCreate(name="d")),
    "list_datasets": lambda: c.list_datasets(None, None),
    "get_dataset": lambda: c.get_dataset("ds_1"),
    "create_upload": lambda: c.create_upload("ds_1", UploadCreate(filename="a.csv", size_bytes=1)),
    "create_dataset_version": lambda: c.create_dataset_version(
        "ds_1",
        DatasetVersionCreate(upload_id="upl_1", mapping=IngestMapping(format="zarr_zip")),
        None,
    ),
    "list_dataset_versions": lambda: c.list_dataset_versions("ds_1", None, None),
    "get_dataset_version": lambda: c.get_dataset_version("ds_1", 3),
    "get_dataset_artifact": lambda: c.get_dataset_artifact("ds_1", 3, "preview.json"),
    "validate_spec": lambda: c.validate_spec(ValidateRequest(yaml=_YAML)),
    "create_spec": lambda: c.create_spec(SpecCreate(yaml=_YAML)),
    "list_specs": lambda: c.list_specs(None, None),
    "get_spec": lambda: c.get_spec("spc_1"),
    "create_spec_revision": lambda: c.create_spec_revision(
        "spc_1", SpecRevisionCreate(yaml=_YAML, parent_revision=1)
    ),
    "list_spec_revisions": lambda: c.list_spec_revisions("spc_1", None, None),
    "get_spec_revision": lambda: c.get_spec_revision("spc_1", 2),
    "dry_run_spec": lambda: c.dry_run_spec("spc_1", 2, None),
    "create_run": lambda: c.create_run(RunCreate(kind="fit", spec_id="spc_1", revision=1), None),
    "list_runs": lambda: c.list_runs(None, None),
    "get_run": lambda: c.get_run("run_1"),
    "run_events": lambda: c.list_run_events("run_1", None, None),
    "run_logs": lambda: c.run_logs("run_1", None, None),
    "cancel_run": lambda: c.cancel_run("run_1"),
    "resume_run": lambda: c.resume_run("run_1"),
    "retry_run": lambda: c.retry_run("run_1", RunRetry(), None),
    "list_models": lambda: c.list_models(None, None),
    "get_model": lambda: c.get_model("mdl_1"),
    "list_model_versions": lambda: c.list_model_versions("mdl_1", None, None),
    "get_model_version": lambda: c.get_model_version("mdl_1", 1),
    "get_model_artifact": lambda: c.get_model_artifact("mdl_1", 1, "bundle.zarr.zip"),
    "score_model": lambda: c.score_model("mdl_1", 1, _SCORE, wait=60, idempotency_key="k"),
    "list_plots": lambda: c.list_plots("mdl_1", 1, None, None),
    "get_plot": lambda: c.get_plot("mdl_1", 1, "core.ppc_fan"),
    "validate_plot": lambda: c.validate_plot(_PLOT),
}


def _pattern(decl: EndpointDecl) -> re.Pattern[str]:
    parts = re.split(r"(\{[a-z_]+\})", decl.path)
    return re.compile(
        "^" + "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts) + "$"
    )


def test_every_endpoint_has_a_builder() -> None:
    assert {d.operation_id for d in M1_ENDPOINTS} == set(BUILDERS)


@pytest.mark.parametrize("decl", M1_ENDPOINTS, ids=lambda d: d.operation_id)
def test_builder_matches_the_contract(decl: EndpointDecl) -> None:
    call = BUILDERS[decl.operation_id]()
    assert call.method == decl.method
    assert _pattern(decl).match(call.path), (call.path, decl.path)
    assert decl.success in call.expect
    assert (call.json is not None) == (decl.request is not None)
    if "Idempotency-Key" in decl.headers:
        assert "Idempotency-Key" in call.headers
    if decl.paginated:
        assert call.parse.__qualname__.startswith("parse_model")
        assert decl.response is not None and decl.response.startswith("Page[")


def test_path_segments_are_encoded() -> None:
    call = c.get_model_artifact("mdl_1", 1, "ui/plot data/a.json")
    assert call.path.endswith("/artifacts/ui/plot%20data/a.json")
    with pytest.raises(ValueError):
        c.get_model_artifact("mdl_1", 1, "ui/../secret")
    with pytest.raises(ValueError):
        c.get_run("..")
    assert c.get_run("a/b").path.endswith("/runs/a%2Fb")


def test_list_parameters() -> None:
    call = c.list_runs(10, "cur", kind="fit", state="running", spec_id="spc_1")
    assert call.params == {
        "limit": 10,
        "cursor": "cur",
        "kind": "fit",
        "state": "running",
        "spec_id": "spc_1",
    }
    with pytest.raises(ValueError):
        c.list_runs(500, None)
    with pytest.raises(ValueError):
        c.list_runs(None, None, state="bogus")
    with pytest.raises(ValueError):
        c.run_logs("run_1", None, None, stream="journal")


def test_prefer_wait_is_capped() -> None:
    call = c.score_model("m", 1, _SCORE, wait=30, idempotency_key="k")
    assert call.headers["Prefer"] == "wait=30"
    with pytest.raises(ValueError):
        c.score_model("m", 1, _SCORE, wait=61, idempotency_key="k")
