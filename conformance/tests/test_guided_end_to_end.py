"""The Guided data path end to end, in process: a table → its column profiles (as the
sandboxed ingest writes them) → role detection → a draft → ingest with the draft's mapping
→ static validation against the real catalog → a fit.

One synthetic table per template and a fit for the choice model (tiny settings). Templates
whose model is a v0.5 plugin not installed here (``mapi_spec.abouts.PLANNED_MODEL_IDS``) are
skipped until that plugin lands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from mapi_plugin_tabular.datasets import CHOICE_TRUTH
from mapi_runner.jobs import IngestJob
from mapi_runner.worker.ingest import build_dataset
from mapi_runner.worker.scorecard import fit_scorecard
from mapi_runner.worker.tabular import IngestLimits, column_profiles, read_table
from mapi_sdk.harness import fit_spec
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import validate_dataset
from mapi_spec.abouts import PLANNED_MODEL_IDS
from mapi_spec.guided import (
    GUIDED_TEMPLATES,
    GuidedAnswers,
    build_draft,
    suggest_roles,
)
from mapi_spec.spec import ModelSpec
from mapi_spec.validate import validate_spec

pytestmark = [pytest.mark.conformance, pytest.mark.slow]

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def registry() -> Registry:
    return default_registry(reload=True)


def _weeks(n: int, start: str = "2024-01-07") -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(start, periods=n, freq="W-SUN")]


def _geo_table(rng: np.random.Generator) -> pd.DataFrame:
    rows = []
    for region in ("north", "south", "east"):
        for w in _weeks(60):
            tv, search = rng.uniform(0, 100), rng.uniform(0, 40)
            rows.append({"Week": w, "Region": region, "Revenue": 500 + 3 * tv + 5 * search,
                         "TV Spend": tv, "Search Spend": search,
                         "Avg Price": rng.uniform(2, 3)})  # fmt: skip
    return pd.DataFrame(rows)


def _mediated_table(rng: np.random.Generator) -> pd.DataFrame:
    rows = []
    for k, w in enumerate(_weeks(80)):
        tv, search = rng.uniform(0, 100), rng.uniform(0, 40)
        visits = 1000 + 8 * tv + 20 * search + rng.normal(0, 50)
        awareness = 0.2 + 0.002 * tv if k % 4 == 0 else None  # a monthly survey
        rows.append({"Week": w, "Online Sales": 300 + 0.4 * visits + 2 * search,
                     "TV Spend": tv, "Search Spend": search, "Site Visits": visits,
                     "Brand Awareness": awareness})  # fmt: skip
    return pd.DataFrame(rows)


def _multi_kpi_table(rng: np.random.Generator) -> pd.DataFrame:
    rows = []
    for w in _weeks(80):
        tv, search = rng.uniform(0, 100), rng.uniform(0, 40)
        rows.append({"week": w, "sales_premium": 200 + 2 * tv, "sales_basic": 800 + 3 * search,
                     "tv_cost": tv, "search_cost": search})  # fmt: skip
    return pd.DataFrame(rows)


def _forecast_table(rng: np.random.Generator) -> pd.DataFrame:
    return pd.DataFrame({"week": _weeks(80), "orders": 100 + rng.poisson(20, 80)})


def _store_table(rng: np.random.Generator) -> pd.DataFrame:
    rows = []
    for store in ("s1", "s2", "s3", "s4"):
        for w in _weeks(40):
            price = rng.uniform(1, 3)
            rows.append({"week": w, "store_id": store, "units_sold": rng.poisson(50 / price),
                         "price": price, "promo": int(rng.uniform() < 0.2)})  # fmt: skip
    return pd.DataFrame(rows)


def _choice_table(rng: np.random.Generator) -> pd.DataFrame:
    products = ("cola", "fizz", "store")
    asc = np.array([CHOICE_TRUTH["asc"][p] for p in products])
    rows = []
    for h in range(60):
        u = rng.normal(0, 0.5, 3)
        u[0] = 0
        for t in range(1, int(rng.integers(4, 9))):
            price = np.linspace(1, 2, 3) * np.exp(rng.normal(0, 0.15, 3))
            util = asc + CHOICE_TRUTH["price"] * price + u + rng.gumbel(size=3)
            prices = {
                f"price_{p}": round(float(x), 3) for p, x in zip(products, price, strict=True)
            }
            brand = products[int(np.argmax(util))]
            rows.append({"household": f"hh{h:03d}", "trip": t, "brand": brand, **prices})
    return pd.DataFrame(rows)


def _impact_table(rng: np.random.Generator) -> pd.DataFrame:
    search = 50 + rng.normal(0, 5, 80)
    sales = 200 + 2 * search + rng.normal(0, 4, 80) + np.where(np.arange(80) >= 60, 30.0, 0.0)
    return pd.DataFrame({"Week": _weeks(80), "Sales": sales, "Search Index": search,
                         "Benchmark": 100 + rng.normal(0, 3, 80)})  # fmt: skip


def _geo_lift_table(rng: np.random.Generator) -> pd.DataFrame:
    base = 100 + rng.normal(0, 5, (6, 60)).cumsum(axis=1) * 0.2
    rows = []
    for g in range(6):
        for k, w in enumerate(_weeks(60)):
            lift = 8.0 if g == 0 and k >= 48 else 0.0
            rows.append({"week": w, "market": f"m{g:02d}", "sales": base[g, k] + lift})
    return pd.DataFrame(rows)


TABLES = {
    "geo_mmm": _geo_table,
    "mediated_mmm": _mediated_table,
    "multi_kpi_mmm": _multi_kpi_table,
    "weekly_forecast": _forecast_table,
    "store_demand": _store_table,
    "customer_choice": _choice_table,
    "campaign_impact": _impact_table,
    "geo_lift": _geo_lift_table,
}
TEXT_ANSWERS: dict[str, dict[str, Any]] = {
    "campaign_impact": {"start": "2025-03-02"},  # week 60 of _impact_table
    "geo_lift": {"treated": "m00", "start": "2024-12-08", "end": "2025-01-05"},  # weeks 48-52
}
"""Answers to the text questions (role detection does not fill them)."""


def _draft_and_ingest(template_id: str, tmp_path: Path, registry: Registry) -> tuple[Any, Any, Any]:
    path = tmp_path / "upload"
    path.write_text(TABLES[template_id](np.random.default_rng(1)).to_csv(index=False))
    profiles = column_profiles(read_table(path, "csv", IngestLimits()))
    template = GUIDED_TEMPLATES[template_id]
    sug = suggest_roles(template, profiles)
    assert not sug.issues, sug.issues  # every required question is pre-filled
    answers = {q.question_id: q.answer for q in sug.questions}
    answers.update(TEXT_ANSWERS.get(template_id, {}))
    req = GuidedAnswers(
        template_id=template_id, name="guided", dataset="upload", answers=answers,
        columns=profiles,
    )  # fmt: skip
    draft = build_draft(template, req, project="p")
    ds, schema, _ = build_dataset(path, IngestJob(mapping=draft.mapping))
    assert not [i for i in validate_dataset(schema, ds) if i.severity.value == "error"]
    spec = ModelSpec.model_validate(draft.spec)
    result = validate_spec(spec, registry.catalog(), schema)
    assert result.ok, [(i.pointer, i.message) for i in result.errors]
    return spec, ds, schema


@pytest.mark.parametrize("template_id", sorted(TABLES))
def test_a_table_becomes_a_valid_spec(template_id: str, tmp_path: Path, registry: Registry) -> None:
    plugin = GUIDED_TEMPLATES[template_id].plugin
    if (
        plugin.split("@")[0] in PLANNED_MODEL_IDS
        and registry.catalog().resolve_model(plugin) is None
    ):
        pytest.skip(f"{plugin} is a v0.5 plugin that is not installed here")
    spec, _, _ = _draft_and_ingest(template_id, tmp_path, registry)
    if template_id == "campaign_impact":
        assert sorted(spec.model.settings["controls"]) == ["benchmark", "search_index"]
    if template_id == "geo_lift":
        assert spec.data.bind.dims == {"unit": "market", "time": "week"}
        assert spec.model.settings["treated"] == ["m00"]


def test_the_choice_draft_fits(tmp_path: Path, registry: Registry) -> None:
    spec, ds, schema = _draft_and_ingest("customer_choice", tmp_path, registry)
    doc = spec.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    doc["model"]["fit"] = {"mode": "evaluate_then_refit", "draws": 10, "seed": 1}
    bundle = fit_spec(ModelSpec.model_validate(doc), ds, registry=registry, raw_schema=schema)
    beta = bundle.model.posterior["beta"].mean().item()
    assert beta < 0  # price lowers the choice probability
    assert bundle.evaluation is not None  # evaluate, then refit on everything
    # the template's scorecard: the holdout hit rate against always guessing one product
    assert {"accuracy", "accuracy_naive", "log_score", "brier"} <= set(bundle.metrics.values)
    card = fit_scorecard(bundle, ds)
    [item] = card.items
    assert item.check == "holdout.hit_rate" and item.message_plain
