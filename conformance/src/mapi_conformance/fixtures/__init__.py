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

Register a fixture by listing its id below.
"""

FIXTURES: list[str] = [
    "e02",
    "c08",
    "e24",
    "xgb_store_demand",
]
