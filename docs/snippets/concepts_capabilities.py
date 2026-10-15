"""Concepts: read a model's manifest, the static JSON the control plane relies on."""

# --8<-- [start:manifest]
from mapi_sdk import default_registry

registry = default_registry()
manifest = registry.model("mmm.hierarchical_geo@^1").manifest()
caps = manifest.capabilities

print(caps.representation.value, caps.inference)  # draws mcmc
print(sorted(caps.score_modes))
# ['counterfactual', 'evaluate', 'forecast', 'predict', 'simulate']
print(caps.decision, caps.contribution_scale)  # True additive
print([d.name for d in manifest.derived])  # ['contributions', 'response_curves', 'marginal_cost']

basic = [k for k, v in manifest.settings_schema["properties"].items() if v.get("x-ui-level") == "basic"]
print(basic)  # ['pooling', 'adstock', 'saturation']: what Guided mode shows

adapter = registry.model("mmm.pymc_marketing@^1").manifest().capabilities
print(sorted(adapter.owns))  # ['carryover', 'inverse_units', 'scaling']
# --8<-- [end:manifest]
assert caps.decision and "scaling" in adapter.owns
