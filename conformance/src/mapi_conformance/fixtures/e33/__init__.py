"""E33: hierarchical geo marketing-mix model (``mmm.hierarchical_geo``).

Daily local-currency spend for markets x channels (one market in GBP), an FX side table
(``score_policy: supplied``) and a holidays table (``pinned``); the pipeline converts to
EUR, resamples to W-SUN weeks (the split stage), mean-scales, builds the lag tensor and
adds Fourier/holiday controls. Score requests: an 8-week forecast, a new market, a weekly
EUR plan entering at the ``weekly`` stage and a counterfactual intervention.
"""
