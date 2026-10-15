"""Concepts: representations decide which summaries and aggregations are honest."""

# --8<-- [start:rules]
from mapi_spec import Representation, can_aggregate, default_summary, summary_allowed

for rep in Representation:
    verdict = can_aggregate(rep, ["store"], how="sum")
    print(f"{rep.value:10} sum over stores: {verdict.allowed!s:5} default summary: {default_summary(rep)}")
# draws      sum over stores: True  default summary: ['mean', 'hdi_90']
# quantiles  sum over stores: False default summary: ['median', 'q5', 'q95']
# interval   sum over stores: False default summary: ['point', 'interval']
# point      sum over stores: True  default summary: ['point']
# moments    sum over stores: False default summary: ['mean', 'sd']

print(can_aggregate(Representation.POINT, ["store"]).flagged)  # True: allowed, but labelled
print(can_aggregate(Representation.QUANTILES, ["store"]).reason)
# Quantiles are marginal per cell; summing quantiles is not a quantile of the sum.
print(summary_allowed(Representation.QUANTILES, "hdi_90"))  # False: no draws to find an HDI
print(summary_allowed(Representation.DRAWS, "q97_5"))  # True
# --8<-- [end:rules]
assert not can_aggregate(Representation.INTERVAL, ["store"]).allowed
assert can_aggregate(Representation.DRAWS, ["store"]).allowed
