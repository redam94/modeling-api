"""About cards everywhere (M3.5): ``GET /v1/catalog/abouts``, the "Help me choose"
questions and the ranked recommendations, over the full installed catalog. Every item a
user can pick - models, preprocessing steps, splits, guided templates, workflow templates,
workflow node kinds and nodes, governance checks, plots - must have a complete About."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_sdk.registry import default_registry
from mapi_server.settings import Settings
from mapi_spec.about import CHOOSER_QUESTIONS, About, AboutKind, about_issues
from mapi_spec.abouts import builtin_abouts
from mapi_spec.governance import GOVERNANCE_CHECKS
from mapi_spec.guided import GUIDED_TEMPLATES
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS, NodeKind
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    """The suite's settings with the catalog of every installed plugin and step."""
    path = tmp_path / "catalog.json"
    path.write_text(default_registry().catalog().model_dump_json())
    return settings.model_copy(update={"catalog_path": path})


async def _all(client: httpx.AsyncClient, **params: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = None
    while True:
        q = {"limit": 200, **params, **({"cursor": cursor} if cursor else {})}
        r = await client.get("/v1/catalog/abouts", params=q)
        assert r.status_code == 200, r.text
        page = r.json()
        items += page["items"]
        cursor = page.get("next_cursor")
        if not cursor:
            return items


async def test_every_selectable_item_has_a_complete_about(client: httpx.AsyncClient) -> None:
    items = await _all(client)
    by_kind: dict[str, set[str]] = {}
    for it in items:
        by_kind.setdefault(it["kind"], set()).add(it["id"])
    catalog = default_registry().catalog()
    assert by_kind["model"] == {m.id for m in catalog.models}
    assert by_kind["step"] == {s.id for s in catalog.steps if not s.yanked}
    assert by_kind["template"] == set(GUIDED_TEMPLATES)
    assert {i.removeprefix("workflow.") for i in by_kind["workflow_template"]} == set(
        WORKFLOW_TEMPLATES
    )
    assert by_kind["node"] == set(NODE_IMPLEMENTATIONS)
    assert len(by_kind["node_kind"]) == len(NodeKind)
    assert by_kind["check"] == set(GOVERNANCE_CHECKS)
    assert by_kind["plot"] == {p.id for p in catalog.plots if not p.yanked}
    assert len(by_kind["split"]) >= 5
    # the MMM engine is mmm-framework and the statsmodels models are installed
    assert {"mmm.bayesian", "mmm.frequentist", "ts.sarimax", "ts.var", "ts.vecm"} <= by_kind[
        "model"
    ]
    known = {it["id"] for it in items} | set(builtin_abouts())
    problems = {
        f"{it['kind']}:{it['id']}": issues
        for it in items
        if (issues := about_issues(About.model_validate(it["about"]), known_ids=known))
    }
    assert not problems, problems
    for it in items:
        about = it["about"]
        assert about["summary_plain"] and about["use_when"] and about["example"]
        assert about["difficulty"] in ("beginner", "intermediate", "expert")

    # related items are items a user can open: never a model that is not installed
    # (regression: mmm.bayesian listed mmm.nested and mmm.multivariate as bare ids)
    served = {it["id"] for it in items}
    dangling = {
        f"{it['kind']}:{it['id']}": sorted(set(it["about"].get("related", [])) - served)
        for it in items
        if set(it["about"].get("related", [])) - served
    }
    assert not dangling, dangling

    only_models = await _all(client, kind="model")
    assert {i["kind"] for i in only_models} == {"model"}


async def test_the_chooser_questions(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/catalog/chooser")
    assert r.status_code == 200, r.text
    questions = r.json()["questions"]
    assert [q["id"] for q in questions] == [q.id for q in CHOOSER_QUESTIONS]
    assert all(q["prompt_plain"] and q["choices"] for q in questions)


async def test_recommendations_for_a_non_technical_user(client: httpx.AsyncClient) -> None:
    r = await client.get(
        "/v1/catalog/recommendations",
        params=[("goal", "explain_marketing"), ("data_shape", "panel"), ("limit", "5")],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    kinds = {i["kind"] for i in body["items"]}
    assert kinds <= {"model", "template", "workflow_template"}  # starting points only
    ids = [i["id"] for i in body["items"]]
    assert "mmm.bayesian" in ids or "geo_mmm" in ids
    first = body["items"][0]
    assert first["reasons_plain"] and first["about"]["summary_plain"]
    assert set(body["answered"]) == {"goal", "data_shape"}

    r = await client.get(
        "/v1/catalog/recommendations",
        params={"goal": "forecast", "data_shape": "several_series", "kind": "model"},
    )
    ranked = [i["id"] for i in r.json()["items"]]
    assert ranked and all(i.startswith("ts.") for i in ranked[:3])
    assert {"ts.var", "ts.vecm"} & set(ranked[:5])

    # every item is made for the goal; none is "Not made for" it (regression: 0% best match)
    for goal in ("check_data", "validate_model", "find_regimes", "explain_drivers"):
        r = await client.get("/v1/catalog/recommendations", params={"goal": goal})
        items = r.json()["items"]
        assert items, goal
        assert all(not any("Not made for" in c for c in i["cautions_plain"]) for i in items)
        assert all(i["score"] > 0.5 for i in items), goal

    r = await client.get("/v1/catalog/recommendations", params={"goal": "astrology"})
    assert r.status_code == 422
    assert r.json()["code"] == "CHOOSER_ANSWER_INVALID"
    r = await client.get("/v1/catalog/recommendations", params={"mood": "happy"})
    assert r.status_code == 422
    assert AboutKind.MODEL.value == "model"
