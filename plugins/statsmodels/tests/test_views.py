"""Model Explorer sections built from statsmodels bundles validate as spec views."""

from __future__ import annotations

import json

import numpy as np
import pytest
from mapi_plugin_statsmodels import PLUGIN_IDS, PLUGINS
from mapi_plugin_statsmodels.views import explorer_views
from mapi_sdk.bundle import Bundle
from mapi_spec.stattests import StatTestResult
from mapi_spec.views import (
    MODEL_SECTIONS,
    DecompositionView,
    FevdView,
    IrfView,
    ModelDiagnosticsView,
    ParamTableView,
    ResidualDiagnosticsView,
)

BY_ID = {p.id: p for p in PLUGINS}


@pytest.mark.parametrize("model_id", PLUGIN_IDS)
def test_every_fit_fills_the_core_sections(model_id: str, bundles: dict[str, Bundle]) -> None:
    fitted = bundles[model_id].model
    views = BY_ID[model_id]().explorer_views(fitted)
    assert {"params", "diagnostics", "residuals"} <= set(views) <= set(MODEL_SECTIONS)
    for name, view in views.items():
        doc = json.loads(view.model_dump_json())
        type(view).model_validate(doc)  # the JSON round-trips through the spec DTO
        assert doc["format"].startswith("mapi.model-explorer."), name
    params = views["params"]
    assert isinstance(params, ParamTableView) and params.rows
    assert all(r.p_value is None or 0 <= r.p_value <= 1 for r in params.rows)
    resid = views["residuals"]
    assert isinstance(resid, ResidualDiagnosticsView)
    assert {t.test for t in resid.tests} <= {"test.ljung_box", "test.jarque_bera", "test.arch_lm"}
    assert resid.tests and resid.series and resid.qq is not None


def test_panel_rows_carry_their_facet(bundles: dict[str, Bundle]) -> None:
    views = explorer_views(bundles["ts.sarimax"].model)
    params = views["params"]
    assert isinstance(params, ParamTableView)
    assert {r.coords.get("geo") for r in params.rows} == {"north", "south"}
    resid = views["residuals"]
    assert isinstance(resid, ResidualDiagnosticsView)
    lb = next(t for t in resid.tests if t.test == "test.ljung_box")
    assert len(lb.per_series) == 2 and lb.reject_share is not None


def test_var_views_have_irf_fevd_and_granger(bundles: dict[str, Bundle]) -> None:
    fitted = bundles["ts.var"].model
    views = explorer_views(fitted)
    irf, fevd = views["irf"], views["fevd"]
    assert isinstance(irf, IrfView) and isinstance(fevd, FevdView)
    assert irf.variables == ["realgdp", "realcons", "realinv"]
    assert irf.lower is not None and irf.horizons == 12
    stored = fitted.groups["irf"]["irf"].transpose("response", "shock", "horizon").values
    np.testing.assert_allclose(np.asarray(irf.values), stored)
    diag = views["diagnostics"]
    assert isinstance(diag, ModelDiagnosticsView)
    tests = [StatTestResult.model_validate(i) for i in diag.items if i.get("kind") == "test"]
    assert len([t for t in tests if t.test == "test.granger"]) == 6


def test_vecm_diagnostics_include_the_johansen_test(bundles: dict[str, Bundle]) -> None:
    views = explorer_views(bundles["ts.vecm"].model)
    diag = views["diagnostics"]
    assert isinstance(diag, ModelDiagnosticsView)
    johansen = next(i for i in diag.items if i.get("test") == "test.johansen")
    assert johansen["decision"] == "cointegrated" and johansen["details"]["rank"] == 1
    assert "irf" in views and "fevd" in views


def test_ecm_diagnostics_include_the_bounds_test(bundles: dict[str, Bundle]) -> None:
    diag = explorer_views(bundles["ts.ecm"].model)["diagnostics"]
    assert isinstance(diag, ModelDiagnosticsView)
    bounds = next(i for i in diag.items if i.get("kind") == "test.pss_bounds")
    assert bounds["case"] == 3 and bounds["speed_of_adjustment"] < 0


def test_unobserved_components_show_a_decomposition(bundles: dict[str, Bundle]) -> None:
    views = explorer_views(bundles["ts.unobserved_components"].model)
    dec = views["decomposition"]
    assert isinstance(dec, DecompositionView)
    assert "level" in dec.components and dec.warnings_plain  # a panel: first series shown
