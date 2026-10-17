"""Concepts: stages, the split stage and static validation (no data is touched)."""

# --8<-- [start:stages]
from mapi_plugin_mmm.examples import E33_SPEC_YAML, e33_dataset, e33_raw_schema
from mapi_sdk import default_registry
from mapi_spec import load_model_spec, validate_spec

catalog = default_registry().catalog()  # static JSON: settings schemas + schema effects
data, _ = e33_dataset(weeks=12)
raw_schema = e33_raw_schema(data)

check = validate_spec(load_model_spec(E33_SPEC_YAML), catalog, raw_schema)
rs = check.resolved
for stage, schema in rs.stages.items():
    y, time = schema.vars["y"], schema.dims["time"]
    print(f"{stage:7} time={time.freq:6} y in {y.units:4} vars={', '.join(schema.vars)}")
# raw     time=D      y in LCU  vars=y, spend
# to_eur  time=D      y in EUR  vars=y, spend
# weekly  time=W-SUN  y in EUR  vars=y, spend
# hols    time=W-SUN  y in EUR  vars=y, spend, holiday
print(rs.split_stage)  # weekly: the holdout is cut in whole weeks
print(rs.last_reshape_stage)  # weekly: outputs are reported in weekly EUR, not days
# --8<-- [end:stages]
assert check.ok, check.errors
assert rs.split_stage == "weekly" and rs.last_reshape_stage == "weekly"

# --8<-- [start:leakage]
# standardise the holiday control, then cut the holdout only after that step
leaky = E33_SPEC_YAML.replace("after_step: weekly", "after_step: std_hol").replace(
    "\nmodel:\n",
    "\n  - {id: std_hol, use: core.standardize@^1, with: {vars: [holiday], over: [time]}}\n"
    "\nmodel:\n",
    1,
)
for issue in validate_spec(load_model_spec(leaky), catalog, raw_schema).errors:
    print(issue.code, issue.pointer)
    print("  ", issue.message)
# LEAKAGE_RISK /data/split/after_step
#    stateful step 'std_hol' (pipeline[3]) runs before the split at stage 'std_hol';
#    its state would be learned from holdout data
# --8<-- [end:leakage]
codes = {i.code for i in validate_spec(load_model_spec(leaky), catalog, raw_schema).errors}
assert "LEAKAGE_RISK" in codes
