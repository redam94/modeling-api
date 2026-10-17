"""Guided MMM with business priors and a lift test (``mmm.bayesian`` on mmm-framework).

The spec is the ``geo_mmm`` Guided template's draft for a weekly region x channel table
(revenue, spend per channel, a price control), exactly as ``POST /v1/guided/drafts`` writes
it, plus the ``calibration`` block a Guided user enters: ROI and contribution-share ranges,
a baseline share and one lift test. The fit translates the business priors into ROI
priors (no ``translate_priors`` run is recorded) and conditions on the lift test through
an mmm-framework experiment likelihood; the expectations check both against the
simulation's truth, and the Guided scorecard.
"""
