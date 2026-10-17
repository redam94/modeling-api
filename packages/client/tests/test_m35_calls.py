"""The v0.5 request builders beyond the contract table: arguments checked before anything is
sent, resend rules and query encoding (``test_endpoint_coverage.py`` checks every builder
against ``mapi_spec.api.ENDPOINTS``)."""

from __future__ import annotations

from typing import Any

import pytest
from mapi_client import _calls_m35 as m35
from mapi_spec.api import (
    IDEMPOTENCY_KEY_HEADER,
    PREFER_HEADER,
    ApprovalCreate,
    DatasetAboutUpdate,
    EvidencePackRequest,
    GovernancePolicyCreate,
    OptimizeRequest,
    ValidationRequest,
    WorkflowCreate,
    WorkflowRevisionCreate,
    WorkflowRunCreate,
    WorkflowValidateRequest,
)

_WF = "wfl_01jaf3h1j2k3m4n5p6q7r8s9t0"
_VAL = "val_01jaf3h1j2k3m4n5p6q7r8s9t0"


def _ex(model: Any) -> Any:
    return model.model_validate(model.model_json_schema()["examples"][0])


def test_chooser_answers_are_checked_before_sending() -> None:
    call = m35.recommend_catalog_items({"goal": ["forecast", "plan_budget"], "speed": "seconds"})
    assert call.params == {"goal": ["forecast", "plan_budget"], "speed": ["seconds"]}
    with pytest.raises(ValueError, match="unknown chooser question"):
        m35.recommend_catalog_items({"colour": "blue"})
    with pytest.raises(ValueError, match="unknown answers"):
        m35.recommend_catalog_items({"goal": "world_domination"})
    with pytest.raises(ValueError, match="takes one answer"):
        m35.recommend_catalog_items({"speed": ["seconds", "minutes"]})
    with pytest.raises(ValueError):
        m35.recommend_catalog_items({}, limit=51)
    assert m35.CHOOSER_QUESTION_IDS[0] == "goal"


def test_filters_are_validated() -> None:
    with pytest.raises(ValueError):
        m35.list_catalog_abouts(None, None, kind="spaceship")
    with pytest.raises(ValueError):
        m35.list_workflow_nodes(None, None, kind="dance")
    with pytest.raises(ValueError):
        m35.list_workflow_runs(_WF, None, None, status="sleeping")
    with pytest.raises(ValueError):
        m35.list_workflow_node_runs("run_1", None, None, status="sleeping")
    with pytest.raises(ValueError):
        m35.list_validations(None, None, status="maybe")
    with pytest.raises(ValueError):
        m35.list_policies(None, None, scope="galaxy")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        m35.workflow_run_events("run_1", -1, None)
    assert m35.list_workflows(None, None, archived=True).params == {"archived": "true"}
    assert m35.workflow_run_events("run_1", 3, 10).params == {"limit": 10, "after": 3}
    assert m35.workflow_run_events("run_1", None, None).headers == {"Accept": "application/json"}
    path = m35.get_workflow_node_output("r", "a/b", "data").path
    assert path.endswith("/nodes/a%2Fb/outputs/data")
    with pytest.raises(ValueError):
        m35.get_workflow_run("..")


def test_optimize_waits_are_capped() -> None:
    body = _ex(OptimizeRequest)
    with pytest.raises(ValueError):
        m35.optimize_model_version("m", 1, body, wait=61, idempotency_key="k")
    call = m35.optimize_model_version("m", 1, body, wait=None, idempotency_key="k")
    assert PREFER_HEADER not in call.headers and call.server_wait is None
    call = m35.optimize_model_version("m", 1, body, wait=20, idempotency_key=None)
    assert call.server_wait == 20 and call.headers[IDEMPOTENCY_KEY_HEADER]  # a fresh key


def test_decisions_and_documents_are_never_resent_blindly() -> None:
    """A lost response must not record a second approval or a second revision."""
    assert not m35.decide_validation(_VAL, _ex(ApprovalCreate)).idempotent
    assert not m35.create_workflow(_ex(WorkflowCreate)).idempotent
    assert not m35.create_workflow_revision(_WF, _ex(WorkflowRevisionCreate)).idempotent
    assert not m35.create_policy(_ex(GovernancePolicyCreate)).idempotent
    # keyed starts, static checks, cancels and exports are safe to resend
    assert m35.start_workflow_run(_WF, _ex(WorkflowRunCreate), None).idempotent
    assert m35.validate_workflow(_ex(WorkflowValidateRequest)).idempotent
    assert m35.cancel_workflow_run("run_1").idempotent
    assert m35.export_evidence_pack(_VAL, _ex(EvidencePackRequest)).idempotent
    assert m35.request_model_validation("m", 1, _ex(ValidationRequest), None).idempotent


def test_dataset_about_can_clear_the_description() -> None:
    call = m35.update_dataset_about("ds_1", DatasetAboutUpdate(description_user=None))
    assert call.json == {"description_user": None}
