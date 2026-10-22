"""v0.18 prep presets (WP2): slots, id prefixes, formula caps, settings and effect checks."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from mapi_spec.catalog import Catalog
from mapi_spec.errors import ErrorCode, Issue, Severity, has_errors
from mapi_spec.library import PrepPresetDoc, parse_library_doc
from mapi_spec.prep_presets import MAX_EXPR_CHARS, apply_prep_preset
from mapi_spec.schema import DataSchema

pytestmark = pytest.mark.unit

PRESET: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "PrepPreset",
    "metadata": {"name": "media-prep", "title": "Media prep"},
    "slots": {
        "media": {"kind": "var_list", "label": "Media spend", "role": "media"},
        "price": {"kind": "var", "label": "Price"},
        "time": {"kind": "dim", "label": "Time"},
        "extra": {"kind": "var", "label": "Optional extra", "required": False},
    },
    "steps": [
        {
            "id": "carry",
            "use": "core.adstock@^1",
            "with": {
                "vars": "$slot:media",
                "along": "$slot:time",
                "kind": "geometric",
                "alpha": 0.5,
            },
        },
        {
            "id": "scale",
            "use": "core.max_abs_scale@^1",
            "with": {"vars": ["$slot:media", "$slot:extra", "$slot:price"], "over": ["$slot:time"]},
        },
        {"id": "logp", "use": "core.log1p@^1", "with": {"vars": ["$slot:price"]}},
    ],
}

SCHEMA = DataSchema.model_validate(
    {
        "dims": {"week": {"kind": "time", "policy": "extend"}},
        "vars": {
            "tv": {"dims": ["week"], "dtype": "float64", "role": "media"},
            "search": {"dims": ["week"], "dtype": "float64", "role": "media"},
            "price": {"dims": ["week"], "dtype": "float64"},
            "promo": {"dims": ["week"], "dtype": "float64"},
            "sales": {"dims": ["week"], "dtype": "float64", "role": "target"},
        },
    }
)
SLOTS: dict[str, Any] = {"media": ["tv", "search"], "price": "price", "time": "week"}


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    registry = pytest.importorskip("mapi_sdk.registry")
    return registry.default_registry().catalog()  # type: ignore[no-any-return]


def _doc(raw: dict[str, Any] | None = None) -> PrepPresetDoc:
    doc = parse_library_doc(copy.deepcopy(raw or PRESET))
    assert isinstance(doc, PrepPresetDoc)
    return doc


def _errors(issues: list[Issue]) -> list[tuple[str, str]]:
    return [(i.pointer, i.message) for i in issues if i.severity is Severity.ERROR]


def _dump(steps: Any) -> list[dict[str, Any]]:
    return [s.model_dump(mode="json", by_alias=True, exclude_defaults=True) for s in steps]


def test_slots_fill_whole_values_and_splice_lists(catalog: Catalog) -> None:
    steps, issues = apply_prep_preset(_doc(), SLOTS, schema=SCHEMA, catalog=catalog)
    assert _errors(issues) == []
    assert _dump(steps) == [
        {
            "id": "p_carry",
            "use": "core.adstock@^1",
            "with": {"vars": ["tv", "search"], "along": "week", "kind": "geometric", "alpha": 0.5},
        },
        {
            "id": "p_scale",
            "use": "core.max_abs_scale@^1",
            "with": {"vars": ["tv", "search", "price"], "over": ["week"]},
        },
        {"id": "p_logp", "use": "core.log1p@^1", "with": {"vars": ["price"]}},
    ]


def test_optional_slots_drop_out_and_filled_ones_appear() -> None:
    steps, issues = apply_prep_preset(
        _doc(), {**SLOTS, "extra": "promo"}, schema=None, id_prefix="prep2"
    )
    assert not has_errors(issues)
    assert [s.id for s in steps] == ["prep2_carry", "prep2_scale", "prep2_logp"]
    assert steps[1].with_["vars"] == ["tv", "search", "promo", "price"]


def test_whole_value_optional_slot_removes_the_key() -> None:
    raw = copy.deepcopy(PRESET)
    raw["steps"][1]["with"]["fill_value"] = "$slot:extra"
    steps, issues = apply_prep_preset(_doc(raw), SLOTS, schema=None)
    assert not has_errors(issues)
    assert "fill_value" not in steps[1].with_


def test_unfilled_required_slots_are_refused() -> None:
    _, issues = apply_prep_preset(_doc(), {"media": ["tv"]}, schema=None)
    errors = _errors(issues)
    assert ("/slots/price", "Slot 'price' (Price) needs a value.") in errors
    assert ("/slots/time", "Slot 'time' (Time) needs a value.") in errors
    assert all(i.code is ErrorCode.LIBRARY_INVALID for i in issues if i.is_error)


@pytest.mark.parametrize(
    ("slots", "pointer", "message"),
    [
        ({**SLOTS, "price": "cost"}, "/slots/price", "The dataset has no variable 'cost'."),
        (
            {**SLOTS, "media": ["tv", "radio"]},
            "/slots/media",
            "The dataset has no variable 'radio'.",
        ),
        ({**SLOTS, "time": "day"}, "/slots/time", "The dataset has no dim 'day'."),
        ({**SLOTS, "price": ["price"]}, "/slots/price", "Slot 'price' needs a variable name."),
        (
            {**SLOTS, "media": []},
            "/slots/media",
            "Slot 'media' needs a list of 1 to 256 variable names.",
        ),
        (
            {**SLOTS, "media": ["tv", "tv"]},
            "/slots/media",
            "Slot 'media' names a variable more than once.",
        ),
        ({**SLOTS, "time": 3}, "/slots/time", "Slot 'time' needs a dim name."),
        (
            {**SLOTS, "media": ["not a name!"]},
            "/slots/media",
            "Slot 'media' has a value that is not a valid name.",
        ),
        ({**SLOTS, "bogus": "tv"}, "/slots/bogus", "The preset has no slot 'bogus'."),
    ],
)
def test_wrong_slot_values_are_refused(slots: dict[str, Any], pointer: str, message: str) -> None:
    _, issues = apply_prep_preset(_doc(), slots, schema=SCHEMA)
    assert (pointer, message) in _errors(issues)


def test_a_single_name_fills_a_list_slot_and_roles_only_warn() -> None:
    steps, issues = apply_prep_preset(_doc(), {**SLOTS, "media": "promo"}, schema=SCHEMA)
    assert not has_errors(issues)
    assert steps[0].with_["vars"] == ["promo"]
    warnings = [(i.pointer, i.message) for i in issues if i.severity is Severity.WARNING]
    assert warnings == [
        ("/slots/media", "Variable 'promo' does not have the role 'media' that the slot expects.")
    ]


def test_slot_references_must_exist_and_be_whole_values() -> None:
    raw = copy.deepcopy(PRESET)
    raw["steps"][2]["with"]["vars"] = ["$slot:nope"]
    raw["steps"][0]["with"]["kind"] = "prefix $slot:time"
    _, issues = apply_prep_preset(_doc(raw), SLOTS, schema=None)
    errors = _errors(issues)
    assert ("/steps/2/with/vars/0", "The preset has no slot 'nope'.") in errors
    assert ("/steps/0/with/kind", "A slot must be the whole value, not a part of a text.") in errors


def test_unused_slots_warn() -> None:
    raw = copy.deepcopy(PRESET)
    raw["slots"]["spare"] = {"kind": "var", "label": "Spare", "required": False}
    _, issues = apply_prep_preset(_doc(raw), SLOTS, schema=None)
    assert [(i.pointer, i.severity) for i in issues] == [("/slots/spare", Severity.WARNING)]


def test_id_prefix_rules() -> None:
    with pytest.raises(ValueError, match="invalid step id prefix"):
        apply_prep_preset(_doc(), SLOTS, schema=None, id_prefix="Bad-Prefix")
    raw = copy.deepcopy(PRESET)
    raw["steps"][2]["id"] = "x" * 62
    steps, issues = apply_prep_preset(_doc(raw), SLOTS, schema=None)
    assert ("/steps/2/id", f"The step id {'x' * 62!r} is too long to prefix.") in _errors(issues)
    assert len(steps[2].id) == 63


def _formula(expr: str) -> PrepPresetDoc:
    return _doc(
        {
            "apiVersion": "mapi/v1",
            "kind": "PrepPreset",
            "metadata": {"name": "ratio", "title": "Price ratio"},
            "slots": {"price": {"kind": "var", "label": "Price"}},
            "steps": [
                {
                    "id": "ratio",
                    "use": "core.formula@^1",
                    "with": {"out": "price_ratio", "expr": expr, "dims": ["week"]},
                },
                {"id": "logr", "use": "core.log1p@^1", "with": {"vars": ["price_ratio"]}},
            ],
        }
    )


def test_formula_steps_get_caps_only_checks(catalog: Catalog) -> None:
    steps, issues = apply_prep_preset(
        _formula("price / mean(price)"), {"price": "price"}, schema=SCHEMA, catalog=catalog
    )
    assert _errors(issues) == []
    assert [s.id for s in steps] == ["p_ratio", "p_logr"]
    _, issues = apply_prep_preset(
        _formula("x" * (MAX_EXPR_CHARS + 1)), {"price": "price"}, schema=None
    )
    assert _errors(issues) == [
        ("/steps/0/with/expr", "The formula is longer than 4000 characters.")
    ]
    _, issues = apply_prep_preset(_formula("price × 2"), {"price": "price"}, schema=None)
    assert _errors(issues) == [
        ("/steps/0/with/expr", "The formula can contain only printable ASCII characters.")
    ]
    # no grammar in the control plane: a nonsense formula passes the caps (the sandbox parses)
    _, issues = apply_prep_preset(_formula("price +* ("), {"price": "price"}, schema=None)
    assert _errors(issues) == []


def test_catalog_checks_steps_and_settings(catalog: Catalog) -> None:
    raw = copy.deepcopy(PRESET)
    raw["steps"][0]["with"]["kind"] = "boxcar"
    raw["steps"].append({"id": "mystery", "use": "acme.mystery@^1", "with": {}})
    _, issues = apply_prep_preset(_doc(raw), SLOTS, schema=SCHEMA, catalog=catalog)
    by_ptr = {i.pointer: i for i in issues if i.is_error}
    assert by_ptr["/steps/0/with/kind"].message == "value is not one of the allowed options"
    assert by_ptr["/steps/0/with/kind"].code is ErrorCode.LIBRARY_INVALID
    assert by_ptr["/steps/3/use"].code is ErrorCode.EXTENSION_UNAVAILABLE


def test_the_effect_chain_is_checked(catalog: Catalog) -> None:
    raw = copy.deepcopy(PRESET)
    raw["steps"][2]["with"]["vars"] = ["price_index"]  # nothing makes it
    _, issues = apply_prep_preset(_doc(raw), SLOTS, schema=SCHEMA, catalog=catalog)
    errors = _errors(issues)
    assert errors and errors[0][0] == "/steps/2"
    assert "price_index" in errors[0][1]
    # a formula output feeds a later step: the static effects know it
    _, issues = apply_prep_preset(
        _formula("price * 2"), {"price": "price"}, schema=SCHEMA, catalog=catalog
    )
    assert _errors(issues) == []
