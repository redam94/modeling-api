"""E33: hierarchical geo marketing-mix model on mmm-framework (``mmm.bayesian``).

Daily local-currency spend for markets x channels (one market in GBP), an FX side table
(``score_policy: supplied``) and a holidays table (``pinned``); the pipeline converts to
EUR, resamples to W-SUN weeks (the split stage) and adds the holiday control - the model
owns scaling and carry-over. Score requests: an 8-week forecast, a new market (drawn from
the fitted geo spread), a weekly EUR plan entering at the ``weekly`` stage and a
counterfactual intervention.
"""
