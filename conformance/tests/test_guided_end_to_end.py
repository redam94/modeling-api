"""The Guided data path end to end, in process: a table → its column profiles (as the
sandboxed ingest writes them) → role detection → a draft → ingest with the draft's mapping
→ static validation against the real catalog → a fit.

One synthetic table per template, the BayesInsight import on a slice of the BayesInsight
example data, and fits for the choice model, the imported geo MMM and the dedicated
mmm.bayesinsight import (tiny settings)."""

from __future__ import annotations

import json
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
from mapi_spec.bayesinsight import import_bayesinsight
from mapi_spec.guided import (
    GUIDED_TEMPLATES,
    BayesInsightImport,
    GuidedAnswers,
    build_draft,
    suggest_roles,
)
from mapi_spec.ingest import BayesInsightMetadata, BayesInsightModelDef
from mapi_spec.spec import ModelSpec
from mapi_spec.validate import validate_spec

pytestmark = [pytest.mark.conformance, pytest.mark.slow]

ROOT = Path(__file__).resolve().parents[2]
BI_FIXTURES = ROOT / "packages" / "spec" / "tests" / "fixtures" / "bayesinsight"
BI_MFF = ROOT / "packages" / "runner" / "tests" / "fixtures" / "bayesinsight_mff.csv"


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


TABLES = {
    "geo_mmm": _geo_table,
    "weekly_forecast": _forecast_table,
    "store_demand": _store_table,
    "customer_choice": _choice_table,
}


def _draft_and_ingest(template_id: str, tmp_path: Path, registry: Registry) -> tuple[Any, Any, Any]:
    path = tmp_path / "upload"
    path.write_text(TABLES[template_id](np.random.default_rng(1)).to_csv(index=False))
    profiles = column_profiles(read_table(path, "csv", IngestLimits()))
    template = GUIDED_TEMPLATES[template_id]
    sug = suggest_roles(template, profiles)
    assert not sug.issues, sug.issues  # every required question is pre-filled
    answers = {q.question_id: q.answer for q in sug.questions}
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
    _draft_and_ingest(template_id, tmp_path, registry)


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


def test_the_imported_bayesinsight_model_fits(registry: Registry) -> None:
    md = json.loads((BI_FIXTURES / "complex_model_def.json").read_text())
    extra = dict(md["variable_details"][2], variable_name="media_var_1_Total_Total_Total")
    md["variable_details"].append(extra)
    meta = json.loads((BI_FIXTURES / "complex_metadata.json").read_text())
    req = BayesInsightImport(
        name="bi",
        dataset="bi",
        model_def=BayesInsightModelDef.model_validate(md),
        metadata=BayesInsightMetadata.model_validate(meta),
    )
    draft = import_bayesinsight(req, project="p")
    ds, schema, _ = build_dataset(BI_MFF, IngestJob(mapping=draft.mapping))
    spec = ModelSpec.model_validate(draft.spec)
    result = validate_spec(spec, registry.catalog(), schema)
    assert result.ok, [(i.pointer, i.message) for i in result.errors]
    # BayesInsight's example media are standardised noise: make them spend-like to fit
    ds["spend"] = abs(ds["spend"])
    ds["stock"] = ds["stock"] + 1.0
    doc = spec.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    doc["model"]["fit"] = {"mode": "train_only", "tune": 30, "draws": 30, "chains": 1,
                           "seed": 1, "engine": "pymc"}  # fmt: skip
    bundle = fit_spec(ModelSpec.model_validate(doc), ds, registry=registry, raw_schema=schema)
    post = bundle.model.posterior
    assert post["alpha"].sizes["channel"] == 2  # delayed adstock per channel, as imported
    assert "half" in post and "slope" in post  # Hill saturation


def test_the_dedicated_bayesinsight_plugin_fits_the_import(registry: Registry) -> None:
    """``target: mmm.bayesinsight``: BayesInsight's own model (here a Poisson KPI with
    random effects per geography and product) runs as model_def.json defines it."""
    md = json.loads((BI_FIXTURES / "complex_model_def.json").read_text())
    extra = dict(md["variable_details"][2], variable_name="media_var_1_Total_Total_Total",
                 adstock="weibull", media_transform="sshaped")  # fmt: skip
    md["variable_details"].append(extra)
    meta = json.loads((BI_FIXTURES / "complex_metadata.json").read_text())
    req = BayesInsightImport(
        name="bi",
        dataset="bi",
        model_def=BayesInsightModelDef.model_validate(md),
        metadata=BayesInsightMetadata.model_validate(meta),
        target="mmm.bayesinsight",
    )
    draft = import_bayesinsight(req, project="p")
    assert not [i for i in draft.issues if i.severity.value == "error"]
    ds, schema, _ = build_dataset(BI_MFF, IngestJob(mapping=draft.mapping))
    assert set(ds.dims) == {"date", "geo", "product"}  # every row id is a panel dim
    spec = ModelSpec.model_validate(draft.spec)
    result = validate_spec(spec, registry.catalog(), schema)
    assert result.ok, [(i.pointer, i.message) for i in result.errors]
    for name in ("media_var_0", "media_var_1"):  # standardised noise: make it spend-like
        ds[name] = abs(ds[name])
    doc = spec.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    doc["model"]["fit"] = {"mode": "evaluate_then_refit", "tune": 30, "draws": 30,
                           "chains": 2, "seed": 1, "engine": "pymc"}  # fmt: skip
    bundle = fit_spec(ModelSpec.model_validate(doc), ds, registry=registry, raw_schema=schema)
    post = bundle.model.posterior
    assert post["intercept__coef_z"].sizes == {"chain": 2, "draw": 30, "geo": 2, "product": 2}
    assert "media_var_1__w_lam" in post and "media_var_1__s_alpha" in post
    assert bundle.metrics.values.get("mape") is not None
    card = fit_scorecard(bundle, ds)
    assert {i.check for i in card.items} >= {"convergence.rhat", "holdout.mape", "data.history"}
