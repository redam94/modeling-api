"""pymc-marketing MMM adapter (``mmm.pymc_marketing``), the plan's M0 ``owns`` fixture.

The same simulated EUR media data as E33, but the model owns scaling, carry-over and the
inverse to original units: the pipeline only converts currencies, resamples to weeks and
adds holidays. Score requests: a 4-week forecast, a recent window and the full series
(carry-over equivalence) and a counterfactual intervention.
"""
