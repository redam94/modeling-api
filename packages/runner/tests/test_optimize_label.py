"""``label_result``: the KPI of a budget plan gets the dataset name of the target, unless the
plugin plans on something else and says so (``mmm.multivariate``'s weighted total)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from mapi_runner.worker.optimize import label_result
from mapi_spec.optimize import OptimizeResult


def _bundle() -> Any:
    stage = SimpleNamespace(
        vars={"y": SimpleNamespace(units="EUR"), "spend": SimpleNamespace(units="EUR")}
    )
    return SimpleNamespace(
        model_schema=SimpleNamespace(targets=["y"]),
        binding=SimpleNamespace(to_dataset=lambda name: {"y": "revenue", "spend": "spend"}[name]),
        resolved=SimpleNamespace(stages={"model": stage}, model_stage="model"),
    )


def _result(kpi: str) -> OptimizeResult:
    example = OptimizeResult.model_json_schema()["examples"][0]
    return OptimizeResult.model_validate({**example, "kpi": kpi, "kpi_units": None})


def test_the_target_gets_its_dataset_name_and_units() -> None:
    out = label_result(_result("y"), _bundle())
    assert (out.kpi, out.kpi_units, out.spend_units) == ("revenue", "EUR", "EUR")
    assert out.computed_at is not None


def test_a_plugin_label_is_kept_without_units() -> None:
    out = label_result(_result("weighted total of the outcomes"), _bundle())
    assert (out.kpi, out.kpi_units, out.spend_units) == (
        "weighted total of the outcomes",
        None,
        "EUR",
    )
