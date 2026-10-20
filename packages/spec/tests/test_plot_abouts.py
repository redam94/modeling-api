"""The plot About cards stand on their own: complete, related only to other plot cards.

``mapi_plots`` checks the cards against its recipes (``test_plot_catalog.py``); this checks
what the spec package alone can, including the v0.10 classifier, training and LOO cards."""

from __future__ import annotations

import pytest
from mapi_spec.about import about_issues
from mapi_spec.plot_abouts import PLOT_ABOUTS

pytestmark = pytest.mark.unit

V010_CARDS = ("core.roc_pr", "core.confusion", "core.loss_curve", "core.loo_pit")


@pytest.mark.parametrize("plot_id", sorted(PLOT_ABOUTS))
def test_every_plot_card_is_complete_and_relates_to_plot_cards(plot_id: str) -> None:
    assert not about_issues(PLOT_ABOUTS[plot_id], known_ids=PLOT_ABOUTS)


@pytest.mark.parametrize("plot_id", V010_CARDS)
def test_the_v010_cards_cite_their_method_and_name_their_settings(plot_id: str) -> None:
    about = PLOT_ABOUTS[plot_id]
    assert about.references and all(r.url for r in about.references)
    assert about.expert_notes and "`" in about.expert_notes


def test_changed_cards_describe_the_new_inputs() -> None:
    calibration = " ".join(PLOT_ABOUTS["core.calibration"].data_needs)
    assert "one predicted probability" in calibration
    state = PLOT_ABOUTS["core.state_sequence"].expert_notes or ""
    for word in ("hidden_states", "regimes", "summary", "state_dim='auto'", "select"):
        assert word in state
    assert "decomposition='exclude'" in (PLOT_ABOUTS["core.decomposition"].expert_notes or "")


V012_CARDS = (
    "mmm.roi",
    "mmm.adstock",
    "mmm.waterfall",
    "mmm.spend_effect",
    "mmm.lift_tests",
    "mmm.effectiveness",
    "mmm.mediation",
    "mmm.cross_effects",
    "core.energy",
    "core.prior_posterior",
)


@pytest.mark.parametrize("plot_id", V012_CARDS)
def test_the_v012_cards_name_their_settings_and_relate_to_plots(plot_id: str) -> None:
    about = PLOT_ABOUTS[plot_id]
    assert about.references and about.related
    assert set(about.related) <= set(PLOT_ABOUTS)
    notes = about.expert_notes or ""
    assert "`hdi_prob`" in notes if plot_id.startswith("mmm.") else "`" in notes


def test_the_outcome_selection_is_described_where_it_applies() -> None:
    assert "`outcome`" in (PLOT_ABOUTS["mmm.waterfall"].expert_notes or "")
    assert "main outcome" in " ".join(PLOT_ABOUTS["mmm.waterfall"].assumptions)
    assert "`breakeven`" in (PLOT_ABOUTS["mmm.roi"].expert_notes or "")
    assert "`bfmi_threshold`" in (PLOT_ABOUTS["core.energy"].expert_notes or "")


CAUSAL_CARDS = ("causal.impact", "causal.pre_fit", "causal.donor_weights", "causal.event_study")


@pytest.mark.parametrize("plot_id", CAUSAL_CARDS)
def test_the_causal_cards_fit_measure_impact_and_cite_their_method(plot_id: str) -> None:
    about = PLOT_ABOUTS[plot_id]
    assert about.fits == {"goal": ["measure_impact"]}
    assert about.references and all(r.url for r in about.references)
    assert set(about.related) <= set(PLOT_ABOUTS) and about.related
    assert "`hdi_prob`" in (about.expert_notes or "")


def test_the_impact_card_names_its_unit_and_panel_settings() -> None:
    notes = PLOT_ABOUTS["causal.impact"].expert_notes or ""
    for word in ("`unit`", "`panels`", "`post", "`intervention_start`"):
        assert word in notes
    assert "staggered" in (PLOT_ABOUTS["causal.event_study"].expert_notes or "")
