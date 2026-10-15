from __future__ import annotations

from typing import Any

import pytest
from mapi_spec.schema import (
    DataSchema,
    DataSchemaTemplate,
    DimPolicy,
    DimSpec,
    RaggedSpec,
    Role,
    VarSpec,
    policy_is_at_least_as_tight,
    tighten_policy,
)
from mapi_spec.template import TemplateError
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def test_dimspec_rules() -> None:
    DimSpec(kind="time", policy=DimPolicy.EXTEND, freq="W-SUN", max_extension=52)
    with pytest.raises(ValidationError, match="freq is only valid"):
        DimSpec(kind="categorical", freq="D")
    with pytest.raises(ValidationError, match="max_extension requires"):
        DimSpec(kind="time", max_extension=3)
    with pytest.raises(ValidationError, match="new_level_strategy requires"):
        DimSpec(kind="categorical", new_level_strategy="pooled")
    with pytest.raises(ValidationError, match="unsupported frequency"):
        DimSpec(kind="time", freq="fortnightly")
    with pytest.raises(ValidationError):
        DimSpec(kind="time", bogus=1)  # type: ignore[call-arg]


def test_varspec_rules() -> None:
    v = VarSpec(dims=("geo", "time"), dtype="float64", role=Role.TARGET, bounds=(0, None))
    assert v.dims == ("geo", "time")
    with pytest.raises(ValidationError, match="duplicate dims"):
        VarSpec(dims=("a", "a"), dtype="float64")
    with pytest.raises(ValidationError, match="bounds"):
        VarSpec(dims=(), dtype="float64", bounds=(2, 1))
    with pytest.raises(ValidationError, match="integer dtype"):
        VarSpec(dims=("obs",), dtype="float64", index_of="county")
    with pytest.raises(ValidationError):
        VarSpec(dims=("bad name",), dtype="float64")


def test_ragged_rules() -> None:
    with pytest.raises(ValidationError, match="differ"):
        RaggedSpec(group_dim="hh", within_dim="hh", form="long")
    with pytest.raises(ValidationError, match="mask_var"):
        RaggedSpec(group_dim="hh", within_dim="trip", form="padded")


def _radon() -> dict[str, Any]:
    return {
        "layout": "long",
        "dims": {
            "obs": {"kind": "index", "policy": "extend"},
            "county": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "hyperprior",
            },
        },
        "vars": {
            "log_radon": {"dims": ["obs"], "dtype": "float64", "role": "target"},
            "county_idx": {
                "dims": ["obs"],
                "dtype": "int64",
                "role": "index",
                "index_of": "county",
            },
            "floor": {"dims": ["obs"], "dtype": "float64"},
        },
    }


def test_dataschema_integrity_and_helpers() -> None:
    s = DataSchema.model_validate(_radon())
    assert s.targets == ["log_radon"]
    assert s.vars_with_role(Role.INDEX) == ["county_idx"]
    assert s.dims_in_use() == {"obs", "county"}
    assert s.has_name("county") and not s.has_name("nope")
    assert DataSchema.from_json(s.to_json()) == s

    bad = _radon()
    bad["vars"]["floor"]["dims"] = ["obs", "house"]
    with pytest.raises(ValidationError, match="undeclared dims"):
        DataSchema.model_validate(bad)
    bad = _radon()
    bad["vars"]["county_idx"]["index_of"] = "state"
    with pytest.raises(ValidationError, match="undeclared dim 'state'"):
        DataSchema.model_validate(bad)
    bad = _radon()
    bad["vars"]["county"] = {"dims": [], "dtype": "str"}
    with pytest.raises(ValidationError, match="both as dim and variable"):
        DataSchema.model_validate(bad)


def test_ragged_padded_schema() -> None:
    base = {
        "dims": {
            "animal": {"kind": "categorical"},
            "step": {"kind": "integer", "policy": "extend"},
        },
        "vars": {
            "x": {"dims": ["animal", "step"], "dtype": "float64", "missing": "mask"},
            "m": {"dims": ["animal", "step"], "dtype": "bool", "role": "mask"},
        },
        "ragged": [
            {"group_dim": "animal", "within_dim": "step", "form": "padded", "mask_var": "m"}
        ],
    }
    DataSchema.model_validate(base)
    base["vars"]["m"]["dtype"] = "float64"
    with pytest.raises(ValidationError, match="dtype bool"):
        DataSchema.model_validate(base)
    base["ragged"][0]["mask_var"] = "zz"
    with pytest.raises(ValidationError, match="not a variable"):
        DataSchema.model_validate(base)
    base["ragged"][0]["group_dim"] = "zz"
    with pytest.raises(ValidationError, match="undeclared dim 'zz'"):
        DataSchema.model_validate(base)


def test_renamed_updates_references() -> None:
    s = DataSchema.model_validate(_radon())
    r = s.renamed({"county": "region", "log_radon": "y"})
    assert "region" in r.dims and "y" in r.vars
    assert r.vars["county_idx"].index_of == "region"
    padded = DataSchema.model_validate(
        {
            "dims": {"a": {"kind": "categorical"}, "b": {"kind": "integer"}},
            "vars": {"m": {"dims": ["a", "b"], "dtype": "bool"}},
            "ragged": [{"group_dim": "a", "within_dim": "b", "form": "padded", "mask_var": "m"}],
        }
    )
    rr = padded.renamed({"a": "hh", "m": "mask"})
    assert rr.ragged[0].group_dim == "hh" and rr.ragged[0].mask_var == "mask"


def test_policy_lattice() -> None:
    F, E, N = DimPolicy.FIXED, DimPolicy.EXTEND, DimPolicy.NEW_LEVELS
    assert policy_is_at_least_as_tight(F, E) and policy_is_at_least_as_tight(F, N)
    assert not policy_is_at_least_as_tight(E, F) and not policy_is_at_least_as_tight(N, E)
    assert policy_is_at_least_as_tight(E, E)
    assert tighten_policy(E, E) is E and tighten_policy(E, N) is F and tighten_policy(N, F) is F


# ---------------------------------------------------------------------------- templates

MMM_TEMPLATE: dict[str, Any] = {
    "params": {
        "controls": {"type": "string_list", "default": []},
        "lmax": {"type": "integer", "path": "adstock.l_max", "default": 4},
        "pooling": {"type": "string", "enum": ["partial", "full"], "default": "partial"},
    },
    "template": {
        "dims": {
            "geo": {"kind": "categorical"},
            "time": {
                "kind": "time",
                "policy": "extend",
                "freq": "W-SUN",
                "max_extension": "{lmax}",
            },
        },
        "vars": {
            "y": {"dims": ["geo", "time"], "dtype": "float64", "role": "target"},
            "{*controls as c}": {"dims": ["geo", "time"], "dtype": "float64", "role": "control"},
        },
    },
    "cases": [
        {
            "when": {"pooling": "partial"},
            "merge": {
                "dims": {"geo": {"policy": "new_levels", "new_level_strategy": "hyperprior"}}
            },
        }
    ],
}


def test_template_resolution() -> None:
    t = DataSchemaTemplate.model_validate(MMM_TEMPLATE)
    s = t.resolve({"controls": ["price", "promo"], "adstock": {"l_max": 8}})
    assert set(s.vars) == {"y", "price", "promo"}
    assert s.vars["price"].role is Role.CONTROL
    assert s.dims["time"].max_extension == 8
    assert s.dims["geo"].policy is DimPolicy.NEW_LEVELS
    s2 = t.resolve({"pooling": "full"})
    assert set(s2.vars) == {"y"} and s2.dims["geo"].policy is DimPolicy.FIXED
    assert s2.dims["time"].max_extension == 4


def test_template_parameter_errors() -> None:
    t = DataSchemaTemplate.model_validate(MMM_TEMPLATE)
    with pytest.raises(TemplateError, match="string_list"):
        t.resolve({"controls": "price"})
    with pytest.raises(TemplateError, match="allowed values"):
        t.resolve({"pooling": "none"})
    schema, err = t.try_resolve({"controls": ["y"]})
    assert schema is None and err is not None and "duplicate key" in err
    req = DataSchemaTemplate.model_validate(
        {"params": {"rows": {"type": "string_list", "required": True}}, "template": {}}
    )
    with pytest.raises(TemplateError, match="required"):
        req.resolve({})
    schema, err = DataSchemaTemplate.model_validate(
        {"template": {"vars": {"x": {"dims": ["nope"], "dtype": "float64"}}}}
    ).try_resolve()
    assert schema is None and err is not None and "undeclared dims" in err


def test_template_undeclared_params_rejected() -> None:
    with pytest.raises(ValidationError, match="undeclared params"):
        DataSchemaTemplate.model_validate({"template": {"dims": {"{d}": {"kind": "time"}}}})
    with pytest.raises(ValidationError, match="undeclared params"):
        DataSchemaTemplate.model_validate(
            {"template": {}, "cases": [{"when": {"p": 1}, "merge": {}}]}
        )


@pytest.mark.parametrize(
    ("ptype", "good", "bad"),
    [
        ("string", "a", 1),
        ("integer", 1, True),
        ("number", 1.5, "x"),
        ("boolean", False, 0),
        ("string_list", ["a"], ["a", 1]),
        ("integer_list", [1, 2], [1.5]),
    ],
)
def test_template_param_types(ptype: str, good: Any, bad: Any) -> None:
    t = DataSchemaTemplate.model_validate({"params": {"p": {"type": ptype}}, "template": {}})
    assert t.param_values({"p": good}) == {"p": good}
    with pytest.raises(TemplateError):
        t.param_values({"p": bad})


def test_constant_template_from_schema() -> None:
    s = DataSchema.model_validate(_radon())
    assert DataSchemaTemplate.from_schema(s).resolve() == s
