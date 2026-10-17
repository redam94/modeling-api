"""Concepts: read a model's manifest, the static JSON the control plane relies on."""

# --8<-- [start:manifest]
from mapi_sdk import default_registry

registry = default_registry()
manifest = registry.model("mmm.bayesian@^1").manifest()
caps = manifest.capabilities

print(caps.representation.value, caps.inference)  # draws mcmc
print(sorted(caps.score_modes))
# ['counterfactual', 'evaluate', 'forecast', 'predict']
print(caps.decision, caps.contribution_scale)  # True additive
print(sorted(caps.owns))  # ['carryover', 'inverse_units', 'scaling']: mmm-framework owns them
print([d.name for d in manifest.derived])  # ['contributions', 'response_curves', 'marginal_cost']

basic = [k for k, v in manifest.settings_schema["properties"].items() if v.get("x-ui-level") == "basic"]
print(basic)  # ['adstock', 'saturation', 'trend', 'seasonality', 'hierarchy']: what Guided shows
print(manifest.about.title if manifest.about else None)  # Marketing mix model
# --8<-- [end:manifest]
assert caps.decision and "scaling" in caps.owns
assert basic == ["adstock", "saturation", "trend", "seasonality", "hierarchy"], basic
