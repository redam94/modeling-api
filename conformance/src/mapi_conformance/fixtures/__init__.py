"""The conformance corpus: one sub-package per fixture (pymc-challenges style, plan §13).

A fixture ``fixtures/<id>/`` holds:

``data.py``
    ``generate(seed=0, size="tiny") -> (xr.Dataset, {side_table_name: xr.Dataset})``.
    Optional: ``schema() -> DataSchema`` (the raw schema; otherwise ``attrs["mapi_schema"]``
    of the generated data, otherwise inferred) and payload builders
    ``payload_<name>(data, seed, size) -> xr.Dataset`` referenced by score requests.
``spec.yaml``
    The ModelSpec (dataset names bound to the plugin's canonical names).
``score_requests.yaml``
    A list of ``{name, request, payload, expect, expect_error}`` entries (see
    :mod:`mapi_conformance.runner`); ``request`` may omit ``apiVersion``/``kind``/``model``.
``expectations.py`` (optional)
    ``TINY_FIT``/``TINY_SETTINGS`` (fit and model-settings overrides at size tiny),
    ``MODEL_CHECKS`` (the ``check_model`` checks to run; default all),
    ``registry(base) -> Registry`` (extra plugins), ``check_fit(bundle, data)``,
    ``check_score(name, tree, bundle, data)`` and ``check_evaluate(tree, bundle, data)``
    raising ``AssertionError`` on failure.

Register a fixture by listing its id below. Plan §13 corpus entries that are not ported yet
are listed in :data:`DEFERRED` with the reason, so the gap is explicit (a test keeps the
plan's list, the fixtures and the deferrals in step).
"""

FIXTURES: list[str] = [
    "e02",
    "c08",
    "e24",
    "xgb_store_demand",
    "sklearn_store_demand",
    "e33",
    "mmm_frequentist",
    "e84_lite",
    "guided_mmm",
    "sm_arimax",
    "sm_vecm_macro",
    "e07_lgcp",
    "e18_areal",
    "e50_kpp",
    "e51_heat_plate",
    "e60_traction",
    "e80_lens",
    "e61_turing",
    "e58_storm",
    "e31_ibp",
    "e77_colour_naming",
    "e79_cmb",
    "e21_network",
]

DEFERRED: dict[str, str] = {
    "E13": (
        "state-space model on pymc_extras: needs a state-space plugin with the 'filter' score "
        "mode and loo_valid=False, which no built-in plugin provides in M1 (vetted-plugin "
        "ports, M4)"
    ),
    "E82-slim": (
        "GP Bayesian optimisation: needs the 'recommend' mode and plugin-owned Campaign runs, "
        "which the plan schedules with Campaign loops (M6)"
    ),
    "E45": (
        "not ported: no built-in M1 plugin implements its model, so it waits for the "
        "vetted-plugin ports (M4)"
    ),
}
"""Plan §13 corpus entries without a fixture yet, with why and until when."""
