"""mmm-framework's fast frequentist MMM (``mmm.frequentist``), the corpus's ``owns`` fixture.

The same simulated EUR media data as E33, fitted by mmm-framework's ridge estimator with
bootstrap intervals. The model owns scaling, carry-over and the inverse to original units:
the pipeline only converts currencies, resamples to weeks and adds holidays. Score
requests: a 4-week forecast, a recent window and the full series (carry-over equivalence)
and a counterfactual intervention. (Replaces the v0.4 pymc-marketing adapter fixture.)
"""
